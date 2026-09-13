from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from redis.asyncio import Redis
from sqlalchemy import and_, exists, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.auth.schemas import (
    GuildNavigationGuildItem,
    GuildNavigationUpdate,
    SettingsPatch,
)
from app.automod.service import evaluate_member_profile
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.payloads import user_payload
from app.chat.permissions import get_permissions
from app.chat.presence import broadcast_presence_preference
from app.chat.privacy import lock_dm_privacy
from app.chat.schemas import ProfilePatch, ReadStateUpdate
from app.core.guild_navigation import normalize_guild_navigation, parse_stored_guild_navigation
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference, validate_entity_reference
from app.db.models import (
    Channel,
    DMParticipant,
    Guild,
    GuildMember,
    GuildNotificationSetting,
    InboxDismissal,
    Message,
    MessageProjection,
    ReadState,
    User,
    UserSettings,
)
from app.federation.relationships import queue_profile_updates
from app.federation.users import resolve_handle
from app.tasks import federation_deliver, federation_presence_fanout

router = APIRouter(prefix="/api/v1/users", tags=["users"])
log = structlog.get_logger()


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
        "age_assurance_state": getattr(user, "age_assurance_state", "unknown"),
    }


@router.patch("/@me")
async def patch_me(
    payload: ProfilePatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
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
        profile_rule_guild_refs: list[tuple[int, str]] = []
        if "display_name" in values:
            profile_rule_guild_refs = list(
                (
                    await session.execute(
                        select(Guild.id, Guild.origin_domain)
                        .join(
                            GuildMember,
                            (GuildMember.guild_id == Guild.id)
                            & (GuildMember.guild_domain == Guild.origin_domain),
                        )
                        .where(
                            Guild.origin_domain == settings.domain,
                            GuildMember.user_id == user.id,
                            GuildMember.user_domain == user.origin_domain,
                        )
                        .order_by(Guild.id)
                    )
                ).tuples()
            )
        for field, value in values.items():
            setattr(user, field, value)
        user.profile_version += 1
        destinations = await queue_profile_updates(session, settings, user)
        await session.commit()
        await publish_dispatch(
            redis,
            user_topic(user.origin_domain, user.id),
            "USER_UPDATE",
            user_payload(user),
        )
        for destination in destinations:
            await enqueue_best_effort(federation_deliver, destination)
        # Evaluate each authoritative guild in its own short guild-locked
        # transaction. Holding every guild row while changing a global profile
        # would create a large lock fan-out for well-connected accounts.
        for guild_id, guild_domain in profile_rule_guild_refs:
            guild = await session.scalar(
                select(Guild)
                .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
                .with_for_update()
            )
            if guild is None:
                await session.rollback()
                continue
            current_user = await session.get(
                User,
                (user.id, user.origin_domain),
                populate_existing=True,
            )
            if current_user is None:
                await session.rollback()
                continue
            automod_post_commit = await evaluate_member_profile(
                session,
                settings,
                snowflake,
                guild,
                current_user,
            )
            await session.commit()
            await automod_post_commit.publish(redis)
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
        "share_locale_with_bots": settings.share_locale_with_bots,
        "age_restricted_dm_commands_enabled": bool(
            getattr(settings, "age_restricted_dm_commands_enabled", False)
        ),
        "presence_preference": presence_preference,
        "notification_settings": notification_settings,
    }


async def accessible_guild_navigation_refs(
    session: AsyncSession, auth: AuthenticatedUser
) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(Guild.id, Guild.origin_domain)
            .join(
                GuildMember,
                (GuildMember.guild_id == Guild.id)
                & (GuildMember.guild_domain == Guild.origin_domain),
            )
            .where(
                GuildMember.user_id == auth.user.id,
                GuildMember.user_domain == auth.user.origin_domain,
            )
            .order_by(func.lower(Guild.name), Guild.id, Guild.origin_domain)
        )
    ).all()
    return [(guild_id, guild_domain) for guild_id, guild_domain in rows]


@router.get("/@me/guild-navigation")
async def get_guild_navigation(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    account_settings = await load_settings(session, auth)
    accessible = await accessible_guild_navigation_refs(session, auth)
    return normalize_guild_navigation(
        parse_stored_guild_navigation(account_settings.guild_navigation),
        accessible,
        settings.domain,
    )


@router.put("/@me/guild-navigation")
async def put_guild_navigation(
    payload: GuildNavigationUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    account_settings = await session.scalar(
        select(UserSettings)
        .where(
            UserSettings.user_id == auth.user.id,
            UserSettings.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if account_settings is None:
        raise HTTPException(status_code=500, detail={"code": "SETTINGS_MISSING"})
    accessible = await accessible_guild_navigation_refs(session, auth)
    accessible_set = {f"{guild_id}@{domain}" for guild_id, domain in accessible}
    requested: list[str] = []
    for item in payload.items:
        refs = (item.guild,) if isinstance(item, GuildNavigationGuildItem) else item.guilds
        for raw in refs:
            guild_id, domain = raw.resolve(settings.domain)
            requested.append(f"{guild_id}@{domain}")
    if len(requested) != len(set(requested)):
        raise HTTPException(status_code=422, detail={"code": "GUILD_NAVIGATION_DUPLICATE_GUILD"})
    if not set(requested).issubset(accessible_set):
        raise HTTPException(status_code=409, detail={"code": "GUILD_NAVIGATION_GUILD_UNAVAILABLE"})
    rendered = normalize_guild_navigation(payload, accessible, settings.domain)
    account_settings.guild_navigation = rendered
    await session.commit()
    try:
        await publish_dispatch(
            redis,
            user_topic(auth.user.origin_domain, auth.user.id),
            "GUILD_NAVIGATION_UPDATE",
            rendered,
        )
    except Exception:
        # The durable account layout is already committed. Other clients can
        # recover it on their next navigation refresh even if realtime fanout
        # is temporarily unavailable.
        log.exception(
            "guild_navigation_dispatch_failed",
            user_id=str(auth.user.id),
            user_domain=auth.user.origin_domain,
        )
    return rendered


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
    redis: Redis = Depends(get_redis),
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
    presence_topics: list[str] = []
    if presence_preference is not None:
        settings.notification_settings = {
            **settings.notification_settings,
            "presence_preference": presence_preference,
        }
        guild_refs = (
            await session.execute(
                select(GuildMember.guild_id, GuildMember.guild_domain).where(
                    GuildMember.user_id == auth.user.id,
                    GuildMember.user_domain == auth.user.origin_domain,
                )
            )
        ).all()
        presence_topics = [
            user_topic(auth.user.origin_domain, auth.user.id),
            *(guild_topic(domain, guild_id) for guild_id, domain in guild_refs),
        ]
    await session.commit()
    if presence_preference is not None:
        visible_status, generation = await broadcast_presence_preference(
            redis,
            auth.user,
            presence_preference,
            presence_topics,
        )
        await enqueue_best_effort(
            federation_presence_fanout,
            auth.user.id,
            auth.user.origin_domain,
            visible_status,
            generation,
        )
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
    history_access: dict[tuple[int, str], bool] = {}
    visible_channels: list[Channel] = []
    for channel in channels:
        if channel.guild_id is None or channel.guild_domain is None:
            visible_channels.append(channel)
            history_access[(channel.id, channel.origin_domain)] = True
            continue
        guild = guild_by_ref.get((channel.guild_id, channel.guild_domain))
        if guild is None:
            continue
        permissions = await get_permissions(session, redis, guild, auth.user, channel=channel)
        if permissions & Permission.VIEW_CHANNEL:
            visible_channels.append(channel)
            history_access[(channel.id, channel.origin_domain)] = bool(
                permissions & Permission.READ_MESSAGE_HISTORY
            )
    channels = visible_channels
    muted_guilds = {
        (row.guild_id, row.guild_domain)
        for row in await session.scalars(
            select(GuildNotificationSetting).where(
                GuildNotificationSetting.user_id == auth.user.id,
                GuildNotificationSetting.user_domain == auth.user.origin_domain,
                GuildNotificationSetting.level == "none",
            )
        )
    }
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
    first_unread = {}
    if channel_refs:
        rows = await session.execute(
            select(Message.channel_id, Message.channel_domain, Message.id, Message.origin_domain)
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
            .distinct(Message.channel_id, Message.channel_domain)
            .order_by(Message.channel_id, Message.channel_domain, Message.id, Message.origin_domain)
        )
        first_unread = {(row[0], row[1]): (str(row[2]), row[3]) for row in rows}
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
                "channel_name": channel.name,
                "can_read_history": history_access.get((channel.id, channel.origin_domain), False),
                "muted": (channel.guild_id, channel.guild_domain) in muted_guilds,
                "guild_id": str(channel.guild_id) if channel.guild_id is not None else None,
                "guild_domain": channel.guild_domain,
                "last_message_id": str(latest.id) if latest is not None else None,
                "last_message_domain": latest.domain if latest is not None else None,
                "first_unread_message_id": first_unread.get(
                    (channel.id, channel.origin_domain), (None, None)
                )[0],
                "first_unread_message_domain": first_unread.get(
                    (channel.id, channel.origin_domain), (None, None)
                )[1],
                "read_message_id": str(read.id) if read is not None else None,
                "read_message_domain": read.domain if read is not None else None,
                "read_version": state.read_version if state is not None else 0,
                "mention_count": state.mention_count if state is not None else 0,
                "unread_count": unread_count,
                "unread": unread_count > 0,
            }
        )
    return result


@router.get("/@me/inbox/mentions")
async def inbox_mentions(
    before: EntityRef | None = None,
    guild: EntityRef | None = None,
    include_everyone: bool = True,
    include_roles: bool = True,
    limit: int = Query(default=50, ge=1, le=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    states = await list_read_states(auth, session, redis)
    visible = {}
    for state in states:
        if guild is not None and (state["guild_id"], state["guild_domain"]) != (
            str(guild.id),
            guild.domain,
        ):
            continue
        if not state.get("can_read_history"):
            continue
        reference = EntityReference(int(str(state["channel_id"])), str(state["channel_domain"]))
        visible[(reference.id, reference.domain)] = state
    if not visible:
        return []
    dismissed = exists().where(
        InboxDismissal.user_id == auth.user.id,
        InboxDismissal.user_domain == auth.user.origin_domain,
        InboxDismissal.message_id == Message.id,
        InboxDismissal.message_domain == Message.origin_domain,
    )
    statement = (
        select(Message)
        .join(
            MessageProjection,
            and_(
                MessageProjection.message_id == Message.id,
                MessageProjection.message_domain == Message.origin_domain,
            ),
        )
        .where(
            tuple_(Message.channel_id, Message.channel_domain).in_(visible),
            MessageProjection.mention_user_refs.contains(
                [{"id": str(auth.user.id), "origin_domain": auth.user.origin_domain}]
            ),
            Message.created_at >= datetime.now(UTC) - timedelta(days=7),
            Message.deleted_at.is_(None),
            tuple_(Message.author_id, Message.author_domain)
            != (auth.user.id, auth.user.origin_domain),
            ~dismissed,
        )
    )
    if before is not None:
        statement = statement.where(
            tuple_(Message.id, Message.origin_domain) < (before.id, before.domain)
        )
    if not include_everyone:
        statement = statement.where(Message.mention_everyone.is_(False))
    if not include_roles:
        statement = statement.where(Message.mention_role_refs == [])
    messages = await session.scalars(
        statement.order_by(Message.id.desc(), Message.origin_domain.desc()).limit(limit)
    )
    return [
        {
            **visible[(message.channel_id, message.channel_domain)],
            "message_id": str(message.id),
            "message_domain": message.origin_domain,
            "created_at": message.created_at.isoformat(),
        }
        for message in messages
    ]


@router.post("/@me/inbox/dismiss", status_code=204)
async def dismiss_inbox_mention(
    payload: ReadStateUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    # Only a message that actually mentions this account can create a dismissal.
    message = await session.scalar(
        select(MessageProjection).where(
            MessageProjection.message_id == payload.message_id.id,
            MessageProjection.message_domain == payload.message_id.domain,
            MessageProjection.mention_user_refs.contains(
                [{"id": str(auth.user.id), "origin_domain": auth.user.origin_domain}]
            ),
        )
    )
    if message is None:
        raise HTTPException(status_code=404, detail="Mention not found")
    from app.chat.channel_access import load_channel_access

    access = await load_channel_access(
        session, settings, auth.user, EntityReference(message.channel_id, message.channel_domain)
    )
    if access.guild is not None:
        permissions = await get_permissions(
            session, redis, access.guild, auth.user, channel=access.channel
        )
        required = Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
        if permissions & required != required:
            raise HTTPException(status_code=404, detail="Mention not found")
    await session.execute(
        pg_insert(InboxDismissal)
        .values(
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            user_is_local=True,
            message_id=message.message_id,
            message_domain=message.message_domain,
        )
        .on_conflict_do_nothing()
    )
    await session.commit()
    await publish_dispatch(
        redis,
        user_topic(auth.user.origin_domain, auth.user.id),
        "INBOX_MENTION_DISMISS",
        {"message_id": str(message.message_id), "message_domain": message.message_domain},
    )
    return Response(status_code=204)
