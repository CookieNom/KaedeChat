from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, delete, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.federation import terminal_room_event_ref, terminal_room_generation
from app.core.settings import Settings
from app.db.models import (
    Channel,
    DMConversation,
    DMParticipant,
    FederationEvent,
    FederationInbox,
    FederationOutbox,
    Guild,
    Instance,
    MediaTombstoneDestination,
    Message,
    RoomFederationRecipient,
    TerminalRoomDeletion,
    User,
)
from app.federation.events import build_envelope, queue_event
from app.federation.network import normalize_domain


@dataclass(frozen=True)
class TerminalRoomActorRef:
    id: int
    origin_domain: str


def terminal_room_key_id(envelope: dict[str, Any], origin_domain: str) -> str:
    signatures = envelope.get("signatures")
    origin_signatures = signatures.get(origin_domain) if isinstance(signatures, dict) else None
    if not isinstance(origin_signatures, dict) or len(origin_signatures) != 1:
        raise RuntimeError("terminal-room signature set is invalid")
    key_id = next(iter(origin_signatures))
    if not isinstance(key_id, str) or not key_id:
        raise RuntimeError("terminal-room signing key is invalid")
    return key_id


async def lock_terminal_room(
    session: AsyncSession,
    room_kind: str,
    room_id: int,
    room_domain: str,
) -> None:
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"kaede-terminal-room:{room_kind}:{room_domain}:{room_id}", 0)
            )
        )
    )


def _event_content(
    room_kind: str,
    base_content: dict[str, Any],
    destination: str,
    generation: int,
) -> dict[str, Any]:
    content = dict(base_content)
    content["_terminal_generation"] = str(generation)
    if room_kind == "guild":
        content["target_domain"] = destination
    return content


def terminal_room_base_content(envelope: dict[str, Any]) -> dict[str, Any]:
    """Strip per-destination/generation transport fields from deletion truth."""

    if terminal_room_event_ref(envelope) is None:
        raise ValueError("event is not an exact terminal room deletion")
    content = dict(envelope["content"])
    content.pop("_terminal_generation", None)
    content.pop("target_domain", None)
    return content


async def queue_terminal_room_deletion(
    session: AsyncSession,
    settings: Settings,
    *,
    room_kind: str,
    room_id: int,
    room_domain: str,
    actor: User | TerminalRoomActorRef,
    event_type: str,
    content: dict[str, Any],
    context: dict[str, Any],
    destinations: set[str],
) -> set[str]:
    """Queue one current-key terminal proof per unacknowledged destination.

    Rows are independent of Guild/Channel/Message lifetime.  A signing-key
    rollover replaces only unacknowledged proofs and compacts their superseded
    events, while an accepted/duplicate result permanently satisfies that
    destination.
    """

    if room_domain != settings.domain:
        raise ValueError("only the room authority may queue terminal room deletion")
    if (room_kind, event_type) not in {
        ("guild", "guild.instance_access.revoked"),
        ("group_dm", "dm.group.state"),
    }:
        raise ValueError("terminal room kind does not match its event type")
    if not 0 <= room_id <= (1 << 63) - 1:
        raise ValueError("terminal room ID is invalid")
    room_domain = normalize_domain(room_domain)
    normalized_destinations = {
        normalize_domain(destination)
        for destination in destinations
        if normalize_domain(destination) != settings.domain
    }
    if actor.id < 0 or actor.origin_domain == "":
        raise ValueError("terminal room actor is invalid")
    await lock_terminal_room(session, room_kind, room_id, room_domain)
    normalized_destinations.update(
        await session.scalars(
            select(RoomFederationRecipient.destination_domain).where(
                RoomFederationRecipient.room_kind == room_kind,
                RoomFederationRecipient.room_id == room_id,
                RoomFederationRecipient.room_domain == room_domain,
            )
        )
    )
    instance = await session.get(Instance, settings.domain)
    if instance is None or not instance.is_self or not instance.current_key_id:
        raise RuntimeError("local federation signing identity is unavailable")
    rows = list(
        await session.scalars(
            select(TerminalRoomDeletion)
            .where(
                TerminalRoomDeletion.room_kind == room_kind,
                TerminalRoomDeletion.room_id == room_id,
                TerminalRoomDeletion.room_domain == room_domain,
            )
            .order_by(TerminalRoomDeletion.destination_domain)
            .with_for_update()
        )
    )
    by_destination = {row.destination_domain: row for row in rows}
    if rows:
        template = rows[0]
        if (
            template.actor_id != actor.id
            or template.actor_domain != actor.origin_domain
            or template.event_type != event_type
            or template.content != content
            or template.context != context
        ):
            raise RuntimeError("terminal room deletion conflicts with retained truth")

    max_generation = max((row.generation for row in rows), default=0)
    rotating = any(
        row.acknowledged_at is None and row.key_id != instance.current_key_id for row in rows
    )
    generation = max_generation + 1 if rotating or max_generation == 0 else max_generation
    if generation > (1 << 63) - 1:
        raise RuntimeError("terminal room generation is exhausted")

    old_event_ids: set[str] = set()
    wakes: set[str] = set()
    for destination in sorted(normalized_destinations | set(by_destination)):
        row = by_destination.get(destination)
        if row is not None and row.acknowledged_at is not None:
            continue
        event = (
            await session.get(FederationEvent, (settings.domain, row.event_id))
            if row is not None and row.key_id == instance.current_key_id and not rotating
            else None
        )
        envelope: dict[str, Any]
        if (
            event is not None
            and isinstance(event.envelope, dict)
            and terminal_room_event_ref(event.envelope) == (room_kind, room_id, room_domain)
            and terminal_room_generation(event.envelope) == generation
        ):
            envelope = event.envelope
        else:
            envelope = await build_envelope(
                session,
                settings,
                event_type,
                cast(User, actor),
                _event_content(room_kind, content, destination, generation),
                context=context,
                # This helper only emits exact terminal room controls.  The
                # envelope builder independently restricts remote guild actors
                # to the exact guild-deleted shape.
                retained_authority_attested_actor=actor.origin_domain != settings.domain,
            )
            if terminal_room_event_ref(envelope) != (room_kind, room_id, room_domain):
                raise RuntimeError("generated terminal room envelope is invalid")
        await queue_event(session, settings, destination, envelope)
        event_id = str(envelope["event_id"])
        key_id = terminal_room_key_id(envelope, settings.domain)
        if row is None:
            row = TerminalRoomDeletion(
                room_kind=room_kind,
                room_id=room_id,
                room_domain=room_domain,
                destination_domain=destination,
                actor_id=actor.id,
                actor_domain=actor.origin_domain,
                event_type=event_type,
                content=content,
                context=context,
                event_id=event_id,
                key_id=key_id,
                generation=generation,
                updated_at=datetime.now(UTC),
            )
            session.add(row)
            by_destination[destination] = row
        else:
            if row.event_id != event_id:
                old_event_ids.add(row.event_id)
            row.event_id = event_id
            row.key_id = key_id
            row.generation = generation
            row.updated_at = datetime.now(UTC)
        wakes.add(destination)

    if old_event_ids:
        await session.flush()
        await session.execute(
            delete(FederationEvent).where(
                FederationEvent.origin_domain == settings.domain,
                FederationEvent.event_id.in_(old_event_ids),
            )
        )
    # The per-destination terminal rows are now the authoritative historical
    # route/ACK ledger. Consuming the pre-delete access ledger here prevents
    # room history from becoming permanent duplicate state.
    await session.execute(
        delete(RoomFederationRecipient).where(
            RoomFederationRecipient.room_kind == room_kind,
            RoomFederationRecipient.room_id == room_id,
            RoomFederationRecipient.room_domain == room_domain,
        )
    )
    return wakes


async def acknowledge_terminal_room_delivery(
    session: AsyncSession,
    *,
    destination: str,
    envelope: dict[str, Any],
) -> bool:
    ref = terminal_room_event_ref(envelope)
    if ref is None:
        return False
    room_kind, room_id, room_domain = ref
    row = await session.get(
        TerminalRoomDeletion,
        (room_kind, room_id, room_domain, normalize_domain(destination)),
        with_for_update=True,
    )
    if row is None:
        return False
    # Deletion is immutable. Acceptance of any valid generation satisfies the
    # destination even if key rollover advanced the retained current proof
    # before this response was committed locally.
    actor = envelope.get("actor")
    if (
        not isinstance(actor, dict)
        or actor.get("id") != str(row.actor_id)
        or actor.get("domain") != row.actor_domain
        or envelope.get("type") != row.event_type
    ):
        raise RuntimeError("terminal room acknowledgement conflicts with retained truth")
    row.acknowledged_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return True


async def repair_terminal_room_delivery_acks(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> int:
    """Repair the post-outbox-commit acknowledgement crash window."""

    if not 1 <= limit <= 10_000:
        raise ValueError("invalid terminal room ACK repair limit")
    refs = list(
        (
            await session.execute(
                select(
                    TerminalRoomDeletion.room_kind,
                    TerminalRoomDeletion.room_id,
                    TerminalRoomDeletion.room_domain,
                    TerminalRoomDeletion.destination_domain,
                )
                .where(
                    TerminalRoomDeletion.acknowledged_at.is_(None),
                    exists(
                        select(FederationOutbox.id).where(
                            FederationOutbox.destination == TerminalRoomDeletion.destination_domain,
                            FederationOutbox.event_origin_domain
                            == TerminalRoomDeletion.room_domain,
                            FederationOutbox.event_id == TerminalRoomDeletion.event_id,
                            FederationOutbox.status == "delivered",
                        )
                    ),
                )
                .order_by(
                    TerminalRoomDeletion.room_kind,
                    TerminalRoomDeletion.room_domain,
                    TerminalRoomDeletion.room_id,
                    TerminalRoomDeletion.destination_domain,
                )
                .limit(limit)
            )
        ).tuples()
    )
    repaired = 0
    for room_kind, room_id, room_domain, destination in refs:
        await lock_terminal_room(session, room_kind, room_id, room_domain)
        row = await session.get(
            TerminalRoomDeletion,
            (room_kind, room_id, room_domain, destination),
            with_for_update=True,
        )
        if row is None or row.acknowledged_at is not None:
            continue
        delivered = await session.scalar(
            select(FederationOutbox.id).where(
                FederationOutbox.destination == destination,
                FederationOutbox.event_origin_domain == room_domain,
                FederationOutbox.event_id == row.event_id,
                FederationOutbox.status == "delivered",
            )
        )
        if delivered is None:
            continue
        row.acknowledged_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        repaired += 1
    return repaired


async def rotate_terminal_room_deletions(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 100,
) -> set[str]:
    """Re-sign unacknowledged terminal room truth after local key rollover."""

    if not 1 <= limit <= 10_000:
        raise ValueError("invalid terminal room rotation limit")
    instance = await session.get(Instance, settings.domain)
    if instance is None or not instance.is_self or not instance.current_key_id:
        raise RuntimeError("local federation signing identity is unavailable")
    room_refs = list(
        (
            await session.execute(
                select(
                    TerminalRoomDeletion.room_kind,
                    TerminalRoomDeletion.room_id,
                    TerminalRoomDeletion.room_domain,
                )
                .where(
                    TerminalRoomDeletion.room_domain == settings.domain,
                    TerminalRoomDeletion.acknowledged_at.is_(None),
                    TerminalRoomDeletion.key_id != instance.current_key_id,
                )
                .distinct()
                .order_by(
                    TerminalRoomDeletion.room_kind,
                    TerminalRoomDeletion.room_domain,
                    TerminalRoomDeletion.room_id,
                )
                .limit(limit)
            )
        ).tuples()
    )
    wakes: set[str] = set()
    for room_kind, room_id, room_domain in room_refs:
        template = await session.scalar(
            select(TerminalRoomDeletion)
            .where(
                TerminalRoomDeletion.room_kind == room_kind,
                TerminalRoomDeletion.room_id == room_id,
                TerminalRoomDeletion.room_domain == room_domain,
            )
            .order_by(TerminalRoomDeletion.destination_domain)
        )
        if template is None:
            continue
        wakes.update(
            await queue_terminal_room_deletion(
                session,
                settings,
                room_kind=room_kind,
                room_id=room_id,
                room_domain=room_domain,
                actor=TerminalRoomActorRef(
                    id=template.actor_id,
                    origin_domain=template.actor_domain,
                ),
                event_type=template.event_type,
                content=template.content,
                context=template.context,
                destinations=set(),
            )
        )
    return wakes


def _terminal_room_cleanup_candidates(
    settings: Settings,
    *,
    cutoff: datetime,
    limit: int,
) -> Any:
    """Select fully cleanable rooms before applying the cleanup batch limit."""

    row = aliased(TerminalRoomDeletion)
    blocker = aliased(TerminalRoomDeletion)
    incomplete_room = exists(
        select(blocker.room_id).where(
            blocker.room_kind == row.room_kind,
            blocker.room_id == row.room_id,
            blocker.room_domain == row.room_domain,
            or_(
                blocker.acknowledged_at.is_(None),
                blocker.updated_at >= cutoff,
            ),
        )
    ).correlate(row)
    retained_media_route = exists(
        select(MediaTombstoneDestination.attachment_id).where(
            MediaTombstoneDestination.room_kind == row.room_kind,
            MediaTombstoneDestination.room_id == row.room_id,
            MediaTombstoneDestination.room_domain == row.room_domain,
        )
    ).correlate(row)
    guild_projection = exists(
        select(Guild.id).where(
            Guild.id == row.room_id,
            Guild.origin_domain == row.room_domain,
        )
    ).correlate(row)
    group_conversation = exists(
        select(DMConversation.id).where(
            DMConversation.id == row.room_id,
            DMConversation.origin_domain == row.room_domain,
        )
    ).correlate(row)
    terminal_group_conversation = exists(
        select(DMConversation.id).where(
            DMConversation.id == row.room_id,
            DMConversation.origin_domain == row.room_domain,
            DMConversation.type == "group",
        )
    ).correlate(row)
    group_channel = exists(
        select(Channel.id).where(
            Channel.id == row.room_id,
            Channel.origin_domain == row.room_domain,
        )
    ).correlate(row)
    unavailable_group_channel = exists(
        select(Channel.id).where(
            Channel.id == row.room_id,
            Channel.origin_domain == row.room_domain,
            Channel.guild_id.is_(None),
            Channel.unavailable.is_(True),
        )
    ).correlate(row)
    group_participant = exists(
        select(DMParticipant.user_id).where(
            DMParticipant.conversation_id == row.room_id,
            DMParticipant.conversation_domain == row.room_domain,
        )
    ).correlate(row)
    group_message = exists(
        select(Message.id).where(
            Message.channel_id == row.room_id,
            Message.channel_domain == row.room_domain,
        )
    ).correlate(row)
    group_projection_absent = and_(~group_conversation, ~group_channel)
    group_projection_terminal = and_(
        terminal_group_conversation,
        unavailable_group_channel,
        ~group_participant,
        ~group_message,
    )
    receiver_cleanable = and_(
        row.room_domain != settings.domain,
        ~retained_media_route,
        or_(
            and_(row.room_kind == "guild", ~guild_projection),
            and_(
                row.room_kind == "group_dm",
                or_(group_projection_absent, group_projection_terminal),
            ),
        ),
    )
    return (
        select(row.room_kind, row.room_id, row.room_domain)
        .where(
            row.acknowledged_at.is_not(None),
            row.updated_at < cutoff,
            ~incomplete_room,
            or_(row.room_domain == settings.domain, receiver_cleanable),
        )
        .distinct()
        .order_by(row.room_kind, row.room_domain, row.room_id)
        .limit(limit)
    )


async def _remove_terminal_group_projection(
    session: AsyncSession,
    *,
    room_id: int,
    room_domain: str,
) -> bool:
    """Remove a receiver's empty terminal projection after the replay horizon."""

    conversation = await session.get(DMConversation, (room_id, room_domain))
    channel = await session.get(Channel, (room_id, room_domain))
    if conversation is None and channel is None:
        return True
    if (
        conversation is None
        or channel is None
        or conversation.type != "group"
        or not channel.unavailable
        or channel.guild_id is not None
    ):
        return False
    if (
        await session.scalar(
            select(DMParticipant.user_id)
            .where(
                DMParticipant.conversation_id == room_id,
                DMParticipant.conversation_domain == room_domain,
            )
            .limit(1)
        )
        is not None
        or await session.scalar(
            select(Message.id)
            .where(
                Message.channel_id == room_id,
                Message.channel_domain == room_domain,
            )
            .limit(1)
        )
        is not None
    ):
        return False
    # The Channel and DMConversation identities mutually reference one another
    # with ON DELETE CASCADE. Deleting the conversation tears down the empty
    # unavailable channel and its remaining projection-only dependants.
    await session.delete(conversation)
    await session.flush()
    return True


async def cleanup_terminal_room_deletions(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    """Compact fully acknowledged terminal-room transport state.

    Sender rows become the complete historical destination ledger when a room
    is deleted. Once every destination acknowledged and the ordinary replay
    horizon elapsed, their outboxes/events and room-scoped media routes are no
    longer needed. Receiver receipts remain while any room projection or route
    can still be resurrected from local retained state.
    """

    if not 1 <= limit <= 10_000:
        raise ValueError("invalid terminal room cleanup limit")
    cutoff = now - timedelta(
        days=settings.federation_event_retention_days,
        seconds=settings.federation_clock_skew_seconds,
    )
    refs = list(
        (
            await session.execute(
                _terminal_room_cleanup_candidates(
                    settings,
                    cutoff=cutoff,
                    limit=limit,
                )
            )
        ).tuples()
    )
    cleaned = 0
    for room_kind, room_id, room_domain in refs:
        await lock_terminal_room(session, room_kind, room_id, room_domain)
        rows = list(
            await session.scalars(
                select(TerminalRoomDeletion)
                .where(
                    TerminalRoomDeletion.room_kind == room_kind,
                    TerminalRoomDeletion.room_id == room_id,
                    TerminalRoomDeletion.room_domain == room_domain,
                )
                .order_by(TerminalRoomDeletion.destination_domain)
                .with_for_update()
            )
        )
        if not rows or any(row.acknowledged_at is None or row.updated_at >= cutoff for row in rows):
            continue
        if room_domain == settings.domain:
            # Every historical room destination acknowledged the authoritative
            # terminal control; those ACKs supersede route-only media history.
            await session.execute(
                delete(MediaTombstoneDestination).where(
                    MediaTombstoneDestination.room_kind == room_kind,
                    MediaTombstoneDestination.room_id == room_id,
                    MediaTombstoneDestination.room_domain == room_domain,
                )
            )
            await session.execute(
                delete(RoomFederationRecipient).where(
                    RoomFederationRecipient.room_kind == room_kind,
                    RoomFederationRecipient.room_id == room_id,
                    RoomFederationRecipient.room_domain == room_domain,
                )
            )
        else:
            route_exists = (
                await session.scalar(
                    select(MediaTombstoneDestination.attachment_id)
                    .where(
                        MediaTombstoneDestination.room_kind == room_kind,
                        MediaTombstoneDestination.room_id == room_id,
                        MediaTombstoneDestination.room_domain == room_domain,
                    )
                    .limit(1)
                )
                is not None
            )
            if route_exists:
                continue
            if room_kind == "guild":
                if await session.get(Guild, (room_id, room_domain)) is not None:
                    continue
            elif not await _remove_terminal_group_projection(
                session,
                room_id=room_id,
                room_domain=room_domain,
            ):
                continue
            await session.execute(
                delete(RoomFederationRecipient).where(
                    RoomFederationRecipient.room_kind == room_kind,
                    RoomFederationRecipient.room_id == room_id,
                    RoomFederationRecipient.room_domain == room_domain,
                )
            )
        event_refs = {(row.room_domain, row.event_id) for row in rows}
        await session.execute(
            delete(TerminalRoomDeletion).where(
                TerminalRoomDeletion.room_kind == room_kind,
                TerminalRoomDeletion.room_id == room_id,
                TerminalRoomDeletion.room_domain == room_domain,
            )
        )
        await session.execute(
            delete(FederationInbox).where(
                tuple_(FederationInbox.origin_domain, FederationInbox.event_id).in_(event_refs)
            )
        )
        await session.execute(
            delete(FederationEvent).where(
                tuple_(FederationEvent.origin_domain, FederationEvent.event_id).in_(event_refs)
            )
        )
        cleaned += len(rows)
    return cleaned
