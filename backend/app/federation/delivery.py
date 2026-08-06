from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.events import publish_dispatch, user_topic
from app.core.federation import (
    POLICY_HELD_OUTBOX_PREFIX,
    canonical_json,
    federation_policy_holds_event,
    policy_held_retry_at,
)
from app.core.metrics import increment_metric
from app.core.settings import Settings
from app.db.models import (
    FederationEvent,
    FederationInbox,
    FederationOutbox,
    Guild,
    GuildMember,
    Instance,
    User,
)
from app.federation.client import signed_request
from app.federation.link import FederationLinkError, send_link_batch
from app.federation.network import ensure_peer
from app.federation.security import lock_block_policy_shared, matching_block

BACKOFF_SECONDS = (5, 30, 120, 600, 1_800, 3_600)
MAX_BATCH_EVENTS = 100
MAX_QUEUE_EVENTS = 50_000
MAX_QUEUE_AGE = timedelta(days=7)
MAX_EXPIRY_DESTINATIONS = 100
_JITTER = secrets.SystemRandom()


def expired_guild_context(event: FederationEvent, local_domain: str) -> tuple[int, str] | None:
    """Return a locally authoritative guild context that needs a resync marker."""

    if not event.event_type.startswith("guild."):
        return None
    context = event.envelope.get("context")
    if not isinstance(context, dict) or context.get("guild_domain") != local_domain:
        return None
    raw_guild_id = context.get("guild_id")
    if (
        not isinstance(raw_guild_id, str)
        or not raw_guild_id.isascii()
        or not raw_guild_id.isdecimal()
        or (len(raw_guild_id) > 1 and raw_guild_id.startswith("0"))
    ):
        return None
    guild_id = int(raw_guild_id)
    if not 0 <= guild_id <= (1 << 63) - 1:
        return None
    return guild_id, local_domain


def retry_delay(attempts: int) -> timedelta:
    base = BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)]
    return timedelta(seconds=base * _JITTER.uniform(0.85, 1.15))


async def enforce_queue_limits(session: AsyncSession, destination: str) -> None:
    pending = await session.scalar(
        select(func.count(FederationOutbox.id)).where(
            FederationOutbox.destination == destination,
            FederationOutbox.status.in_(("pending", "retry", "circuit")),
        )
    )
    if int(pending or 0) >= MAX_QUEUE_EVENTS:
        raise RuntimeError("federation outbox destination cap reached")


async def lock_outbox_destinations(session: AsyncSession, destinations: Iterable[str]) -> None:
    """Acquire ordered outbox locks before any destination row locks."""

    for destination in sorted(set(destinations)):
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(f"kaede-outbox:{destination}", 0))
            )
        )


async def drain_destination(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    destination: str,
    redis: Redis | None = None,
) -> int:
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        await lock_block_policy_shared(session)
        # A destination is an ordered stream. Row-level SKIP LOCKED alone lets
        # concurrent Taskiq jobs claim later batches and deliver them before an
        # earlier batch. Serialize drains per peer so DM references and guild
        # sequence events cannot overtake one another.
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"kaede-outbox-drain:{destination}", 0)
                )
            )
        )
        rows = list(
            await session.scalars(
                select(FederationOutbox)
                .where(
                    FederationOutbox.destination == destination,
                    FederationOutbox.status.in_(("pending", "retry", "circuit")),
                    FederationOutbox.next_retry_at <= now,
                )
                .order_by(FederationOutbox.id)
                .limit(MAX_BATCH_EVENTS)
                .with_for_update(skip_locked=True)
            )
        )
        if not rows:
            return 0
        event_refs = [(row.event_origin_domain, row.event_id) for row in rows]
        events = list(
            await session.scalars(
                select(FederationEvent).where(
                    tuple_(FederationEvent.origin_domain, FederationEvent.event_id).in_(event_refs)
                )
            )
        )
        by_ref = {(event.origin_domain, event.event_id): event for event in events}
        block = await matching_block(session, destination)
        if block is not None:
            held_rows = [
                row
                for row in rows
                if (
                    block.level == "suspend"
                    or (row.event_origin_domain, row.event_id) not in by_ref
                    or federation_policy_holds_event(
                        block.level,
                        by_ref[(row.event_origin_domain, row.event_id)].event_type,
                    )
                )
            ]
            for row in held_rows:
                row.status = "circuit"
                row.next_retry_at = policy_held_retry_at(now)
                row.last_error = f"{POLICY_HELD_OUTBOX_PREFIX} {block.level}"
                event = by_ref.get((row.event_origin_domain, row.event_id))
                if event is not None:
                    event.expires_at = None
            held_ids = {row.id for row in held_rows}
            rows = [row for row in rows if row.id not in held_ids]
            if not rows:
                await session.commit()
                return 0
        claimed_rows = rows
        bounded_rows: list[FederationOutbox] = []
        bounded_events: list[dict[str, object]] = []
        for row in rows:
            event = by_ref.get((row.event_origin_domain, row.event_id))
            if event is None:
                continue
            candidate_events = [*bounded_events, event.envelope]
            if len(canonical_json({"events": candidate_events})) > 1024 * 1024:
                break
            bounded_rows.append(row)
            bounded_events.append(event.envelope)
        rows = bounded_rows
        if not rows:
            oversized = claimed_rows[0]
            oversized.status = "failed"
            oversized.last_error = "event exceeds the 1 MiB federation transport limit"
            await session.commit()
            return 0
        payload = {"events": bounded_events}
        retry_override: timedelta | None = None
        try:
            if any(row.status == "circuit" for row in rows):
                await ensure_peer(session, settings, destination, force=True)
            try:
                response_payload = await send_link_batch(
                    session,
                    settings,
                    destination,
                    bounded_events,
                )
            except FederationLinkError:
                response = await signed_request(
                    session,
                    settings,
                    "POST",
                    destination,
                    "/_kaede/v1/inbox",
                    payload=payload,
                    request_timeout=15,
                )
                response.raise_for_status()
                response_payload = response.json()
            if not isinstance(response_payload, dict):
                raise ValueError("peer returned an invalid inbox response")
            raw_results = response_payload.get("results")
            if not isinstance(raw_results, list):
                raise ValueError("peer returned an invalid inbox response")
            results = {
                str(item["event_id"]): item
                for item in raw_results
                if isinstance(item, dict) and "event_id" in item
            }
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            await increment_metric(redis, "federation_delivery_failures")
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                try:
                    retry_seconds = float(exc.response.headers.get("Retry-After", ""))
                except ValueError:
                    retry_seconds = 0
                if retry_seconds > 0:
                    retry_override = timedelta(seconds=min(retry_seconds, 3600))
            for row in rows:
                row.attempts += 1
                age = now - row.created_at
                row.status = "circuit" if age >= timedelta(hours=24) else "retry"
                row.next_retry_at = now + (
                    timedelta(hours=1)
                    if row.status == "circuit"
                    else retry_override or retry_delay(row.attempts)
                )
                row.last_error = str(exc)[:500]
            await session.execute(
                update(Guild).where(Guild.origin_domain == destination).values(unavailable=True)
            )
            await session.commit()
            return 0
        delivered = 0
        delivery_updates: list[tuple[FederationEvent, str, str | None]] = []
        for row in rows:
            result = results.get(row.event_id)
            status = result.get("status") if result else None
            if status in {"accepted", "duplicate"}:
                row.status = "delivered"
                row.last_error = None
                delivered += 1
                event = by_ref.get((row.event_origin_domain, row.event_id))
                if event is not None:
                    delivery_updates.append((event, "delivered", None))
            elif status == "rejected":
                await increment_metric(redis, "federation_delivery_failures")
                row.status = "failed"
                row.last_error = str((result or {}).get("code") or "peer rejected event")[:500]
                event = by_ref.get((row.event_origin_domain, row.event_id))
                if event is not None:
                    delivery_updates.append((event, "failed", row.last_error))
            else:
                await increment_metric(redis, "federation_delivery_failures")
                row.attempts += 1
                row.status = "retry"
                row.next_retry_at = now + retry_delay(row.attempts)
                row.last_error = str((result or {}).get("code") or "missing result")[:500]
        instance = await session.get(Instance, destination)
        if instance is not None:
            instance.last_seen_at = now
        await session.execute(
            update(Guild).where(Guild.origin_domain == destination).values(unavailable=False)
        )
        await session.commit()
        if redis is not None:
            for event, delivery_status, code in delivery_updates:
                await publish_dm_delivery_update(
                    redis,
                    settings,
                    event,
                    destination,
                    delivery_status,
                    code,
                )
        return delivered


async def due_destinations(session: AsyncSession) -> list[str]:
    return list(
        await session.scalars(
            select(FederationOutbox.destination)
            .where(
                FederationOutbox.status.in_(("pending", "retry", "circuit")),
                FederationOutbox.next_retry_at <= datetime.now(UTC),
                FederationOutbox.created_at >= datetime.now(UTC) - MAX_QUEUE_AGE,
            )
            .distinct()
        )
    )


async def publish_dm_delivery_update(
    redis: Redis,
    settings: Settings,
    event: FederationEvent,
    destination: str,
    status: str,
    code: str | None,
) -> None:
    if event.event_type != "dm.message.create":
        return
    envelope = event.envelope
    actor = envelope.get("actor")
    message = envelope.get("content", {}).get("message")
    if (
        not isinstance(actor, dict)
        or not isinstance(message, dict)
        or actor.get("domain") != settings.domain
    ):
        return
    try:
        actor_id = int(actor["id"])
    except (KeyError, TypeError, ValueError):
        return
    payload: dict[str, object] = {
        "message_id": str(message.get("id", "")),
        "message_domain": str(message.get("origin_domain", "")),
        "channel_id": str(message.get("channel_id", "")),
        "channel_domain": str(message.get("channel_domain", "")),
        "destination": destination,
        "status": status,
    }
    if code:
        payload["code"] = code
    await publish_dispatch(
        redis,
        user_topic(settings.domain, actor_id),
        "MESSAGE_DELIVERY_UPDATE",
        payload,
    )


async def expire_stale_outbox(
    session: AsyncSession,
    settings: Settings | None = None,
    redis: Redis | None = None,
) -> int:
    cutoff = datetime.now(UTC) - MAX_QUEUE_AGE
    stale_destinations = list(
        await session.scalars(
            select(FederationOutbox.destination)
            .where(
                FederationOutbox.status.in_(("pending", "retry", "circuit")),
                FederationOutbox.created_at < cutoff,
                or_(
                    FederationOutbox.last_error.is_(None),
                    ~FederationOutbox.last_error.startswith(POLICY_HELD_OUTBOX_PREFIX),
                ),
            )
            .distinct()
            .order_by(FederationOutbox.destination)
            .limit(MAX_EXPIRY_DESTINATIONS)
        )
    )
    # Block administration takes destination advisory locks before locking
    # outbox rows. Match that order so expiration cannot form O→R / R→O.
    await lock_outbox_destinations(session, stale_destinations)
    stale_rows = list(
        await session.scalars(
            select(FederationOutbox)
            .where(
                FederationOutbox.destination.in_(stale_destinations),
                FederationOutbox.status.in_(("pending", "retry", "circuit")),
                FederationOutbox.created_at < cutoff,
                or_(
                    FederationOutbox.last_error.is_(None),
                    ~FederationOutbox.last_error.startswith(POLICY_HELD_OUTBOX_PREFIX),
                ),
            )
            .with_for_update(skip_locked=True)
        )
    )
    event_refs = [(row.event_origin_domain, row.event_id) for row in stale_rows]
    stale_events = {
        (event.origin_domain, event.event_id): event
        for event in (
            list(
                await session.scalars(
                    select(FederationEvent).where(
                        tuple_(FederationEvent.origin_domain, FederationEvent.event_id).in_(
                            event_refs
                        )
                    )
                )
            )
            if event_refs
            else []
        )
    }
    resync_targets: set[tuple[str, int, str]] = set()
    for row in stale_rows:
        row.status = "expired"
        row.last_error = "delivery window expired; guild destinations must gap-fill"
        event = stale_events.get((row.event_origin_domain, row.event_id))
        context = expired_guild_context(event, settings.domain) if event and settings else None
        if context is not None:
            resync_targets.add((row.destination, context[0], context[1]))

    if settings is not None:
        # Import lazily: events imports this module's queue-limit helper.
        from app.federation.events import build_envelope, queue_event

        for destination, guild_id, guild_domain in sorted(resync_targets):
            guild = await session.get(Guild, (guild_id, guild_domain))
            if guild is None:
                continue
            still_participating = await session.scalar(
                select(GuildMember.user_id)
                .where(
                    GuildMember.guild_id == guild_id,
                    GuildMember.guild_domain == guild_domain,
                    GuildMember.user_domain == destination,
                )
                .limit(1)
            )
            if still_participating is None:
                continue
            owner = await session.get(User, (guild.owner_id, guild.owner_domain))
            if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
                raise RuntimeError("local guild owner cannot sign delivery-expiry reconciliation")
            marker = await build_envelope(
                session,
                settings,
                "guild.resync.required",
                owner,
                {"reason": "delivery_window_expired"},
                context={
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "snapshot_seq": str(guild.next_event_seq - 1),
                },
            )
            # The destination outbox advisory is already held. Queue directly
            # instead of taking the guild-resync advisory in the opposite order.
            await queue_event(
                session,
                settings,
                destination,
                marker,
                discover_destination=False,
            )
    await session.commit()
    if redis is not None and settings is not None:
        for row in stale_rows:
            event = stale_events.get((row.event_origin_domain, row.event_id))
            if event is not None:
                await publish_dm_delivery_update(
                    redis,
                    settings,
                    event,
                    row.destination,
                    "failed",
                    "KAED_FED_DELIVERY_EXPIRED",
                )
    return len(stale_rows)


async def cleanup_federation_retention(session: AsyncSession, settings: Settings) -> int:
    now = datetime.now(UTC)
    inbox_cutoff = now - timedelta(days=settings.federation_event_retention_days)
    events = await session.execute(delete(FederationEvent).where(FederationEvent.expires_at < now))
    inbox = await session.execute(
        delete(FederationInbox).where(FederationInbox.received_at < inbox_cutoff)
    )
    await session.commit()
    return int(events.rowcount or 0) + int(inbox.rowcount or 0)  # type: ignore[attr-defined]
