from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channels import (
    add_reaction,
    create_message,
    delete_message,
    edit_message,
    list_messages,
    list_reaction_users,
    remove_own_reaction,
)
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, get_snowflake
from app.api.moderation import list_members
from app.bots.auth import BotPrincipal, require_bot
from app.chat.payloads import channel_payload, guild_payload, role_payload, user_payload
from app.chat.permissions import get_permissions
from app.chat.schemas import MessageCreate, MessageEdit, ReactionCreate
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation
from app.db.models import Channel, Guild, GuildMember, Role, User

router = APIRouter(prefix="/api/v1/bots", tags=["bot api"])


def user_auth(principal: BotPrincipal) -> AuthenticatedUser:
    # Existing message services only consume auth.user. Keeping the adapter
    # here preserves one permission and federation implementation.
    return cast(AuthenticatedUser, principal)


async def installation_for_channel(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    channel_ref: EntityRef,
    scope: str,
) -> tuple[Channel, BotInstallation | None]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    if channel is None or channel.unavailable:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.encryption_mode == "e2ee" and scope in {
        "messages.content",
        "messages.history",
    }:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_CONTENT_UNAVAILABLE"})
    if channel.guild_id is None:
        principal.require_scope(scope)
        if scope != "messages.send" or "dm.send" not in principal.scopes:
            raise HTTPException(status_code=403, detail={"code": "BOT_DM_SCOPE_REQUIRED"})
        return channel, None
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == channel.guild_id,
            BotInstallation.guild_domain == channel.guild_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            BotInstallation.status == "active",
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if scope not in installation.granted_scopes or scope not in principal.scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )
    return channel, installation


@router.get("/@me")
async def bot_identity(
    principal: Annotated[BotPrincipal, Depends(require_bot)],
) -> dict[str, object]:
    return {
        "user": user_payload(principal.user),
        "application_ref": (f"{principal.application.id}@{principal.application.origin_domain}"),
        "worker_id": str(principal.worker.id),
        "scopes": sorted(principal.scopes),
        "intents": sorted(principal.intents),
        "token_expires_at": principal.token.expires_at.isoformat(),
    }


@router.get("/guilds")
async def bot_guilds(
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    principal.require_scope("guilds.read")
    rows = (
        await session.execute(
            select(BotInstallation, Guild)
            .join(
                Guild,
                (Guild.id == BotInstallation.guild_id)
                & (Guild.origin_domain == BotInstallation.guild_domain),
            )
            .where(
                BotInstallation.application_id == principal.application.id,
                BotInstallation.application_domain == principal.application.origin_domain,
                BotInstallation.bot_user_id == principal.user.id,
                BotInstallation.bot_user_domain == principal.user.origin_domain,
                BotInstallation.status == "active",
            )
            .order_by(Guild.id)
        )
    ).all()
    return [
        guild_payload(guild)
        | {
            "installation_id": str(installation.id),
            "granted_scopes": installation.granted_scopes,
            "granted_intents": installation.granted_intents,
            "capability_revision": str(installation.grant_revision),
            "e2ee_mode": installation.e2ee_mode,
        }
        for installation, guild in rows
    ]


@router.get("/guilds/{guild_ref}/channels")
async def bot_guild_channels(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    principal.require_scope("channels.read")
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.status == "active",
        )
    )
    if installation is None or "channels.read" not in installation.granted_scopes:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.unavailable.is_(False),
            )
            .order_by(Channel.position, Channel.id)
        )
    )
    result: list[dict[str, object]] = []
    for channel in channels:
        channel_permissions = await get_permissions(
            session, redis, guild, principal.user, channel=channel
        )
        if channel_permissions & Permission.VIEW_CHANNEL:
            result.append(channel_payload(channel) | {"permissions": str(int(channel_permissions))})
    return result


@router.get("/guilds/{guild_ref}/members")
async def bot_guild_members(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=100, ge=1, le=1000),
    after: EntityRef | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=100),
) -> list[dict[str, object]]:
    principal.require_scope("members.read")
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == guild_id,
            BotInstallation.guild_domain == guild_domain,
            BotInstallation.status == "active",
            BotInstallation.granted_scopes.contains(["members.read"]),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    return await list_members(
        guild_ref, limit, after, query, user_auth(principal), session, redis, settings
    )


@router.get("/guilds/{guild_ref}/roles")
async def bot_guild_roles(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    principal.require_scope("roles.read")
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == guild_id,
            BotInstallation.guild_domain == guild_domain,
            BotInstallation.status == "active",
            BotInstallation.granted_scopes.contains(["roles.read"]),
        )
    )
    if guild is None or installation is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, principal.user)
    if not permissions & Permission.VIEW_CHANNEL:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    roles = list(
        await session.scalars(
            select(Role)
            .where(Role.guild_id == guild.id, Role.guild_domain == guild.origin_domain)
            .order_by(Role.position, Role.id)
        )
    )
    return [role_payload(role) for role in roles]


@router.get("/channels/{channel_ref}/messages")
async def bot_list_messages(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    before: EntityRef | None = None,
    after: EntityRef | None = None,
    around: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, object]]:
    _, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "messages.history"
    )
    messages = await list_messages(
        channel_ref,
        before,
        after,
        around,
        limit,
        user_auth(principal),
        session,
        redis,
        settings,
    )
    if (
        "messages.content" not in principal.scopes
        or installation is None
        or "messages.content" not in installation.granted_scopes
    ):
        for message in messages:
            message["content"] = None
            message["attachments"] = []
            message["content_unavailable"] = True
    return messages


@router.post("/channels/{channel_ref}/messages", status_code=201)
async def bot_create_message(
    channel_ref: EntityRef,
    payload: MessageCreate,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "messages.send"
    )
    if channel.encryption_mode == "e2ee" and (
        installation is None or installation.e2ee_mode != "participant" or payload.e2ee is None
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_ENVELOPE_REQUIRED"},
        )
    return await create_message(
        channel_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.patch("/channels/{channel_ref}/messages/{message_ref}")
async def bot_edit_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    payload: MessageEdit,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_channel(session, settings, principal, channel_ref, "messages.edit.own")
    return await edit_message(
        channel_ref, message_ref, payload, user_auth(principal), session, redis, settings
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}",
    status_code=204,
)
async def bot_delete_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, channel_ref, "messages.delete.own")
    return await delete_message(
        channel_ref, message_ref, user_auth(principal), session, redis, settings
    )


@router.post(
    "/channels/{channel_ref}/messages/{message_ref}/reactions",
    status_code=204,
)
async def bot_add_reaction(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    payload: ReactionCreate,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, channel_ref, "reactions.write")
    return await add_reaction(
        channel_ref,
        message_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}",
    status_code=204,
)
async def bot_remove_reaction(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    emoji: str,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, channel_ref, "reactions.write")
    return await remove_own_reaction(
        channel_ref,
        message_ref,
        response,
        emoji,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.get(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}",
)
async def bot_reaction_users(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    emoji: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    after: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    await installation_for_channel(session, settings, principal, channel_ref, "reactions.read")
    return await list_reaction_users(
        channel_ref,
        message_ref,
        emoji,
        after,
        limit,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.get("/users/{user_ref}")
async def bot_get_user(
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    principal.require_scope("members.read")
    user_id, user_domain = user_ref.resolve(settings.domain)
    user = await session.get(User, (user_id, user_domain))
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    shared_guild = await session.scalar(
        select(GuildMember.guild_id)
        .join(
            BotInstallation,
            (BotInstallation.guild_id == GuildMember.guild_id)
            & (BotInstallation.guild_domain == GuildMember.guild_domain),
        )
        .where(
            GuildMember.user_id == user.id,
            GuildMember.user_domain == user.origin_domain,
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.status == "active",
            BotInstallation.granted_scopes.contains(["members.read"]),
        )
        .limit(1)
    )
    if shared_guild is None and (user.id, user.origin_domain) != (
        principal.user.id,
        principal.user.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    payload = user_payload(user)
    payload["handle"] = f"{user.username}@{user.origin_domain}"
    return payload
