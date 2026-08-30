from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    String,
    and_,
    delete,
    exists,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.chat.permissions import calculate_permissions
from app.core.federation import canonical_json
from app.core.permissions import Permission
from app.core.settings import Settings
from app.db.bot_models import BotInstallation, BotUserInstallation
from app.db.models import (
    Attachment,
    AttachmentFederationRecipient,
    Channel,
    DMConversation,
    DMParticipant,
    FederationEvent,
    FederationInbox,
    FederationOutbox,
    Guild,
    GuildEvent,
    GuildHistoryStagedMessage,
    GuildMember,
    Instance,
    MediaTombstoneDestination,
    MediaTombstoneSource,
    Message,
    ReadState,
    RemoteMediaCache,
    RemoteMediaTombstone,
    RoomFederationRecipient,
    User,
)
from app.federation.events import (
    build_envelope,
    media_delete_generation,
    queue_event,
)
from app.media.digest_revocation import (
    DIGEST_REVOCATION_STATUSES,
    TERMINAL_ATTACHMENT_STATUSES,
    try_lock_asset_digest,
    valid_content_digest,
)


@dataclass(frozen=True)
class MediaDeleteSignerRef:
    id: int
    origin_domain: str


async def _locked_user(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    user_domain: str,
) -> User | None:
    user = await session.scalar(
        select(User)
        .where(
            User.id == user_id,
            User.origin_domain == user_domain,
            User.is_local.is_(user_domain == settings.domain),
        )
        # Tombstone preparation commonly already owns media-ref and Attachment
        # locks. Ordinary asset mutation owns User/Guild before Attachment, so
        # never wait here and form the inverse cycle; the enclosing worker or
        # request rolls back and retries the durable deletion intent.
        .with_for_update(nowait=True)
    )
    return user


async def resolve_media_delete_signer(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    guild: Guild | None,
) -> User:
    """Choose the truthful retained actor for an attachment-home tombstone.

    The attachment origin, rather than the uploader's home, authorizes byte
    invalidation. Prefer the exact uploader even when remote; local owner and
    installer fallbacks exist for legacy rows whose uploader projection is no
    longer available.
    """

    uploader = await _locked_user(
        session,
        settings,
        attachment.uploader_id,
        attachment.uploader_domain,
    )
    if uploader is not None:
        return uploader

    if guild is not None and guild.origin_domain == settings.domain:
        owner = await _locked_user(
            session,
            settings,
            guild.owner_id,
            guild.owner_domain,
        )
        if owner is not None:
            return owner

    if attachment.bot_installation_id is not None:
        guild_installation = await session.get(BotInstallation, attachment.bot_installation_id)
        if guild_installation is not None:
            installer = await _locked_user(
                session,
                settings,
                guild_installation.installer_id,
                guild_installation.installer_domain,
            )
            if installer is not None:
                return installer

    if attachment.bot_user_installation_id is not None:
        user_installation = await session.get(
            BotUserInstallation, attachment.bot_user_installation_id
        )
        if user_installation is not None:
            installer = await _locked_user(
                session,
                settings,
                user_installation.user_id,
                user_installation.user_domain,
            )
            if installer is not None:
                return installer

    raise RuntimeError("terminal local attachment has no retained tombstone actor")


async def terminal_attachment_destinations(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    channel: Channel,
    guild: Guild | None,
) -> set[str]:
    """Return every current remote replica that could have received the media."""

    historical_destinations = await historical_attachment_destinations(session, attachment)

    if guild is None:
        return historical_destinations | set(
            await session.scalars(
                select(DMParticipant.user_domain)
                .where(
                    DMParticipant.conversation_id == channel.id,
                    DMParticipant.conversation_domain == channel.origin_domain,
                    DMParticipant.user_domain != settings.domain,
                )
                .distinct()
            )
        )

    remote_users = list(
        await session.scalars(
            select(User)
            .join(
                GuildMember,
                (GuildMember.user_id == User.id) & (GuildMember.user_domain == User.origin_domain),
            )
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                User.origin_domain != settings.domain,
            )
            .order_by(User.origin_domain, User.id)
        )
    )
    destinations = historical_destinations
    for user in remote_users:
        permissions, _member = await calculate_permissions(session, guild, user, channel=channel)
        if permissions & Permission.VIEW_CHANNEL:
            destinations.add(user.origin_domain)
    return destinations


async def historical_attachment_destinations(
    session: AsyncSession,
    attachment: Attachment,
) -> set[str]:
    """Return domains that already received this attachment's metadata."""

    return await historical_attachment_destinations_by_ref(
        session,
        attachment.id,
        attachment.origin_domain,
    )


async def historical_attachment_destinations_by_ref(
    session: AsyncSession,
    attachment_id: int,
    attachment_domain: str,
) -> set[str]:
    """Return durable routes after attachment and membership rows disappear."""

    ledger_destinations = set(
        await session.scalars(
            select(AttachmentFederationRecipient.destination_domain).where(
                AttachmentFederationRecipient.attachment_id == attachment_id,
                AttachmentFederationRecipient.attachment_domain == attachment_domain,
            )
        )
    )
    tombstone_destinations = set(
        await session.scalars(
            select(MediaTombstoneDestination.destination_domain).where(
                MediaTombstoneDestination.attachment_id == attachment_id,
                MediaTombstoneDestination.attachment_domain == attachment_domain,
            )
        )
    )
    # Outbox history is an independent delivery receipt and closes the small
    # window between upgrading the recipient-ledger schema and recording a
    # newly generated proof. It is read before old proof generations are
    # compacted.
    outbox_destinations = set(
        await session.scalars(
            select(FederationOutbox.destination)
            .join(
                FederationEvent,
                (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                & (FederationEvent.event_id == FederationOutbox.event_id),
            )
            .where(
                FederationEvent.origin_domain == attachment_domain,
                FederationEvent.event_type == "media.delete",
                FederationEvent.envelope["content"]["attachment_id"].as_string()
                == str(attachment_id),
                FederationEvent.envelope["content"]["origin_domain"].as_string()
                == attachment_domain,
            )
            .distinct()
        )
    )
    return ledger_destinations | tombstone_destinations | outbox_destinations


async def lock_media_tombstone_ref(
    session: AsyncSession,
    attachment_id: int,
    attachment_domain: str,
) -> None:
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"kaede-media-delete:{attachment_domain}:{attachment_id}", 0)
            )
        )
    )


async def lock_terminal_room_media_fences(
    session: AsyncSession,
    *,
    room_kind: str,
    room_id: int,
    room_domain: str,
) -> list[tuple[int, str]]:
    """Lock a terminal room and every media ref it can still disclose.

    Federation event admission locks the global quota ledger.  Deletion and
    disclosure paths must therefore take the complete room/media fence first,
    so an attachment delete (cache -> media -> global) cannot deadlock a room
    delete (room -> global -> cache/media).  The room fence makes the bound +
    durable-route snapshot closed while the remaining locks are acquired.
    """

    if room_kind not in {"guild", "group_dm"}:
        raise ValueError("terminal room kind is invalid")
    from app.federation.terminal_rooms import lock_terminal_room

    await lock_terminal_room(session, room_kind, room_id, room_domain)
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    if room_kind == "guild":
        channel_refs = select(Channel.id, Channel.origin_domain).where(
            Channel.guild_id == room_id,
            Channel.guild_domain == room_domain,
        )
        message_refs = select(Message.id, Message.origin_domain).where(
            tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs)
        )
        bound_refs = select(Attachment.id, Attachment.origin_domain).where(
            tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs)
        )
    else:
        bound_refs = (
            select(Attachment.id, Attachment.origin_domain)
            .join(
                Message,
                (Message.id == Attachment.message_id)
                & (Message.origin_domain == Attachment.message_domain),
            )
            .where(
                Message.channel_id == room_id,
                Message.channel_domain == room_domain,
            )
        )
    routed_refs = select(
        MediaTombstoneDestination.attachment_id,
        MediaTombstoneDestination.attachment_domain,
    ).where(
        MediaTombstoneDestination.room_kind == room_kind,
        MediaTombstoneDestination.room_id == room_id,
        MediaTombstoneDestination.room_domain == room_domain,
    )
    refs = sorted(
        set((await session.execute(bound_refs.union(routed_refs))).tuples()),
        key=lambda ref: (ref[1], ref[0]),
    )
    for attachment_id, attachment_domain in refs:
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    return refs


def _media_delete_key_id(envelope: dict[str, Any], origin_domain: str) -> str:
    signatures = envelope.get("signatures")
    origin_signatures = signatures.get(origin_domain) if isinstance(signatures, dict) else None
    if not isinstance(origin_signatures, dict) or len(origin_signatures) != 1:
        raise RuntimeError("media tombstone signature set is invalid")
    key_id = next(iter(origin_signatures))
    if not isinstance(key_id, str) or not key_id:
        raise RuntimeError("media tombstone signing key is invalid")
    return key_id


async def _retain_media_delete_event(
    session: AsyncSession,
    envelope: dict[str, Any],
) -> FederationEvent:
    origin = str(envelope["origin"])
    event_id = str(envelope["event_id"])
    await session.scalar(
        pg_insert(FederationEvent)
        .values(
            event_id=event_id,
            origin_domain=origin,
            event_type="media.delete",
            envelope=envelope,
            envelope_bytes=len(canonical_json(envelope)),
            expires_at=None,
        )
        .on_conflict_do_nothing(index_elements=["origin_domain", "event_id"])
        .returning(FederationEvent.event_id)
    )
    event = await session.get(FederationEvent, (origin, event_id), populate_existing=True)
    if event is None or event.event_type != "media.delete" or event.envelope != envelope:
        raise RuntimeError("media tombstone event ID conflicts with another envelope")
    event.expires_at = None
    return event


async def record_media_tombstone_destinations(
    session: AsyncSession,
    attachment_id: int,
    attachment_domain: str,
    destinations: set[str],
    *,
    room_ref: tuple[str, int, str] | None = None,
) -> None:
    """Persist routes independently of attachment/message lifetime."""

    for destination in sorted(destinations):
        values: dict[str, Any] = {
            "attachment_id": attachment_id,
            "attachment_domain": attachment_domain,
            "destination_domain": destination,
        }
        if room_ref is not None:
            values.update(
                room_kind=room_ref[0],
                room_id=room_ref[1],
                room_domain=room_ref[2],
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


async def queue_media_delete_tombstone(
    session: AsyncSession,
    settings: Settings,
    *,
    attachment_id: int,
    attachment_domain: str,
    destinations: set[str],
    signer: User | MediaDeleteSignerRef | None = None,
    room_ref: tuple[str, int, str] | None = None,
) -> set[str]:
    """Create or reuse one bounded local proof and queue all durable routes."""

    if attachment_domain != settings.domain:
        raise ValueError("only the attachment origin may sign a media tombstone")
    await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    source = await session.scalar(
        select(MediaTombstoneSource)
        .where(
            MediaTombstoneSource.attachment_id == attachment_id,
            MediaTombstoneSource.attachment_domain == attachment_domain,
        )
        .with_for_update()
    )
    instance = await session.get(Instance, settings.domain)
    if instance is None or not instance.is_self or not instance.current_key_id:
        raise RuntimeError("local federation signing identity is unavailable")
    durable_destinations = set(
        await session.scalars(
            select(MediaTombstoneDestination.destination_domain).where(
                MediaTombstoneDestination.attachment_id == attachment_id,
                MediaTombstoneDestination.attachment_domain == attachment_domain,
            )
        )
    )
    durable_destinations.update(destinations)
    durable_destinations.discard(settings.domain)

    current_event = (
        await session.get(FederationEvent, (settings.domain, source.event_id))
        if source is not None
        else None
    )
    reuse_current = bool(
        source is not None
        and source.generation > 0
        and source.key_id == instance.current_key_id
        and current_event is not None
        and isinstance(current_event.envelope, dict)
        and media_delete_generation(current_event.envelope) == source.generation
        and _media_delete_key_id(current_event.envelope, settings.domain) == instance.current_key_id
    )
    old_event_id = source.event_id if source is not None else None
    if reuse_current:
        if current_event is None:  # pragma: no cover - narrowed by reuse_current
            raise RuntimeError("media tombstone source event disappeared")
        envelope = current_event.envelope
    else:
        if signer is None and source is None:
            raise RuntimeError("initial media tombstone requires a signer")
        if signer is None:
            if source is None:  # pragma: no cover - guarded above
                raise RuntimeError("media tombstone source disappeared")
            signer = MediaDeleteSignerRef(
                id=source.signer_id,
                origin_domain=source.signer_domain,
            )
        signer_ref: User | MediaDeleteSignerRef = signer
        prior_generation = source.generation if source is not None else 0
        if prior_generation >= (1 << 63) - 1:
            raise RuntimeError("media tombstone generation is exhausted")
        envelope = await build_envelope(
            session,
            settings,
            "media.delete",
            cast(User, signer_ref),
            {
                "attachment_id": str(attachment_id),
                "origin_domain": attachment_domain,
                "generation": str(prior_generation + 1),
            },
            retained_authority_attested_actor=signer_ref.origin_domain != settings.domain,
        )
        await _retain_media_delete_event(session, envelope)

    event_id = str(envelope["event_id"])
    generation = media_delete_generation(envelope)
    key_id = _media_delete_key_id(envelope, settings.domain)
    if generation <= 0:
        raise RuntimeError("new media tombstone generation must be positive")
    for destination in sorted(durable_destinations):
        await queue_event(session, settings, destination, envelope)
    await record_media_tombstone_destinations(
        session,
        attachment_id,
        attachment_domain,
        durable_destinations,
        room_ref=room_ref,
    )

    now = datetime.now(UTC)
    if source is None:
        if signer is None:
            raise RuntimeError("initial media tombstone signer disappeared")
        source = MediaTombstoneSource(
            attachment_id=attachment_id,
            attachment_domain=attachment_domain,
            signer_id=signer.id,
            signer_domain=signer.origin_domain,
            event_id=event_id,
            key_id=key_id,
            generation=generation,
            updated_at=now,
        )
        session.add(source)
    else:
        source.event_id = event_id
        source.key_id = key_id
        source.generation = generation
        source.updated_at = now

    if not reuse_current:
        await session.flush()
        await session.execute(
            delete(FederationEvent).where(
                FederationEvent.origin_domain == settings.domain,
                FederationEvent.event_type == "media.delete",
                FederationEvent.event_id != event_id,
                FederationEvent.envelope["content"]["attachment_id"].as_string()
                == str(attachment_id),
                FederationEvent.envelope["content"]["origin_domain"].as_string()
                == attachment_domain,
            )
        )
        if old_event_id == event_id:
            raise RuntimeError("media tombstone generation did not advance")
    return durable_destinations


async def queue_retained_media_delete_proof(
    session: AsyncSession,
    settings: Settings,
    attachment_id: int,
    attachment_domain: str,
    destination: str,
) -> bool:
    """Queue the selected origin proof to a newly discovered replica."""

    if attachment_domain == settings.domain:
        source = await session.get(
            MediaTombstoneSource,
            (attachment_id, attachment_domain),
        )
        if source is None:
            return False
        await queue_media_delete_tombstone(
            session,
            settings,
            attachment_id=attachment_id,
            attachment_domain=attachment_domain,
            destinations={destination},
        )
        return True
    await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    source = await session.get(
        MediaTombstoneSource,
        (attachment_id, attachment_domain),
    )
    if source is None:
        return False
    event = await session.get(FederationEvent, (attachment_domain, source.event_id))
    if event is None or not isinstance(event.envelope, dict):
        raise RuntimeError("retained remote media tombstone proof disappeared")
    await queue_event(session, settings, destination, event.envelope)
    await record_media_tombstone_destinations(
        session,
        attachment_id,
        attachment_domain,
        {destination},
    )
    return True


async def build_media_delete_envelope(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    signer: User,
) -> dict[str, Any]:
    """Build the next proof without storing it (focused-test helper)."""

    source = await session.get(
        MediaTombstoneSource,
        (attachment.id, attachment.origin_domain),
    )
    prior_generation = source.generation if source is not None else 0
    envelope = await build_envelope(
        session,
        settings,
        "media.delete",
        signer,
        {
            "attachment_id": str(attachment.id),
            "origin_domain": attachment.origin_domain,
            "generation": str(prior_generation + 1),
        },
        retained_authority_attested_actor=signer.origin_domain != settings.domain,
    )
    return envelope


async def queue_terminal_attachment_tombstone(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    *,
    force_authoritative: bool = False,
) -> set[str]:
    """Durably queue one signed tombstone to all known remote recipients.

    The caller commits before waking delivery. Reusing the retained source
    envelope makes retries idempotent while still allowing another destination
    to be attached to the same signed event.
    """

    if attachment.origin_domain != settings.domain:
        return set()

    guild: Guild | None = None
    room_ref: tuple[str, int, str] | None = None
    delete_is_authoritative = force_authoritative or attachment.deleted_at is not None
    destinations: set[str]
    if attachment.message_id is None or attachment.message_domain is None:
        # Proxy metadata is disclosed to the remote authority before its
        # committed message returns and binds the local attachment. The
        # recipient ledger is therefore sufficient routing evidence even when
        # binding never completes (crash, leave, or permission revocation).
        destinations = await historical_attachment_destinations(session, attachment)
    else:
        message = await session.get(
            Message,
            (attachment.message_id, attachment.message_domain),
        )
        if message is None:
            destinations = await historical_attachment_destinations(session, attachment)
        else:
            delete_is_authoritative = delete_is_authoritative or message.deleted_at is not None
            channel = await session.get(Channel, (message.channel_id, message.channel_domain))
            if channel is None:
                destinations = await historical_attachment_destinations(session, attachment)
            else:
                guild = (
                    await session.get(Guild, (channel.guild_id, channel.guild_domain))
                    if channel.guild_id is not None and channel.guild_domain is not None
                    else None
                )
                if guild is not None:
                    room_ref = ("guild", guild.id, guild.origin_domain)
                else:
                    conversation = await session.get(
                        DMConversation, (channel.id, channel.origin_domain)
                    )
                    if conversation is not None and conversation.type == "group":
                        room_ref = ("group_dm", channel.id, channel.origin_domain)
                if channel.guild_id is not None and guild is None:
                    destinations = await historical_attachment_destinations(session, attachment)
                else:
                    destinations = await terminal_attachment_destinations(
                        session,
                        settings,
                        attachment,
                        channel,
                        guild,
                    )
    if not delete_is_authoritative:
        return set()
    destinations.discard(settings.domain)
    source = await session.get(
        MediaTombstoneSource,
        (attachment.id, attachment.origin_domain),
    )
    signer = (
        None
        if source is not None
        else await resolve_media_delete_signer(session, settings, attachment, guild)
    )
    return await queue_media_delete_tombstone(
        session,
        settings,
        attachment_id=attachment.id,
        attachment_domain=attachment.origin_domain,
        destinations=destinations,
        signer=signer,
        room_ref=room_ref,
    )


def _attachment_json_carrier(
    value: Any,
    attachment_id: int,
    attachment_domain: str,
) -> Any:
    """PostgreSQL predicate for a rendered attachment ref at any JSON depth."""

    return func.jsonb_path_exists(
        value,
        "$.**.attachments[*] ? "
        "((@.id == $wire_id || @.id == $number_id) && @.origin_domain == $domain)",
        func.jsonb_build_object(
            "wire_id",
            str(attachment_id),
            "number_id",
            attachment_id,
            "domain",
            attachment_domain,
        ),
    )


def _attachment_json_carrier_for_source(value: Any, source: Any) -> Any:
    """Correlate the carrier predicate to a cleanup-candidate source row."""

    return func.jsonb_path_exists(
        value,
        "$.**.attachments[*] ? "
        "((@.id == $wire_id || @.id == $number_id) && @.origin_domain == $domain)",
        func.jsonb_build_object(
            "wire_id",
            sql_cast(source.attachment_id, String),
            "number_id",
            source.attachment_id,
            "domain",
            source.attachment_domain,
        ),
    )


def _media_tombstone_cleanup_candidates(
    settings: Settings,
    *,
    now: datetime,
    cutoff: datetime,
    limit: int,
) -> Any:
    """Select only presently cleanable refs before applying the batch limit.

    Merely limiting the oldest source rows lets a fixed prefix of live carriers
    or offline destinations starve every later, fully acknowledged proof.  Keep
    the in-loop checks as concurrency revalidation, but exclude every known
    blocker in SQL so an ineligible prefix consumes no cleanup slots.
    """

    source = aliased(MediaTombstoneSource)
    terminal_attachment = aliased(Attachment)
    active_public_attachment = aliased(Attachment)
    unsafe_attachment = exists(
        select(Attachment.id).where(
            Attachment.id == source.attachment_id,
            Attachment.origin_domain == source.attachment_domain,
            or_(
                Attachment.deleted_at.is_(None),
                and_(
                    Attachment.origin_domain == settings.domain,
                    or_(
                        Attachment.staging_object_key.is_not(None),
                        and_(
                            Attachment.upload_expires_at.is_not(None),
                            Attachment.upload_expires_at > now,
                        ),
                    ),
                ),
            ),
        )
    ).correlate(source)
    revocation_dependent_duplicate = exists(
        select(active_public_attachment.id)
        .join(
            terminal_attachment,
            terminal_attachment.content_sha256 == active_public_attachment.content_sha256,
        )
        .where(
            source.attachment_domain == settings.domain,
            terminal_attachment.id == source.attachment_id,
            terminal_attachment.origin_domain == source.attachment_domain,
            terminal_attachment.content_sha256.is_not(None),
            terminal_attachment.scan_status.in_(DIGEST_REVOCATION_STATUSES),
            active_public_attachment.origin_domain == settings.domain,
            active_public_attachment.purpose != "attachment",
            or_(
                active_public_attachment.asset_binding.is_not(None),
                and_(
                    active_public_attachment.scan_status == "clean",
                    active_public_attachment.deleted_at.is_(None),
                ),
            ),
        )
    ).correlate(source)
    cached_variant = exists(
        select(RemoteMediaCache.attachment_id).where(
            RemoteMediaCache.origin_domain == source.attachment_domain,
            RemoteMediaCache.attachment_id == source.attachment_id,
        )
    ).correlate(source)
    delivered_current_proof = exists(
        select(FederationOutbox.id).where(
            FederationOutbox.destination == MediaTombstoneDestination.destination_domain,
            FederationOutbox.event_origin_domain == source.attachment_domain,
            FederationOutbox.event_id == source.event_id,
            FederationOutbox.status == "delivered",
        )
    ).correlate(source, MediaTombstoneDestination)
    undelivered_destination = exists(
        select(MediaTombstoneDestination.destination_domain).where(
            MediaTombstoneDestination.attachment_id == source.attachment_id,
            MediaTombstoneDestination.attachment_domain == source.attachment_domain,
            ~delivered_current_proof,
        )
    ).correlate(source)
    retained_federation_carrier = exists(
        select(FederationEvent.event_id).where(
            ~(
                (FederationEvent.origin_domain == source.attachment_domain)
                & (FederationEvent.event_id == source.event_id)
            ),
            _attachment_json_carrier_for_source(FederationEvent.envelope, source),
        )
    ).correlate(source)
    retained_guild_carrier = exists(
        select(GuildEvent.seq).where(
            _attachment_json_carrier_for_source(GuildEvent.envelope, source)
        )
    ).correlate(source)
    staged_history_carrier = exists(
        select(GuildHistoryStagedMessage.message_id).where(
            _attachment_json_carrier_for_source(GuildHistoryStagedMessage.payload, source)
        )
    ).correlate(source)
    return (
        select(source.attachment_id, source.attachment_domain)
        .where(
            source.updated_at < cutoff,
            ~unsafe_attachment,
            ~revocation_dependent_duplicate,
            ~cached_variant,
            ~undelivered_destination,
            ~retained_federation_carrier,
            ~retained_guild_carrier,
            ~staged_history_carrier,
        )
        .order_by(source.attachment_domain, source.attachment_id)
        .limit(limit)
    )


async def cleanup_media_tombstone_sources(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    """Bound durable media proof state after every possible carrier is gone.

    A proof is compacted only after its current generation reached every
    durable route, no attachment/cache/tombstone remains, and no retained
    federation, guild-history, or staged-history payload can disclose the ref.
    Disclosure and cleanup share the per-media advisory fence, so a new route
    cannot appear between the final carrier check and deletion.
    """

    if not 1 <= limit <= 10_000:
        raise ValueError("invalid media tombstone cleanup limit")
    cutoff = now - timedelta(
        days=settings.federation_event_retention_days,
        seconds=settings.federation_clock_skew_seconds,
    )
    # Cache admission and inbound deletion take this before the media-ref lock.
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    refs = list(
        (
            await session.execute(
                _media_tombstone_cleanup_candidates(
                    settings,
                    now=now,
                    cutoff=cutoff,
                    limit=limit,
                )
            )
        ).tuples()
    )
    cleaned = 0
    for attachment_id, attachment_domain in refs:
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
        source = await session.scalar(
            select(MediaTombstoneSource)
            .where(
                MediaTombstoneSource.attachment_id == attachment_id,
                MediaTombstoneSource.attachment_domain == attachment_domain,
            )
            .with_for_update()
        )
        if source is None or source.updated_at >= cutoff:
            continue
        attachment = await session.scalar(
            select(Attachment)
            .where(
                Attachment.id == attachment_id,
                Attachment.origin_domain == attachment_domain,
            )
            .with_for_update()
        )
        if (
            await session.scalar(
                select(RemoteMediaCache.attachment_id)
                .where(
                    RemoteMediaCache.origin_domain == attachment_domain,
                    RemoteMediaCache.attachment_id == attachment_id,
                )
                .limit(1)
            )
            is not None
        ):
            continue
        if attachment is not None and (
            attachment.deleted_at is None
            or (
                attachment.origin_domain == settings.domain
                and (
                    attachment.staging_object_key is not None
                    or (
                        attachment.upload_expires_at is not None
                        and attachment.upload_expires_at > now
                    )
                )
            )
        ):
            continue
        if (
            attachment is not None
            and attachment.origin_domain == settings.domain
            and valid_content_digest(attachment.content_sha256)
            and attachment.scan_status in TERMINAL_ATTACHMENT_STATUSES
        ):
            # Cleanup already owns this Attachment and other media locks. A
            # bind can own digest while waiting to replace an Attachment row,
            # so never block here: skip this proof and retry another day.
            if not await try_lock_asset_digest(session, attachment.content_sha256):
                continue
            if (
                await session.scalar(
                    select(Attachment.id)
                    .where(
                        Attachment.origin_domain == settings.domain,
                        Attachment.content_sha256 == attachment.content_sha256,
                        Attachment.purpose != "attachment",
                        or_(
                            Attachment.asset_binding.is_not(None),
                            and_(
                                Attachment.scan_status == "clean",
                                Attachment.deleted_at.is_(None),
                            ),
                        ),
                    )
                    .limit(1)
                )
                is not None
            ):
                # Retain the digest evidence for every existing clean
                # bind-capable upload, even while unbound, and for any stale
                # projection still awaiting bounded invalidation repair.
                continue
        undelivered_destination = await session.scalar(
            select(MediaTombstoneDestination.destination_domain)
            .where(
                MediaTombstoneDestination.attachment_id == attachment_id,
                MediaTombstoneDestination.attachment_domain == attachment_domain,
                ~exists(
                    select(FederationOutbox.id).where(
                        FederationOutbox.destination
                        == MediaTombstoneDestination.destination_domain,
                        FederationOutbox.event_origin_domain == attachment_domain,
                        FederationOutbox.event_id == source.event_id,
                        FederationOutbox.status == "delivered",
                    )
                ),
            )
            .limit(1)
        )
        if undelivered_destination is not None:
            continue
        carrier_exists = any(
            (
                await session.scalar(
                    select(FederationEvent.event_id)
                    .where(
                        ~(
                            (FederationEvent.origin_domain == attachment_domain)
                            & (FederationEvent.event_id == source.event_id)
                        ),
                        _attachment_json_carrier(
                            FederationEvent.envelope,
                            attachment_id,
                            attachment_domain,
                        ),
                    )
                    .limit(1)
                )
                is not None,
                await session.scalar(
                    select(GuildEvent.seq)
                    .where(
                        _attachment_json_carrier(
                            GuildEvent.envelope,
                            attachment_id,
                            attachment_domain,
                        )
                    )
                    .limit(1)
                )
                is not None,
                await session.scalar(
                    select(GuildHistoryStagedMessage.message_id)
                    .where(
                        _attachment_json_carrier(
                            GuildHistoryStagedMessage.payload,
                            attachment_id,
                            attachment_domain,
                        )
                    )
                    .limit(1)
                )
                is not None,
            )
        )
        if carrier_exists:
            continue
        event_ref = (attachment_domain, source.event_id)
        if attachment is not None:
            await session.delete(attachment)
        await session.execute(
            delete(RemoteMediaTombstone).where(
                RemoteMediaTombstone.origin_domain == attachment_domain,
                RemoteMediaTombstone.attachment_id == attachment_id,
            )
        )
        await session.execute(
            delete(MediaTombstoneDestination).where(
                MediaTombstoneDestination.attachment_id == attachment_id,
                MediaTombstoneDestination.attachment_domain == attachment_domain,
            )
        )
        await session.delete(source)
        await session.execute(
            delete(FederationInbox).where(
                FederationInbox.origin_domain == event_ref[0],
                FederationInbox.event_id == event_ref[1],
            )
        )
        await session.execute(
            delete(FederationEvent).where(
                FederationEvent.origin_domain == event_ref[0],
                FederationEvent.event_id == event_ref[1],
            )
        )
        cleaned += 1
    return cleaned


async def prepare_terminal_channel_media(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
) -> tuple[list[tuple[int, str]], set[str], set[str]]:
    """Establish deletion truth for every attachment in a terminal room.

    Group-DM final deletion is authority-signed room state, but each attachment
    home must sign its own media deletion. Return local object-purge refs,
    every historical domain that must receive the terminal room state, and
    media-delete delivery wakes. Local Attachment rows are detached from their
    Message before replica/history cleanup can cascade them, leaving the source
    sweep enough object metadata to repair a lost post-commit purge wake.
    """

    from app.federation.terminal_rooms import lock_terminal_room

    await lock_terminal_room(session, "group_dm", channel.id, channel.origin_domain)
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    bound_refs = (
        select(Attachment.id, Attachment.origin_domain)
        .join(
            Message,
            (Message.id == Attachment.message_id)
            & (Message.origin_domain == Attachment.message_domain),
        )
        .where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
        )
    )
    routed_refs = select(
        MediaTombstoneDestination.attachment_id,
        MediaTombstoneDestination.attachment_domain,
    ).where(
        MediaTombstoneDestination.room_kind == "group_dm",
        MediaTombstoneDestination.room_id == channel.id,
        MediaTombstoneDestination.room_domain == channel.origin_domain,
    )
    attachment_refs = sorted(
        set((await session.execute(bound_refs.union(routed_refs))).tuples()),
        key=lambda ref: (ref[1], ref[0]),
    )
    for attachment_id, attachment_domain in attachment_refs:
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(tuple_(Attachment.id, Attachment.origin_domain).in_(attachment_refs))
            .order_by(Attachment.origin_domain, Attachment.id)
            .with_for_update()
        )
    )
    purge_refs: list[tuple[int, str]] = []
    state_destinations: set[str] = set()
    delivery_wakes: set[str] = set()
    durable_routes = list(
        await session.scalars(
            select(MediaTombstoneDestination).where(
                MediaTombstoneDestination.room_kind == "group_dm",
                MediaTombstoneDestination.room_id == channel.id,
                MediaTombstoneDestination.room_domain == channel.origin_domain,
            )
        )
    )
    for route in durable_routes:
        state_destinations.update({route.attachment_domain, route.destination_domain})
    state_destinations.update(
        await session.scalars(
            select(RoomFederationRecipient.destination_domain).where(
                RoomFederationRecipient.room_kind == "group_dm",
                RoomFederationRecipient.room_id == channel.id,
                RoomFederationRecipient.room_domain == channel.origin_domain,
            )
        )
    )
    for attachment in attachments:
        state_destinations.update(await historical_attachment_destinations(session, attachment))
        state_destinations.add(attachment.origin_domain)
        if attachment.origin_domain != settings.domain:
            continue
        delivery_wakes.update(
            await queue_terminal_attachment_tombstone(
                session,
                settings,
                attachment,
                force_authoritative=True,
            )
        )
        purge_refs.append((attachment.id, attachment.origin_domain))
        attachment.message_id = None
        attachment.message_domain = None
    remote_refs = [
        (attachment_id, attachment_domain)
        for attachment_id, attachment_domain in attachment_refs
        if attachment_domain != settings.domain
    ]
    if remote_refs:
        await session.execute(
            update(RemoteMediaCache)
            .where(
                tuple_(RemoteMediaCache.attachment_id, RemoteMediaCache.origin_domain).in_(
                    remote_refs
                )
            )
            .values(expires_at=datetime.now(UTC))
        )
    message_refs = select(Message.id, Message.origin_domain).where(
        Message.channel_id == channel.id,
        Message.channel_domain == channel.origin_domain,
    )
    await session.execute(
        update(ReadState)
        .where(
            ReadState.channel_id == channel.id,
            ReadState.channel_domain == channel.origin_domain,
        )
        .values(last_message_id=None, last_message_domain=None, mention_count=0)
    )
    channel.last_message_id = None
    channel.last_message_domain = None
    await session.execute(
        update(Message)
        .where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
            Message.referenced_message_id.is_not(None),
        )
        .values(referenced_message_id=None, referenced_message_domain=None)
    )
    await session.flush()
    await session.execute(
        delete(Message).where(tuple_(Message.id, Message.origin_domain).in_(message_refs))
    )
    state_destinations.discard(settings.domain)
    return purge_refs, state_destinations, delivery_wakes


async def prepare_terminal_guild_media(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
) -> tuple[list[tuple[int, str]], set[str], set[str]]:
    """Terminalize every local object and cache associated with a deleted guild."""

    from app.federation.terminal_rooms import lock_terminal_room

    await lock_terminal_room(session, "guild", guild.id, guild.origin_domain)
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    channel_refs = select(Channel.id, Channel.origin_domain).where(
        Channel.guild_id == guild.id,
        Channel.guild_domain == guild.origin_domain,
    )
    message_refs = select(Message.id, Message.origin_domain).where(
        tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs)
    )
    bound_refs = select(Attachment.id, Attachment.origin_domain).where(
        tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs)
    )
    routed_refs = select(
        MediaTombstoneDestination.attachment_id,
        MediaTombstoneDestination.attachment_domain,
    ).where(
        MediaTombstoneDestination.room_kind == "guild",
        MediaTombstoneDestination.room_id == guild.id,
        MediaTombstoneDestination.room_domain == guild.origin_domain,
    )
    attachment_refs = sorted(
        set((await session.execute(bound_refs.union(routed_refs))).tuples()),
        key=lambda ref: (ref[1], ref[0]),
    )
    for attachment_id, attachment_domain in attachment_refs:
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(tuple_(Attachment.id, Attachment.origin_domain).in_(attachment_refs))
            .order_by(Attachment.origin_domain, Attachment.id)
            .with_for_update()
        )
    )
    routes = list(
        await session.scalars(
            select(MediaTombstoneDestination).where(
                MediaTombstoneDestination.room_kind == "guild",
                MediaTombstoneDestination.room_id == guild.id,
                MediaTombstoneDestination.room_domain == guild.origin_domain,
            )
        )
    )
    destinations: set[str] = set()
    for route in routes:
        destinations.update({route.attachment_domain, route.destination_domain})
    destinations.update(
        await session.scalars(
            select(RoomFederationRecipient.destination_domain).where(
                RoomFederationRecipient.room_kind == "guild",
                RoomFederationRecipient.room_id == guild.id,
                RoomFederationRecipient.room_domain == guild.origin_domain,
            )
        )
    )
    purge_refs: list[tuple[int, str]] = []
    wakes: set[str] = set()
    remote_refs: list[tuple[int, str]] = [
        (attachment_id, attachment_domain)
        for attachment_id, attachment_domain in attachment_refs
        if attachment_domain != settings.domain
    ]
    for attachment in attachments:
        destinations.update(await historical_attachment_destinations(session, attachment))
        destinations.add(attachment.origin_domain)
        if attachment.origin_domain == settings.domain:
            wakes.update(
                await queue_terminal_attachment_tombstone(
                    session,
                    settings,
                    attachment,
                    force_authoritative=True,
                )
            )
            purge_refs.append((attachment.id, attachment.origin_domain))
            attachment.message_id = None
            attachment.message_domain = None
    if remote_refs:
        await session.execute(
            update(RemoteMediaCache)
            .where(
                tuple_(RemoteMediaCache.attachment_id, RemoteMediaCache.origin_domain).in_(
                    remote_refs
                )
            )
            .values(expires_at=datetime.now(UTC))
        )
    destinations.discard(settings.domain)
    return purge_refs, destinations, wakes


async def prepare_terminal_room_media_by_ref(
    session: AsyncSession,
    settings: Settings,
    *,
    room_kind: str,
    room_id: int,
    room_domain: str,
) -> tuple[list[tuple[int, str]], list[tuple[str, int]], set[str], set[str]]:
    """Terminalize media after a room projection has already been removed.

    Independent disclosure routes retain the room association for detached
    local-origin uploads and remote cache entries.  This is the receiver-side
    fallback used by a durable terminal-room proof after replica GC.
    """

    if room_kind not in {"guild", "group_dm"}:
        raise ValueError("terminal room kind is invalid")
    from app.federation.terminal_rooms import lock_terminal_room

    await lock_terminal_room(session, room_kind, room_id, room_domain)
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    routes = list(
        await session.scalars(
            select(MediaTombstoneDestination).where(
                MediaTombstoneDestination.room_kind == room_kind,
                MediaTombstoneDestination.room_id == room_id,
                MediaTombstoneDestination.room_domain == room_domain,
            )
        )
    )
    attachment_refs = sorted(
        {(route.attachment_id, route.attachment_domain) for route in routes},
        key=lambda ref: (ref[1], ref[0]),
    )
    for attachment_id, attachment_domain in attachment_refs:
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    attachments = (
        list(
            await session.scalars(
                select(Attachment)
                .where(tuple_(Attachment.id, Attachment.origin_domain).in_(attachment_refs))
                .order_by(Attachment.origin_domain, Attachment.id)
                .with_for_update()
            )
        )
        if attachment_refs
        else []
    )

    destinations: set[str] = set()
    for route in routes:
        destinations.update({route.attachment_domain, route.destination_domain})
    destinations.update(
        await session.scalars(
            select(RoomFederationRecipient.destination_domain).where(
                RoomFederationRecipient.room_kind == room_kind,
                RoomFederationRecipient.room_id == room_id,
                RoomFederationRecipient.room_domain == room_domain,
            )
        )
    )
    local_purges: list[tuple[int, str]] = []
    wakes: set[str] = set()
    for attachment in attachments:
        destinations.update(await historical_attachment_destinations(session, attachment))
        destinations.add(attachment.origin_domain)
        if attachment.origin_domain != settings.domain:
            continue
        wakes.update(
            await queue_terminal_attachment_tombstone(
                session,
                settings,
                attachment,
                force_authoritative=True,
            )
        )
        local_purges.append((attachment.id, attachment.origin_domain))
        attachment.message_id = None
        attachment.message_domain = None

    remote_purges = sorted(
        {
            (attachment_domain, attachment_id)
            for attachment_id, attachment_domain in attachment_refs
            if attachment_domain != settings.domain
        }
    )
    if remote_purges:
        await session.execute(
            update(RemoteMediaCache)
            .where(
                tuple_(RemoteMediaCache.origin_domain, RemoteMediaCache.attachment_id).in_(
                    remote_purges
                )
            )
            .values(expires_at=datetime.now(UTC))
        )
    destinations.discard(settings.domain)
    return local_purges, remote_purges, destinations, wakes


async def terminal_attachment_refs_for_messages(
    session: AsyncSession,
    settings: Settings,
    message_refs: set[tuple[int, str]],
) -> list[tuple[int, str]]:
    """Find local terminal attachments that became bound during replication."""

    if not message_refs:
        return []
    return list(
        (
            await session.execute(
                select(Attachment.id, Attachment.origin_domain)
                .where(
                    Attachment.origin_domain == settings.domain,
                    Attachment.message_id.is_not(None),
                    Attachment.message_domain.is_not(None),
                    Attachment.deleted_at.is_not(None),
                    Attachment.scan_status.in_(TERMINAL_ATTACHMENT_STATUSES),
                    tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs),
                )
                .order_by(Attachment.id)
            )
        ).tuples()
    )
