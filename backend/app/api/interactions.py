from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import (
    installation_for_channel,
    require_installation_scope,
    require_owned_attachments_for_installation,
    user_auth,
)
from app.api.channels import create_message
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.bots.auth import BotPrincipal, require_bot
from app.bots.installations import installation_has_membership
from app.chat.channel_access import load_channel_access
from app.chat.e2ee import validate_e2ee_envelope
from app.chat.events import guild_topic, publish_dispatch
from app.chat.permissions import require_permissions
from app.chat.schemas import MessageCreate
from app.core.json_limits import JsonTreeLimits, validate_json_tree
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import (
    ApplicationCommand,
    BotApplication,
    BotInstallation,
    BotInteraction,
)
from app.db.models import Guild, User
from app.federation.client import signed_request
from app.federation.network import decode_federation_response_json
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)

router = APIRouter(prefix="/api/v1", tags=["application interactions"])
federation_router = APIRouter(tags=["application interaction federation"])
INTERACTION_LIMIT = ClientRateLimit("application-interaction", 30, 60)
INTERACTION_LIFETIME = timedelta(minutes=15)
INTERACTION_OPTION_LIMITS = JsonTreeLimits(
    max_depth=8,
    max_nodes=512,
    max_object_members=25,
    max_array_members=100,
    max_key_bytes=100,
    max_string_bytes=64 * 1024,
)


class InteractionCreate(BaseModel):
    application_ref: EntityRef
    command_name: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    command_type: Literal["chat_input", "user", "message"] = "chat_input"
    options: dict[str, Any] = Field(default_factory=dict)
    encrypted_payload: dict[str, Any] | None = None

    @field_validator("options")
    @classmethod
    def bounded_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 25:
            raise ValueError("command options may contain at most 25 values")
        validate_json_tree(value, limits=INTERACTION_OPTION_LIMITS, label="command options")
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("command options are too large")
        return value

    @field_validator("encrypted_payload")
    @classmethod
    def valid_encrypted_payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_e2ee_envelope(value)


class FederatedInteractionCreate(BaseModel):
    user_id: str = Field(pattern=r"^[0-9]{1,20}$")
    interaction: InteractionCreate


class InteractionResponse(BaseModel):
    message: MessageCreate


def command_payload(
    command: ApplicationCommand,
    application: BotApplication,
) -> dict[str, object]:
    return {
        "id": str(command.id),
        "application_ref": f"{application.id}@{application.origin_domain}",
        "application_name": application.name,
        **command.definition,
    }


async def _local_application_commands(
    session: AsyncSession, guild: Guild
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(ApplicationCommand, BotApplication)
            .join(
                BotInstallation,
                (BotInstallation.application_id == ApplicationCommand.application_id)
                & (BotInstallation.application_domain == ApplicationCommand.application_domain),
            )
            .join(
                BotApplication,
                (BotApplication.id == ApplicationCommand.application_id)
                & (BotApplication.origin_domain == ApplicationCommand.application_domain),
            )
            .join(
                User,
                (User.id == BotInstallation.bot_user_id)
                & (User.origin_domain == BotInstallation.bot_user_domain),
            )
            .where(
                BotInstallation.guild_id == guild.id,
                BotInstallation.guild_domain == guild.origin_domain,
                BotInstallation.bot_user_id == BotApplication.bot_user_id,
                BotInstallation.bot_user_domain == BotApplication.bot_user_domain,
                BotInstallation.status == "active",
                installation_has_membership(),
                BotInstallation.granted_scopes.contains(["applications.commands"]),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                ApplicationCommand.state == "active",
                (
                    ApplicationCommand.guild_id.is_(None)
                    | (
                        (ApplicationCommand.guild_id == guild.id)
                        & (ApplicationCommand.guild_domain == guild.origin_domain)
                    )
                ),
            )
            .order_by(ApplicationCommand.name, BotApplication.name)
        )
    ).all()
    return [command_payload(command, application) for command, application in rows]


@router.get("/guilds/{guild_ref}/application-commands")
async def guild_application_commands(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await require_permissions(session, redis, guild, auth.user, Permission.VIEW_CHANNEL)
    if guild.origin_domain != settings.domain:
        upstream = await signed_request(
            session,
            settings,
            "GET",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/application-commands",
            query={"user_id": str(auth.user.id)},
            request_timeout=10,
            max_response_bytes=512 * 1024,
        )
        if upstream.status_code != 200:
            raise HTTPException(status_code=503, detail={"code": "FEDERATED_COMMANDS_UNAVAILABLE"})
        raw = decode_federation_response_json(upstream)
        if not isinstance(raw, list) or len(raw) > 100:
            raise HTTPException(status_code=502, detail={"code": "FEDERATED_COMMANDS_INVALID"})
        commands: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise HTTPException(status_code=502, detail={"code": "FEDERATED_COMMANDS_INVALID"})
            commands.append({str(key): value for key, value in item.items()})
        return commands
    return await _local_application_commands(session, guild)


@federation_router.get("/_kaede/v1/guilds/{guild_id}/application-commands")
async def federation_guild_application_commands(
    guild_id: int,
    user_id: str,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-command-list", capacity=600, refill_per_minute=600
    )
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail={"code": "USER_REF_INVALID"})
    user = await session.get(User, (int(user_id), principal.origin))
    guild = await session.get(Guild, (guild_id, settings.domain))
    if user is None or guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await require_permissions(session, redis, guild, user, Permission.VIEW_CHANNEL)
    return await _local_application_commands(session, guild)


@router.post("/channels/{channel_ref}/interactions", status_code=202)
async def create_interaction(
    channel_ref: EntityRef,
    payload: InteractionCreate,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await enforce_keyed_rate_limit(
        redis,
        response,
        INTERACTION_LIMIT,
        identity=f"{auth.user.origin_domain}:{auth.user.id}",
    )
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.guild is None:
        raise HTTPException(status_code=409, detail={"code": "GUILD_INTERACTION_REQUIRED"})
    await require_permissions(
        session,
        redis,
        access.guild,
        auth.user,
        required_permissions("message.list"),
        channel=access.channel,
    )
    if access.guild.origin_domain != settings.domain:
        upstream = await signed_request(
            session,
            settings,
            "POST",
            access.guild.origin_domain,
            f"/_kaede/v1/channels/{access.channel.id}/interactions",
            payload={
                "user_id": str(auth.user.id),
                "interaction": payload.model_dump(mode="json"),
            },
            request_timeout=10,
            max_response_bytes=64 * 1024,
        )
        if upstream.status_code != 202:
            detail: dict[str, object] = {"code": "FEDERATED_INTERACTION_UNAVAILABLE"}
            if upstream.status_code in {400, 403, 404, 409, 422, 429, 507}:
                raw = decode_federation_response_json(upstream)
                if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                    detail = {str(key): value for key, value in raw["detail"].items()}
            raise HTTPException(status_code=upstream.status_code, detail=detail)
        raw = decode_federation_response_json(upstream)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=502, detail={"code": "FEDERATED_INTERACTION_INVALID"})
        return {str(key): value for key, value in raw.items()}
    app_id, app_domain = payload.application_ref.resolve(settings.domain)
    row = (
        await session.execute(
            select(BotInstallation, ApplicationCommand, BotApplication, User)
            .join(
                ApplicationCommand,
                (ApplicationCommand.application_id == BotInstallation.application_id)
                & (ApplicationCommand.application_domain == BotInstallation.application_domain),
            )
            .join(
                BotApplication,
                (BotApplication.id == BotInstallation.application_id)
                & (BotApplication.origin_domain == BotInstallation.application_domain),
            )
            .join(
                User,
                (User.id == BotInstallation.bot_user_id)
                & (User.origin_domain == BotInstallation.bot_user_domain),
            )
            .where(
                BotInstallation.application_id == app_id,
                BotInstallation.application_domain == app_domain,
                BotInstallation.guild_id == access.guild.id,
                BotInstallation.guild_domain == access.guild.origin_domain,
                BotInstallation.bot_user_id == BotApplication.bot_user_id,
                BotInstallation.bot_user_domain == BotApplication.bot_user_domain,
                BotInstallation.status == "active",
                installation_has_membership(),
                BotInstallation.granted_scopes.contains(["applications.commands"]),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                ApplicationCommand.name == payload.command_name,
                ApplicationCommand.type == payload.command_type,
                ApplicationCommand.state == "active",
                (
                    ApplicationCommand.guild_id.is_(None)
                    | (
                        (ApplicationCommand.guild_id == access.guild.id)
                        & (ApplicationCommand.guild_domain == access.guild.origin_domain)
                    )
                ),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    installation, command, application, bot = row
    encrypted = access.channel.encryption_mode == "e2ee"
    if encrypted and payload.encrypted_payload is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_INTERACTION_PAYLOAD_REQUIRED"},
        )
    if encrypted and installation.e2ee_mode == "disabled":
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_DISABLED"})
    if not encrypted and payload.encrypted_payload is not None:
        raise HTTPException(status_code=422, detail={"code": "UNEXPECTED_ENCRYPTED_PAYLOAD"})
    interaction = BotInteraction(
        id=await snowflake.mint(),
        application_id=application.id,
        application_domain=application.origin_domain,
        installation_id=installation.id,
        guild_id=access.guild.id,
        guild_domain=access.guild.origin_domain,
        channel_id=access.channel.id,
        channel_domain=access.channel.origin_domain,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        command_name=command.name,
        command_type=command.type,
        payload={} if encrypted else {"options": payload.options},
        encrypted_payload=payload.encrypted_payload if encrypted else None,
        expires_at=datetime.now(UTC) + INTERACTION_LIFETIME,
    )
    session.add(interaction)
    await session.commit()
    await publish_dispatch(
        redis,
        guild_topic(access.guild.origin_domain, access.guild.id),
        "INTERACTION_CREATE",
        {
            "id": str(interaction.id),
            "application_ref": f"{application.id}@{application.origin_domain}",
            "installation_id": str(installation.id),
            "guild_ref": f"{access.guild.id}@{access.guild.origin_domain}",
            "channel_ref": f"{access.channel.id}@{access.channel.origin_domain}",
            "channel_id": str(access.channel.id),
            "channel_domain": access.channel.origin_domain,
            "user": {
                "id": str(auth.user.id),
                "origin_domain": auth.user.origin_domain,
                "username": auth.user.username,
                "display_name": auth.user.display_name,
            },
            "command": command.definition,
            "options": None if encrypted else payload.options,
            "encrypted_payload": payload.encrypted_payload if encrypted else None,
            "expires_at": interaction.expires_at.isoformat(),
            "bot_user_ref": f"{bot.id}@{bot.origin_domain}",
        },
        audience_user_refs=(f"{bot.id}@{bot.origin_domain}",),
    )
    return {"id": str(interaction.id), "status": interaction.status}


async def bot_interaction(
    session: AsyncSession,
    principal: BotPrincipal,
    interaction_id: int,
    *required_scopes: str,
) -> tuple[BotInteraction, BotInstallation]:
    for scope in required_scopes:
        principal.require_scope(scope)
    row = (
        await session.execute(
            select(BotInteraction, BotInstallation)
            .join(BotInstallation, BotInstallation.id == BotInteraction.installation_id)
            .where(
                BotInteraction.id == interaction_id,
                BotInteraction.application_id == principal.application.id,
                BotInteraction.application_domain == principal.application.origin_domain,
                BotInstallation.application_id == BotInteraction.application_id,
                BotInstallation.application_domain == BotInteraction.application_domain,
                BotInstallation.guild_id == BotInteraction.guild_id,
                BotInstallation.guild_domain == BotInteraction.guild_domain,
                BotInstallation.bot_user_id == principal.user.id,
                BotInstallation.bot_user_domain == principal.user.origin_domain,
                BotInstallation.status == "active",
                installation_has_membership(),
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"})
    interaction, installation = row
    if (
        interaction.installation_id != installation.id
        or (installation.application_id, installation.application_domain)
        != (principal.application.id, principal.application.origin_domain)
        or (installation.bot_user_id, installation.bot_user_domain)
        != (principal.user.id, principal.user.origin_domain)
        or (installation.guild_id, installation.guild_domain)
        != (interaction.guild_id, interaction.guild_domain)
        or installation.status != "active"
    ):
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"})
    for scope in required_scopes:
        require_installation_scope(principal, installation, scope)
    if interaction.expires_at <= datetime.now(UTC):
        interaction.status = "expired"
        await session.commit()
        raise HTTPException(status_code=410, detail={"code": "INTERACTION_EXPIRED"})
    return interaction, installation


@router.post("/bots/interactions/{interaction_id}/defer", status_code=204)
async def defer_interaction(
    interaction_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    interaction, _ = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
    )
    if interaction.status not in {"pending", "deferred"}:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_ALREADY_RESPONDED"})
    interaction.status = "deferred"
    await session.commit()
    return Response(status_code=204)


@router.post("/bots/interactions/{interaction_id}/response", status_code=201)
async def respond_interaction(
    interaction_id: int,
    payload: InteractionResponse,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    required_scopes = ["interactions.respond"]
    if payload.message.attachment_ids:
        required_scopes.append("attachments.write")
    interaction, interaction_installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        *required_scopes,
    )
    if interaction.status not in {"pending", "deferred"}:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_ALREADY_RESPONDED"})
    channel_ref = EntityRef(f"{interaction.channel_id}@{interaction.channel_domain}")
    _, channel_installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "interactions.respond",
    )
    if channel_installation is None or channel_installation.id != interaction_installation.id:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"})
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        interaction_installation,
        [int(attachment_id) for attachment_id in payload.message.attachment_ids],
    )
    result = await create_message(
        channel_ref,
        payload.message,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )
    message_id = result.get("id")
    message_domain = result.get("origin_domain")
    interaction.status = "responded"
    interaction.responded_at = datetime.now(UTC)
    if isinstance(message_id, str) and message_id.isdigit() and isinstance(message_domain, str):
        interaction.response_message_id = int(message_id)
        interaction.response_message_domain = message_domain
    await session.commit()
    return result


@federation_router.post("/_kaede/v1/channels/{channel_id}/interactions", status_code=202)
async def federation_create_interaction(
    channel_id: int,
    payload: FederatedInteractionCreate,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-command-create", capacity=300, refill_per_minute=300
    )
    user = await session.get(User, (int(payload.user_id), principal.origin))
    if user is None or user.account_type != "human":
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    auth = AuthenticatedUser(
        user=user,
        grant=cast(Any, None),
        access_token="",
        cookie_authenticated=False,
    )
    return await create_interaction(
        EntityRef(f"{channel_id}@{settings.domain}"),
        payload.interaction,
        Response(),
        auth,
        session,
        redis,
        snowflake,
        settings,
    )
