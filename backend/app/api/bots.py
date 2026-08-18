from __future__ import annotations

import json
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channels import (
    add_reaction,
    bulk_delete_messages,
    create_message,
    delete_message,
    edit_message,
    list_messages,
    list_pins,
    list_reaction_users,
    pin_message,
    remove_own_reaction,
    remove_user_reaction,
    typing,
    unpin_message,
)
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, get_snowflake
from app.api.dms import open_direct_message
from app.api.guilds import create_channel, create_role
from app.api.invites import create_invite, list_invites, revoke_invite
from app.api.management import (
    assign_role,
    remove_role,
    reorder_channels,
    reorder_roles,
    replace_member_roles,
    update_guild,
)
from app.api.management import (
    delete_channel as delete_guild_channel,
)
from app.api.management import (
    delete_role as delete_guild_role,
)
from app.api.management import (
    update_channel as update_guild_channel,
)
from app.api.management import (
    update_role as update_guild_role,
)
from app.api.media import (
    authorized_attachment,
    create_emoji,
    delete_emoji,
    require_image_type,
    ticket_payload,
)
from app.api.moderation import (
    ban_member,
    kick_member,
    list_bans,
    list_members,
    remove_ban,
    update_member,
)
from app.api.voice import (
    channel_voice_occupancy,
    disconnect_member_voice,
    move_member_voice,
    update_member_voice_moderation,
)
from app.api.webhooks import (
    WebhookCreate,
    WebhookPatch,
    create_webhook,
    delete_webhook,
    list_webhooks,
    patch_webhook,
    rotate_webhook,
)
from app.bots.auth import BotPrincipal, require_bot
from app.bots.installations import installation_has_membership
from app.chat.payloads import (
    channel_payload,
    emoji_payload,
    guild_payload,
    role_payload,
    user_payload,
)
from app.chat.permissions import get_permissions
from app.chat.schemas import (
    BanCreate,
    ChannelCreate,
    ChannelPositionBatch,
    ChannelUpdate,
    DMOpenRequest,
    GuildUpdate,
    InviteCreate,
    MemberRoleSet,
    MemberUpdate,
    MessageBulkDelete,
    MessageCreate,
    MessageEdit,
    ReactionCreate,
    RoleCreate,
    RolePositionBatch,
    RoleUpdate,
)
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation
from app.db.models import (
    Attachment,
    Channel,
    Emoji,
    Guild,
    GuildMember,
    Invite,
    Message,
    Role,
    User,
    Webhook,
)
from app.media.schemas import EmojiCommitRequest, UploadTicketRequest
from app.media.service import attachment_payload, create_upload_ticket
from app.voice.schemas import VoiceModerationUpdate, VoiceMoveRequest

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
            installation_has_membership(),
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


async def installation_for_guild(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    guild_ref: EntityRef,
    scope: str,
) -> tuple[Guild, BotInstallation]:
    principal.require_scope(scope)
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            BotInstallation.status == "active",
            installation_has_membership(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if scope not in installation.granted_scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )
    return guild, installation


def require_installation_scope(
    principal: BotPrincipal,
    installation: BotInstallation,
    scope: str,
) -> None:
    principal.require_scope(scope)
    if scope not in installation.granted_scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )


async def exact_installation_by_id(
    session: AsyncSession,
    principal: BotPrincipal,
    installation_id: int | None,
    *scopes: str,
) -> BotInstallation:
    """Resolve the caller-selected, active installation for non-guild actions."""

    if installation_id is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_INSTALLATION_REQUIRED"})
    for scope in scopes:
        principal.require_scope(scope)
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.id == installation_id,
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            BotInstallation.status == "active",
            installation_has_membership(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    for scope in scopes:
        require_installation_scope(principal, installation, scope)
    return installation


async def installation_attachment(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    attachment_ref: EntityRef,
    scope: str,
    *,
    require_bound_message: bool,
) -> tuple[Attachment, BotInstallation]:
    """Resolve an attachment without crossing installation or target boundaries.

    An installation may inspect its own unbound upload tickets. Once an
    attachment is bound to a message, ``attachments.read`` follows ordinary
    channel access and can therefore read human-authored media too. The
    durable upload owner is still checked when present, preventing one guild
    installation from laundering its quota into another installation.
    """

    principal.require_scope(scope)
    attachment_id, attachment_domain = attachment_ref.resolve(settings.domain)
    attachment = await session.get(Attachment, (attachment_id, attachment_domain))
    if attachment is None or attachment.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    owning_installation: BotInstallation | None = None
    if attachment.bot_installation_id is not None:
        owning_installation = await session.scalar(
            select(BotInstallation).where(
                BotInstallation.id == attachment.bot_installation_id,
                BotInstallation.application_id == principal.application.id,
                BotInstallation.application_domain == principal.application.origin_domain,
                BotInstallation.bot_user_id == principal.user.id,
                BotInstallation.bot_user_domain == principal.user.origin_domain,
                BotInstallation.status == "active",
                installation_has_membership(),
            )
        )
        if owning_installation is None:
            raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})

    if attachment.message_id is not None and attachment.message_domain is not None:
        message = await session.get(Message, (attachment.message_id, attachment.message_domain))
        if message is None or message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        channel_ref = EntityRef(f"{message.channel_id}@{message.channel_domain}")
        channel, channel_installation = await installation_for_channel(
            session, settings, principal, channel_ref, scope
        )
        if channel.guild_id is None or channel_installation is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        if owning_installation is not None and owning_installation.id != channel_installation.id:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        return attachment, channel_installation

    if require_bound_message or owning_installation is None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    require_installation_scope(principal, owning_installation, scope)
    return attachment, owning_installation


async def require_owned_attachments_for_installation(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    installation: BotInstallation,
    attachment_ids: list[int],
) -> None:
    if not attachment_ids:
        return
    require_installation_scope(principal, installation, "attachments.write")
    rows = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.id.in_(attachment_ids),
                Attachment.origin_domain == settings.domain,
                Attachment.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    if len(rows) != len(set(attachment_ids)) or any(
        attachment.bot_installation_id != installation.id
        or (attachment.uploader_id, attachment.uploader_domain)
        != (principal.user.id, principal.user.origin_domain)
        for attachment in rows
    ):
        # A single indistinguishable error avoids turning attachment IDs into
        # an ownership or cross-installation oracle.
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})


def visible_presence(raw: object) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode(errors="ignore")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("status") in {"online", "idle", "dnd"}:
            return str(value["status"])
    return "offline"


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
                installation_has_membership(),
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


@router.get("/guilds/{guild_ref}")
async def bot_get_guild(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "guilds.read"
    )
    return guild_payload(guild) | {
        "installation_id": str(installation.id),
        "granted_scopes": installation.granted_scopes,
        "granted_intents": installation.granted_intents,
        "capability_revision": str(installation.grant_revision),
        "e2ee_mode": installation.e2ee_mode,
    }


@router.get("/guilds/{guild_ref}/channels")
async def bot_guild_channels(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "channels.read"
    )
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


@router.get("/channels/{channel_ref}")
async def bot_get_channel(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    channel, _ = await installation_for_channel(
        session, settings, principal, channel_ref, "channels.read"
    )
    permissions = Permission(0)
    if channel.guild_id is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        permissions = Permission(
            await get_permissions(session, redis, guild, principal.user, channel=channel)
        )
        if not permissions & Permission.VIEW_CHANNEL:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    return channel_payload(channel) | {"permissions": str(int(permissions))}


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
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "members.read"
    )
    members = await list_members(
        guild_ref, limit, after, query, user_auth(principal), session, redis, settings
    )
    can_read_presence = (
        "guild_presences" in principal.intents and "guild_presences" in installation.granted_intents
    )
    if can_read_presence and members:
        keys: list[str] = []
        for member in members:
            raw_user = member.get("user")
            if not isinstance(raw_user, dict):
                keys.append("")
                continue
            keys.append(f"presence:{raw_user.get('origin_domain', '')}:{raw_user.get('id', '')}")
        async with redis.pipeline(transaction=False) as pipeline:
            for key in keys:
                if key:
                    pipeline.get(key)
                else:
                    pipeline.get("presence:invalid")
            raw_presences = list(await pipeline.execute())
        for member, raw_presence in zip(members, raw_presences, strict=True):
            member["presence"] = visible_presence(raw_presence)
    return members


@router.get("/guilds/{guild_ref}/roles")
async def bot_guild_roles(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _ = await installation_for_guild(session, settings, principal, guild_ref, "roles.read")
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


@router.patch("/guilds/{guild_ref}")
async def bot_update_guild(
    guild_ref: EntityRef,
    payload: GuildUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "guilds.manage")
    return await update_guild(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        if_match,
    )


@router.post("/guilds/{guild_ref}/channels", status_code=201)
async def bot_create_channel(
    guild_ref: EntityRef,
    payload: ChannelCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    return await create_channel(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.patch("/guilds/{guild_ref}/channels/{channel_ref}")
async def bot_update_channel(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    payload: ChannelUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    return await update_guild_channel(
        guild_ref,
        channel_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        if_match,
    )


@router.patch("/guilds/{guild_ref}/channels")
async def bot_reorder_channels(
    guild_ref: EntityRef,
    payload: ChannelPositionBatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    return await reorder_channels(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.delete("/guilds/{guild_ref}/channels/{channel_ref}", status_code=204)
async def bot_delete_channel(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    return await delete_guild_channel(
        guild_ref,
        channel_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.post("/guilds/{guild_ref}/roles", status_code=201)
async def bot_create_role(
    guild_ref: EntityRef,
    payload: RoleCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await create_role(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.patch("/guilds/{guild_ref}/roles/{role_ref}")
async def bot_update_role(
    guild_ref: EntityRef,
    role_ref: EntityRef,
    payload: RoleUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await update_guild_role(
        guild_ref,
        role_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        if_match,
    )


@router.patch("/guilds/{guild_ref}/roles")
async def bot_reorder_roles(
    guild_ref: EntityRef,
    payload: RolePositionBatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await reorder_roles(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.delete("/guilds/{guild_ref}/roles/{role_ref}", status_code=204)
async def bot_delete_role(
    guild_ref: EntityRef,
    role_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await delete_guild_role(
        guild_ref,
        role_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.put("/guilds/{guild_ref}/members/{user_ref}/roles/{role_ref}", status_code=204)
async def bot_assign_role(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    role_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await assign_role(
        guild_ref,
        user_ref,
        role_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.put("/guilds/{guild_ref}/members/{user_ref}/roles")
async def bot_replace_member_roles(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: MemberRoleSet,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await replace_member_roles(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.delete("/guilds/{guild_ref}/members/{user_ref}/roles/{role_ref}", status_code=204)
async def bot_remove_role(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    role_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await remove_role(
        guild_ref,
        user_ref,
        role_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.post("/channels/{channel_ref}/attachments", status_code=201)
async def bot_create_attachment_ticket(
    channel_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "attachments.write"
    )
    if installation is None:
        # Direct-message attachments need an application-level storage owner,
        # which is deliberately outside the guild-installation quota model.
        raise HTTPException(status_code=409, detail={"code": "BOT_DM_ATTACHMENTS_UNAVAILABLE"})
    guild = await session.get(Guild, (installation.guild_id, installation.guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, principal.user, channel=channel)
    if not permissions & Permission.ATTACH_FILES:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    expected_mode = "e2ee" if channel.encryption_mode == "e2ee" else "plaintext"
    if channel.encryption_mode == "e2ee" and installation.e2ee_mode != "participant":
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_DISABLED"})
    if expected_mode == "e2ee" and channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    if payload.encryption_mode != expected_mode:
        raise HTTPException(
            status_code=409,
            detail={
                "code": (
                    "E2EE_ATTACHMENT_REQUIRED" if expected_mode == "e2ee" else "E2EE_NOT_ENABLED"
                )
            },
        )
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=principal.user.id,
        user_domain=principal.user.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        principal.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        encryption_mode=payload.encryption_mode,
        encryption_protocol=payload.encryption_protocol,
        bot_installation=installation,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.get("/attachments/{attachment_ref}")
async def bot_attachment_status(
    attachment_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    attachment, installation = await installation_attachment(
        session,
        settings,
        principal,
        attachment_ref,
        "attachments.read",
        require_bound_message=False,
    )
    if attachment.message_id is not None and attachment.message_domain is not None:
        message = await session.get(Message, (attachment.message_id, attachment.message_domain))
        if message is None or message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if channel is None or channel.guild_id is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        permissions = await get_permissions(session, redis, guild, principal.user, channel=channel)
        if permissions & (Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY) != (
            Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
        ):
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    return attachment_payload(attachment) | {"installation_id": str(installation.id)}


@router.get("/attachments/{attachment_ref}/{variant}")
async def bot_download_attachment(
    attachment_ref: EntityRef,
    variant: str,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    if variant not in {"original", "thumbnail_128", "thumbnail_512", "thumbnail_1024", "poster"}:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    attachment, installation = await installation_attachment(
        session,
        settings,
        principal,
        attachment_ref,
        "attachments.read",
        require_bound_message=True,
    )
    if attachment.message_id is None or attachment.message_domain is None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    message = await session.get(Message, (attachment.message_id, attachment.message_domain))
    if message is None or message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None or (channel.guild_id, channel.guild_domain) != (
        installation.guild_id,
        installation.guild_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    _, channel_installation = await installation_for_channel(
        session,
        settings,
        principal,
        EntityRef(f"{channel.id}@{channel.origin_domain}"),
        "attachments.read",
    )
    if channel_installation is None or channel_installation.id != installation.id:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    return await authorized_attachment(
        attachment.origin_domain,
        attachment.id,
        response,
        variant,
        user_auth(principal),
        session,
        redis,
        settings,
        snowflake,
    )


@router.post("/dms", status_code=201)
async def bot_open_direct_message(
    payload: DMOpenRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> dict[str, object]:
    installation = await exact_installation_by_id(session, principal, installation_id, "dm.send")
    result = await open_direct_message(
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )
    result["bot_installation_id"] = str(installation.id)
    return result


@router.post("/guilds/{guild_ref}/invites", status_code=201)
async def bot_create_invite(
    guild_ref: EntityRef,
    payload: InviteCreate,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "invites.manage")
    return await create_invite(
        guild_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.get("/guilds/{guild_ref}/invites")
async def bot_list_invites(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    await installation_for_guild(session, settings, principal, guild_ref, "invites.manage")
    return await list_invites(guild_ref, user_auth(principal), session, redis, settings)


@router.delete("/guilds/{guild_ref}/invites/{code}", status_code=204)
async def bot_revoke_invite(
    guild_ref: EntityRef,
    code: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "invites.manage"
    )
    invite = await session.get(Invite, code)
    if invite is None or (invite.guild_id, invite.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    return await revoke_invite(
        code,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.post("/guilds/{guild_ref}/channels/{channel_ref}/webhooks", status_code=201)
async def bot_create_webhook(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    payload: WebhookCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "webhooks.manage")
    return await create_webhook(
        guild_ref,
        channel_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.get("/guilds/{guild_ref}/webhooks")
async def bot_list_webhooks(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    await installation_for_guild(session, settings, principal, guild_ref, "webhooks.manage")
    return await list_webhooks(guild_ref, user_auth(principal), session, redis, settings)


async def bot_guild_webhook(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    guild_ref: EntityRef,
    webhook_id: int,
) -> None:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "webhooks.manage"
    )
    webhook = await session.get(Webhook, webhook_id)
    if webhook is None or (webhook.guild_id, webhook.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})


@router.patch("/guilds/{guild_ref}/webhooks/{webhook_id}")
async def bot_update_webhook(
    guild_ref: EntityRef,
    webhook_id: int,
    payload: WebhookPatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await bot_guild_webhook(session, settings, principal, guild_ref, webhook_id)
    return await patch_webhook(webhook_id, payload, user_auth(principal), session, redis, settings)


@router.post("/guilds/{guild_ref}/webhooks/{webhook_id}/rotate")
async def bot_rotate_webhook(
    guild_ref: EntityRef,
    webhook_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await bot_guild_webhook(session, settings, principal, guild_ref, webhook_id)
    return await rotate_webhook(webhook_id, user_auth(principal), session, redis, settings)


@router.delete("/guilds/{guild_ref}/webhooks/{webhook_id}", status_code=204)
async def bot_delete_webhook(
    guild_ref: EntityRef,
    webhook_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await bot_guild_webhook(session, settings, principal, guild_ref, webhook_id)
    return await delete_webhook(webhook_id, user_auth(principal), session, redis, settings)


@router.get("/guilds/{guild_ref}/emojis")
async def bot_list_emojis(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "emojis.manage"
    )
    rows = await session.scalars(
        select(Emoji)
        .where(Emoji.guild_id == guild.id, Emoji.guild_domain == guild.origin_domain)
        .order_by(Emoji.name, Emoji.id)
    )
    return [emoji_payload(emoji) for emoji in rows]


@router.post("/guilds/{guild_ref}/emojis/tickets", status_code=201)
async def bot_create_emoji_ticket(
    guild_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "emojis.manage"
    )
    require_installation_scope(principal, installation, "attachments.write")
    require_image_type(payload.content_type)
    if payload.encryption_mode != "plaintext":
        raise HTTPException(status_code=409, detail={"code": "E2EE_NOT_ENABLED"})
    if payload.size > settings.media_max_emoji_bytes:
        raise HTTPException(
            status_code=413,
            detail={"code": "EMOJI_TOO_LARGE", "max_bytes": settings.media_max_emoji_bytes},
        )
    permissions = await get_permissions(session, redis, guild, principal.user)
    if not permissions & Permission.MANAGE_EMOJIS:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=principal.user.id,
        user_domain=principal.user.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        principal.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="emoji",
        bot_installation=installation,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.post("/guilds/{guild_ref}/emojis", status_code=201)
async def bot_create_emoji(
    guild_ref: EntityRef,
    payload: EmojiCommitRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "emojis.manage"
    )
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        installation,
        [int(payload.attachment_id)],
    )
    return await create_emoji(
        guild_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.delete("/guilds/{guild_ref}/emojis/{emoji_id}", status_code=204)
async def bot_delete_emoji(
    guild_ref: EntityRef,
    emoji_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "emojis.manage")
    return await delete_emoji(guild_ref, emoji_id, user_auth(principal), session, redis, settings)


@router.patch("/guilds/{guild_ref}/members/{user_ref}/voice", status_code=204)
async def bot_update_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: VoiceModerationUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "voice.moderate")
    return await update_member_voice_moderation(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/members/{user_ref}/voice", status_code=204)
async def bot_disconnect_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "voice.moderate")
    return await disconnect_member_voice(
        guild_ref,
        user_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/members/{user_ref}/voice/move", status_code=204)
async def bot_move_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: VoiceMoveRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "voice.moderate")
    return await move_member_voice(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


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
    can_read_content = (
        "messages.content" in principal.scopes
        and installation is not None
        and "messages.content" in installation.granted_scopes
    )
    can_read_attachments = (
        "attachments.read" in principal.scopes
        and installation is not None
        and "attachments.read" in installation.granted_scopes
    )
    if not can_read_content:
        for message in messages:
            message["content"] = None
            message["content_unavailable"] = True
    if not can_read_attachments:
        for message in messages:
            message["attachments"] = []
            message["attachments_unavailable"] = True
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
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "messages.send"
    )
    if installation is None:
        await exact_installation_by_id(
            session,
            principal,
            installation_id,
            "messages.send",
            "dm.send",
        )
    if channel.encryption_mode == "e2ee" and (
        installation is None or installation.e2ee_mode != "participant" or payload.e2ee is None
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_ENVELOPE_REQUIRED"},
        )
    if payload.attachment_ids:
        if installation is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_DM_ATTACHMENTS_UNAVAILABLE"},
            )
        await require_owned_attachments_for_installation(
            session,
            settings,
            principal,
            installation,
            [int(item) for item in payload.attachment_ids],
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
    message_id, message_domain = message_ref.resolve(settings.domain)
    message = await session.get(Message, (message_id, message_domain))
    if message is None or (message.channel_id, message.channel_domain) != channel_ref.resolve(
        settings.domain
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    scope = (
        "messages.delete.own"
        if (message.author_id, message.author_domain)
        == (principal.user.id, principal.user.origin_domain)
        else "moderation.messages"
    )
    await installation_for_channel(session, settings, principal, channel_ref, scope)
    return await delete_message(
        channel_ref, message_ref, user_auth(principal), session, redis, settings
    )


@router.post("/channels/{channel_ref}/messages/bulk-delete", status_code=204)
async def bot_bulk_delete_messages(
    channel_ref: EntityRef,
    payload: MessageBulkDelete,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, channel_ref, "moderation.messages")
    return await bulk_delete_messages(
        channel_ref, payload, user_auth(principal), session, redis, settings
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


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}/{user_ref}",
    status_code=204,
)
async def bot_remove_user_reaction(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    user_ref: EntityRef,
    emoji: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, channel_ref, "messages.manage")
    return await remove_user_reaction(
        channel_ref,
        message_ref,
        user_ref,
        emoji,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.get("/channels/{channel_ref}/pins")
async def bot_list_pins(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    await installation_for_channel(session, settings, principal, channel_ref, "messages.history")
    return await list_pins(channel_ref, user_auth(principal), session, redis, settings)


@router.put("/channels/{channel_ref}/pins/{message_ref}", status_code=204)
async def bot_pin_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, channel_ref, "messages.manage")
    return await pin_message(
        channel_ref, message_ref, user_auth(principal), session, redis, settings
    )


@router.delete("/channels/{channel_ref}/pins/{message_ref}", status_code=204)
async def bot_unpin_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, channel_ref, "messages.manage")
    return await unpin_message(
        channel_ref, message_ref, user_auth(principal), session, redis, settings
    )


@router.post("/channels/{channel_ref}/typing", status_code=204)
async def bot_typing(
    channel_ref: EntityRef,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    _, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "messages.send"
    )
    if installation is None:
        await exact_installation_by_id(
            session,
            principal,
            installation_id,
            "messages.send",
            "dm.send",
        )
    return await typing(channel_ref, response, user_auth(principal), session, redis, settings)


@router.get("/channels/{channel_ref}/voice/occupancy")
async def bot_voice_occupancy(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_channel(session, settings, principal, channel_ref, "voice.states.read")
    return await channel_voice_occupancy(
        channel_ref, user_auth(principal), session, redis, settings
    )


@router.patch("/guilds/{guild_ref}/members/{user_ref}")
async def bot_update_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: MemberUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.members")
    return await update_member(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/members/{user_ref}", status_code=204)
async def bot_kick_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.members")
    return await kick_member(
        guild_ref,
        user_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/bans")
async def bot_list_bans(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=50, ge=1, le=1000),
    after: EntityRef | None = None,
) -> list[dict[str, object]]:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.members")
    return await list_bans(
        guild_ref,
        limit,
        after,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put("/guilds/{guild_ref}/bans/{user_ref}", status_code=204)
async def bot_ban_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: BanCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.members")
    return await ban_member(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/bans/{user_ref}", status_code=204)
async def bot_unban_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.members")
    return await remove_ban(
        guild_ref,
        user_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
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
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            BotInstallation.status == "active",
            installation_has_membership(),
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
