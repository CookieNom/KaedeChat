from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.federation import (
    POLICY_HELD_OUTBOX_PREFIX,
    SECURITY_CRITICAL_GUILD_EVENTS,
    federation_policy_holds_event,
    policy_held_retry_at,
    sign_envelope,
)
from app.core.settings import Settings
from app.db.models import FederationEvent, FederationOutbox, Instance, User
from app.federation.delivery import MAX_QUEUE_AGE, enforce_queue_limits
from app.federation.network import FederationNetworkError, normalize_domain
from app.federation.security import matching_block, self_private_key


async def build_envelope(
    session: AsyncSession,
    settings: Settings,
    event_type: str,
    actor: User,
    content: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if actor.origin_domain != settings.domain:
        raise ValueError("an instance may only sign events for its own users")
    key_id, private_key = await self_private_key(session, settings)
    envelope: dict[str, Any] = {
        "event_id": f"kcfe_{secrets.token_urlsafe(24)}",
        "origin": settings.domain,
        "type": event_type,
        "ts": int(time.time() * 1000),
        "actor": {"id": str(actor.id), "domain": actor.origin_domain},
        "context": context or {},
        "content": content,
    }
    signature = sign_envelope(envelope, private_key)
    envelope["signatures"] = {settings.domain: {key_id: signature}}
    return envelope


async def queue_event(
    session: AsyncSession,
    settings: Settings,
    destination: str,
    envelope: dict[str, Any],
    *,
    discover_destination: bool = True,
) -> None:
    destination = normalize_domain(destination)
    # Serialize the authoritative policy check and destination setup with block
    # mutations. A completed block therefore fences all later discovery and
    # insertion, while a block request waits for an already-started queue write.
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"kaede-outbox:{destination}", 0)))
    )
    block = await matching_block(session, destination)
    event_type = str(envelope.get("type", ""))
    policy_held = block is not None and federation_policy_holds_event(block.level, event_type)
    await ensure_queue_destination(
        session,
        settings,
        destination,
        discover_destination=discover_destination,
        create_offline_placeholder=policy_held,
    )
    event_id = str(envelope["event_id"])
    existing_outbox = await session.scalar(
        select(FederationOutbox.id).where(
            FederationOutbox.destination == destination,
            FederationOutbox.event_origin_domain == settings.domain,
            FederationOutbox.event_id == event_id,
        )
    )
    if existing_outbox is not None:
        return
    # Access revocations and resync markers are security reconciliation state.
    # They must remain durable even when an operator block has filled or paused
    # the ordinary destination queue.
    if event_type not in SECURITY_CRITICAL_GUILD_EVENTS:
        await enforce_queue_limits(session, destination)
    now = datetime.now(UTC)
    inserted = await session.scalar(
        pg_insert(FederationEvent)
        .values(
            event_id=event_id,
            origin_domain=settings.domain,
            event_type=event_type,
            envelope=envelope,
            # Keep the source envelope beyond the seven-day delivery cutoff so
            # the expiry sweep can inspect it and enqueue a resync marker even
            # when operators configure the minimum retention window.
            expires_at=(
                None
                if policy_held
                else now
                + max(
                    timedelta(days=settings.federation_event_retention_days),
                    MAX_QUEUE_AGE + timedelta(days=1),
                )
            ),
        )
        .on_conflict_do_nothing(index_elements=["origin_domain", "event_id"])
        .returning(FederationEvent.event_id)
    )
    if inserted is None:
        existing = await session.get(FederationEvent, (settings.domain, event_id))
        if existing is None or (
            existing.origin_domain != settings.domain or existing.envelope != envelope
        ):
            raise RuntimeError("federation event ID conflicts with another envelope")
        if policy_held:
            existing.expires_at = None
    outbox_insert = pg_insert(FederationOutbox).values(
        destination=destination,
        event_origin_domain=settings.domain,
        event_id=event_id,
        status="circuit" if policy_held else "pending",
        next_retry_at=policy_held_retry_at(now) if policy_held else now,
        last_error=(
            f"{POLICY_HELD_OUTBOX_PREFIX} {block.level}"
            if policy_held and block is not None
            else None
        ),
    )
    if policy_held:
        if block is None:
            raise RuntimeError("policy-held federation event is missing its block policy")
        outbox_insert = outbox_insert.on_conflict_do_update(
            index_elements=["destination", "event_origin_domain", "event_id"],
            set_={
                "status": "circuit",
                "next_retry_at": policy_held_retry_at(now),
                "last_error": f"{POLICY_HELD_OUTBOX_PREFIX} {block.level}",
            },
            where=FederationOutbox.status.in_(("pending", "retry", "circuit")),
        )
    else:
        outbox_insert = outbox_insert.on_conflict_do_nothing(
            index_elements=["destination", "event_origin_domain", "event_id"]
        )
    await session.execute(outbox_insert)


async def ensure_queue_destination(
    session: AsyncSession,
    settings: Settings,
    destination: str,
    *,
    discover_destination: bool,
    create_offline_placeholder: bool = False,
) -> Instance:
    """Create durable first-contact state without networking on a write path.

    Delivery performs authoritative discovery before sending. Keeping discovery
    out of this transaction avoids both coupling a local write to peer uptime
    and acquiring peer-network locks while the ordered outbox lock is held.
    """

    known_destination = await session.get(Instance, destination)
    if known_destination is None:
        if not create_offline_placeholder and not discover_destination:
            raise FederationNetworkError("federation destination is unknown")
        # A local block must never trigger discovery or any other exchange with
        # the blocked peer. All other first-contact events are also staged
        # offline so peer uptime cannot abort an otherwise valid local write.
        await session.execute(
            pg_insert(Instance)
            .values(
                domain=destination,
                is_self=False,
                display_name=destination,
                software_version="unresolved",
            )
            .on_conflict_do_nothing(index_elements=["domain"])
        )
        known_destination = await session.get(Instance, destination, populate_existing=True)
        if known_destination is None:
            raise RuntimeError("federation destination placeholder disappeared")
    return known_destination
