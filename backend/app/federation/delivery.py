from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import Exists

from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import user_payload
from app.chat.privacy import lock_relationship_pair, relationship
from app.core.dm import dm_pair_key
from app.core.federation import (
    DURABLE_LATEST_STATE_EVENTS,
    POLICY_HELD_OUTBOX_PREFIX,
    canonical_json,
    durable_guild_media_delete_request,
    durable_terminal_room_event,
    federation_policy_holds_event,
    policy_held_retry_at,
)
from app.core.metrics import increment_metric
from app.core.settings import Settings
from app.db.models import (
    Attachment,
    FederationEvent,
    FederationInbox,
    FederationOutbox,
    Guild,
    GuildMember,
    Instance,
    PeerKey,
    RemoteMediaCache,
    RemoteMediaTombstone,
    User,
)
from app.federation.client import signed_request
from app.federation.link import FederationLinkError, send_link_batch
from app.federation.network import decode_federation_response_json, ensure_peer
from app.federation.schemas import DMOpenFederationRequest, RelationshipEventContent
from app.federation.security import lock_block_policy_shared, matching_block
from app.federation.storage import reconcile_federation_storage_usage

BACKOFF_SECONDS = (5, 30, 120, 600, 1_800, 3_600)
MAX_BATCH_EVENTS = 100
MAX_QUEUE_EVENTS = 50_000
MAX_QUEUE_AGE = timedelta(days=7)
MAX_EXPIRY_DESTINATIONS = 100
_JITTER = secrets.SystemRandom()
TERMINAL_RELATIONSHIP_CAPACITY_CODES = frozenset(
    {
        "KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED",
        "KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED",
        "KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED",
    }
)
TERMINAL_LOCAL_DELIVERY_CODES = frozenset(
    {
        "KAED_FED_DELIVERY_EXPIRED",
        "KAED_FED_EVENT_TOO_LARGE",
    }
)


class FederationOutboxCapacityExceeded(RuntimeError):
    """A destination's bounded durable delivery queue is full."""

    code = "FEDERATION_OUTBOX_CAPACITY_EXCEEDED"
    federation_code = "KAED_FED_OUTBOX_CAPACITY_EXCEEDED"

    def detail(self, *, federation: bool = False) -> dict[str, object]:
        # Queue depth is operator-only data and must not become a peer oracle.
        return {"code": self.federation_code if federation else self.code}


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


def without_event_id_collisions(
    rows: Iterable[FederationOutbox],
) -> list[FederationOutbox]:
    """Return the ordered first row for each wire-level event ID.

    Inbox results currently identify events only by ``event_id``, while relay
    outboxes are keyed by ``(origin, event_id)``. A rare cross-origin collision
    therefore cannot safely share one batch: defer the later row to the next
    drain so each response remains unambiguous.
    """

    selected: list[FederationOutbox] = []
    seen: set[str] = set()
    for row in rows:
        if row.event_id in seen:
            # Preserve the destination's total ordering: the colliding row is
            # a barrier, so no later event may overtake it in this drain.
            break
        seen.add(row.event_id)
        selected.append(row)
    return selected


def due_ordered_prefix(
    rows: Iterable[FederationOutbox],
    now: datetime,
) -> list[FederationOutbox]:
    """Return only the due prefix of one destination's total-order stream.

    A retry delay on the head row is a delivery barrier. Selecting only due
    rows in SQL would let a later pending mutation overtake that barrier and
    can invert reaction, pin, edit, or deletion state at the replica.
    """

    selected: list[FederationOutbox] = []
    for row in rows:
        if row.next_retry_at > now:
            break
        selected.append(row)
    return selected


def group_state_rejection_is_upgrade_retryable(
    event: FederationEvent,
    row: FederationOutbox,
    code: str,
    now: datetime,
) -> bool:
    """Keep generic group protocol rejections live across a rolling upgrade."""

    return bool(
        event.event_type
        in {
            "dm.group.state",
            "dm.group.message.proposed",
            "dm.group.message.committed",
            "dm.group.call.create",
        }
        and code == "KAED_FED_EVENT_REJECTED"
        and now - row.created_at < timedelta(hours=24)
    )


def retry_rejected_durable_event(
    event: FederationEvent | None,
    row: FederationOutbox,
    code: str,
    now: datetime,
) -> bool:
    """Keep authoritative reconciliation state retryable across peer upgrades."""

    if event is None or (
        event.event_type not in DURABLE_LATEST_STATE_EVENTS | {"media.delete"}
        and not durable_terminal_room_event(event.envelope)
        and not durable_guild_media_delete_request(event.envelope)
    ):
        return False
    row.attempts += 1
    row.status = "circuit" if now - row.created_at >= timedelta(hours=24) else "retry"
    row.next_retry_at = now + (
        timedelta(hours=1) if row.status == "circuit" else retry_delay(row.attempts)
    )
    row.last_error = code
    return True


def bound_delivered_projection_retention(
    event: FederationEvent,
    settings: Settings,
    now: datetime,
) -> None:
    """Return acknowledged latest-state projections to ordinary retention."""

    if event.event_type in DURABLE_LATEST_STATE_EVENTS:
        event.expires_at = now + timedelta(days=settings.federation_event_retention_days)


async def enforce_queue_limits(session: AsyncSession, destination: str) -> None:
    pending = await session.scalar(
        select(func.count(FederationOutbox.id)).where(
            FederationOutbox.destination == destination,
            FederationOutbox.status.in_(("pending", "retry", "circuit")),
        )
    )
    if int(pending or 0) >= MAX_QUEUE_EVENTS:
        raise FederationOutboxCapacityExceeded("federation outbox destination cap reached")


async def lock_outbox_destinations(session: AsyncSession, destinations: Iterable[str]) -> None:
    """Acquire ordered outbox locks before any destination row locks."""

    for destination in sorted(set(destinations)):
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(f"kaede-outbox:{destination}", 0))
            )
        )


async def reconcile_relationship_capacity_rejection(
    session: AsyncSession,
    settings: Settings,
    event: FederationEvent,
    code: str,
) -> tuple[User, User] | None:
    """Remove only the exact pending request that terminally failed delivery.

    A late rejection must never erase a newer request, an accepted friendship,
    or a block. The relationship-pair lock serializes this comparison with all
    user actions that can replace that state.
    """

    # relationships imports events (which imports this module), so keep this
    # small pure matcher import local to avoid an import cycle at startup.
    from app.federation.relationships import acceptance_matches

    if (
        event.event_type != "relationship.request"
        or code not in TERMINAL_RELATIONSHIP_CAPACITY_CODES | TERMINAL_LOCAL_DELIVERY_CODES
    ):
        return None
    envelope = event.envelope
    raw_actor = envelope.get("actor")
    try:
        content = RelationshipEventContent.model_validate(envelope.get("content"))
        if not isinstance(raw_actor, dict):
            return None
        actor_id = int(content.actor.id)
        target_id = int(content.target.id)
    except (TypeError, ValueError):
        return None
    if (
        content.request_id is None
        or content.actor.origin_domain != settings.domain
        or raw_actor.get("id") != content.actor.id
        or raw_actor.get("domain") != content.actor.origin_domain
    ):
        return None
    actor = await session.get(User, (actor_id, content.actor.origin_domain))
    target = await session.get(User, (target_id, content.target.domain))
    if actor is None or not actor.is_local or target is None:
        return None
    await lock_relationship_pair(session, actor, target)
    current = await relationship(session, actor, target)
    if current is None or not acceptance_matches(
        current.type,
        current.request_id,
        content.request_id,
    ):
        return None
    await session.delete(current)
    return actor, target


def dm_open_failure_target(
    settings: Settings,
    event: FederationEvent,
    code: str,
) -> tuple[int, str, dict[str, object]] | None:
    """Return the local initiator and safe payload for a failed queued DM open."""

    if event.event_type != "dm.open.request":
        return None
    envelope = event.envelope
    raw_actor = envelope.get("actor")
    raw_content = envelope.get("content")
    if not isinstance(raw_actor, dict) or not isinstance(raw_content, dict):
        return None
    raw_actor_id = raw_actor.get("id")
    raw_actor_domain = raw_actor.get("domain")
    if (
        isinstance(raw_actor_id, bool)
        or not isinstance(raw_actor_id, (str, int))
        or not isinstance(raw_actor_domain, str)
    ):
        return None
    try:
        actor_id = int(raw_actor_id)
        actor_domain = raw_actor_domain
        request = DMOpenFederationRequest.model_validate(
            {"participants": raw_content.get("participants")}
        )
        expected_pair_key = dm_pair_key(
            *(f"{profile.username}@{profile.origin_domain}" for profile in request.participants)
        )
    except (TypeError, ValidationError, ValueError):
        return None
    if actor_domain != settings.domain or not any(
        int(profile.id) == actor_id and profile.origin_domain == actor_domain
        for profile in request.participants
    ):
        return None
    if raw_content.get("pair_key") != expected_pair_key:
        return None
    return (
        actor_id,
        actor_domain,
        {"pair_key": expected_pair_key, "code": code},
    )


async def publish_terminal_outbox_failure(
    redis: Redis,
    settings: Settings,
    event: FederationEvent,
    destination: str,
    code: str,
    relationship_result: tuple[User, User] | None,
) -> None:
    """Resolve optimistic client state after a durable terminal failure."""

    await publish_dm_delivery_update(
        redis,
        settings,
        event,
        destination,
        "failed",
        code,
    )
    if relationship_result is not None:
        actor, target = relationship_result
        await publish_dispatch(
            redis,
            user_topic(actor.origin_domain, actor.id),
            "USER_UPDATE",
            {
                "relationship": {
                    "type": "none",
                    "user": user_payload(target),
                    "error_code": code,
                }
            },
        )
    dm_open = dm_open_failure_target(settings, event, code)
    if dm_open is not None:
        actor_id, actor_domain, payload = dm_open
        payload["authority_domain"] = destination
        await publish_dispatch(
            redis,
            user_topic(actor_domain, actor_id),
            "DM_OPEN_REJECTED",
            payload,
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
        admitted = await session.scalar(
            select(
                func.pg_try_advisory_xact_lock(
                    func.hashtextextended(f"kaede-outbox-drain:{destination}", 0)
                )
            )
        )
        if admitted is not True:
            # Duplicate wakeups are normal. Never let them queue while holding
            # separate DB connections behind a slow destination; the active
            # drainer or periodic sweep will process the ordered stream.
            return 0
        rows = list(
            await session.scalars(
                select(FederationOutbox)
                .where(
                    FederationOutbox.destination == destination,
                    FederationOutbox.status.in_(("pending", "retry", "circuit")),
                )
                .order_by(FederationOutbox.id)
                .limit(MAX_BATCH_EVENTS)
                .with_for_update(skip_locked=True)
            )
        )
        rows = due_ordered_prefix(rows, now)
        if not rows:
            return 0
        rows = without_event_id_collisions(rows)
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
                    (row.event_origin_domain, row.event_id) not in by_ref
                    or (
                        by_ref[(row.event_origin_domain, row.event_id)].event_type != "media.delete"
                        and not durable_terminal_room_event(
                            by_ref[(row.event_origin_domain, row.event_id)].envelope
                        )
                        and not durable_guild_media_delete_request(
                            by_ref[(row.event_origin_domain, row.event_id)].envelope
                        )
                        and (
                            block.level == "suspend"
                            or federation_policy_holds_event(
                                block.level,
                                by_ref[(row.event_origin_domain, row.event_id)].event_type,
                                context=by_ref[
                                    (row.event_origin_domain, row.event_id)
                                ].envelope.get("context"),
                            )
                        )
                    )
                )
            ]
            for row in held_rows:
                row.status = "circuit"
                row.next_retry_at = policy_held_retry_at(now)
                row.last_error = f"{POLICY_HELD_OUTBOX_PREFIX} {block.level}"
                event = by_ref.get((row.event_origin_domain, row.event_id))
                if event is not None and event.event_type != "bot.interaction.response":
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
            oversized.last_error = "KAED_FED_EVENT_TOO_LARGE"
            oversized_event = by_ref.get((oversized.event_origin_domain, oversized.event_id))
            relationship_result = (
                await reconcile_relationship_capacity_rejection(
                    session,
                    settings,
                    oversized_event,
                    oversized.last_error,
                )
                if oversized_event is not None
                else None
            )
            await session.commit()
            if redis is not None and oversized_event is not None:
                await publish_terminal_outbox_failure(
                    redis,
                    settings,
                    oversized_event,
                    oversized.destination,
                    oversized.last_error,
                    relationship_result,
                )
            return 0
        payload = {"events": bounded_events}
        retry_override: timedelta | None = None
        response_payload: object
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
                response_payload = decode_federation_response_json(response)
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
        terminal_room_acks: list[tuple[str, dict[str, Any]]] = []
        guild_media_delete_acks: list[tuple[str, dict[str, Any]]] = []
        delivery_updates: list[tuple[FederationEvent, str, str | None]] = []
        relationship_rejections: dict[tuple[int, str, int, str], tuple[User, User, str]] = {}
        for row in rows:
            result = results.get(row.event_id)
            status = result.get("status") if result else None
            if status in {"accepted", "duplicate"}:
                row.status = "delivered"
                row.last_error = None
                delivered += 1
                event = by_ref.get((row.event_origin_domain, row.event_id))
                if event is not None:
                    bound_delivered_projection_retention(event, settings, now)
                    delivery_updates.append((event, "delivered", None))
                    if durable_terminal_room_event(event.envelope):
                        terminal_room_acks.append((row.destination, event.envelope))
                    if durable_guild_media_delete_request(event.envelope):
                        guild_media_delete_acks.append((row.destination, event.envelope))
            elif status == "rejected":
                await increment_metric(redis, "federation_delivery_failures")
                event = by_ref.get((row.event_origin_domain, row.event_id))
                rejection_code = str((result or {}).get("code") or "peer rejected event")[:500]
                if retry_rejected_durable_event(event, row, rejection_code, now):
                    # A peer can reject this event while running an older
                    # protocol build or while a recoverable tombstone quota is
                    # full. Terminal invalidation must survive that rolling
                    # upgrade/outage without relying on another disclosure to
                    # wake a relay-only outbox.
                    if event is None:
                        raise RuntimeError("media tombstone retry lost its source event")
                    delivery_updates.append((event, "retrying", rejection_code))
                    continue
                # Group federation support has evolved across rolling releases.
                # An older peer reports only the generic rejection code for a
                # newly introduced group event. Keep it retryable for one day so
                # the peer can upgrade without losing state, messages, or calls.
                if event is not None and group_state_rejection_is_upgrade_retryable(
                    event, row, rejection_code, now
                ):
                    row.attempts += 1
                    row.status = "retry"
                    row.next_retry_at = now + retry_delay(row.attempts)
                    row.last_error = rejection_code
                    delivery_updates.append((event, "retrying", rejection_code))
                    continue
                row.status = "failed"
                row.last_error = rejection_code
                if event is not None:
                    delivery_updates.append((event, "failed", row.last_error))
                    reconciled = await reconcile_relationship_capacity_rejection(
                        session,
                        settings,
                        event,
                        row.last_error,
                    )
                    if reconciled is not None:
                        actor, target = reconciled
                        relationship_rejections[
                            (actor.id, actor.origin_domain, target.id, target.origin_domain)
                        ] = (actor, target, row.last_error)
            else:
                await increment_metric(redis, "federation_delivery_failures")
                row.attempts += 1
                row.status = "retry"
                row.next_retry_at = now + retry_delay(row.attempts)
                row.last_error = str((result or {}).get("code") or "missing result")[:500]
                event = by_ref.get((row.event_origin_domain, row.event_id))
                if event is not None:
                    delivery_updates.append((event, "retrying", row.last_error))
        instance = await session.get(Instance, destination)
        if instance is not None:
            instance.last_seen_at = now
        await session.execute(
            update(Guild).where(Guild.origin_domain == destination).values(unavailable=False)
        )
        await session.commit()
        # Generation rollover takes terminal-room state before outbox state.
        # Mark the delivery in a second transaction after releasing outbox row
        # locks so delivery cannot form an outbox -> room deadlock.
        from app.federation.terminal_rooms import acknowledge_terminal_room_delivery

        for ack_destination, ack_envelope in terminal_room_acks:
            await acknowledge_terminal_room_delivery(
                session,
                destination=ack_destination,
                envelope=ack_envelope,
            )
        if terminal_room_acks:
            await session.commit()
        from app.federation.guild_media_deletions import (
            acknowledge_guild_media_delete_request,
        )

        for ack_destination, ack_envelope in guild_media_delete_acks:
            await acknowledge_guild_media_delete_request(
                session,
                destination=ack_destination,
                envelope=ack_envelope,
            )
        if guild_media_delete_acks:
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
            for actor, target, code in relationship_rejections.values():
                await publish_dispatch(
                    redis,
                    user_topic(actor.origin_domain, actor.id),
                    "USER_UPDATE",
                    {
                        "relationship": {
                            "type": "none",
                            "user": user_payload(target),
                            "error_code": code,
                        }
                    },
                )
        return delivered


async def due_destinations(session: AsyncSession) -> list[str]:
    durable_outbox_event = _durable_outbox_event_exists()
    return list(
        await session.scalars(
            select(FederationOutbox.destination)
            .where(
                FederationOutbox.status.in_(("pending", "retry", "circuit")),
                FederationOutbox.next_retry_at <= datetime.now(UTC),
                or_(
                    FederationOutbox.created_at >= datetime.now(UTC) - MAX_QUEUE_AGE,
                    durable_outbox_event,
                ),
            )
            .distinct()
        )
    )


def _durable_outbox_event_exists() -> Exists:
    """Match queue state that must survive the ordinary delivery cutoff."""

    return exists(
        select(FederationEvent.event_id).where(
            FederationEvent.origin_domain == FederationOutbox.event_origin_domain,
            FederationEvent.event_id == FederationOutbox.event_id,
            or_(
                FederationEvent.event_type.in_(DURABLE_LATEST_STATE_EVENTS),
                FederationEvent.event_type == "media.delete",
                FederationEvent.event_type == "guild.media.delete.request",
                (
                    (FederationEvent.event_type == "guild.instance_access.revoked")
                    & (FederationEvent.envelope["content"]["reason"].as_string() == "guild_deleted")
                ),
                (
                    (FederationEvent.event_type == "dm.group.state")
                    & (
                        FederationEvent.envelope["content"]["conversation"]["deleted"]
                        .as_boolean()
                        .is_(True)
                    )
                ),
            ),
        )
    )


async def rearm_failed_media_delete_outbox(
    session: AsyncSession,
    *,
    limit: int = MAX_BATCH_EVENTS,
) -> set[str]:
    """Repair tombstone rows made terminal by an older delivery implementation."""

    if not 1 <= limit <= 10_000:
        raise ValueError("invalid media tombstone rearm limit")
    candidate_destinations = list(
        await session.scalars(
            select(FederationOutbox.destination)
            .join(
                FederationEvent,
                (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                & (FederationEvent.event_id == FederationOutbox.event_id),
            )
            .where(
                or_(
                    FederationEvent.event_type == "media.delete",
                    FederationEvent.event_type == "guild.media.delete.request",
                    (
                        (FederationEvent.event_type == "guild.instance_access.revoked")
                        & (
                            FederationEvent.envelope["content"]["reason"].as_string()
                            == "guild_deleted"
                        )
                    ),
                    (
                        (FederationEvent.event_type == "dm.group.state")
                        & (
                            FederationEvent.envelope["content"]["conversation"]["deleted"]
                            .as_boolean()
                            .is_(True)
                        )
                    ),
                ),
                FederationOutbox.status.in_(("failed", "expired")),
                or_(
                    FederationOutbox.last_error.is_(None),
                    FederationOutbox.last_error != "KAED_FED_TOMBSTONE_SUPERSEDED",
                ),
            )
            .distinct()
            .order_by(FederationOutbox.destination)
            .limit(limit)
        )
    )
    await lock_outbox_destinations(session, candidate_destinations)
    if not candidate_destinations:
        return set()
    rows = list(
        await session.scalars(
            select(FederationOutbox)
            .join(
                FederationEvent,
                (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                & (FederationEvent.event_id == FederationOutbox.event_id),
            )
            .where(
                or_(
                    FederationEvent.event_type == "media.delete",
                    FederationEvent.event_type == "guild.media.delete.request",
                    (
                        (FederationEvent.event_type == "guild.instance_access.revoked")
                        & (
                            FederationEvent.envelope["content"]["reason"].as_string()
                            == "guild_deleted"
                        )
                    ),
                    (
                        (FederationEvent.event_type == "dm.group.state")
                        & (
                            FederationEvent.envelope["content"]["conversation"]["deleted"]
                            .as_boolean()
                            .is_(True)
                        )
                    ),
                ),
                FederationOutbox.destination.in_(candidate_destinations),
                FederationOutbox.status.in_(("failed", "expired")),
                or_(
                    FederationOutbox.last_error.is_(None),
                    FederationOutbox.last_error != "KAED_FED_TOMBSTONE_SUPERSEDED",
                ),
            )
            .order_by(FederationOutbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    now = datetime.now(UTC)
    for row in rows:
        row.status = "pending"
        row.attempts = 0
        row.next_retry_at = now
        row.last_error = None
    await session.commit()
    return {row.destination for row in rows}


async def publish_dm_delivery_update(
    redis: Redis,
    settings: Settings,
    event: FederationEvent,
    destination: str,
    status: str,
    code: str | None,
) -> None:
    if event.event_type not in {"dm.message.create", "dm.group.message.proposed"}:
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
    durable_outbox_event = _durable_outbox_event_exists()
    stale_destinations = list(
        await session.scalars(
            select(FederationOutbox.destination)
            .where(
                FederationOutbox.status.in_(("pending", "retry", "circuit")),
                FederationOutbox.created_at < cutoff,
                ~durable_outbox_event,
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
                ~durable_outbox_event,
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
    relationship_failures: dict[tuple[str, str, str], tuple[User, User]] = {}
    for row in stale_rows:
        row.status = "expired"
        row.last_error = "KAED_FED_DELIVERY_EXPIRED"
        event = stale_events.get((row.event_origin_domain, row.event_id))
        context = expired_guild_context(event, settings.domain) if event and settings else None
        if context is not None:
            resync_targets.add((row.destination, context[0], context[1]))
        if event is not None and settings is not None:
            reconciled = await reconcile_relationship_capacity_rejection(
                session,
                settings,
                event,
                row.last_error,
            )
            if reconciled is not None:
                relationship_failures[(row.event_origin_domain, row.event_id, row.destination)] = (
                    reconciled
                )

    if settings is not None:
        # Import lazily: events imports this module's queue-limit helper.
        from app.chat.guild_revision import (
            build_guild_authority_envelope,
            guild_authority_owner,
        )
        from app.federation.events import queue_event

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
            owner = await guild_authority_owner(session, settings, guild)
            marker = await build_guild_authority_envelope(
                session,
                settings,
                guild,
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
                await publish_terminal_outbox_failure(
                    redis,
                    settings,
                    event,
                    row.destination,
                    "KAED_FED_DELIVERY_EXPIRED",
                    relationship_failures.get(
                        (row.event_origin_domain, row.event_id, row.destination)
                    ),
                )
    return len(stale_rows)


async def cleanup_federation_retention(session: AsyncSession, settings: Settings) -> int:
    # Only one retention sweep may delete and reconcile quota accounting at a
    # time. Event admission serializes per peer, while this daily repair also
    # corrects counters after interrupted migrations or operator maintenance.
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended("kaede-federation-retention", 0)))
    )
    # Durable deletion proofs have their own carrier/ack predicates.  Compact
    # them before taking the global quota rows so their canonical lock order
    # remains room/cache -> media -> global, matching event admission.
    from app.federation.terminal_rooms import cleanup_terminal_room_deletions
    from app.media.tombstones import cleanup_media_tombstone_sources

    now = datetime.now(UTC)
    durable_cleaned = await cleanup_terminal_room_deletions(
        session,
        settings,
        now=now,
    )
    durable_cleaned += await cleanup_media_tombstone_sources(
        session,
        settings,
        now=now,
    )
    # Admission takes the self/global ledger before an origin row. Keep that
    # order while retention rebuilds both counters so the sweep cannot deadlock
    # a concurrent accepted event or omit it from its database snapshot.
    await session.execute(
        select(Instance.domain).where(Instance.is_self.is_(True)).with_for_update()
    )
    await session.execute(
        select(Instance.domain)
        .where(Instance.is_self.is_(False))
        .order_by(Instance.domain)
        .with_for_update()
    )
    inbox_cutoff = now - timedelta(days=settings.federation_event_retention_days)
    events = await session.execute(delete(FederationEvent).where(FederationEvent.expires_at < now))
    inbox = await session.execute(
        delete(FederationInbox).where(
            FederationInbox.received_at < inbox_cutoff,
            ~exists(
                select(FederationEvent.event_id).where(
                    FederationEvent.origin_domain == FederationInbox.origin_domain,
                    FederationEvent.event_id == FederationInbox.event_id,
                )
            ),
        )
    )
    keys = await session.execute(
        delete(PeerKey).where(
            PeerKey.expired_at.is_not(None),
            PeerKey.expired_at < inbox_cutoff,
        )
    )
    tombstones = await session.execute(
        delete(RemoteMediaTombstone).where(
            RemoteMediaTombstone.expires_at < now,
            ~exists(
                select(Attachment.id).where(
                    Attachment.origin_domain == RemoteMediaTombstone.origin_domain,
                    Attachment.id == RemoteMediaTombstone.attachment_id,
                )
            ),
            ~exists(
                select(RemoteMediaCache.attachment_id).where(
                    RemoteMediaCache.origin_domain == RemoteMediaTombstone.origin_domain,
                    RemoteMediaCache.attachment_id == RemoteMediaTombstone.attachment_id,
                )
            ),
        )
    )
    await reconcile_federation_storage_usage(session)
    await session.commit()
    return (
        durable_cleaned
        + int(getattr(events, "rowcount", 0) or 0)
        + int(getattr(inbox, "rowcount", 0) or 0)
        + int(getattr(keys, "rowcount", 0) or 0)
        + int(getattr(tombstones, "rowcount", 0) or 0)
    )
