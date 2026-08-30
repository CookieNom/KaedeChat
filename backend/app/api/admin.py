from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.chat.guild_revision import build_guild_authority_envelope, guild_authority_owner
from app.core.federation import (
    BLOCK_POLICY_ADVISORY_NAME,
    POLICY_HELD_OUTBOX_PREFIX,
    block_covers_domain,
    federation_policy_holds_event,
    policy_held_retry_at,
)
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import (
    FederationEvent,
    FederationOutbox,
    Guild,
    GuildMember,
    Instance,
    InstanceBlock,
)
from app.federation.delivery import MAX_QUEUE_AGE
from app.federation.events import queue_event
from app.federation.network import FederationNetworkError, ensure_peer, normalize_domain
from app.federation.schemas import InstanceBlockPut
from app.federation.security import admin_authorized, matching_block
from app.tasks import federation_deliver, federation_guild_sync

router = APIRouter(prefix="/api/v1/admin/federation", tags=["federation administration"])
ACTIVE_OUTBOX_STATUSES = ("pending", "retry", "circuit")


async def lock_block_policy(session: AsyncSession) -> None:
    """Serialize policy mutations before taking per-destination delivery locks."""

    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(BLOCK_POLICY_ADVISORY_NAME, 0)))
    )


async def affected_peer_domains(
    session: AsyncSession,
    rules: Iterable[tuple[str, bool]],
) -> set[str]:
    concrete_rules = tuple(rules)
    if not concrete_rules:
        return set()
    peers = set(await session.scalars(select(Instance.domain).where(Instance.is_self.is_(False))))
    return {
        peer
        for peer in peers
        if any(
            block_covers_domain(block_domain, include_subdomains, peer)
            for block_domain, include_subdomains in concrete_rules
        )
    }


async def lock_destination_policy(session: AsyncSession, destinations: Iterable[str]) -> None:
    """Fence both deliveries and concurrent outbox inserts for each destination."""

    for destination in sorted(set(destinations)):
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"kaede-outbox-drain:{destination}", 0)
                )
            )
        )
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(f"kaede-outbox:{destination}", 0))
            )
        )


async def effective_blocked_destinations(
    session: AsyncSession, destinations: Iterable[str]
) -> set[str]:
    blocked: set[str] = set()
    for destination in destinations:
        if await matching_block(session, destination) is not None:
            blocked.add(destination)
    return blocked


async def reconcile_destination_policy(
    session: AsyncSession,
    settings: Settings,
    destination: str,
    *,
    now: datetime,
) -> tuple[bool, set[str]]:
    """Apply the current block policy to all live outbox rows for one peer.

    Policy-held rows have no retention deadline. Releasing one resets its
    delivery window so time spent deliberately held does not consume the
    normal seven-day retry budget.
    """

    block = await matching_block(session, destination)
    rows = (
        (
            await session.execute(
                select(FederationOutbox, FederationEvent)
                .join(
                    FederationEvent,
                    (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                    & (FederationEvent.event_id == FederationOutbox.event_id),
                )
                .where(
                    FederationOutbox.destination == destination,
                    FederationOutbox.status.in_(ACTIVE_OUTBOX_STATUSES),
                )
                .order_by(FederationOutbox.id)
                .with_for_update()
            )
        )
        .tuples()
        .all()
    )
    if block is not None:
        await session.execute(
            update(Guild)
            .where(Guild.origin_domain == destination)
            .values(sync_status="stale", unavailable=True)
        )
    released_event_ids: set[str] = set()
    for outbox, event in rows:
        block_level = block.level if block is not None else None
        should_hold = block_level is not None and federation_policy_holds_event(
            block_level,
            event.event_type,
            context=getattr(event, "envelope", {}).get("context"),
        )
        was_policy_held = bool(
            outbox.last_error and outbox.last_error.startswith(POLICY_HELD_OUTBOX_PREFIX)
        )
        if should_hold:
            outbox.status = "circuit"
            outbox.next_retry_at = policy_held_retry_at(now)
            outbox.last_error = f"{POLICY_HELD_OUTBOX_PREFIX} {block_level}"
            event.expires_at = None
        elif was_policy_held:
            outbox.status = "pending"
            outbox.attempts = 0
            outbox.next_retry_at = now
            outbox.last_error = None
            outbox.created_at = now
            released_event_ids.add(event.event_id)
    return block is not None, released_event_ids


async def rearm_released_event_retention(
    session: AsyncSession,
    settings: Settings,
    released_event_ids: set[str],
    *,
    now: datetime,
) -> None:
    deadline = now + max(
        timedelta(days=settings.federation_event_retention_days),
        MAX_QUEUE_AGE + timedelta(days=1),
    )
    for event_id in released_event_ids:
        still_held = await session.scalar(
            select(FederationOutbox.id)
            .where(
                FederationOutbox.event_origin_domain == settings.domain,
                FederationOutbox.event_id == event_id,
                FederationOutbox.status.in_(ACTIVE_OUTBOX_STATUSES),
                FederationOutbox.last_error.startswith(POLICY_HELD_OUTBOX_PREFIX),
            )
            .limit(1)
        )
        if still_held is None:
            event = await session.get(FederationEvent, (settings.domain, event_id))
            if event is not None:
                event.expires_at = deadline


async def queue_unblock_reconciliation(
    session: AsyncSession,
    settings: Settings,
    destinations: Iterable[str],
) -> set[tuple[str, int]]:
    """Queue a fresh authority snapshot marker for every still-shared guild."""

    replica_syncs: set[tuple[str, int]] = set()
    for destination in sorted(set(destinations)):
        replica_ids = set(
            await session.scalars(select(Guild.id).where(Guild.origin_domain == destination))
        )
        replica_syncs.update((destination, guild_id) for guild_id in replica_ids)
        guilds = list(
            await session.scalars(
                select(Guild)
                .join(
                    GuildMember,
                    (GuildMember.guild_id == Guild.id)
                    & (GuildMember.guild_domain == Guild.origin_domain),
                )
                .where(
                    Guild.origin_domain == settings.domain,
                    GuildMember.user_domain == destination,
                )
                .distinct()
            )
        )
        for guild in guilds:
            owner = await guild_authority_owner(session, settings, guild)
            marker = await build_guild_authority_envelope(
                session,
                settings,
                guild,
                "guild.resync.required",
                owner,
                {"reason": "instance_block_removed"},
                context={
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "snapshot_seq": str(guild.next_event_seq - 1),
                },
            )
            # The policy transaction already owns this destination's outbox
            # lock. Queue directly without the normal guild-resync advisory
            # lock so the administration path never inverts guild→outbox into
            # outbox→guild. A distinct marker is harmless and retry-safe.
            await queue_event(
                session,
                settings,
                destination,
                marker,
                discover_destination=False,
            )
    return replica_syncs


async def reconcile_policy_change(
    session: AsyncSession,
    settings: Settings,
    destinations: set[str],
    previously_blocked: set[str],
) -> tuple[set[str], set[tuple[str, int]]]:
    now = datetime.now(UTC)
    released_event_ids: set[str] = set()
    released_destinations: set[str] = set()
    currently_blocked: set[str] = set()
    for destination in sorted(destinations):
        blocked, released = await reconcile_destination_policy(
            session, settings, destination, now=now
        )
        if blocked:
            currently_blocked.add(destination)
        if released:
            released_destinations.add(destination)
            released_event_ids.update(released)
    await rearm_released_event_retention(session, settings, released_event_ids, now=now)
    fully_unblocked = previously_blocked - currently_blocked
    replica_syncs = await queue_unblock_reconciliation(session, settings, fully_unblocked)
    return released_destinations | fully_unblocked, replica_syncs


async def wake_policy_reconciliation(
    destinations: set[str], replica_syncs: set[tuple[str, int]]
) -> None:
    for destination in sorted(destinations):
        await enqueue_best_effort(federation_deliver, destination)
    for origin, guild_id in sorted(replica_syncs):
        await enqueue_best_effort(federation_guild_sync, origin, guild_id)


def safe_admin_domain(value: str) -> str:
    try:
        return normalize_domain(value)
    except FederationNetworkError:
        raise HTTPException(status_code=400, detail={"code": "INVALID_DOMAIN"}) from None


def spreadsheet_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


@router.get("/peers")
async def list_peers(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    admin_authorized(request, settings)
    peers = list(
        await session.scalars(
            select(Instance).where(Instance.is_self.is_(False)).order_by(Instance.domain)
        )
    )
    result: list[dict[str, object]] = []
    for peer in peers:
        pending, oldest = (
            await session.execute(
                select(
                    func.count(FederationOutbox.id),
                    func.min(FederationOutbox.created_at),
                ).where(
                    FederationOutbox.destination == peer.domain,
                    FederationOutbox.status.in_(("pending", "retry", "circuit")),
                )
            )
        ).one()
        result.append(
            {
                "domain": peer.domain,
                "last_seen_at": peer.last_seen_at.isoformat() if peer.last_seen_at else None,
                "suspended_at": peer.suspended_at.isoformat() if peer.suspended_at else None,
                "approved": peer.federation_mode == "allowlist",
                "outbox_pending": int(pending),
                "outbox_oldest_at": oldest.isoformat() if oldest else None,
            }
        )
    return result


@router.post("/peers/{domain}/drain", status_code=202)
async def drain_peer(
    domain: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    admin_authorized(request, settings)
    normalized = safe_admin_domain(domain)
    await session.execute(
        update(FederationOutbox)
        .where(
            FederationOutbox.destination == normalized,
            FederationOutbox.status.in_(("pending", "retry", "circuit")),
            or_(
                FederationOutbox.last_error.is_(None),
                ~FederationOutbox.last_error.startswith(POLICY_HELD_OUTBOX_PREFIX),
            ),
        )
        .values(status="pending", next_retry_at=func.now())
    )
    await session.commit()
    await enqueue_best_effort(federation_deliver, normalized)
    return {"status": "queued", "domain": normalized}


@router.put("/peers/{domain}")
async def approve_peer(
    domain: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    admin_authorized(request, settings)
    instance = await ensure_peer(session, settings, safe_admin_domain(domain), force=True)
    instance.federation_mode = "allowlist"
    await session.commit()
    return {"status": "approved", "domain": instance.domain}


@router.get("/blocks")
async def list_blocks(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    admin_authorized(request, settings)
    blocks = list(await session.scalars(select(InstanceBlock).order_by(InstanceBlock.domain)))
    return [
        {
            "domain": block.domain,
            "level": block.level,
            "include_subdomains": block.include_subdomains,
            "reason": block.reason,
        }
        for block in blocks
    ]


@router.put("/blocks", status_code=204)
async def put_block(
    payload: InstanceBlockPut,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    admin_authorized(request, settings)
    domain = safe_admin_domain(payload.domain)
    if domain == settings.domain:
        raise HTTPException(status_code=400, detail={"code": "CANNOT_BLOCK_SELF"})
    await lock_block_policy(session)
    block = await session.scalar(
        select(InstanceBlock).where(InstanceBlock.domain == domain).with_for_update()
    )
    rules = [(domain, payload.include_subdomains)]
    if block is not None:
        rules.append((block.domain, block.include_subdomains))
    destinations = await affected_peer_domains(session, rules)
    await lock_destination_policy(session, destinations)
    previously_blocked = await effective_blocked_destinations(session, destinations)
    if block is None:
        block = InstanceBlock(domain=domain, level=payload.level)
        session.add(block)
    block.level = payload.level
    block.include_subdomains = payload.include_subdomains
    block.reason = payload.reason
    await session.flush()
    wake_destinations, replica_syncs = await reconcile_policy_change(
        session, settings, destinations, previously_blocked
    )
    await session.commit()
    await wake_policy_reconciliation(wake_destinations, replica_syncs)
    return Response(status_code=204)


@router.delete("/blocks/{domain}", status_code=204)
async def delete_block(
    domain: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    admin_authorized(request, settings)
    normalized = safe_admin_domain(domain)
    await lock_block_policy(session)
    block = await session.scalar(
        select(InstanceBlock).where(InstanceBlock.domain == normalized).with_for_update()
    )
    if block is not None:
        destinations = await affected_peer_domains(
            session, ((block.domain, block.include_subdomains),)
        )
        await lock_destination_policy(session, destinations)
        previously_blocked = await effective_blocked_destinations(session, destinations)
        await session.delete(block)
        await session.flush()
        wake_destinations, replica_syncs = await reconcile_policy_change(
            session, settings, destinations, previously_blocked
        )
        await session.commit()
        await wake_policy_reconciliation(wake_destinations, replica_syncs)
    return Response(status_code=204)


@router.get("/blocks/export")
async def export_blocks(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    admin_authorized(request, settings)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        ["#domain", "severity", "reject_media", "reject_reports", "public_comment", "obfuscate"]
    )
    blocks = list(await session.scalars(select(InstanceBlock).order_by(InstanceBlock.domain)))
    for block in blocks:
        writer.writerow(
            [
                block.domain,
                "limit" if block.level == "silence" else "suspend",
                "false",
                "false",
                spreadsheet_safe(block.reason or ""),
                "false",
            ]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=kaede-domain-blocks.csv"},
    )


@router.post("/blocks/import")
async def import_blocks(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, int]:
    admin_authorized(request, settings)
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise HTTPException(status_code=413, detail={"code": "IMPORT_TOO_LARGE"})
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail={"code": "INVALID_CSV"}) from None
    reader = csv.DictReader(io.StringIO(decoded))
    imported_blocks: dict[str, tuple[str, str | None]] = {}
    for row in reader:
        raw_domain = row.get("#domain") or row.get("domain")
        severity = (row.get("severity") or "suspend").lower()
        if not raw_domain or severity not in {"limit", "silence", "suspend"}:
            continue
        level = "silence" if severity in {"limit", "silence"} else "suspend"
        try:
            domain = normalize_domain(raw_domain)
        except FederationNetworkError:
            continue
        if domain == settings.domain:
            continue
        imported_blocks[domain] = (
            level,
            (row.get("public_comment") or "")[:500] or None,
        )
    await lock_block_policy(session)
    existing_blocks: dict[str, InstanceBlock | None] = {}
    rules: list[tuple[str, bool]] = []
    for domain in sorted(imported_blocks):
        block = await session.scalar(
            select(InstanceBlock).where(InstanceBlock.domain == domain).with_for_update()
        )
        existing_blocks[domain] = block
        rules.append((domain, block.include_subdomains if block is not None else True))
    destinations = await affected_peer_domains(session, rules)
    await lock_destination_policy(session, destinations)
    previously_blocked = await effective_blocked_destinations(session, destinations)
    for domain, (level, reason) in imported_blocks.items():
        block = existing_blocks[domain]
        if block is None:
            block = InstanceBlock(domain=domain, level=level)
            session.add(block)
        block.level = level
        block.reason = reason
    await session.flush()
    wake_destinations, replica_syncs = await reconcile_policy_change(
        session, settings, destinations, previously_blocked
    )
    await session.commit()
    await wake_policy_reconciliation(wake_destinations, replica_syncs)
    return {"imported": len(imported_blocks)}
