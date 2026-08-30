from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.target_contract import authority_attested_application_target
from app.chat.dm_mutations import authority_attested_dm_message_mutation
from app.chat.e2ee_controls import (
    authority_attested_direct_dm_control,
    authority_attested_room_policy_change,
)
from app.chat.forwarding import authority_attested_forward_source
from app.chat.poll_results import (
    authority_attested_direct_poll_result,
    authority_attested_dm_poll_mutation,
)
from app.core.federation import (
    DURABLE_LATEST_STATE_EVENTS,
    POLICY_HELD_OUTBOX_PREFIX,
    SECURITY_CRITICAL_GUILD_EVENTS,
    authority_attested_group_event_ref,
    authority_attested_guild_owner_actor,
    authority_attested_media_delete_ref,
    authority_attested_terminal_guild_actor,
    canonical_json,
    durable_guild_media_delete_request,
    durable_terminal_room_event,
    federation_policy_holds_event,
    guild_crosspost_authority_event_ref,
    guild_media_delete_request_ref,
    guild_message_authority_event_refs,
    policy_held_retry_at,
    sign_envelope,
)
from app.core.settings import Settings
from app.db.models import (
    Attachment,
    AttachmentFederationRecipient,
    Channel,
    FederationEvent,
    FederationOutbox,
    Guild,
    GuildMember,
    Instance,
    MediaTombstoneDestination,
    MediaTombstoneSource,
    RoomFederationRecipient,
    User,
)
from app.federation.delivery import MAX_QUEUE_AGE, enforce_queue_limits
from app.federation.network import (
    FederationNetworkError,
    ensure_remote_instance_record,
    normalize_domain,
)
from app.federation.security import matching_block, self_private_key


def attachment_refs_from_payloads(payloads: object) -> set[tuple[int, str]]:
    """Extract normalized attachment identities from rendered message payloads."""

    if not isinstance(payloads, list):
        return set()
    attachment_refs: set[tuple[int, str]] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            continue
        for raw in attachments:
            if not isinstance(raw, dict):
                continue
            raw_id = raw.get("id")
            raw_domain = raw.get("origin_domain")
            if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool):
                continue
            if not isinstance(raw_domain, str):
                continue
            try:
                attachment_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not (0 <= attachment_id <= (1 << 63) - 1) or str(attachment_id) != str(raw_id):
                continue
            try:
                attachment_domain = normalize_domain(raw_domain)
            except FederationNetworkError:
                continue
            attachment_refs.add((attachment_id, attachment_domain))
    return attachment_refs


def media_delete_generation(envelope: dict[str, Any]) -> int:
    """Return the signed monotonic generation of a media tombstone.

    Generation zero is reserved for pre-generation protocol events. New
    senders always emit a positive canonical decimal string. Keeping the value
    inside the signed content lets every relay independently reject a delayed
    older proof after signing-key rotation without trusting receive order.
    """

    content = envelope.get("content")
    if not isinstance(content, dict):
        raise ValueError("media tombstone content is invalid")
    raw_generation = content.get("generation")
    if raw_generation is None:
        return 0
    if (
        not isinstance(raw_generation, str)
        or not raw_generation.isascii()
        or not raw_generation.isdecimal()
        or (len(raw_generation) > 1 and raw_generation.startswith("0"))
    ):
        raise ValueError("media tombstone generation is invalid")
    generation = int(raw_generation)
    if not 1 <= generation <= (1 << 63) - 1:
        raise ValueError("media tombstone generation is invalid")
    return generation


def media_delete_order(envelope: dict[str, Any]) -> tuple[int, int, str]:
    """Return a deterministic signed ordering key for one tombstone proof."""

    if envelope.get("type") != "media.delete":
        raise ValueError("event is not a media tombstone")
    raw_timestamp = envelope.get("ts")
    event_id = envelope.get("event_id")
    if not isinstance(raw_timestamp, int) or isinstance(raw_timestamp, bool):
        raise ValueError("media tombstone timestamp is invalid")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("media tombstone event ID is invalid")
    return media_delete_generation(envelope), raw_timestamp, event_id


async def retained_media_delete_events(
    session: AsyncSession,
    attachment_id: int,
    attachment_domain: str,
) -> list[FederationEvent]:
    """Load retained origin proofs for an attachment in deterministic order."""

    attachment_domain = normalize_domain(attachment_domain)
    events = list(
        await session.scalars(
            select(FederationEvent).where(
                FederationEvent.origin_domain == attachment_domain,
                FederationEvent.event_type == "media.delete",
                FederationEvent.envelope["content"]["attachment_id"].as_string()
                == str(attachment_id),
                FederationEvent.envelope["content"]["origin_domain"].as_string()
                == attachment_domain,
            )
        )
    )
    valid: list[FederationEvent] = []
    for event in events:
        if not isinstance(event.envelope, dict):
            continue
        try:
            media_delete_order(event.envelope)
        except ValueError:
            continue
        valid.append(event)
    return sorted(valid, key=lambda event: media_delete_order(event.envelope), reverse=True)


async def locked_retained_media_delete_events(
    session: AsyncSession,
    attachment_id: int,
    attachment_domain: str,
) -> list[FederationEvent]:
    """Serialize proof selection with remote tombstone/cache mutation."""

    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    return await retained_media_delete_events(
        session,
        attachment_id,
        attachment_domain,
    )


def message_attachment_refs(envelope: dict[str, Any]) -> set[tuple[int, str]]:
    """Extract media origins disclosed by a message federation event.

    Authoritative message events nest the rendered message under ``message``.
    Offline remote-guild proxy proposals carry the rendered attachments
    directly in ``content``. Both shapes disclose identical media metadata.
    """

    content = envelope.get("content")
    if not isinstance(content, dict):
        return set()
    message = content.get("message")
    payloads: list[dict[str, Any]] = []
    if isinstance(message, dict):
        payloads.append(message)
    if isinstance(content.get("attachments"), list):
        payloads.append({"attachments": content["attachments"]})
    return attachment_refs_from_payloads(payloads)


def metadata_room_ref(envelope: dict[str, Any]) -> tuple[str, int, str] | None:
    """Return a durable room identity carried by event metadata."""

    context = envelope.get("context")
    if not isinstance(context, dict):
        return None
    raw_guild_id = context.get("guild_id")
    guild_domain = context.get("guild_domain")
    if isinstance(guild_domain, str) and raw_guild_id is not None:
        try:
            guild_id = int(str(raw_guild_id))
        except ValueError:
            return None
        if 0 <= guild_id <= (1 << 63) - 1:
            return "guild", guild_id, normalize_domain(guild_domain)
    if envelope.get("type") == "e2ee.room-policy.changed":
        scope = context.get("scope")
        if isinstance(scope, dict) and scope.get("type") == "guild":
            raw_scope_id = scope.get("id")
            scope_domain = scope.get("domain")
            if isinstance(scope_domain, str) and raw_scope_id is not None:
                try:
                    scope_id = int(str(raw_scope_id))
                except ValueError:
                    return None
                if 0 <= scope_id <= (1 << 63) - 1:
                    return "guild", scope_id, normalize_domain(scope_domain)
    if envelope.get("type") in {
        "dm.group.call.create",
        "dm.group.message.proposed",
        "dm.group.message.committed",
    }:
        raw_conversation_id = context.get("conversation_id")
        conversation_domain = context.get("conversation_domain")
        if isinstance(conversation_domain, str) and raw_conversation_id is not None:
            try:
                conversation_id = int(str(raw_conversation_id))
            except ValueError:
                return None
            if 0 <= conversation_id <= (1 << 63) - 1:
                return "group_dm", conversation_id, normalize_domain(conversation_domain)
    if envelope.get("type") == "dm.group.state":
        content = envelope.get("content")
        conversation = content.get("conversation") if isinstance(content, dict) else None
        if isinstance(conversation, dict):
            raw_conversation_id = conversation.get("id")
            conversation_domain = conversation.get("origin_domain")
            if isinstance(conversation_domain, str) and raw_conversation_id is not None:
                try:
                    conversation_id = int(str(raw_conversation_id))
                except ValueError:
                    return None
                if 0 <= conversation_id <= (1 << 63) - 1:
                    return "group_dm", conversation_id, normalize_domain(conversation_domain)
    return None


async def lock_room_federation_ref(
    session: AsyncSession,
    room_ref: tuple[str, int, str],
) -> None:
    room_kind, room_id, room_domain = room_ref
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"kaede-terminal-room:{room_kind}:{room_domain}:{room_id}", 0)
            )
        )
    )


async def record_room_federation_recipient(
    session: AsyncSession,
    room_ref: tuple[str, int, str] | None,
    destination: str,
    *,
    _lock_held: bool = False,
) -> bool:
    if room_ref is None:
        return False
    room_kind, room_id, room_domain = room_ref
    room_domain = normalize_domain(room_domain)
    destination = normalize_domain(destination)
    if room_kind not in {"guild", "group_dm"} or not 0 <= room_id <= (1 << 63) - 1:
        raise ValueError("federation room recipient reference is invalid")
    normalized_ref = (room_kind, room_id, room_domain)
    if not _lock_held:
        await lock_room_federation_ref(session, normalized_ref)
    if destination == room_domain:
        return False
    await session.execute(
        pg_insert(RoomFederationRecipient)
        .values(
            room_kind=room_kind,
            room_id=room_id,
            room_domain=room_domain,
            destination_domain=destination,
        )
        .on_conflict_do_nothing(
            index_elements=[
                "room_kind",
                "room_id",
                "room_domain",
                "destination_domain",
            ]
        )
    )
    return True


async def record_attachment_recipients(
    session: AsyncSession,
    attachment_refs: set[tuple[int, str]],
    destination: str,
    *,
    room_ref: tuple[str, int, str] | None = None,
    _locks_held: bool = False,
    _room_lock_held: bool = False,
) -> set[tuple[int, str]]:
    """Persist every extant attachment whose metadata reached a peer.

    Federation history can retain an event after its attachment row has been
    retired. Limiting inserts to rows that still exist avoids turning such a
    harmless replay into a foreign-key failure while preserving every media
    object that can still require a later tombstone.
    """

    destination = normalize_domain(destination)
    candidates = {
        (attachment_id, normalize_domain(attachment_domain))
        for attachment_id, attachment_domain in attachment_refs
        if 0 <= attachment_id <= (1 << 63) - 1
        and normalize_domain(attachment_domain) != destination
    }
    normalized_room: tuple[str, int, str] | None = None
    if room_ref is not None:
        room_kind, room_id, room_domain = room_ref
        if room_kind not in {"guild", "group_dm"} or not 0 <= room_id <= (1 << 63) - 1:
            raise ValueError("attachment disclosure room is invalid")
        normalized_room = (room_kind, room_id, normalize_domain(room_domain))
        await record_room_federation_recipient(
            session,
            normalized_room,
            destination,
            _lock_held=_room_lock_held,
        )
        room = (
            await session.get(Guild, (room_id, normalized_room[2]))
            if room_kind == "guild"
            else await session.get(Channel, (room_id, normalized_room[2]))
        )
        if room is None or room in session.deleted or room.unavailable:
            raise RuntimeError("federation room became terminal before disclosure")
    # Global room-bearing order is room fence -> sorted media refs -> outbox.
    # Non-room media disclosure begins at the media-ref step.
    if not _locks_held:
        from app.media.tombstones import lock_media_tombstone_ref

        for attachment_id, attachment_domain in sorted(
            candidates, key=lambda ref: (ref[1], ref[0])
        ):
            await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    if not candidates:
        return set()
    existing = set(
        (
            await session.execute(
                select(Attachment.id, Attachment.origin_domain).where(
                    tuple_(Attachment.id, Attachment.origin_domain).in_(candidates)
                )
            )
        ).tuples()
    )
    for attachment_id, attachment_domain in sorted(existing):
        await session.execute(
            pg_insert(AttachmentFederationRecipient)
            .values(
                attachment_id=attachment_id,
                attachment_domain=attachment_domain,
                destination_domain=destination,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "attachment_id",
                    "attachment_domain",
                    "destination_domain",
                ]
            )
        )
    # This route must survive Attachment/message eviction. A remote authority
    # can fan A's metadata to C and later lose its own replica before A reaches
    # a terminal verdict; the independent row is what lets B relay A's eventual
    # origin-signed proof to C.
    for attachment_id, attachment_domain in sorted(candidates):
        values: dict[str, Any] = {
            "attachment_id": attachment_id,
            "attachment_domain": attachment_domain,
            "destination_domain": destination,
        }
        if normalized_room is not None:
            values.update(
                room_kind=normalized_room[0],
                room_id=normalized_room[1],
                room_domain=normalized_room[2],
            )
        inserted = pg_insert(MediaTombstoneDestination).values(**values)
        await session.execute(
            inserted.on_conflict_do_update(
                index_elements=[
                    "attachment_id",
                    "attachment_domain",
                    "destination_domain",
                ],
                set_={
                    "room_kind": func.coalesce(
                        MediaTombstoneDestination.room_kind, inserted.excluded.room_kind
                    ),
                    "room_id": func.coalesce(
                        MediaTombstoneDestination.room_id, inserted.excluded.room_id
                    ),
                    "room_domain": func.coalesce(
                        MediaTombstoneDestination.room_domain, inserted.excluded.room_domain
                    ),
                },
            )
        )
        if normalized_room is not None:
            retained_route = await session.get(
                MediaTombstoneDestination,
                (attachment_id, attachment_domain, destination),
                populate_existing=True,
            )
            if (
                retained_route is None
                or (
                    retained_route.room_kind,
                    retained_route.room_id,
                    retained_route.room_domain,
                )
                != normalized_room
            ):
                raise RuntimeError("attachment was disclosed under conflicting room identities")
    # Callers use a non-empty return to decide whether a synchronous metadata
    # response needs a commit. Return every durable route, including raw
    # history references whose Attachment replica was already evicted.
    return candidates


async def build_envelope(
    session: AsyncSession,
    settings: Settings,
    event_type: str,
    actor: User,
    content: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    authority_attested_actor: bool = False,
    authority_attested_guild: Guild | None = None,
    authority_attested_guild_message_author: GuildMember | None = None,
    retained_authority_attested_actor: bool = False,
) -> dict[str, Any]:
    # Imported lazily because developer projections publish through this
    # module; keeping the contract check here avoids an import cycle.
    from app.bots.developer_projection import authority_attested_developer_team_snapshot
    from app.bots.dm_capability import authority_attested_bot_dm_capability
    from app.bots.interaction_events import authority_attested_interaction_response
    from app.chat.expression_authorization import authority_attested_expression_use

    draft_guild_media_request = {
        "event_id": "kcfe_authority-check",
        "origin": settings.domain,
        "type": event_type,
        "ts": 0,
        "actor": {"id": str(actor.id), "domain": actor.origin_domain},
        "context": context or {},
        "content": content,
        "signatures": {settings.domain: {"authority-check": "signature"}},
    }
    exact_guild_media_request = (
        guild_media_delete_request_ref(draft_guild_media_request) is not None
    )
    exact_media_delete = (
        authority_attested_media_delete_ref(
            draft_guild_media_request,
            expected_authority=settings.domain,
        )
        is not None
    )
    explicit_authority_actor = authority_attested_actor and (
        authority_attested_group_event_ref(
            event_type,
            content,
            context or {},
            expected_authority=settings.domain,
            actor_id=str(actor.id),
            actor_domain=actor.origin_domain,
        )
        is not None
        or authority_attested_direct_dm_control(
            event_type,
            content,
            expected_authority=settings.domain,
            actor_id=str(actor.id),
            actor_domain=actor.origin_domain,
        )
        or authority_attested_room_policy_change(
            event_type,
            content,
            context,
            expected_authority=settings.domain,
            actor_id=str(actor.id),
            actor_domain=actor.origin_domain,
        )
        or authority_attested_direct_poll_result(
            event_type,
            content,
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
        or authority_attested_dm_poll_mutation(
            event_type,
            content,
            context or {},
            expected_authority=settings.domain,
        )
        or authority_attested_dm_message_mutation(
            event_type,
            content,
            context or {},
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
        or authority_attested_application_target(
            event_type,
            content,
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
        or authority_attested_bot_dm_capability(
            event_type,
            content,
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
        or authority_attested_developer_team_snapshot(
            event_type,
            content,
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
        or authority_attested_interaction_response(
            event_type,
            content,
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
        or authority_attested_forward_source(
            event_type,
            content,
            context or {},
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
        or authority_attested_expression_use(
            event_type,
            content,
            context or {},
            expected_authority=settings.domain,
            actor=(str(actor.id), actor.origin_domain),
        )
    )
    retained_authority_actor = retained_authority_attested_actor and (
        authority_attested_terminal_guild_actor(
            event_type,
            content,
            context,
            expected_authority=settings.domain,
            actor_id=str(actor.id),
            actor_domain=actor.origin_domain,
        )
        or exact_guild_media_request
        or exact_media_delete
    )
    authority_message_refs = guild_message_authority_event_refs(
        event_type,
        content,
        context,
        expected_authority=settings.domain,
    )
    authority_message_author = bool(
        authority_attested_guild is not None
        and authority_attested_guild_message_author is not None
        and authority_message_refs
        == (
            authority_attested_guild.id,
            authority_attested_guild.origin_domain,
            authority_attested_guild_message_author.user_id,
            authority_attested_guild_message_author.user_domain,
        )
        and (
            authority_attested_guild_message_author.guild_id,
            authority_attested_guild_message_author.guild_domain,
        )
        == (
            authority_attested_guild.id,
            authority_attested_guild.origin_domain,
        )
    )
    guild_owner_actor = bool(
        authority_attested_guild is not None
        and authority_attested_guild.origin_domain == settings.domain
        and (
            authority_attested_guild.owner_id,
            authority_attested_guild.owner_domain,
        )
        == (actor.id, actor.origin_domain)
        and (
            authority_attested_guild_owner_actor(
                event_type,
                context,
                expected_authority=settings.domain,
                expected_guild_id=authority_attested_guild.id,
                expected_owner=(
                    authority_attested_guild.owner_id,
                    authority_attested_guild.owner_domain,
                ),
                actor=(actor.id, actor.origin_domain),
            )
            or authority_message_author
            or guild_crosspost_authority_event_ref(
                event_type,
                content,
                context,
                expected_authority=settings.domain,
            )
            == (authority_attested_guild.id, settings.domain)
            or exact_guild_media_request
        )
    )
    remote_authority_actor = actor.origin_domain != settings.domain and (
        explicit_authority_actor or retained_authority_actor or guild_owner_actor
    )
    if actor.origin_domain != settings.domain and not remote_authority_actor:
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


async def discard_superseded_latest_state_event(
    session: AsyncSession,
    *,
    destination: str,
    event_type: str,
    actor_ref: tuple[int, str] | None = None,
    channel_ref: tuple[int, str] | None = None,
    application_ref: tuple[int, str] | None = None,
    team_ref: tuple[int, str] | None = None,
    target_domain: str | None = None,
    grant_id: str | None = None,
) -> None:
    """Keep one durable latest-state projection for an exact destination key."""

    if event_type not in DURABLE_LATEST_STATE_EVENTS:
        raise ValueError("latest-state compaction requires a durable event type")
    if not any((actor_ref, channel_ref, application_ref, team_ref, grant_id)):
        raise ValueError("latest-state compaction requires an identity")
    destination = normalize_domain(destination)
    conditions = [
        FederationOutbox.destination == destination,
        FederationEvent.event_type == event_type,
    ]
    if actor_ref is not None:
        conditions.extend(
            (
                FederationEvent.envelope["actor"]["id"].as_string() == str(actor_ref[0]),
                FederationEvent.envelope["actor"]["domain"].as_string() == actor_ref[1],
            )
        )
    if channel_ref is not None:
        conditions.extend(
            (
                FederationEvent.envelope["content"]["channel_id"].as_string()
                == str(channel_ref[0]),
                FederationEvent.envelope["content"]["channel_domain"].as_string() == channel_ref[1],
            )
        )
    if application_ref is not None:
        conditions.extend(
            (
                FederationEvent.envelope["content"]["application_id"].as_string()
                == str(application_ref[0]),
                FederationEvent.envelope["content"]["application_domain"].as_string()
                == application_ref[1],
            )
        )
    if team_ref is not None:
        conditions.extend(
            (
                FederationEvent.envelope["content"]["team_id"].as_string() == str(team_ref[0]),
                FederationEvent.envelope["content"]["team_domain"].as_string() == team_ref[1],
            )
        )
    if target_domain is not None:
        conditions.append(
            FederationEvent.envelope["content"]["target_domain"].as_string()
            == normalize_domain(target_domain)
        )
    if grant_id is not None:
        conditions.append(FederationEvent.envelope["content"]["grant_id"].as_string() == grant_id)
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"kaede-outbox:{destination}", 0)))
    )
    old_refs = list(
        (
            await session.execute(
                select(FederationEvent.origin_domain, FederationEvent.event_id)
                .join(
                    FederationOutbox,
                    (FederationOutbox.event_origin_domain == FederationEvent.origin_domain)
                    & (FederationOutbox.event_id == FederationEvent.event_id),
                )
                .where(*conditions)
            )
        ).all()
    )
    if not old_refs:
        return
    await session.execute(
        delete(FederationOutbox).where(
            FederationOutbox.destination == destination,
            tuple_(FederationOutbox.event_origin_domain, FederationOutbox.event_id).in_(old_refs),
        )
    )
    await session.execute(
        delete(FederationEvent).where(
            tuple_(FederationEvent.origin_domain, FederationEvent.event_id).in_(old_refs),
            ~exists(
                select(FederationOutbox.id).where(
                    FederationOutbox.event_origin_domain == FederationEvent.origin_domain,
                    FederationOutbox.event_id == FederationEvent.event_id,
                )
            ),
        )
    )


async def queue_event(
    session: AsyncSession,
    settings: Settings,
    destination: str,
    envelope: dict[str, Any],
    *,
    discover_destination: bool = True,
    requeue_existing: bool = False,
) -> None:
    destination = normalize_domain(destination)
    attachment_refs = message_attachment_refs(envelope)
    room_ref = metadata_room_ref(envelope)
    durable_room_delete = durable_terminal_room_event(envelope)
    durable_media_request = durable_guild_media_delete_request(envelope)
    # Global room-bearing order is room fence -> media refs -> outbox.
    if room_ref is not None:
        await lock_room_federation_ref(session, room_ref)
    if attachment_refs:
        from app.media.tombstones import lock_media_tombstone_ref

        for attachment_id, attachment_domain in sorted(
            attachment_refs, key=lambda ref: (ref[1], ref[0])
        ):
            await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    if attachment_refs and str(envelope.get("type", "")) != "media.delete":
        # A remote origin's terminal proof can win while this room authority is
        # paused immediately before fanout. Put that proof into the same
        # destination stream before the raw message metadata. Local-origin
        # publication is instead fenced at finalize_attachment and cannot
        # disclose a row after its terminal verdict.
        from app.media.tombstones import queue_retained_media_delete_proof

        for attachment_id, attachment_domain in sorted(
            attachment_refs, key=lambda ref: (ref[1], ref[0])
        ):
            if attachment_domain != settings.domain:
                await queue_retained_media_delete_proof(
                    session,
                    settings,
                    attachment_id,
                    attachment_domain,
                    destination,
                )
    # Serialize the authoritative policy check and destination setup with block
    # mutations. A completed block therefore fences all later discovery and
    # insertion, while a block request waits for an already-started queue write.
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"kaede-outbox:{destination}", 0)))
    )
    block = await matching_block(session, destination)
    event_type = str(envelope.get("type", ""))
    event_origin = normalize_domain(str(envelope.get("origin", "")))
    interaction_response_expiry: datetime | None = None
    if event_type == "bot.interaction.response":
        content = envelope.get("content")
        if not isinstance(content, dict):
            raise ValueError("interaction response event content is invalid")
        try:
            interaction_response_expiry = datetime.fromisoformat(str(content["expires_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("interaction response event expiry is invalid") from exc
        if interaction_response_expiry.tzinfo is None:
            raise ValueError("interaction response event expiry is invalid")
    if event_origin != settings.domain and event_type != "media.delete":
        raise ValueError("only origin-signed media tombstones may be relayed")
    deletion_control = event_type == "media.delete" or durable_room_delete or durable_media_request
    durable_projection = event_type in DURABLE_LATEST_STATE_EVENTS
    policy_held = (
        block is not None
        and not deletion_control
        and federation_policy_holds_event(
            block.level,
            event_type,
            context=envelope.get("context"),
        )
    )
    await ensure_queue_destination(
        session,
        settings,
        destination,
        discover_destination=discover_destination,
        create_offline_placeholder=policy_held,
    )
    event_id = str(envelope["event_id"])
    await record_attachment_recipients(
        session,
        attachment_refs,
        destination,
        room_ref=None if durable_room_delete else room_ref,
        _locks_held=True,
        _room_lock_held=room_ref is not None and not durable_room_delete,
    )
    existing_outbox = await session.scalar(
        select(FederationOutbox).where(
            FederationOutbox.destination == destination,
            FederationOutbox.event_origin_domain == event_origin,
            FederationOutbox.event_id == event_id,
        )
    )
    if existing_outbox is not None:
        if (requeue_existing and existing_outbox.status in {"delivered", "expired", "failed"}) or (
            (
                event_type == "media.delete"
                or durable_room_delete
                or durable_media_request
                or durable_projection
            )
            and existing_outbox.status in {"expired", "failed"}
        ):
            existing_outbox.status = "circuit" if policy_held else "pending"
            existing_outbox.attempts = 0
            existing_outbox.next_retry_at = (
                policy_held_retry_at(datetime.now(UTC)) if policy_held else datetime.now(UTC)
            )
            existing_outbox.last_error = (
                f"{POLICY_HELD_OUTBOX_PREFIX} {block.level}"
                if policy_held and block is not None
                else None
            )
        return
    # Access revocations and resync markers are security reconciliation state.
    # They must remain durable even when an operator block has filled or paused
    # the ordinary destination queue.
    if event_type not in SECURITY_CRITICAL_GUILD_EVENTS and not durable_room_delete:
        await enforce_queue_limits(session, destination)
    now = datetime.now(UTC)
    inserted = await session.scalar(
        pg_insert(FederationEvent)
        .values(
            event_id=event_id,
            origin_domain=event_origin,
            event_type=event_type,
            envelope=envelope,
            envelope_bytes=len(canonical_json(envelope)),
            # Keep the source envelope beyond the seven-day delivery cutoff so
            # the expiry sweep can inspect it and enqueue a resync marker even
            # when operators configure the minimum retention window.
            expires_at=(
                interaction_response_expiry
                if interaction_response_expiry is not None
                else None
                if policy_held
                or event_type == "media.delete"
                or durable_room_delete
                or durable_media_request
                or durable_projection
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
        existing = await session.get(FederationEvent, (event_origin, event_id))
        if existing is None or (
            existing.origin_domain != event_origin or existing.envelope != envelope
        ):
            raise RuntimeError("federation event ID conflicts with another envelope")
        if (
            policy_held
            or event_type == "media.delete"
            or durable_room_delete
            or durable_media_request
            or durable_projection
        ):
            existing.expires_at = None
    outbox_insert = pg_insert(FederationOutbox).values(
        destination=destination,
        event_origin_domain=event_origin,
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


async def record_disclosed_attachment_recipients(
    session: AsyncSession,
    settings: Settings,
    attachment_refs: set[tuple[int, str]],
    destination: str,
    *,
    room_ref: tuple[str, int, str] | None = None,
) -> tuple[set[tuple[int, str]], set[str], set[tuple[int, str]]]:
    """Record synchronous disclosure and requeue any prior terminal verdict.

    A peer can join and request a retained guild gap or history page after an
    attachment was already rejected. The original origin-signed media.delete
    envelope is retained, so attaching that envelope to the new destination's
    outbox prevents a one-time authority relay from missing late metadata
    consumers. The caller commits before returning the metadata and wakes each
    returned destination after that commit.
    """

    destination = normalize_domain(destination)
    recorded = await record_attachment_recipients(
        session,
        attachment_refs,
        destination,
        room_ref=room_ref,
    )
    terminal_sources = (
        list(
            await session.scalars(
                select(MediaTombstoneSource).where(
                    tuple_(
                        MediaTombstoneSource.attachment_id,
                        MediaTombstoneSource.attachment_domain,
                    ).in_(recorded)
                )
            )
        )
        if recorded
        else []
    )
    terminal_refs = {
        (source.attachment_id, source.attachment_domain) for source in terminal_sources
    }
    wake_destinations: set[str] = set()

    # Locally authoritative proof state is independent of Attachment lifetime,
    # so a hard room/message cascade cannot prevent current-key regeneration.
    from app.media.tombstones import (
        queue_media_delete_tombstone,
        queue_retained_media_delete_proof,
    )

    for attachment_id, attachment_domain in sorted(terminal_refs):
        if attachment_domain == settings.domain:
            wake_destinations.update(
                await queue_media_delete_tombstone(
                    session,
                    settings,
                    attachment_id=attachment_id,
                    attachment_domain=attachment_domain,
                    destinations={destination},
                )
            )

    # Raw retained history can disclose attachment metadata after its replica
    # Attachment row was evicted. The independent source/destination records,
    # not row existence, are therefore the authority for every remaining ref.
    for attachment_id, attachment_domain in sorted(
        ref for ref in terminal_refs if ref[1] != settings.domain
    ):
        if await queue_retained_media_delete_proof(
            session,
            settings,
            attachment_id,
            attachment_domain,
            destination,
        ):
            wake_destinations.add(destination)
    return recorded, wake_destinations, terminal_refs


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
        await ensure_remote_instance_record(
            session,
            settings,
            destination,
            display_name=destination,
            software_version="unresolved",
        )
        known_destination = await session.get(Instance, destination, populate_existing=True)
        if known_destination is None:
            raise RuntimeError("federation destination placeholder disappeared")
    return known_destination
