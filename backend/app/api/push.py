from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.auth.security import encrypt_secret
from app.chat.payloads import public_user_display_name
from app.chat.permissions import get_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.db.models import (
    Attachment,
    Channel,
    DMParticipant,
    Guild,
    GuildNotificationSetting,
    Message,
    PushDevice,
    ReadState,
    User,
    UserSettings,
)
from app.push.presentation import (
    notification_previews_enabled,
    push_body,
    push_presentation,
)
from app.push.schemas import (
    PushDeviceCreate,
    PushDeviceResponse,
    PushNotificationRedeem,
    PushNotificationResponse,
)
from app.push.service import PUSH_TOKEN_CONTEXT
from app.push.sync import claim_push_sync, load_push_sync

router = APIRouter(prefix="/api/v1/users/@me/push-devices", tags=["push devices"])


def rotated_token_fields(
    body: PushDeviceCreate,
    *,
    digest: bytes,
    encrypted: bytes,
    now: datetime,
) -> dict[str, Any]:
    """Return the fields that must change together when a provider token rotates."""

    return {
        "platform": body.platform,
        "token_hash": digest,
        "token_encrypted": encrypted,
        "device_name": body.device_name,
        "enabled": True,
        "last_seen_at": now,
        "updated_at": now,
    }


def payload(device: PushDevice) -> PushDeviceResponse:
    return PushDeviceResponse(
        id=device.id,
        platform=cast(Literal["android", "ios"], device.platform),
        device_name=device.device_name,
        enabled=device.enabled,
        last_seen_at=device.last_seen_at.isoformat(),
    )


def _push_event_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "PUSH_EVENT_NOT_FOUND",
            "message": "This notification is no longer available",
        },
    )


async def _claim_or_not_found(redis: Redis, token: str, encoded: str | bytes) -> None:
    if not await claim_push_sync(redis, token, encoded):
        raise _push_event_not_found()


async def _suppress_push(redis: Redis, token: str, encoded: str | bytes) -> Response:
    await _claim_or_not_found(redis, token, encoded)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("")
async def list_push_devices(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[PushDeviceResponse]:
    devices = list(
        await session.scalars(
            select(PushDevice)
            .where(
                PushDevice.user_id == auth.user.id,
                PushDevice.user_domain == auth.user.origin_domain,
            )
            .order_by(PushDevice.last_seen_at.desc())
        )
    )
    return [payload(device) for device in devices]


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_push_device(
    body: PushDeviceCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PushDeviceResponse:
    if not settings.push_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "PUSH_DISABLED", "message": "Mobile push is disabled"},
        )
    digest = hashlib.sha256(body.token.encode("utf-8")).digest()
    now = datetime.now(UTC)
    encrypted = encrypt_secret(
        body.token,
        settings.secret_key_bytes,
        context=PUSH_TOKEN_CONTEXT,
    )
    device_id = str(body.installation_id)

    # A provider token can rotate and an installation can change accounts. Lock
    # both identities in a deterministic order so concurrent refresh/login
    # callbacks cannot leave the same physical installation attached to two
    # users or violate the token uniqueness constraint.
    lock_names = sorted((f"installation:{device_id}", f"push-token:{digest.hex()}"))
    for lock_name in lock_names:
        await session.scalar(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_name, 0)))
        )
    await session.execute(
        delete(PushDevice).where(
            PushDevice.token_hash == digest,
            PushDevice.id != device_id,
        )
    )
    statement = pg_insert(PushDevice).values(
        id=device_id,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        user_is_local=True,
        platform=body.platform,
        token_hash=digest,
        token_encrypted=encrypted,
        device_name=body.device_name,
        enabled=True,
        last_seen_at=now,
    )
    device_id = str(
        await session.scalar(
            statement.on_conflict_do_update(
                index_elements=[PushDevice.id],
                set_={
                    "user_id": auth.user.id,
                    "user_domain": auth.user.origin_domain,
                    "user_is_local": True,
                    # Provider tokens rotate over the lifetime of one app
                    # installation.  Keep the lookup digest paired with the
                    # encrypted token or a later registration could collide
                    # with a stale digest and invalid-token cleanup would act
                    # on the wrong credential.
                    **rotated_token_fields(
                        body,
                        digest=digest,
                        encrypted=encrypted,
                        now=now,
                    ),
                },
            ).returning(PushDevice.id)
        )
    )
    await session.commit()
    device = await session.get(PushDevice, device_id)
    if device is None:  # pragma: no cover - defensive against external deletion
        raise RuntimeError("push registration disappeared after commit")
    return payload(device)


@router.post(
    "/notifications/redeem",
    response_model=PushNotificationResponse,
    responses={status.HTTP_204_NO_CONTENT: {"description": "Notification suppressed"}},
)
async def redeem_push_notification(
    body: PushNotificationRedeem,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> PushNotificationResponse | Response:
    """Redeem one opaque, short-lived wake token over the authenticated API."""

    loaded = await load_push_sync(redis, body.event_token)
    if loaded is None:
        raise _push_event_not_found()
    encoded, event = loaded
    if (
        event.device_id != str(body.installation_id)
        or event.user_id != auth.user.id
        or event.user_domain != auth.user.origin_domain
    ):
        raise _push_event_not_found()

    device = await session.scalar(
        select(PushDevice).where(
            PushDevice.id == event.device_id,
            PushDevice.user_id == auth.user.id,
            PushDevice.user_domain == auth.user.origin_domain,
            PushDevice.enabled.is_(True),
        )
    )
    if device is None:
        raise _push_event_not_found()

    message = await session.get(Message, (event.message_id, event.message_domain))
    if (
        message is None
        or message.deleted_at is not None
        or (message.author_id == auth.user.id and message.author_domain == auth.user.origin_domain)
    ):
        return await _suppress_push(redis, body.event_token, encoded)
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None:
        return await _suppress_push(redis, body.event_token, encoded)

    preferences_row = await session.get(
        UserSettings,
        (auth.user.id, auth.user.origin_domain),
    )
    preferences = preferences_row.notification_settings if preferences_row is not None else {}
    if preferences.get("presence_preference") == "dnd":
        return await _suppress_push(redis, body.event_token, encoded)

    is_dm = channel.type == 1
    is_mention = event.kind == "mention"
    guild: Guild | None = None
    if is_dm:
        participant = await session.scalar(
            select(DMParticipant.user_id).where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
                DMParticipant.user_id == auth.user.id,
                DMParticipant.user_domain == auth.user.origin_domain,
            )
        )
        if participant is None or event.kind != "direct_message":
            return await _suppress_push(redis, body.event_token, encoded)
        if not bool(preferences.get("direct_messages", True)):
            return await _suppress_push(redis, body.event_token, encoded)
    elif channel.guild_id is not None and channel.guild_domain is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None or guild.unavailable:
            return await _suppress_push(redis, body.event_token, encoded)
        permissions = await get_permissions(session, redis, guild, auth.user, channel=channel)
        if not permissions & Permission.VIEW_CHANNEL:
            return await _suppress_push(redis, body.event_token, encoded)
        notification_setting = await session.get(
            GuildNotificationSetting,
            (auth.user.id, auth.user.origin_domain, guild.id, guild.origin_domain),
        )
        level = notification_setting.level if notification_setting is not None else "mentions"
        if level == "none" or (level == "mentions" and not is_mention):
            return await _suppress_push(redis, body.event_token, encoded)
        if is_mention and not bool(preferences.get("mentions", True)):
            return await _suppress_push(redis, body.event_token, encoded)
        if event.kind not in {"mention", "guild_message"}:
            return await _suppress_push(redis, body.event_token, encoded)
    else:
        return await _suppress_push(redis, body.event_token, encoded)

    read_state = await session.get(
        ReadState,
        (auth.user.id, auth.user.origin_domain, channel.id, channel.origin_domain),
    )
    if read_state is not None and read_state.last_message_id is not None:
        last_read = (read_state.last_message_id, read_state.last_message_domain or "")
        if last_read >= (message.id, message.origin_domain):
            return await _suppress_push(redis, body.event_token, encoded)

    author = await session.get(User, (message.author_id, message.author_domain))
    if author is None:
        return await _suppress_push(redis, body.event_token, encoded)
    has_attachment = bool(
        await session.scalar(
            select(
                exists().where(
                    Attachment.message_id == message.id,
                    Attachment.message_domain == message.origin_domain,
                    Attachment.deleted_at.is_(None),
                )
            )
        )
    )
    author_name = message.webhook_name or public_user_display_name(author)
    avatar_hash = message.webhook_avatar_hash or author.avatar_hash
    if avatar_hash is not None and (
        len(avatar_hash) != 64
        or any(character not in "0123456789abcdef" for character in avatar_hash)
    ):
        avatar_hash = None
    if is_dm:
        title = author_name
    else:
        if guild is None:  # Defensive: guild channels were validated above.
            return await _suppress_push(redis, body.event_token, encoded)
        title = f"{author_name} in {guild.name}"
    show_preview = notification_previews_enabled(preferences)
    title, notification_body = push_presentation(
        show_preview=show_preview,
        is_dm=is_dm,
        is_mention=is_mention,
        title=title,
        body=push_body(message, has_attachment),
    )
    await _claim_or_not_found(redis, body.event_token, encoded)
    return PushNotificationResponse(
        kind=cast(Literal["direct_message", "mention", "guild_message"], event.kind),
        title=title,
        body=notification_body,
        channel_ref=f"{channel.id}@{channel.origin_domain}",
        message_ref=f"{message.id}@{message.origin_domain}",
        sender_name=author_name if show_preview else None,
        sender_ref=(f"{author.id}@{author.origin_domain}" if show_preview else None),
        sender_avatar_hash=avatar_hash if show_preview else None,
        sent_at=message.created_at.isoformat(),
    )


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_push_device(
    device_id: UUID,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    device = await session.scalar(
        select(PushDevice)
        .where(
            PushDevice.id == str(device_id),
            PushDevice.user_id == auth.user.id,
            PushDevice.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PUSH_DEVICE_NOT_FOUND", "message": "Push device not found"},
        )
    await session.delete(device)
    await session.commit()
