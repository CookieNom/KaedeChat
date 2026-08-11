from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import and_, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.auth.schemas import SettingsPatch
from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import user_payload
from app.chat.permissions import get_permissions
from app.chat.privacy import lock_dm_privacy
from app.chat.schemas import ProfilePatch
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityReference, validate_entity_reference
from app.db.models import (
    Channel,
    DMParticipant,
    Guild,
    GuildMember,
    Message,
    ReadState,
    User,
    UserSettings,
)
from app.federation.relationships import queue_friend_profile_updates
from app.federation.users import resolve_handle
from app.tasks import federation_deliver

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/lookup")
async def lookup_user(
    handle: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    requester_key = f"{auth.user.origin_domain}:{auth.user.id}"
    return user_payload(await resolve_handle(session, settings, redis, requester_key, handle))


@router.get("/@me")
async def get_me(auth: AuthenticatedUser = Depends(require_user)) -> dict[str, object]:
    user = auth.user
    return {
        "id": str(user.id),
        "origin_domain": user.origin_domain,
        "username": user.username,
        "handle": f"{user.username}@{user.origin_domain}",
        "display_name": user.display_name,
        "avatar_hash": user.avatar_hash,
        "banner_hash": user.banner_hash,
        "bio": user.bio,
        "custom_status": user.custom_status,
        "profile_version": str(user.profile_version),
        "email": user.email,
        "email_verified": user.email_verified_at is not None,
        "mfa_enabled": user.totp_secret_encrypted is not None,
    }


@router.patch("/@me")
async def patch_me(
    payload: ProfilePatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    user = await session.scalar(
        select(User)
        .where(
            User.id == auth.user.id,
            User.origin_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED"})
    values = payload.model_dump(exclude_unset=True)
    if any(getattr(user, field) != value for field, value in values.items()):
        for field, value in values.items():
            setattr(user, field, value)
        user.profile_version += 1
        destinations = await queue_friend_profile_updates(session, settings, user)
        await session.commit()
        await publish_dispatch(
            redis,
            user_topic(user.origin_domain, user.id),
            "USER_UPDATE",
            user_payload(user),
        )
        for destination in destinations:
            await enqueue_best_effort(federation_deliver, destination)
    return await get_me(auth)


async def load_settings(session: AsyncSession, auth: AuthenticatedUser) -> UserSettings:
    settings = await session.scalar(
        select(UserSettings).where(
            UserSettings.user_id == auth.user.id,
            UserSettings.user_domain == auth.user.origin_domain,
        )
    )
    if settings is None:
        raise HTTPException(status_code=500, detail={"code": "SETTINGS_MISSING"})
    return settings


def settings_payload(settings: UserSettings) -> dict[str, object]:
    presence_preference = settings.notification_settings.get("presence_preference", "online")
    if presence_preference not in {"online", "idle", "dnd", "invisible"}:
        presence_preference = "online"
    notification_settings = {
        key: value
        for key, value in settings.notification_settings.items()
        if key != "presence_preference"
    }
    return {
        "locale": settings.locale,
        "theme": settings.theme,
        "dm_privacy": settings.dm_privacy,
        "presence_preference": presence_preference,
        "notification_settings": notification_settings,
    }


@router.get("/@me/settings")
async def get_user_settings(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    return settings_payload(await load_settings(session, auth))


@router.patch("/@me/settings")
async def patch_user_settings(
    payload: SettingsPatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    settings = await session.scalar(
        select(UserSettings)
        .where(
            UserSettings.user_id == auth.user.id,
            UserSettings.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if settings is None:
        raise HTTPException(status_code=500, detail={"code": "SETTINGS_MISSING"})
    if "dm_privacy" in payload.model_fields_set:
        await lock_dm_privacy(session, auth.user)
    changes = payload.model_dump(exclude_unset=True)
    presence_preference = changes.pop("presence_preference", None)
    notification_settings = changes.pop("notification_settings", None)
    for field, value in changes.items():
        setattr(settings, field, value)
    if notification_settings is not None:
        # Presence is an account-level preference. Generic notification updates
        # must not erase a concurrent presence selection made by another client.
        merged_notification_settings = dict(notification_settings)
        merged_notification_settings.pop("presence_preference", None)
        existing_presence = settings.notification_settings.get("presence_preference")
        if existing_presence is not None:
            merged_notification_settings["presence_preference"] = existing_presence
        settings.notification_settings = merged_notification_settings
    if presence_preference is not None:
        settings.notification_settings = {
            **settings.notification_settings,
            "presence_preference": presence_preference,
        }
    await session.commit()
    return settings_payload(settings)


@router.get("/@me/read-states")
async def list_read_states(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> list[dict[str, object]]:
    guild_access = exists().where(
        GuildMember.guild_id == Channel.guild_id,
        GuildMember.guild_domain == Channel.guild_domain,
        GuildMember.user_id == auth.user.id,
        GuildMember.user_domain == auth.user.origin_domain,
    )
    dm_access = exists().where(
        DMParticipant.conversation_id == Channel.id,
        DMParticipant.conversation_domain == Channel.origin_domain,
        DMParticipant.user_id == auth.user.id,
        DMParticipant.user_domain == auth.user.origin_domain,
    )
    channels = list(
        await session.scalars(
            select(Channel)
            .where(Channel.unavailable.is_(False), or_(guild_access, dm_access))
            .order_by(Channel.id)
        )
    )
    guild_refs = {
        (channel.guild_id, channel.guild_domain)
        for channel in channels
        if channel.guild_id is not None and channel.guild_domain is not None
    }
    guilds = (
        list(
            await session.scalars(
                select(Guild).where(tuple_(Guild.id, Guild.origin_domain).in_(guild_refs))
            )
        )
        if guild_refs
        else []
    )
    guild_by_ref = {(guild.id, guild.origin_domain): guild for guild in guilds}
    visible_channels: list[Channel] = []
    for channel in channels:
        if channel.guild_id is None or channel.guild_domain is None:
            visible_channels.append(channel)
            continue
        guild = guild_by_ref.get((channel.guild_id, channel.guild_domain))
        if guild is None:
            continue
        permissions = await get_permissions(session, redis, guild, auth.user, channel=channel)
        if permissions & Permission.VIEW_CHANNEL:
            visible_channels.append(channel)
    channels = visible_channels
    states = list(
        await session.scalars(
            select(ReadState).where(
                ReadState.user_id == auth.user.id,
                ReadState.user_domain == auth.user.origin_domain,
            )
        )
    )
    by_channel = {(state.channel_id, state.channel_domain): state for state in states}
    channel_refs = [(channel.id, channel.origin_domain) for channel in channels]
    unread_counts = (
        {
            (channel_id, channel_domain): int(count)
            for channel_id, channel_domain, count in (
                await session.execute(
                    select(
                        Message.channel_id,
                        Message.channel_domain,
                        func.count(Message.id),
                    )
                    .outerjoin(
                        ReadState,
                        and_(
                            ReadState.user_id == auth.user.id,
                            ReadState.user_domain == auth.user.origin_domain,
                            ReadState.channel_id == Message.channel_id,
                            ReadState.channel_domain == Message.channel_domain,
                        ),
                    )
                    .where(
                        tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs),
                        or_(
                            ReadState.last_message_id.is_(None),
                            tuple_(Message.id, Message.origin_domain)
                            > tuple_(ReadState.last_message_id, ReadState.last_message_domain),
                        ),
                    )
                    .group_by(Message.channel_id, Message.channel_domain)
                )
            ).tuples()
        }
        if channel_refs
        else {}
    )
    keys = [f"channel:last_message:{channel.origin_domain}:{channel.id}" for channel in channels]
    cached = await redis.mget(keys) if keys else []
    result: list[dict[str, object]] = []
    for channel, cached_last in zip(channels, cached, strict=True):
        state = by_channel.get((channel.id, channel.origin_domain))
        unread_count = unread_counts.get((channel.id, channel.origin_domain), 0)
        latest: EntityReference | None = None
        if cached_last is not None:
            try:
                parsed = validate_entity_reference(cached_last)
                latest = EntityReference(parsed.id, parsed.domain or channel.origin_domain)
            except ValueError:
                await redis.delete(f"channel:last_message:{channel.origin_domain}:{channel.id}")
        if latest is None and channel.last_message_id is not None:
            latest = EntityReference(channel.last_message_id, channel.last_message_domain)
        read = (
            EntityReference(state.last_message_id, state.last_message_domain)
            if state is not None and state.last_message_id is not None
            else None
        )
        result.append(
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "guild_id": str(channel.guild_id) if channel.guild_id is not None else None,
                "guild_domain": channel.guild_domain,
                "last_message_id": str(latest.id) if latest is not None else None,
                "last_message_domain": latest.domain if latest is not None else None,
                "read_message_id": str(read.id) if read is not None else None,
                "read_message_domain": read.domain if read is not None else None,
                "mention_count": state.mention_count if state is not None else 0,
                "unread_count": unread_count,
                "unread": unread_count > 0,
            }
        )
    return result
