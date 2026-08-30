from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.guild_revision import build_guild_authority_envelope, guild_authority_owner
from app.core.federation import guild_media_delete_request_ref
from app.core.settings import Settings
from app.db.models import (
    Attachment,
    FederationEvent,
    FederationOutbox,
    Guild,
    GuildMediaDeletionRequest,
    Instance,
    Message,
    User,
)
from app.federation.events import build_envelope, queue_event
from app.federation.network import normalize_domain
from app.federation.terminal_rooms import lock_terminal_room
from app.media.tombstones import lock_media_tombstone_ref


@dataclass(frozen=True)
class GuildMediaDeletionActorRef:
    id: int
    origin_domain: str


def _key_id(envelope: dict[str, object], origin: str) -> str:
    signatures = envelope.get("signatures")
    origin_signatures = signatures.get(origin) if isinstance(signatures, dict) else None
    if not isinstance(origin_signatures, dict) or len(origin_signatures) != 1:
        raise RuntimeError("guild media deletion request signature is invalid")
    key_id = next(iter(origin_signatures))
    if not isinstance(key_id, str) or not key_id:
        raise RuntimeError("guild media deletion request signing key is invalid")
    return key_id


def _content(
    row: GuildMediaDeletionRequest | None,
    *,
    guild: Guild,
    message: Message,
    attachment: Attachment,
    deleted_at: datetime,
    generation: int,
) -> dict[str, object]:
    if row is not None:
        guild_id = row.guild_id
        guild_domain = row.guild_domain
        message_id = row.message_id
        message_domain = row.message_domain
        attachment_id = row.attachment_id
        attachment_domain = row.attachment_domain
        deleted_at = row.deleted_at
    else:
        guild_id = guild.id
        guild_domain = guild.origin_domain
        message_id = message.id
        message_domain = message.origin_domain
        attachment_id = attachment.id
        attachment_domain = attachment.origin_domain
    return {
        "guild": {"id": str(guild_id), "origin_domain": guild_domain},
        "message": {"id": str(message_id), "origin_domain": message_domain},
        "attachment": {
            "id": str(attachment_id),
            "origin_domain": attachment_domain,
        },
        "deleted_at": deleted_at.isoformat(),
        "_deletion_generation": str(generation),
    }


async def queue_guild_media_delete_request(
    session: AsyncSession,
    settings: Settings,
    *,
    guild: Guild,
    message: Message,
    attachment: Attachment,
    deleted_at: datetime,
) -> str | None:
    """Queue one durable authority request to a departed attachment home."""

    if guild.origin_domain != settings.domain:
        raise ValueError("only the guild authority may request origin media deletion")
    if attachment.origin_domain == settings.domain:
        return None
    if message.origin_domain != guild.origin_domain or (
        attachment.message_id,
        attachment.message_domain,
    ) != (message.id, message.origin_domain):
        raise ValueError("guild media deletion request binding is invalid")
    await lock_terminal_room(session, "guild", guild.id, guild.origin_domain)
    await lock_media_tombstone_ref(session, attachment.id, attachment.origin_domain)
    row = await session.scalar(
        select(GuildMediaDeletionRequest)
        .where(
            GuildMediaDeletionRequest.guild_id == guild.id,
            GuildMediaDeletionRequest.guild_domain == guild.origin_domain,
            GuildMediaDeletionRequest.attachment_id == attachment.id,
            GuildMediaDeletionRequest.attachment_domain == attachment.origin_domain,
        )
        .with_for_update()
    )
    if row is not None and row.acknowledged_at is not None:
        return None
    if row is not None and (
        row.message_id != message.id
        or row.message_domain != message.origin_domain
        or row.deleted_at != deleted_at
    ):
        raise RuntimeError("guild media deletion request conflicts with retained truth")
    instance = await session.get(Instance, settings.domain)
    if instance is None or not instance.is_self or not instance.current_key_id:
        raise RuntimeError("local federation signing identity is unavailable")
    signer: User | GuildMediaDeletionActorRef
    if row is None:
        signer = await guild_authority_owner(session, settings, guild)
    else:
        signer = GuildMediaDeletionActorRef(id=row.actor_id, origin_domain=row.actor_domain)
    current_event = (
        await session.get(FederationEvent, (settings.domain, row.event_id))
        if row is not None and row.key_id == instance.current_key_id
        else None
    )
    reuse = bool(
        row is not None
        and current_event is not None
        and isinstance(current_event.envelope, dict)
        and guild_media_delete_request_ref(current_event.envelope)
        == (
            row.guild_id,
            row.guild_domain,
            row.message_id,
            row.message_domain,
            row.attachment_id,
            row.attachment_domain,
            row.generation,
        )
    )
    old_event_id = row.event_id if row is not None else None
    if reuse:
        if current_event is None:  # pragma: no cover - narrowed above
            raise RuntimeError("guild media deletion request event disappeared")
        envelope = current_event.envelope
        generation = row.generation if row is not None else 0
    else:
        generation = (row.generation if row is not None else 0) + 1
        if generation > (1 << 63) - 1:
            raise RuntimeError("guild media deletion request generation is exhausted")
        content = _content(
            row,
            guild=guild,
            message=message,
            attachment=attachment,
            deleted_at=deleted_at,
            generation=generation,
        )
        if row is None:
            envelope = await build_guild_authority_envelope(
                session,
                settings,
                guild,
                "guild.media.delete.request",
                cast(User, signer),
                content,
                context={},
            )
        else:
            envelope = await build_envelope(
                session,
                settings,
                "guild.media.delete.request",
                cast(User, signer),
                content,
                context={},
                retained_authority_attested_actor=signer.origin_domain != settings.domain,
            )
        if guild_media_delete_request_ref(envelope) is None:
            raise RuntimeError("generated guild media deletion request is invalid")
    await queue_event(session, settings, attachment.origin_domain, envelope)
    now = datetime.now(UTC)
    event_id = str(envelope["event_id"])
    key_id = _key_id(envelope, settings.domain)
    if row is None:
        row = GuildMediaDeletionRequest(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            attachment_id=attachment.id,
            attachment_domain=attachment.origin_domain,
            message_id=message.id,
            message_domain=message.origin_domain,
            actor_id=signer.id,
            actor_domain=signer.origin_domain,
            deleted_at=deleted_at,
            event_id=event_id,
            key_id=key_id,
            generation=generation,
            updated_at=now,
        )
        session.add(row)
    else:
        row.event_id = event_id
        row.key_id = key_id
        row.generation = generation
        row.updated_at = now
    if old_event_id is not None and old_event_id != event_id:
        await session.flush()
        await session.execute(
            delete(FederationEvent).where(
                FederationEvent.origin_domain == settings.domain,
                FederationEvent.event_id == old_event_id,
            )
        )
    return attachment.origin_domain


async def acknowledge_guild_media_delete_request(
    session: AsyncSession,
    *,
    destination: str,
    envelope: dict[str, object],
) -> bool:
    ref = guild_media_delete_request_ref(envelope)
    if ref is None:
        return False
    guild_id, guild_domain, _message_id, _message_domain, attachment_id, attachment_domain, _ = ref
    destination = normalize_domain(destination)
    if destination != attachment_domain:
        raise RuntimeError("guild media deletion acknowledgement destination is invalid")
    await lock_terminal_room(session, "guild", guild_id, guild_domain)
    await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    row = await session.get(
        GuildMediaDeletionRequest,
        (guild_id, guild_domain, attachment_id, attachment_domain),
        with_for_update=True,
    )
    if row is None:
        return False
    if row.event_id != envelope.get("event_id"):
        # A key rollover may have advanced the current generation before the
        # older response arrived. Acceptance still proves the immutable delete
        # reached its only destination, so it satisfies the source row.
        actor = envelope.get("actor")
        if (
            not isinstance(actor, dict)
            or actor.get("id") != str(row.actor_id)
            or actor.get("domain") != row.actor_domain
        ):
            raise RuntimeError("guild media deletion acknowledgement conflicts with truth")
    await session.execute(
        delete(FederationEvent).where(
            FederationEvent.origin_domain == guild_domain,
            FederationEvent.event_id == row.event_id,
        )
    )
    await session.delete(row)
    return True


async def repair_guild_media_delete_request_acks(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> int:
    rows = list(
        await session.scalars(
            select(GuildMediaDeletionRequest)
            .where(
                exists(
                    select(FederationOutbox.id).where(
                        FederationOutbox.destination == GuildMediaDeletionRequest.attachment_domain,
                        FederationOutbox.event_origin_domain
                        == GuildMediaDeletionRequest.guild_domain,
                        FederationOutbox.event_id == GuildMediaDeletionRequest.event_id,
                        FederationOutbox.status == "delivered",
                    )
                )
            )
            .order_by(
                GuildMediaDeletionRequest.guild_domain,
                GuildMediaDeletionRequest.guild_id,
                GuildMediaDeletionRequest.attachment_domain,
                GuildMediaDeletionRequest.attachment_id,
            )
            .limit(limit)
        )
    )
    repaired = 0
    for row in rows:
        event = await session.get(FederationEvent, (row.guild_domain, row.event_id))
        if event is None or not isinstance(event.envelope, dict):
            continue
        if await acknowledge_guild_media_delete_request(
            session,
            destination=row.attachment_domain,
            envelope=event.envelope,
        ):
            repaired += 1
    return repaired


async def rotate_guild_media_delete_requests(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 100,
) -> set[str]:
    instance = await session.get(Instance, settings.domain)
    if instance is None or not instance.is_self or not instance.current_key_id:
        raise RuntimeError("local federation signing identity is unavailable")
    rows = list(
        await session.scalars(
            select(GuildMediaDeletionRequest)
            .where(
                GuildMediaDeletionRequest.guild_domain == settings.domain,
                GuildMediaDeletionRequest.acknowledged_at.is_(None),
                GuildMediaDeletionRequest.key_id != instance.current_key_id,
            )
            .order_by(
                GuildMediaDeletionRequest.guild_id,
                GuildMediaDeletionRequest.attachment_domain,
                GuildMediaDeletionRequest.attachment_id,
            )
            .limit(limit)
        )
    )
    wakes: set[str] = set()
    for row in rows:
        guild = Guild(id=row.guild_id, origin_domain=row.guild_domain)
        message = Message(
            id=row.message_id,
            origin_domain=row.message_domain,
            channel_id=0,
            channel_domain=row.guild_domain,
            author_id=row.actor_id,
            author_domain=row.actor_domain,
        )
        attachment = Attachment(
            id=row.attachment_id,
            origin_domain=row.attachment_domain,
            uploader_id=0,
            uploader_domain=row.attachment_domain,
            purpose="attachment",
            filename="deleted",
            content_type="application/octet-stream",
            size=0,
            object_key="deleted",
            message_id=row.message_id,
            message_domain=row.message_domain,
        )
        destination = await queue_guild_media_delete_request(
            session,
            settings,
            guild=guild,
            message=message,
            attachment=attachment,
            deleted_at=row.deleted_at,
        )
        if destination is not None:
            wakes.add(destination)
    return wakes
