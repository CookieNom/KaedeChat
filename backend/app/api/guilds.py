from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.chat.audit import add_audit_entry
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.hierarchy import highest_role, require_can_manage_member, require_can_manage_role
from app.chat.payloads import (
    channel_payload,
    emoji_payload,
    guild_payload,
    role_payload,
    sticker_payload,
)
from app.chat.permissions import get_permissions, require_permissions
from app.chat.schemas import (
    ChannelCreate,
    GuildCreate,
    GuildNotificationSettingsUpdate,
    OverwritePut,
    RoleCreate,
)
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef, EntityReference, EntityReferenceLike
from app.db.models import (
    Channel,
    ChannelOverwrite,
    Emoji,
    Guild,
    GuildMember,
    GuildNotificationSetting,
    Role,
    Sticker,
    User,
)
from app.tracker.service import create_tracker_state

router = APIRouter(prefix="/api/v1", tags=["guilds"])
log = structlog.get_logger()

DEFAULT_PERMISSIONS = int(
    Permission.CREATE_INVITE
    | Permission.ADD_REACTIONS
    | Permission.VIEW_CHANNEL
    | Permission.SEND_MESSAGES
    | Permission.EMBED_LINKS
    | Permission.ATTACH_FILES
    | Permission.READ_MESSAGE_HISTORY
    | Permission.USE_EXTERNAL_EMOJIS
    | Permission.CONNECT
    | Permission.SPEAK
    | Permission.USE_VAD
    | Permission.STREAM
    | Permission.CHANGE_NICKNAME
    | Permission.USE_APPLICATION_COMMANDS
    | Permission.CREATE_PUBLIC_THREADS
    | Permission.CREATE_PRIVATE_THREADS
    | Permission.SEND_MESSAGES_IN_THREADS
    | Permission.CREATE_TRACKER_TASKS
    | Permission.EDIT_OWN_TRACKER_TASKS
)


async def forum_tags_payload(
    tags: list[object],
    snowflake: SnowflakeGenerator,
    *,
    existing_ids: set[int] | None = None,
) -> list[dict[str, object]]:
    rendered: list[dict[str, object]] = []
    for value in tags:
        raw = value.model_dump()  # type: ignore[attr-defined]
        if raw.get("id") is not None:
            tag_id = int(raw["id"])
            if existing_ids is None or tag_id not in existing_ids:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "FORUM_TAG_ID_INVALID"},
                )
        else:
            tag_id = await snowflake.mint()
        rendered.append(
            {
                "id": str(tag_id),
                "name": raw["name"],
                "moderated": bool(raw["moderated"]),
                "emoji_id": (str(raw["emoji_id"]) if raw.get("emoji_id") is not None else None),
                "emoji_name": raw.get("emoji_name"),
            }
        )
    return rendered


async def validate_forum_emoji_ids(
    session: AsyncSession,
    guild: Guild,
    tags: list[object],
    default_reaction: object | None,
) -> None:
    """Reject deleted, foreign-guild, and unknown custom emoji references."""

    emoji_ids: set[int] = set()
    for value in tags:
        raw = value if isinstance(value, dict) else value.model_dump()  # type: ignore[attr-defined]
        if raw.get("emoji_id") is not None:
            emoji_ids.add(int(raw["emoji_id"]))
    if default_reaction is not None:
        raw = (
            default_reaction
            if isinstance(default_reaction, dict)
            else default_reaction.model_dump()  # type: ignore[attr-defined]
        )
        if raw.get("emoji_id") is not None:
            emoji_ids.add(int(raw["emoji_id"]))
    if not emoji_ids:
        return
    live_ids = set(
        await session.scalars(
            select(Emoji.id).where(
                Emoji.id.in_(emoji_ids),
                Emoji.origin_domain == guild.origin_domain,
                Emoji.guild_id == guild.id,
                Emoji.guild_domain == guild.origin_domain,
            )
        )
    )
    if live_ids != emoji_ids:
        raise HTTPException(status_code=400, detail={"code": "FORUM_EMOJI_INVALID"})


def forum_reaction_payload(value: object | None) -> dict[str, object] | None:
    if value is None:
        return None
    raw = value.model_dump()  # type: ignore[attr-defined]
    return {
        "emoji_id": str(raw["emoji_id"]) if raw.get("emoji_id") is not None else None,
        "emoji_name": raw.get("emoji_name"),
    }


async def overwrite_source_channel(session: AsyncSession, channel: Channel) -> Channel:
    if channel.parent_id is None or not channel.permissions_synced:
        return channel
    parent = await session.get(Channel, (channel.parent_id, channel.parent_domain))
    if (
        parent is None
        or parent.type != 4
        or (
            parent.guild_id,
            parent.guild_domain,
        )
        != (channel.guild_id, channel.guild_domain)
    ):
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_PARENT_INVALID"})
    return parent


async def copy_overwrites(session: AsyncSession, source: Channel, destination: Channel) -> None:
    await session.execute(
        delete(ChannelOverwrite).where(
            ChannelOverwrite.channel_id == destination.id,
            ChannelOverwrite.channel_domain == destination.origin_domain,
        )
    )
    rows = list(
        await session.scalars(
            select(ChannelOverwrite).where(
                ChannelOverwrite.channel_id == source.id,
                ChannelOverwrite.channel_domain == source.origin_domain,
            )
        )
    )
    session.add_all(
        [
            ChannelOverwrite(
                channel_id=destination.id,
                channel_domain=destination.origin_domain,
                guild_id=destination.guild_id,
                guild_domain=destination.guild_domain,
                target_id=item.target_id,
                target_domain=item.target_domain,
                target_type=item.target_type,
                allow=item.allow,
                deny=item.deny,
            )
            for item in rows
        ]
    )
    await session.flush()


async def validate_overwrite_target(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    target_type: str,
    target_id: int,
    target_domain: str,
) -> None:
    if target_type == "role":
        target = await session.scalar(
            select(Role).where(
                Role.id == target_id,
                Role.origin_domain == target_domain,
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
            )
        )
        if target is None:
            raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
        await require_can_manage_role(session, guild, actor, target)
        return
    await require_can_manage_member(session, guild, actor, target_id, target_domain)


async def channel_payload_for(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    channel: Channel,
) -> dict[str, object]:
    rendered = channel_payload(channel)
    rendered["permissions"] = str(
        int(await get_permissions(session, redis, guild, actor, channel=channel))
    )
    return rendered


async def local_guild(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityReferenceLike,
    *,
    for_update: bool = False,
) -> Guild:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    statement = select(Guild).where(Guild.id == guild_id, Guild.origin_domain == settings.domain)
    if for_update:
        statement = statement.with_for_update()
    guild = await session.scalar(statement)
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


async def guild_channel(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityReferenceLike,
    channel_ref: EntityReferenceLike,
) -> Channel:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    if guild_domain != settings.domain or channel_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    channel = await session.scalar(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.origin_domain == channel_domain,
            Channel.guild_id == guild_id,
            Channel.guild_domain == guild_domain,
            Channel.unavailable.is_(False),
        )
    )
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    return channel


@router.post("/guilds", status_code=status.HTTP_201_CREATED)
async def create_guild(
    payload: GuildCreate,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["guild_create"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    guild_id = await snowflake.mint()
    channel_id = await snowflake.mint()
    now = datetime.now(UTC)
    guild = Guild(
        id=guild_id,
        origin_domain=settings.domain,
        name=payload.name,
        owner_id=auth.user.id,
        owner_domain=auth.user.origin_domain,
    )
    everyone = Role(
        id=guild_id,
        origin_domain=settings.domain,
        guild_id=guild_id,
        guild_domain=settings.domain,
        name="@everyone",
        permissions=DEFAULT_PERMISSIONS,
        position=0,
    )
    member = GuildMember(
        guild_id=guild_id,
        guild_domain=settings.domain,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        joined_at=now,
    )
    channel = Channel(
        id=channel_id,
        origin_domain=settings.domain,
        guild_id=guild_id,
        guild_domain=settings.domain,
        type=0,
        name="general",
        position=0,
        created_floor_id=channel_id,
    )
    session.add(guild)
    await session.flush()
    session.add_all([everyone, member, channel])
    await session.flush()
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        1,
        target_type="guild",
        target_ref={"id": str(guild.id)},
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    result = {
        **guild_payload(guild),
        "channels": [await channel_payload_for(session, redis, guild, auth.user, channel)],
    }
    try:
        await publish_dispatch(
            redis, guild_topic(settings.domain, guild_id), "GUILD_CREATE", result
        )
        await publish_dispatch(
            redis,
            user_topic(auth.user.origin_domain, auth.user.id),
            "GUILD_CREATE",
            result,
        )
    except Exception:
        log.exception(
            "guild_postcommit_projection_failed",
            guild_id=str(guild.id),
            guild_domain=guild.origin_domain,
        )
    return result


@router.get("/users/@me/guilds")
async def list_my_guilds(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> list[dict[str, object]]:
    guilds = list(
        await session.scalars(
            select(Guild)
            .join(
                GuildMember,
                (GuildMember.guild_id == Guild.id)
                & (GuildMember.guild_domain == Guild.origin_domain),
            )
            .where(
                GuildMember.user_id == auth.user.id,
                GuildMember.user_domain == auth.user.origin_domain,
            )
            .order_by(func.lower(Guild.name), Guild.id)
        )
    )
    channels_by_guild: dict[tuple[int, str], list[dict[str, object]]] = {
        (guild.id, guild.origin_domain): [] for guild in guilds
    }
    if guilds:
        channels = await session.scalars(
            select(Channel)
            .where(
                tuple_(Channel.guild_id, Channel.guild_domain).in_(
                    [(guild.id, guild.origin_domain) for guild in guilds]
                ),
                Channel.unavailable.is_(False),
                Channel.type.not_in({10, 11, 12}),
            )
            .order_by(Channel.position, Channel.id)
        )
        guild_by_ref = {(guild.id, guild.origin_domain): guild for guild in guilds}
        for channel in channels:
            if channel.guild_id is not None and channel.guild_domain is not None:
                guild = guild_by_ref[(channel.guild_id, channel.guild_domain)]
                permissions = await get_permissions(
                    session, redis, guild, auth.user, channel=channel
                )
                if permissions & Permission.VIEW_CHANNEL:
                    rendered = channel_payload(channel)
                    rendered["permissions"] = str(int(permissions))
                    channels_by_guild[(channel.guild_id, channel.guild_domain)].append(rendered)
    result: list[dict[str, object]] = []
    for guild in guilds:
        permissions = await get_permissions(session, redis, guild, auth.user)
        result.append(
            {
                **guild_payload(guild),
                "permissions": str(int(permissions)),
                "channels": channels_by_guild[(guild.id, guild.origin_domain)],
            }
        )
    return result


@router.get("/guilds/{guild_id}")
async def get_guild(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild_number, guild_domain = guild_id.resolve(settings.domain)
    guild = await session.scalar(
        select(Guild)
        .join(
            GuildMember,
            (GuildMember.guild_id == Guild.id) & (GuildMember.guild_domain == Guild.origin_domain),
        )
        .where(
            Guild.id == guild_number,
            Guild.origin_domain == guild_domain,
            GuildMember.user_id == auth.user.id,
            GuildMember.user_domain == auth.user.origin_domain,
        )
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.unavailable.is_(False),
                Channel.type.not_in({10, 11, 12}),
            )
            .order_by(Channel.position, Channel.id)
        )
    )
    roles = await session.scalars(
        select(Role)
        .where(Role.guild_id == guild.id, Role.guild_domain == guild.origin_domain)
        .order_by(Role.position, Role.id)
    )
    emojis = list(
        await session.scalars(
            select(Emoji)
            .where(Emoji.guild_id == guild.id, Emoji.guild_domain == guild.origin_domain)
            .order_by(Emoji.name, Emoji.id)
        )
    )
    stickers = list(
        await session.scalars(
            select(Sticker)
            .where(Sticker.guild_id == guild.id, Sticker.guild_domain == guild.origin_domain)
            .order_by(Sticker.name, Sticker.id)
        )
    )
    permissions = await get_permissions(session, redis, guild, auth.user)
    actor_highest_role = await highest_role(session, guild, auth.user.id, auth.user.origin_domain)
    return {
        **guild_payload(guild),
        "permissions": str(int(permissions)),
        "actor_highest_role_id": str(actor_highest_role.id),
        "channels": [
            await channel_payload_for(session, redis, guild, auth.user, channel)
            for channel in channels
            if await get_permissions(session, redis, guild, auth.user, channel=channel)
            & Permission.VIEW_CHANNEL
        ],
        "roles": [role_payload(role) for role in roles],
        "emojis": [emoji_payload(emoji) for emoji in emojis],
        "emoji_limit": settings.media_emoji_limit,
        "emoji_max_bytes": settings.media_max_emoji_bytes,
        "stickers": [sticker_payload(sticker) for sticker in stickers],
        "sticker_limit": settings.media_sticker_limit,
        "sticker_max_bytes": settings.media_max_sticker_bytes,
        "sticker_background_removal_enabled": settings.media_sticker_background_removal_enabled,
    }


def guild_notification_settings_payload(
    setting: GuildNotificationSetting,
) -> dict[str, str]:
    return {
        "guild_id": str(setting.guild_id),
        "guild_domain": setting.guild_domain,
        "level": setting.level,
    }


@router.get("/users/@me/guild-notification-settings")
async def list_guild_notification_settings(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, str]]:
    settings = await session.scalars(
        select(GuildNotificationSetting)
        .where(
            GuildNotificationSetting.user_id == auth.user.id,
            GuildNotificationSetting.user_domain == auth.user.origin_domain,
        )
        .order_by(
            GuildNotificationSetting.guild_domain,
            GuildNotificationSetting.guild_id,
        )
    )
    return [guild_notification_settings_payload(setting) for setting in settings]


@router.get("/guilds/{guild_id}/notification-settings")
async def get_guild_notification_settings(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    guild_number, guild_domain = guild_id.resolve(settings.domain)
    membership = await session.get(
        GuildMember,
        (guild_number, guild_domain, auth.user.id, auth.user.origin_domain),
    )
    if membership is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    preference = await session.get(
        GuildNotificationSetting,
        (auth.user.id, auth.user.origin_domain, guild_number, guild_domain),
    )
    if preference is None:
        return {
            "guild_id": str(guild_number),
            "guild_domain": guild_domain,
            "level": "mentions",
        }
    return guild_notification_settings_payload(preference)


@router.put("/guilds/{guild_id}/notification-settings")
async def put_guild_notification_settings(
    guild_id: EntityRef,
    payload: GuildNotificationSettingsUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    guild_number, guild_domain = guild_id.resolve(settings.domain)
    membership = await session.scalar(
        select(GuildMember)
        .where(
            GuildMember.guild_id == guild_number,
            GuildMember.guild_domain == guild_domain,
            GuildMember.user_id == auth.user.id,
            GuildMember.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if membership is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    preference = await session.get(
        GuildNotificationSetting,
        (auth.user.id, auth.user.origin_domain, guild_number, guild_domain),
    )
    if preference is None:
        preference = GuildNotificationSetting(
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            user_is_local=True,
            guild_id=guild_number,
            guild_domain=guild_domain,
            level=payload.level,
        )
        session.add(preference)
    else:
        preference.level = payload.level
    await session.commit()
    return guild_notification_settings_payload(preference)


@router.post("/guilds/{guild_id}/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(
    guild_id: EntityRef,
    payload: ChannelCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("channel.create")
    )
    if payload.type == 15 and payload.e2ee_required and not settings.e2ee_activation_enabled:
        raise HTTPException(status_code=403, detail={"code": "E2EE_ACTIVATION_DISABLED"})
    if payload.type == 15:
        await validate_forum_emoji_ids(
            session,
            guild,
            list(payload.available_tags),
            payload.default_reaction_emoji,
        )
    if payload.type == 4 and payload.parent_id is not None:
        raise HTTPException(status_code=400, detail={"code": "INVALID_CHANNEL_PARENT"})
    if payload.parent_id is not None:
        parent = await guild_channel(
            session,
            settings,
            guild_id,
            EntityReference(payload.parent_id),
        )
        if parent.type != 4:
            raise HTTPException(status_code=400, detail={"code": "PARENT_NOT_CATEGORY"})
    channel_id = await snowflake.mint()
    now = datetime.now(UTC)
    position = await session.scalar(
        select(func.coalesce(func.max(Channel.position), -1)).where(
            Channel.guild_id == guild.id,
            Channel.guild_domain == guild.origin_domain,
            Channel.type.not_in({10, 11, 12}),
        )
    )
    forum = payload.type == 15
    channel = Channel(
        id=channel_id,
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=payload.type,
        name=payload.name,
        topic=payload.topic,
        position=int(position or 0) + 1,
        parent_id=payload.parent_id,
        parent_domain=settings.domain if payload.parent_id is not None else None,
        permissions_synced=payload.parent_id is not None,
        rate_limit_per_user=payload.rate_limit_per_user,
        flags=payload.flags if forum else 0,
        default_auto_archive_duration=(
            payload.default_auto_archive_duration if payload.type in {0, 5, 15} else None
        ),
        default_thread_rate_limit_per_user=(
            payload.default_thread_rate_limit_per_user if payload.type in {0, 15} else None
        ),
        available_tags=(
            await forum_tags_payload(list(payload.available_tags), snowflake) if forum else []
        ),
        default_reaction_emoji=(
            forum_reaction_payload(payload.default_reaction_emoji) if forum else None
        ),
        default_sort_order=payload.default_sort_order if forum else None,
        default_forum_layout=payload.default_forum_layout if forum else None,
        e2ee_required=payload.e2ee_required if forum else False,
        created_floor_id=channel_id,
        created_at=now,
    )
    session.add(channel)
    if channel.type == 17:
        await create_tracker_state(
            session,
            snowflake,
            channel,
            key_prefix=payload.tracker_key_prefix,
        )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.channel.create",
        {"channel": federation_channel_state(channel)},
        channel=channel,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        10,
        target_type="channel",
        target_ref={"id": str(channel.id)},
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    result = await channel_payload_for(session, redis, guild, auth.user, channel)
    await publish_dispatch(redis, guild_topic(settings.domain, guild.id), "CHANNEL_CREATE", result)
    return result


@router.post("/guilds/{guild_id}/roles", status_code=status.HTTP_201_CREATED)
async def create_role(
    guild_id: EntityRef,
    payload: RoleCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    actor_permissions = await require_permissions(
        session, redis, guild, auth.user, required_permissions("role.create")
    )
    if payload.permissions & ~actor_permissions:
        raise HTTPException(status_code=403, detail={"code": "CANNOT_GRANT_PERMISSIONS"})
    existing_roles = list(
        await session.scalars(
            select(Role)
            .where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                Role.id != guild.id,
            )
            .with_for_update()
        )
    )
    role_id = await snowflake.mint()
    # New roles start directly above @everyone. Shift the existing numeric
    # positions without changing their relative hierarchy, which keeps role
    # positions unique and avoids ever creating a role at the actor's rank.
    for existing_role in existing_roles:
        existing_role.position += 1
    role = Role(
        id=role_id,
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name=payload.name,
        permissions=payload.permissions,
        color=payload.color,
        position=1,
        hoist=payload.hoist,
        mentionable=payload.mentionable,
    )
    guild.permission_generation += 1
    session.add(role)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.role.create",
        {"role": role_payload(role)},
        snapshot_required=True,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        30,
        target_type="role",
        target_ref={"id": str(role.id)},
    )
    await session.commit()
    for existing_role in existing_roles:
        await session.refresh(existing_role)
    await wake_queued_guild_federation(guild)
    result = role_payload(role)
    for existing_role in existing_roles:
        await publish_dispatch(
            redis,
            guild_topic(settings.domain, guild.id),
            "GUILD_ROLE_UPDATE",
            role_payload(existing_role),
        )
    await publish_dispatch(
        redis, guild_topic(settings.domain, guild.id), "GUILD_ROLE_CREATE", result
    )
    return result


@router.put("/guilds/{guild_id}/channels/{channel_id}/overwrites")
async def put_overwrite(
    guild_id: EntityRef,
    channel_id: EntityRef,
    payload: OverwritePut,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, str]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    if channel.type in {10, 11, 12}:
        raise HTTPException(status_code=400, detail={"code": "THREAD_OVERWRITES_UNSUPPORTED"})
    actor_permissions = await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("channel.overwrite.put"),
        channel=channel,
    )
    target_id, target_domain = payload.target_id.resolve(settings.domain)
    await validate_overwrite_target(
        session, guild, auth.user, payload.target_type, target_id, target_domain
    )
    source = await overwrite_source_channel(session, channel)
    if source is not channel:
        await copy_overwrites(session, source, channel)
        channel.permissions_synced = False
    overwrite = await session.scalar(
        select(ChannelOverwrite).where(
            ChannelOverwrite.channel_id == channel.id,
            ChannelOverwrite.channel_domain == channel.origin_domain,
            ChannelOverwrite.target_id == target_id,
            ChannelOverwrite.target_domain == target_domain,
            ChannelOverwrite.target_type == payload.target_type,
        )
    )
    empty_overwrite = payload.allow == 0 and payload.deny == 0
    if overwrite is None:
        changed_bits = payload.allow | payload.deny
        if not empty_overwrite:
            overwrite = ChannelOverwrite(
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                target_id=target_id,
                target_domain=target_domain,
                target_type=payload.target_type,
                allow=payload.allow,
                deny=payload.deny,
            )
            session.add(overwrite)
    else:
        changed_bits = (overwrite.allow ^ payload.allow) | (overwrite.deny ^ payload.deny)
    if changed_bits & ~actor_permissions:
        raise HTTPException(status_code=403, detail={"code": "CANNOT_MANAGE_PERMISSIONS"})
    if overwrite is not None:
        if empty_overwrite:
            await session.delete(overwrite)
        else:
            overwrite.allow = payload.allow
            overwrite.deny = payload.deny
    guild.permission_generation += 1
    e2ee_policy_channels: list[Channel] = []
    event_type = "guild.overwrite.delete" if empty_overwrite else "guild.overwrite.upsert"
    overwrite_payload: dict[str, object] = {
        "channel": {"id": str(channel.id), "origin_domain": channel.origin_domain},
        "target": {"id": str(target_id), "origin_domain": target_domain},
        "target_type": payload.target_type,
    }
    if not empty_overwrite:
        overwrite_payload.update(
            {
                "allow": str(payload.allow),
                "deny": str(payload.deny),
            }
        )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        event_type,
        {"overwrite": overwrite_payload},
        channel=channel,
        snapshot_required=True,
        e2ee_policy_channels=e2ee_policy_channels,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        15,
        target_type="channel",
        target_ref={"id": str(channel.id)},
        reason=reason,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_e2ee_policy_updates(
        session,
        redis,
        settings,
        [
            item
            for item in e2ee_policy_channels
            if (item.id, item.origin_domain) != (channel.id, channel.origin_domain)
        ],
    )
    await publish_dispatch(
        redis,
        guild_topic(settings.domain, guild.id),
        "CHANNEL_UPDATE",
        channel_payload(channel),
    )
    return {"status": "updated"}


@router.get("/guilds/{guild_id}/channels/{channel_id}/overwrites")
async def list_overwrites(
    guild_id: EntityRef,
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, str]]:
    guild = await local_guild(session, settings, guild_id)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    if channel.type in {10, 11, 12}:
        raise HTTPException(status_code=400, detail={"code": "THREAD_OVERWRITES_UNSUPPORTED"})
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("channel.overwrite.list"),
        channel=channel,
    )
    source = await overwrite_source_channel(session, channel)
    overwrites = await session.scalars(
        select(ChannelOverwrite)
        .where(
            ChannelOverwrite.channel_id == source.id,
            ChannelOverwrite.channel_domain == source.origin_domain,
        )
        .order_by(
            ChannelOverwrite.target_type,
            ChannelOverwrite.target_domain,
            ChannelOverwrite.target_id,
        )
    )
    return [
        {
            "target_id": str(overwrite.target_id),
            "target_domain": overwrite.target_domain,
            "target_type": overwrite.target_type,
            "allow": str(overwrite.allow),
            "deny": str(overwrite.deny),
        }
        for overwrite in overwrites
    ]


@router.delete(
    "/guilds/{guild_id}/channels/{channel_id}/overwrites/{target_type}/{target_id}",
    status_code=204,
)
async def delete_overwrite(
    guild_id: EntityRef,
    channel_id: EntityRef,
    target_type: str,
    target_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> Response:
    if target_type not in {"role", "member"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_OVERWRITE_TARGET"})
    guild = await local_guild(session, settings, guild_id, for_update=True)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    actor_permissions = await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("channel.overwrite.delete"),
        channel=channel,
    )
    target_number, target_domain = target_id.resolve(settings.domain)
    await validate_overwrite_target(
        session, guild, auth.user, target_type, target_number, target_domain
    )
    source = await overwrite_source_channel(session, channel)
    if source is not channel:
        await copy_overwrites(session, source, channel)
        channel.permissions_synced = False
    overwrite = await session.get(
        ChannelOverwrite,
        (channel.id, channel.origin_domain, target_number, target_domain, target_type),
    )
    if overwrite is not None:
        if (overwrite.allow | overwrite.deny) & ~actor_permissions:
            raise HTTPException(status_code=403, detail={"code": "CANNOT_MANAGE_PERMISSIONS"})
        await session.delete(overwrite)
    guild.permission_generation += 1
    e2ee_policy_channels: list[Channel] = []
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.overwrite.delete",
        {
            "overwrite": {
                "channel": {"id": str(channel.id), "origin_domain": channel.origin_domain},
                "target": {"id": str(target_number), "origin_domain": target_domain},
                "target_type": target_type,
            }
        },
        channel=channel,
        snapshot_required=True,
        e2ee_policy_channels=e2ee_policy_channels,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        16,
        target_type="channel",
        target_ref={"id": str(channel.id)},
        reason=reason,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_e2ee_policy_updates(
        session,
        redis,
        settings,
        [
            item
            for item in e2ee_policy_channels
            if (item.id, item.origin_domain) != (channel.id, channel.origin_domain)
        ],
    )
    await publish_dispatch(
        redis, guild_topic(settings.domain, guild.id), "CHANNEL_UPDATE", channel_payload(channel)
    )
    return Response(status_code=204)


@router.post("/guilds/{guild_id}/channels/{channel_id}/permissions/sync")
async def sync_channel_permissions(
    guild_id: EntityRef,
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("channel.permissions.sync"),
        channel=channel,
    )
    if channel.parent_id is None or channel.type == 4:
        raise HTTPException(status_code=400, detail={"code": "CHANNEL_HAS_NO_CATEGORY"})
    await session.execute(
        delete(ChannelOverwrite).where(
            ChannelOverwrite.channel_id == channel.id,
            ChannelOverwrite.channel_domain == channel.origin_domain,
        )
    )
    channel.permissions_synced = True
    guild.permission_generation += 1
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.channel.update",
        {"channel": federation_channel_state(channel)},
        channel=channel,
        snapshot_required=True,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        17,
        target_type="channel",
        target_ref={"id": str(channel.id)},
        reason=reason,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    result = await channel_payload_for(session, redis, guild, auth.user, channel)
    await publish_dispatch(redis, guild_topic(settings.domain, guild.id), "CHANNEL_UPDATE", result)
    return result
