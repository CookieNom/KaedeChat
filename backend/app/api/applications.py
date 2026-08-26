from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bot_federation import (
    fetch_bot_manifest,
    materialize_remote_manifest,
    refresh_remote_worker_authorization,
)
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.auth.security import new_token, token_hash
from app.bots.auth import decode_urlsafe, encode_urlsafe, issue_bot_token, worker_assertion_message
from app.bots.installations import (
    active_installation_exists,
    cleanup_installation_roles,
    publish_deleted_installation_roles,
)
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import member_payload, role_payload, user_payload
from app.chat.permissions import get_permissions
from app.chat.thread_membership import (
    cleanup_guild_member_threads,
    publish_guild_thread_member_cleanup,
)
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import DOMAIN_RE, Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import (
    ApplicationCommand,
    BotApplication,
    BotCredential,
    BotInstallation,
    BotInstallTemplate,
    BotInstanceRule,
    BotWorker,
    DeveloperTeam,
    DeveloperTeamMember,
)
from app.db.models import (
    Ban,
    Guild,
    GuildInstanceBan,
    GuildMember,
    InstanceBlock,
    MemberRole,
    Role,
    User,
)
from app.federation.client import signed_request
from app.federation.network import decode_federation_response_json
from app.federation.replication import profile_from_user
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)
from app.tracker.membership import clear_tracker_assignees, wake_tracker_membership_cleanup

router = APIRouter(prefix="/api/v1", tags=["applications"])
federation_router = APIRouter(tags=["bot install federation"])

APPLICATION_CREATE_LIMIT = ClientRateLimit("application-create", 10, 3600)
BOT_INVITE_LIMIT = ClientRateLimit("bot-invite", 30, 60)
BOT_TOKEN_LIMIT = ClientRateLimit("bot-token", 30, 60)
BOT_CONTROL_LIMIT = ClientRateLimit("bot-control", 120, 60)
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
SLUG_CLEAN_RE = re.compile(r"[^a-z0-9_.]+")

SUPPORTED_SCOPES = frozenset(
    {
        "applications.commands",
        "interactions.respond",
        "guilds.read",
        "guilds.manage",
        "channels.read",
        "channels.manage",
        "members.read",
        "roles.read",
        "roles.manage",
        "messages.metadata",
        "messages.content",
        "messages.history",
        "messages.send",
        "messages.edit.own",
        "messages.delete.own",
        "messages.manage",
        "attachments.read",
        "attachments.write",
        "reactions.read",
        "reactions.write",
        "moderation.members",
        "moderation.messages",
        "voice.states.read",
        "voice.moderate",
        "invites.manage",
        "webhooks.manage",
        "emojis.manage",
        "tasks.read",
        "tasks.write",
        "tasks.manage",
        "dm.send",
    }
)
SUPPORTED_INTENTS = frozenset(
    {
        "guilds",
        "guild_members",
        "guild_presences",
        "guild_messages",
        "message_content",
        "message_reactions",
        "guild_typing",
        "voice_states",
        "interactions",
        "guild_tasks",
    }
)
CONTROL_SCOPES = frozenset({"workers.manage", "commands.manage"})


def normalize_values(values: list[str], supported: frozenset[str], label: str) -> list[str]:
    normalized = list(dict.fromkeys(values))
    invalid = sorted(set(normalized) - supported)
    if invalid:
        raise ValueError(f"unsupported {label}: {', '.join(invalid)}")
    return normalized


def default_guild_contexts() -> list[Literal["guild"]]:
    return ["guild"]


async def ensure_bot_install_allowed(
    session: AsyncSession,
    guild: Guild,
    bot: User,
) -> None:
    """Fail closed while a bot identity or its whole home is banned.

    The caller holds the authoritative guild row lock.  Ban writers take the
    same lock, so checking and then creating/reactivating the membership and
    installation is serialized with both per-user and instance-wide bans.
    The matching ban rows are locked as an additional defense and to make the
    lock contract explicit to future mutation paths.
    """

    now = datetime.now(UTC)
    user_ban = await session.scalar(
        select(Ban.user_id)
        .where(
            Ban.guild_id == guild.id,
            Ban.guild_domain == guild.origin_domain,
            Ban.user_id == bot.id,
            Ban.user_domain == bot.origin_domain,
            or_(Ban.expires_at.is_(None), Ban.expires_at > now),
        )
        .with_for_update()
    )
    if user_ban is not None:
        raise HTTPException(status_code=403, detail={"code": "BOT_USER_BANNED"})
    instance_ban = await session.scalar(
        select(GuildInstanceBan.instance_domain)
        .where(
            GuildInstanceBan.guild_id == guild.id,
            GuildInstanceBan.guild_domain == guild.origin_domain,
            GuildInstanceBan.instance_domain == bot.origin_domain,
            or_(GuildInstanceBan.expires_at.is_(None), GuildInstanceBan.expires_at > now),
        )
        .with_for_update()
    )
    if instance_ban is not None:
        raise HTTPException(status_code=403, detail={"code": "BOT_INSTANCE_BANNED"})


async def locked_guild_mutation_signer(session: AsyncSession, guild: Guild) -> User:
    """Lock the local owner used to sign authoritative membership mutations."""

    signer = await session.scalar(
        select(User)
        .where(
            User.id == guild.owner_id,
            User.origin_domain == guild.owner_domain,
            User.is_local.is_(True),
        )
        .with_for_update()
    )
    if signer is None:
        raise RuntimeError("local guild owner is unavailable for bot mutation signing")
    return signer


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    support_url: HttpUrl | None = None
    privacy_url: HttpUrl | None = None
    team_ref: EntityRef | None = None


class DeveloperTeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DeveloperTeamMemberPut(BaseModel):
    user_ref: EntityRef
    role: Literal["owner", "administrator", "developer", "security", "analyst", "support"]


class DeveloperTeamMemberPatch(BaseModel):
    role: Literal["owner", "administrator", "developer", "security", "analyst", "support"]


class ApplicationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    support_url: HttpUrl | None = None
    privacy_url: HttpUrl | None = None
    target_policy: Literal["open", "allowlist", "blocklist", "local_only"] | None = None
    default_scopes: list[str] | None = Field(default=None, max_length=64)
    default_intents: list[str] | None = Field(default=None, max_length=32)
    default_permissions: int | None = Field(default=None, ge=0, le=ALL_PERMISSIONS)
    e2ee_modes: list[Literal["interaction_only", "participant"]] | None = Field(
        default=None, max_length=2
    )

    @field_validator("default_scopes")
    @classmethod
    def valid_scopes(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_values(value, SUPPORTED_SCOPES, "scope")

    @field_validator("default_intents")
    @classmethod
    def valid_intents(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_values(value, SUPPORTED_INTENTS, "intent")


class CredentialCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(
        default_factory=lambda: ["workers.manage", "commands.manage"], max_length=8
    )

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        return normalize_values(value, CONTROL_SCOPES, "control scope")


class WorkerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=43, max_length=44)
    scopes: list[str] = Field(default_factory=list, max_length=64)
    intents: list[str] = Field(default_factory=list, max_length=32)
    target_domains: list[str] = Field(default_factory=list, max_length=100)
    session_limit: int = Field(default=1, ge=1, le=16)

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        return normalize_values(value, SUPPORTED_SCOPES, "scope")

    @field_validator("intents")
    @classmethod
    def valid_intents(cls, value: list[str]) -> list[str]:
        return normalize_values(value, SUPPORTED_INTENTS, "intent")

    @field_validator("target_domains")
    @classmethod
    def valid_targets(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.rstrip(".").lower() for item in value))
        if any(not DOMAIN_RE.fullmatch(item) for item in normalized):
            raise ValueError("target domains must be canonical hostnames")
        return normalized


class TemplateCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    scopes: list[str] = Field(default_factory=list, max_length=64)
    intents: list[str] = Field(default_factory=list, max_length=32)
    permissions: int = Field(default=0, ge=0, le=ALL_PERMISSIONS)
    contexts: list[Literal["guild"]] = Field(default_factory=default_guild_contexts)
    e2ee_mode: Literal["disabled", "interaction_only", "participant"] = "interaction_only"

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        return normalize_values(value, SUPPORTED_SCOPES, "scope")

    @field_validator("intents")
    @classmethod
    def valid_intents(cls, value: list[str]) -> list[str]:
        return normalize_values(value, SUPPORTED_INTENTS, "intent")


class CommandChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    value: str | int | float


class CommandOptionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "subcommand",
        "subcommand_group",
        "string",
        "integer",
        "boolean",
        "user",
        "channel",
        "role",
        "mentionable",
        "number",
        "attachment",
    ]
    name: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    description: str = Field(min_length=1, max_length=100)
    required: bool = False
    autocomplete: bool = False
    choices: list[CommandChoice] = Field(default_factory=list, max_length=25)
    options: list[CommandOptionDefinition] = Field(default_factory=list, max_length=25)
    min_value: float | None = Field(default=None, ge=-(2**53), le=2**53)
    max_value: float | None = Field(default=None, ge=-(2**53), le=2**53)
    min_length: int | None = Field(default=None, ge=0, le=6000)
    max_length: int | None = Field(default=None, ge=1, le=6000)

    @model_validator(mode="after")
    def valid_shape(self) -> CommandOptionDefinition:
        container = self.type in {"subcommand", "subcommand_group"}
        if self.options and not container:
            raise ValueError("only subcommands and groups contain nested options")
        if self.type == "subcommand_group" and not self.options:
            raise ValueError("subcommand groups require at least one subcommand")
        if self.type == "subcommand_group" and any(
            option.type != "subcommand" for option in self.options
        ):
            raise ValueError("subcommand groups may contain only subcommands")
        if self.choices and self.type not in {"string", "integer", "number"}:
            raise ValueError("choices require a string or numeric option")
        if self.autocomplete and (self.choices or self.type not in {"string", "integer", "number"}):
            raise ValueError("autocomplete requires a string or numeric option without choices")
        if (self.min_length is not None or self.max_length is not None) and self.type != "string":
            raise ValueError("length bounds require a string option")
        if (self.min_value is not None or self.max_value is not None) and self.type not in {
            "integer",
            "number",
        }:
            raise ValueError("numeric bounds require a numeric option")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("minimum length exceeds maximum length")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("minimum value exceeds maximum value")
        return self


class CommandDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    type: Literal["chat_input", "user", "message"] = "chat_input"
    description: str = Field(default="", max_length=100)
    default_member_permissions: list[str] = Field(default_factory=list, max_length=64)
    contexts: list[Literal["guild"]] = Field(default_factory=default_guild_contexts)
    options: list[CommandOptionDefinition] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def valid_command_shape(self) -> CommandDefinition:
        if self.type == "chat_input" and not self.description:
            raise ValueError("chat input commands require a description")
        if self.type != "chat_input" and (self.description or self.options):
            raise ValueError("context commands do not have descriptions or options")
        if any(option.type == "subcommand_group" for option in self.options) and any(
            option.type not in {"subcommand", "subcommand_group"} for option in self.options
        ):
            raise ValueError("subcommand containers cannot be mixed with scalar options")
        required_seen = False
        for option in reversed(self.options):
            if option.required:
                required_seen = True
            elif required_seen:
                raise ValueError("required options must precede optional options")
        return self


class CommandsPut(BaseModel):
    commands: list[CommandDefinition] = Field(max_length=100)

    @model_validator(mode="after")
    def bounded_tree(self) -> CommandsPut:
        nodes = 0

        def walk(options: list[CommandOptionDefinition], depth: int) -> None:
            nonlocal nodes
            if depth > 3:
                raise ValueError("command options exceed the maximum nesting depth")
            nodes += len(options)
            if nodes > 500:
                raise ValueError("command set contains too many options")
            for option in options:
                if option.options:
                    walk(option.options, depth + 1)

        for command in self.commands:
            walk(command.options, 1)
        return self


class FederatedBotInstallRequest(BaseModel):
    installer_id: str
    application_ref: EntityRef
    template_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")


class FederatedBotUninstallRequest(BaseModel):
    installer_id: str = Field(pattern=r"^[0-9]{1,20}$")
    application_ref: EntityRef


class WorkerTokenRequest(BaseModel):
    application_ref: EntityRef
    worker_id: int = Field(ge=0)
    audience: str = Field(min_length=1, max_length=2048)
    issued_at: int
    expires_at: int
    nonce: str = Field(min_length=16, max_length=128)
    signature: str = Field(min_length=86, max_length=88)


def application_payload(application: BotApplication, bot: User) -> dict[str, object]:
    return {
        "id": str(application.id),
        "origin_domain": application.origin_domain,
        "ref": f"{application.id}@{application.origin_domain}",
        "name": application.name,
        "description": application.description,
        "icon_hash": application.icon_hash,
        "support_url": application.support_url,
        "privacy_url": application.privacy_url,
        "status": application.status,
        "target_policy": application.target_policy,
        "default_scopes": application.default_scopes,
        "default_intents": application.default_intents,
        "default_permissions": str(application.default_permissions),
        "e2ee_modes": application.e2ee_modes,
        "manifest_generation": str(application.manifest_generation),
        "command_generation": str(application.command_generation),
        "bot_user": {
            "id": str(bot.id),
            "origin_domain": bot.origin_domain,
            "ref": f"{bot.id}@{bot.origin_domain}",
            "username": bot.username,
            "handle": f"{bot.username}@{bot.origin_domain}",
            "display_name": bot.display_name,
            "bot": True,
        },
        "created_at": application.created_at.isoformat(),
        "updated_at": application.updated_at.isoformat(),
    }


async def managed_application(
    session: AsyncSession, settings: Settings, auth: AuthenticatedUser, ref: EntityRef
) -> tuple[BotApplication, DeveloperTeamMember, User]:
    app_id, app_domain = ref.resolve(settings.domain)
    row = (
        await session.execute(
            select(BotApplication, DeveloperTeamMember, User)
            .join(
                DeveloperTeamMember,
                (DeveloperTeamMember.team_id == BotApplication.team_id)
                & (DeveloperTeamMember.team_domain == BotApplication.team_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == app_id,
                BotApplication.origin_domain == app_domain,
                DeveloperTeamMember.user_id == auth.user.id,
                DeveloperTeamMember.user_domain == auth.user.origin_domain,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    return row[0], row[1], row[2]


async def control_application(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    ref: EntityRef,
    required_scope: str,
) -> tuple[BotApplication, BotCredential]:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("BotControl "):
        raise HTTPException(status_code=401, detail={"code": "BOT_CONTROL_AUTH_REQUIRED"})
    raw = authorization.removeprefix("BotControl ")
    if not raw.startswith("kb1_ctl_") or len(raw) > 160:
        raise HTTPException(status_code=401, detail={"code": "BOT_CONTROL_TOKEN_INVALID"})
    app_id, app_domain = ref.resolve(settings.domain)
    now = datetime.now(UTC)
    row = (
        await session.execute(
            select(BotApplication, BotCredential)
            .join(
                BotCredential,
                (BotCredential.application_id == BotApplication.id)
                & (BotCredential.application_domain == BotApplication.origin_domain),
            )
            .where(
                BotApplication.id == app_id,
                BotApplication.origin_domain == app_domain,
                BotApplication.status == "active",
                BotCredential.token_hash == token_hash(raw),
                BotCredential.revoked_at.is_(None),
                (BotCredential.expires_at.is_(None)) | (BotCredential.expires_at > now),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail={"code": "BOT_CONTROL_TOKEN_INVALID"})
    application, credential = row
    if required_scope not in credential.scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_CONTROL_SCOPE_REQUIRED", "scope": required_scope},
        )
    credential.last_used_at = now
    return application, credential


def require_team_role(member: DeveloperTeamMember, *roles: str) -> None:
    if member.role not in roles:
        raise HTTPException(status_code=403, detail={"code": "APPLICATION_PERMISSION_DENIED"})


def bot_username(name: str, app_id: int) -> str:
    base = SLUG_CLEAN_RE.sub("_", name.lower()).strip("_.") or "bot"
    return f"{base[:22]}_{str(app_id)[-8:]}"[:32]


def team_payload(team: DeveloperTeam, role: str) -> dict[str, object]:
    return {
        "ref": f"{team.id}@{team.origin_domain}",
        "name": "Personal" if team.personal else team.name,
        "personal": team.personal,
        "role": role,
        "created_at": team.created_at.isoformat(),
    }


async def ensure_personal_developer_team(
    session: AsyncSession,
    settings: Settings,
    auth: AuthenticatedUser,
    snowflake: SnowflakeGenerator,
) -> tuple[DeveloperTeam, DeveloperTeamMember]:
    """Return the user's permanent personal application workspace."""
    if not auth.user.is_local or auth.user.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "LOCAL_HUMAN_ACCOUNT_REQUIRED"})

    # Provisioning is lazy so existing installations do not need a row for every
    # account up front. The per-user transaction lock makes concurrent portal
    # loads and application creates converge on the same workspace.
    lock_scope = f"kaede-personal-developer-team:{auth.user.origin_domain}:{auth.user.id}"
    await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_scope, 0))))
    row = (
        await session.execute(
            select(DeveloperTeam, DeveloperTeamMember)
            .join(
                DeveloperTeamMember,
                (DeveloperTeamMember.team_id == DeveloperTeam.id)
                & (DeveloperTeamMember.team_domain == DeveloperTeam.origin_domain),
            )
            .where(
                DeveloperTeam.personal.is_(True),
                DeveloperTeamMember.user_id == auth.user.id,
                DeveloperTeamMember.user_domain == auth.user.origin_domain,
                DeveloperTeamMember.role == "owner",
            )
            .order_by(DeveloperTeam.created_at, DeveloperTeam.id)
            .limit(1)
            .with_for_update()
        )
    ).one_or_none()
    if row is not None:
        team, member = row
        # Older deployments used "<display name>'s applications". Normalize it
        # when the owner next opens the portal so the product name is consistent.
        team.name = "Personal"
        member.role = "owner"
        return team, member

    team = DeveloperTeam(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        name="Personal",
        personal=True,
    )
    member = DeveloperTeamMember(
        team_id=team.id,
        team_domain=team.origin_domain,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        user_is_local=True,
        role="owner",
    )
    session.add_all([team, member])
    await session.flush()
    return team, member


async def managed_team(
    session: AsyncSession, settings: Settings, auth: AuthenticatedUser, team_ref: EntityRef
) -> tuple[DeveloperTeam, DeveloperTeamMember]:
    team_id, team_domain = team_ref.resolve(settings.domain)
    if team_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "DEVELOPER_TEAM_NOT_FOUND"})
    row = (
        await session.execute(
            select(DeveloperTeam, DeveloperTeamMember)
            .join(
                DeveloperTeamMember,
                (DeveloperTeamMember.team_id == DeveloperTeam.id)
                & (DeveloperTeamMember.team_domain == DeveloperTeam.origin_domain),
            )
            .where(
                DeveloperTeam.id == team_id,
                DeveloperTeam.origin_domain == team_domain,
                DeveloperTeamMember.user_id == auth.user.id,
                DeveloperTeamMember.user_domain == auth.user.origin_domain,
                (DeveloperTeam.personal.is_(False)) | (DeveloperTeamMember.role == "owner"),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "DEVELOPER_TEAM_NOT_FOUND"})
    return row[0], row[1]


@router.get("/developer-teams")
async def list_developer_teams(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    await ensure_personal_developer_team(session, settings, auth, snowflake)
    await session.commit()
    rows = (
        await session.execute(
            select(DeveloperTeam, DeveloperTeamMember.role)
            .join(
                DeveloperTeamMember,
                (DeveloperTeamMember.team_id == DeveloperTeam.id)
                & (DeveloperTeamMember.team_domain == DeveloperTeam.origin_domain),
            )
            .where(
                DeveloperTeamMember.user_id == auth.user.id,
                DeveloperTeamMember.user_domain == auth.user.origin_domain,
            )
            .order_by(DeveloperTeam.personal.desc(), func.lower(DeveloperTeam.name))
        )
    ).all()
    return [team_payload(team, role) for team, role in rows]


@router.post("/developer-teams", status_code=201)
async def create_developer_team(
    payload: DeveloperTeamCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not auth.user.is_local or auth.user.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "LOCAL_HUMAN_ACCOUNT_REQUIRED"})
    team = DeveloperTeam(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        name=payload.name.strip(),
        personal=False,
    )
    member = DeveloperTeamMember(
        team_id=team.id,
        team_domain=team.origin_domain,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        user_is_local=True,
        role="owner",
    )
    session.add_all([team, member])
    await session.commit()
    return team_payload(team, member.role)


@router.get("/developer-teams/{team_ref}/members")
async def list_developer_team_members(
    team_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    team, _ = await managed_team(session, settings, auth, team_ref)
    rows = (
        await session.execute(
            select(DeveloperTeamMember, User)
            .join(
                User,
                (User.id == DeveloperTeamMember.user_id)
                & (User.origin_domain == DeveloperTeamMember.user_domain),
            )
            .where(
                DeveloperTeamMember.team_id == team.id,
                DeveloperTeamMember.team_domain == team.origin_domain,
            )
            .order_by(DeveloperTeamMember.created_at, DeveloperTeamMember.user_id)
        )
    ).all()
    return [
        {
            "user": user_payload(user),
            "role": member.role,
            "created_at": member.created_at.isoformat(),
        }
        for member, user in rows
    ]


@router.post("/developer-teams/{team_ref}/members", status_code=201)
async def add_developer_team_member(
    team_ref: EntityRef,
    payload: DeveloperTeamMemberPut,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    team, actor = await managed_team(session, settings, auth, team_ref)
    if team.personal:
        raise HTTPException(status_code=409, detail={"code": "PERSONAL_TEAM_MEMBERS_IMMUTABLE"})
    require_team_role(actor, "owner", "administrator")
    if payload.role == "owner" and actor.role != "owner":
        raise HTTPException(status_code=403, detail={"code": "TEAM_OWNER_REQUIRED"})
    user_id, user_domain = payload.user_ref.resolve(settings.domain)
    user = await session.get(User, (user_id, user_domain))
    if (
        user is None
        or user_domain != settings.domain
        or not user.is_local
        or user.account_type != "human"
    ):
        raise HTTPException(status_code=404, detail={"code": "TEAM_MEMBER_NOT_FOUND"})
    member = await session.get(
        DeveloperTeamMember, (team.id, team.origin_domain, user.id, user.origin_domain)
    )
    if member is None:
        member = DeveloperTeamMember(
            team_id=team.id,
            team_domain=team.origin_domain,
            user_id=user.id,
            user_domain=user.origin_domain,
            user_is_local=True,
            role=payload.role,
        )
        session.add(member)
    else:
        if member.role == "owner" and payload.role != "owner":
            await _preserve_team_owner(session, member)
        member.role = payload.role
    await session.commit()
    return {"user": user_payload(user), "role": member.role}


async def _preserve_team_owner(session: AsyncSession, member: DeveloperTeamMember) -> None:
    if member.role != "owner":
        return
    owner_count = await session.scalar(
        select(func.count())
        .select_from(DeveloperTeamMember)
        .where(
            DeveloperTeamMember.team_id == member.team_id,
            DeveloperTeamMember.team_domain == member.team_domain,
            DeveloperTeamMember.role == "owner",
        )
    )
    if int(owner_count or 0) <= 1:
        raise HTTPException(status_code=409, detail={"code": "TEAM_LAST_OWNER"})


@router.patch("/developer-teams/{team_ref}/members/{user_ref}")
async def patch_developer_team_member(
    team_ref: EntityRef,
    user_ref: EntityRef,
    payload: DeveloperTeamMemberPatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    team, actor = await managed_team(session, settings, auth, team_ref)
    if team.personal:
        raise HTTPException(status_code=409, detail={"code": "PERSONAL_TEAM_MEMBERS_IMMUTABLE"})
    require_team_role(actor, "owner", "administrator")
    if payload.role == "owner" and actor.role != "owner":
        raise HTTPException(status_code=403, detail={"code": "TEAM_OWNER_REQUIRED"})
    user_id, user_domain = user_ref.resolve(settings.domain)
    member = await session.get(
        DeveloperTeamMember,
        (team.id, team.origin_domain, user_id, user_domain),
        with_for_update=True,
    )
    user = await session.get(User, (user_id, user_domain))
    if member is None or user is None:
        raise HTTPException(status_code=404, detail={"code": "TEAM_MEMBER_NOT_FOUND"})
    if member.role == "owner" and payload.role != "owner":
        await _preserve_team_owner(session, member)
    member.role = payload.role
    await session.commit()
    return {"user": user_payload(user), "role": member.role}


@router.delete("/developer-teams/{team_ref}/members/{user_ref}", status_code=204)
async def remove_developer_team_member(
    team_ref: EntityRef,
    user_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    team, actor = await managed_team(session, settings, auth, team_ref)
    if team.personal:
        raise HTTPException(status_code=409, detail={"code": "PERSONAL_TEAM_MEMBERS_IMMUTABLE"})
    user_id, user_domain = user_ref.resolve(settings.domain)
    self_remove = (user_id, user_domain) == (auth.user.id, auth.user.origin_domain)
    if not self_remove:
        require_team_role(actor, "owner", "administrator")
    member = await session.get(
        DeveloperTeamMember,
        (team.id, team.origin_domain, user_id, user_domain),
        with_for_update=True,
    )
    if member is None:
        return Response(status_code=204)
    if member.role == "owner" and actor.role != "owner":
        raise HTTPException(status_code=403, detail={"code": "TEAM_OWNER_REQUIRED"})
    await _preserve_team_owner(session, member)
    await session.delete(member)
    await session.commit()
    return Response(status_code=204)


@router.post("/applications", status_code=201)
async def create_application(
    payload: ApplicationCreate,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if auth.user.account_type != "human" or not auth.user.is_local:
        raise HTTPException(status_code=403, detail={"code": "LOCAL_HUMAN_ACCOUNT_REQUIRED"})
    await enforce_keyed_rate_limit(
        redis,
        response,
        APPLICATION_CREATE_LIMIT,
        identity=f"{auth.user.origin_domain}:{auth.user.id}",
    )
    selected_team_member: DeveloperTeamMember | None
    if payload.team_ref is not None:
        _, selected_team_member = await managed_team(session, settings, auth, payload.team_ref)
        require_team_role(selected_team_member, "owner", "administrator", "developer")
    else:
        _, selected_team_member = await ensure_personal_developer_team(
            session, settings, auth, snowflake
        )
    if selected_team_member is None:
        raise RuntimeError("application team could not be resolved")
    app_id = await snowflake.mint()
    bot_id = await snowflake.mint()
    bot = User(
        id=bot_id,
        origin_domain=settings.domain,
        is_local=True,
        account_type="bot",
        username=bot_username(payload.name, app_id),
        display_name=payload.name.strip(),
        password_hash=None,
        profile_resolved=True,
    )
    application = BotApplication(
        id=app_id,
        origin_domain=settings.domain,
        team_id=selected_team_member.team_id,
        team_domain=selected_team_member.team_domain,
        bot_user_id=bot_id,
        bot_user_domain=settings.domain,
        name=payload.name.strip(),
        description=payload.description,
        support_url=str(payload.support_url) if payload.support_url else None,
        privacy_url=str(payload.privacy_url) if payload.privacy_url else None,
    )
    session.add_all([bot, application])
    await session.commit()
    return application_payload(application, bot)


@router.get("/applications")
async def list_applications(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(BotApplication, User)
            .join(
                DeveloperTeamMember,
                (DeveloperTeamMember.team_id == BotApplication.team_id)
                & (DeveloperTeamMember.team_domain == BotApplication.team_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                DeveloperTeamMember.user_id == auth.user.id,
                DeveloperTeamMember.user_domain == auth.user.origin_domain,
                BotApplication.status != "deleted",
            )
            .order_by(func.lower(BotApplication.name), BotApplication.id)
        )
    ).all()
    return [application_payload(app, bot) for app, bot in rows]


@router.get("/applications/{application_ref}")
async def get_application(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, _, bot = await managed_application(session, settings, auth, application_ref)
    return application_payload(app, bot)


@router.patch("/applications/{application_ref}")
async def patch_application(
    application_ref: EntityRef,
    payload: ApplicationPatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, member, bot = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator")
    values = payload.model_dump(exclude_unset=True)
    for key in ("support_url", "privacy_url"):
        if key in values and values[key] is not None:
            values[key] = str(values[key])
    for key, value in values.items():
        setattr(app, key, value)
    if "name" in values:
        bot.display_name = cast(str, values["name"]).strip()
        bot.profile_version += 1
    app.manifest_generation += 1
    await session.commit()
    return application_payload(app, bot)


@router.post("/applications/{application_ref}/credentials", status_code=201)
async def create_credential(
    application_ref: EntityRef,
    payload: CredentialCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "security")
    raw = new_token("kb1_ctl_")
    credential = BotCredential(
        id=await snowflake.mint(),
        application_id=app.id,
        application_domain=app.origin_domain,
        label=payload.label.strip(),
        token_hash=token_hash(raw),
        token_hint=f"{raw[:12]}…{raw[-4:]}",
        scopes=payload.scopes,
    )
    session.add(credential)
    await session.commit()
    return {"id": str(credential.id), "token": raw, "token_hint": credential.token_hint}


@router.get("/applications/{application_ref}/credentials")
async def list_credentials(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "security")
    rows = list(
        await session.scalars(
            select(BotCredential)
            .where(
                BotCredential.application_id == app.id,
                BotCredential.application_domain == app.origin_domain,
            )
            .order_by(BotCredential.created_at.desc())
        )
    )
    return [
        {
            "id": str(row.id),
            "label": row.label,
            "token_hint": row.token_hint,
            "scopes": row.scopes,
            "created_at": row.created_at.isoformat(),
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        }
        for row in rows
    ]


@router.delete("/applications/{application_ref}/credentials/{credential_id}", status_code=204)
async def revoke_credential(
    application_ref: EntityRef,
    credential_id: int,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "security")
    credential = await session.get(BotCredential, credential_id, with_for_update=True)
    if credential is None or (credential.application_id, credential.application_domain) != (
        app.id,
        app.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "BOT_CREDENTIAL_NOT_FOUND"})
    if credential.revoked_at is None:
        credential.revoked_at = datetime.now(UTC)
        app.revocation_generation += 1
        await session.commit()
    return Response(status_code=204)


@router.post("/applications/{application_ref}/workers", status_code=201)
async def create_worker(
    application_ref: EntityRef,
    payload: WorkerCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "developer", "security")
    try:
        public_key = decode_urlsafe(payload.public_key, length=32)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "WORKER_PUBLIC_KEY_INVALID"}) from None
    if not set(payload.scopes).issubset(app.default_scopes) or not set(payload.intents).issubset(
        app.default_intents
    ):
        raise HTTPException(status_code=409, detail={"code": "WORKER_EXCEEDS_APPLICATION"})
    worker = BotWorker(
        id=await snowflake.mint(),
        application_id=app.id,
        application_domain=app.origin_domain,
        name=payload.name.strip(),
        public_key=public_key,
        scopes=payload.scopes,
        intents=payload.intents,
        target_domains=payload.target_domains,
        session_limit=payload.session_limit,
    )
    session.add(worker)
    await session.commit()
    return {"id": str(worker.id), "name": worker.name, "public_key": payload.public_key}


@router.post("/bot-control/applications/{application_ref}/workers", status_code=201)
async def control_create_worker(
    application_ref: EntityRef,
    payload: WorkerCreate,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, credential = await control_application(
        request, session, settings, application_ref, "workers.manage"
    )
    await enforce_keyed_rate_limit(
        redis, response, BOT_CONTROL_LIMIT, identity=f"credential:{credential.id}"
    )
    try:
        public_key = decode_urlsafe(payload.public_key, length=32)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "WORKER_PUBLIC_KEY_INVALID"}) from None
    if not set(payload.scopes).issubset(app.default_scopes) or not set(payload.intents).issubset(
        app.default_intents
    ):
        raise HTTPException(status_code=409, detail={"code": "WORKER_EXCEEDS_APPLICATION"})
    worker = BotWorker(
        id=await snowflake.mint(),
        application_id=app.id,
        application_domain=app.origin_domain,
        name=payload.name.strip(),
        public_key=public_key,
        scopes=payload.scopes,
        intents=payload.intents,
        target_domains=payload.target_domains,
        session_limit=payload.session_limit,
    )
    session.add(worker)
    await session.commit()
    return {"id": str(worker.id), "name": worker.name, "public_key": payload.public_key}


@router.put("/applications/{application_ref}/commands")
async def put_commands(
    application_ref: EntityRef,
    payload: CommandsPut,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "developer")
    keys = [(item.type, item.name) for item in payload.commands]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail={"code": "COMMAND_NAME_DUPLICATE"})
    encoded = payload.model_dump_json()
    if len(encoded.encode()) > 256 * 1024:
        raise HTTPException(status_code=413, detail={"code": "COMMAND_SET_TOO_LARGE"})
    app.command_generation += 1
    await session.execute(
        delete(ApplicationCommand).where(
            ApplicationCommand.application_id == app.id,
            ApplicationCommand.application_domain == app.origin_domain,
            ApplicationCommand.guild_id.is_(None),
        )
    )
    for definition in payload.commands:
        session.add(
            ApplicationCommand(
                id=await snowflake.mint(),
                application_id=app.id,
                application_domain=app.origin_domain,
                name=definition.name,
                type=definition.type,
                definition=definition.model_dump(mode="json"),
                generation=app.command_generation,
                state="active",
            )
        )
    await session.commit()
    return {"generation": str(app.command_generation), "commands": len(payload.commands)}


@router.put("/bot-control/applications/{application_ref}/commands")
async def control_put_commands(
    application_ref: EntityRef,
    payload: CommandsPut,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, credential = await control_application(
        request, session, settings, application_ref, "commands.manage"
    )
    await enforce_keyed_rate_limit(
        redis, response, BOT_CONTROL_LIMIT, identity=f"credential:{credential.id}"
    )
    keys = [(item.type, item.name) for item in payload.commands]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail={"code": "COMMAND_NAME_DUPLICATE"})
    encoded = payload.model_dump_json()
    if len(encoded.encode()) > 256 * 1024:
        raise HTTPException(status_code=413, detail={"code": "COMMAND_SET_TOO_LARGE"})
    app.command_generation += 1
    await session.execute(
        delete(ApplicationCommand).where(
            ApplicationCommand.application_id == app.id,
            ApplicationCommand.application_domain == app.origin_domain,
            ApplicationCommand.guild_id.is_(None),
        )
    )
    for definition in payload.commands:
        session.add(
            ApplicationCommand(
                id=await snowflake.mint(),
                application_id=app.id,
                application_domain=app.origin_domain,
                name=definition.name,
                type=definition.type,
                definition=definition.model_dump(mode="json"),
                generation=app.command_generation,
                state="active",
            )
        )
    await session.commit()
    return {"generation": str(app.command_generation), "commands": len(payload.commands)}


@router.get("/applications/{application_ref}/commands")
async def get_commands(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    app, _, _ = await managed_application(session, settings, auth, application_ref)
    rows = list(
        await session.scalars(
            select(ApplicationCommand)
            .where(
                ApplicationCommand.application_id == app.id,
                ApplicationCommand.application_domain == app.origin_domain,
                ApplicationCommand.guild_id.is_(None),
            )
            .order_by(ApplicationCommand.type, ApplicationCommand.name)
        )
    )
    return [row.definition for row in rows]


@router.post("/applications/{application_ref}/install-templates", status_code=201)
async def create_template(
    application_ref: EntityRef,
    payload: TemplateCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "developer")
    if not set(payload.scopes).issubset(set(app.default_scopes)) or not set(
        payload.intents
    ).issubset(set(app.default_intents)):
        raise HTTPException(status_code=409, detail={"code": "TEMPLATE_EXCEEDS_APPLICATION"})
    template = BotInstallTemplate(
        id=await snowflake.mint(),
        application_id=app.id,
        application_domain=app.origin_domain,
        **payload.model_dump(),
    )
    session.add(template)
    if app.status == "draft":
        app.status = "active"
    await session.commit()
    return {
        "id": str(template.id),
        "slug": template.slug,
        "invite_url": f"https://{settings.domain}/applications/{app.id}@{app.origin_domain}/install/{template.slug}",
    }


@router.get("/bot-invites/{application_ref}/{template_slug}")
async def resolve_bot_invite(
    application_ref: EntityRef,
    template_slug: str,
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_keyed_rate_limit(
        redis,
        response,
        BOT_INVITE_LIMIT,
        identity=request.client.host if request.client else "unknown",
    )
    app_id, app_domain = application_ref.resolve(settings.domain)
    if app_domain != settings.domain:
        manifest = await fetch_bot_manifest(session, settings, app_id, app_domain, template_slug)
        return manifest.model_dump(mode="json")
    row = (
        await session.execute(
            select(BotApplication, BotInstallTemplate, User)
            .join(
                BotInstallTemplate,
                (BotInstallTemplate.application_id == BotApplication.id)
                & (BotInstallTemplate.application_domain == BotApplication.origin_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == app_id,
                BotApplication.origin_domain == app_domain,
                BotApplication.status == "active",
                BotInstallTemplate.slug == template_slug,
                BotInstallTemplate.active.is_(True),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    app, template, bot = row
    return {
        "application": application_payload(app, bot),
        "template": {
            "id": str(template.id),
            "slug": template.slug,
            "name": template.name,
            "description": template.description,
            "scopes": template.scopes,
            "intents": template.intents,
            "permissions": str(template.permissions),
            "e2ee_mode": template.e2ee_mode,
            "generation": str(template.generation),
        },
    }


@router.post("/guilds/{guild_ref}/integrations/bots", status_code=201)
async def install_bot(
    guild_ref: EntityRef,
    application_ref: EntityRef,
    template_slug: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")],
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != settings.domain:
        federation_response = await signed_request(
            session,
            settings,
            "POST",
            guild_domain,
            f"/_kaede/v1/guilds/{guild_id}/bot-install",
            payload={
                "installer_id": str(auth.user.id),
                "application_ref": str(application_ref),
                "template_slug": template_slug,
            },
            request_timeout=15,
            max_response_bytes=64 * 1024,
        )
        if federation_response.status_code != 201:
            detail = {"code": "REMOTE_BOT_INSTALL_FAILED"}
            if federation_response.status_code in {403, 404, 409, 422, 429, 507}:
                raw = decode_federation_response_json(federation_response)
                if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                    detail = raw["detail"]
            raise HTTPException(status_code=federation_response.status_code, detail=detail)
        raw = decode_federation_response_json(federation_response)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=502, detail={"code": "REMOTE_BOT_INSTALL_INVALID"})
        return {str(key): value for key, value in raw.items()}
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, auth.user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    app_id, app_domain = application_ref.resolve(settings.domain)
    if app_domain != settings.domain:
        manifest = await fetch_bot_manifest(session, settings, app_id, app_domain, template_slug)
        await materialize_remote_manifest(session, manifest, settings)
    # Serialize the authorization decision and every following membership,
    # role, and installation mutation with kicks/bans/instance bans.  The
    # preliminary permission check above avoids fetching an untrusted remote
    # manifest for an unauthorized caller; permissions are re-evaluated after
    # the lock in case they changed while federation I/O was in flight.
    guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, auth.user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    mutation_signer = await locked_guild_mutation_signer(session, guild)
    row = (
        await session.execute(
            select(BotApplication, BotInstallTemplate, User)
            .join(
                BotInstallTemplate,
                (BotInstallTemplate.application_id == BotApplication.id)
                & (BotInstallTemplate.application_domain == BotApplication.origin_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == app_id,
                BotApplication.origin_domain == app_domain,
                BotApplication.status == "active",
                BotInstallTemplate.slug == template_slug,
                BotInstallTemplate.active.is_(True),
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
            .with_for_update(of=(BotApplication, BotInstallTemplate, User))
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    app, template, bot = row
    if bot.account_type != "bot" or bot.disabled_at is not None:
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    if await session.get(InstanceBlock, app.origin_domain) is not None:
        raise HTTPException(status_code=403, detail={"code": "BOT_INSTANCE_BLOCKED"})
    await ensure_bot_install_allowed(session, guild, bot)
    existing = await session.scalar(
        select(BotInstallation)
        .where(
            BotInstallation.application_id == app.id,
            BotInstallation.application_domain == app.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
        )
        .with_for_update()
    )
    if existing is not None and existing.status == "active":
        raise HTTPException(status_code=409, detail={"code": "BOT_ALREADY_INSTALLED"})
    if template.permissions & ~int(permissions):
        raise HTTPException(status_code=403, detail={"code": "CANNOT_GRANT_PERMISSIONS"})
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        mutation_signer,
        [existing] if existing is not None else [],
    )
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
    for existing_role in existing_roles:
        existing_role.position += 1
    member = await session.scalar(
        select(GuildMember)
        .where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == bot.id,
            GuildMember.user_domain == bot.origin_domain,
        )
        .with_for_update()
    )
    if member is None:
        member = GuildMember(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=bot.id,
            user_domain=bot.origin_domain,
            joined_at=datetime.now(UTC),
            member_version=1,
        )
        session.add(member)
    role_id = await snowflake.mint()
    role = Role(
        id=role_id,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name=app.name,
        permissions=template.permissions,
        position=1,
    )
    session.add(role)
    session.add(
        MemberRole(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=bot.id,
            user_domain=bot.origin_domain,
            role_id=role.id,
            role_domain=role.origin_domain,
        )
    )
    installation = existing or BotInstallation(
        id=await snowflake.mint(),
        application_id=app.id,
        application_domain=app.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        role_id=role.id,
        role_domain=role.origin_domain,
        installer_id=auth.user.id,
        installer_domain=auth.user.origin_domain,
        granted_scopes=template.scopes,
        granted_intents=template.intents,
        granted_permissions=template.permissions,
        e2ee_mode=template.e2ee_mode,
    )
    if existing is None:
        session.add(installation)
    else:
        installation.bot_user_id = bot.id
        installation.bot_user_domain = bot.origin_domain
        installation.role_id = role.id
        installation.role_domain = role.origin_domain
        installation.installer_id = auth.user.id
        installation.installer_domain = auth.user.origin_domain
        installation.granted_scopes = template.scopes
        installation.granted_intents = template.intents
        installation.granted_permissions = template.permissions
        installation.channel_restrictions = []
        installation.e2ee_mode = template.e2ee_mode
        installation.grant_revision += 1
        installation.status = "active"
        installation.revoked_at = None
    guild.permission_generation += 1
    member.member_version += 1
    await queue_guild_mutation(
        session,
        settings,
        guild,
        mutation_signer,
        "guild.role.create",
        {"role": role_payload(role)},
        snapshot_required=True,
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        mutation_signer,
        "guild.member.add",
        {"user": profile_from_user(bot), "joined_at": member.joined_at.isoformat()},
        pause_e2ee=False,
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        mutation_signer,
        "guild.member.role.add",
        {
            "user": {"id": str(bot.id), "origin_domain": bot.origin_domain},
            "role": {"id": str(role.id), "origin_domain": role.origin_domain},
            "member_version": str(member.member_version),
        },
        snapshot_required=True,
        pause_e2ee=False,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
    for existing_role in existing_roles:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_ROLE_UPDATE",
            role_payload(existing_role),
        )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_ROLE_CREATE", role_payload(role)
    )
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_MEMBER_ADD",
        member_payload(member, bot, [role.id]),
    )
    return {
        "id": str(installation.id),
        "status": installation.status,
        "application_ref": f"{app.id}@{app.origin_domain}",
        "guild_ref": f"{guild.id}@{guild.origin_domain}",
    }


@router.post("/bots/token")
async def create_bot_token(
    payload: WorkerTokenRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_keyed_rate_limit(
        redis, response, BOT_TOKEN_LIMIT, identity=str(payload.application_ref)
    )
    expected_audience = f"https://{settings.domain}/api/v1/bots/token"
    now = int(time.time())
    if (
        payload.audience != expected_audience
        or payload.expires_at - payload.issued_at > 60
        or not payload.issued_at - 60 <= now <= payload.expires_at
    ):
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    app_id, app_domain = payload.application_ref.resolve(settings.domain)
    if app_domain != settings.domain:
        await refresh_remote_worker_authorization(
            session, settings, app_id, app_domain, payload.worker_id
        )
    row = (
        await session.execute(
            select(BotWorker, BotApplication, User)
            .join(
                BotApplication,
                (BotApplication.id == BotWorker.application_id)
                & (BotApplication.origin_domain == BotWorker.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotWorker.id == payload.worker_id,
                BotWorker.application_id == app_id,
                BotWorker.application_domain == app_domain,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    worker, app, bot_user = row
    if bot_user.account_type != "bot" or bot_user.disabled_at is not None:
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    if worker.target_domains and settings.domain not in worker.target_domains:
        raise HTTPException(status_code=403, detail={"code": "BOT_TARGET_NOT_DELEGATED"})
    replay_digest = hashlib.sha256(payload.nonce.encode()).hexdigest()
    replay_key = f"bot:assertion:{app.origin_domain}:{worker.id}:{replay_digest}"
    if not await redis.set(replay_key, "1", ex=120, nx=True):
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_REPLAYED"})
    try:
        Ed25519PublicKey.from_public_bytes(worker.public_key).verify(
            decode_urlsafe(payload.signature, length=64),
            worker_assertion_message(
                str(payload.application_ref),
                worker.id,
                payload.audience,
                payload.issued_at,
                payload.expires_at,
                payload.nonce,
            ),
        )
    except (InvalidSignature, ValueError):
        await redis.delete(replay_key)
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"}) from None
    if not await session.scalar(
        select(
            active_installation_exists(
                application_id=app.id,
                application_domain=app.origin_domain,
                bot_user_id=bot_user.id,
                bot_user_domain=bot_user.origin_domain,
            )
        )
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    thumbprint = encode_urlsafe(hashlib.sha256(worker.public_key).digest())
    token, raw = await issue_bot_token(
        session,
        token_id=await snowflake.mint(),
        worker=worker,
        application=app,
        dpop_thumbprint=thumbprint,
    )
    await session.commit()
    return {
        "access_token": raw,
        "token_type": "Bot",
        "expires_in": max(1, int((token.expires_at - datetime.now(UTC)).total_seconds())),
        "dpop_thumbprint": thumbprint,
    }


class InstanceRulePut(BaseModel):
    effect: Literal["allow", "deny"]


@router.get("/applications/{application_ref}/instance-rules")
async def list_instance_rules(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, str]]:
    app, _, _ = await managed_application(session, settings, auth, application_ref)
    rules = list(
        await session.scalars(
            select(BotInstanceRule)
            .where(
                BotInstanceRule.application_id == app.id,
                BotInstanceRule.application_domain == app.origin_domain,
            )
            .order_by(BotInstanceRule.target_domain)
        )
    )
    return [{"target_domain": row.target_domain, "effect": row.effect} for row in rules]


@router.put("/applications/{application_ref}/instance-rules/{target_domain}")
async def put_instance_rule(
    application_ref: EntityRef,
    target_domain: str,
    payload: InstanceRulePut,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "security")
    target_domain = target_domain.rstrip(".").lower()
    if not DOMAIN_RE.fullmatch(target_domain) or target_domain == settings.domain:
        raise HTTPException(status_code=422, detail={"code": "INSTANCE_DOMAIN_INVALID"})
    rule = await session.get(BotInstanceRule, (app.id, app.origin_domain, target_domain))
    if rule is None:
        rule = BotInstanceRule(
            application_id=app.id,
            application_domain=app.origin_domain,
            target_domain=target_domain,
            effect=payload.effect,
        )
        session.add(rule)
    else:
        rule.effect = payload.effect
    app.manifest_generation += 1
    await session.commit()
    return {"target_domain": target_domain, "effect": payload.effect}


@router.delete("/applications/{application_ref}/instance-rules/{target_domain}", status_code=204)
async def delete_instance_rule(
    application_ref: EntityRef,
    target_domain: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "security")
    target_domain = target_domain.rstrip(".").lower()
    await session.execute(
        delete(BotInstanceRule).where(
            BotInstanceRule.application_id == app.id,
            BotInstanceRule.application_domain == app.origin_domain,
            BotInstanceRule.target_domain == target_domain,
        )
    )
    app.manifest_generation += 1
    await session.commit()
    return Response(status_code=204)


@router.get("/applications/{application_ref}/workers")
async def list_workers(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    app, _, _ = await managed_application(session, settings, auth, application_ref)
    workers = list(
        await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == app.id,
                BotWorker.application_domain == app.origin_domain,
            )
            .order_by(BotWorker.created_at.desc())
        )
    )
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "scopes": row.scopes,
            "intents": row.intents,
            "target_domains": row.target_domains,
            "generation": str(row.generation),
            "session_limit": row.session_limit,
            "created_at": row.created_at.isoformat(),
            "last_used_at": None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        }
        for row in workers
    ]


@router.delete("/applications/{application_ref}/workers/{worker_id}", status_code=204)
async def revoke_worker(
    application_ref: EntityRef,
    worker_id: int,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    app, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "security")
    worker = await session.get(BotWorker, worker_id, with_for_update=True)
    if worker is None or (worker.application_id, worker.application_domain) != (
        app.id,
        app.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    worker.revoked_at = datetime.now(UTC)
    worker.generation += 1
    app.revocation_generation += 1
    await session.commit()
    return Response(status_code=204)


@router.get("/applications/{application_ref}/install-templates")
async def list_templates(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    app, _, _ = await managed_application(session, settings, auth, application_ref)
    rows = list(
        await session.scalars(
            select(BotInstallTemplate)
            .where(
                BotInstallTemplate.application_id == app.id,
                BotInstallTemplate.application_domain == app.origin_domain,
            )
            .order_by(BotInstallTemplate.created_at.desc())
        )
    )
    return [
        {
            "id": str(row.id),
            "slug": row.slug,
            "name": row.name,
            "description": row.description,
            "scopes": row.scopes,
            "intents": row.intents,
            "permissions": str(row.permissions),
            "e2ee_mode": row.e2ee_mode,
            "active": row.active,
            "invite_url": f"https://{settings.domain}/applications/{app.id}@{app.origin_domain}/install/{row.slug}",
        }
        for row in rows
    ]


@router.get("/applications/{application_ref}/installations")
async def list_installations(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    app, _, _ = await managed_application(session, settings, auth, application_ref)
    rows = list(
        await session.scalars(
            select(BotInstallation)
            .where(
                BotInstallation.application_id == app.id,
                BotInstallation.application_domain == app.origin_domain,
            )
            .order_by(BotInstallation.created_at.desc())
            .limit(1000)
        )
    )
    return [
        {
            "id": str(row.id),
            "guild_ref": f"{row.guild_id}@{row.guild_domain}",
            "status": row.status,
            "scopes": row.granted_scopes,
            "intents": row.granted_intents,
            "permissions": str(row.granted_permissions),
            "e2ee_mode": row.e2ee_mode,
            "grant_revision": str(row.grant_revision),
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@federation_router.post("/_kaede/v1/guilds/{guild_id}/bot-install", status_code=201)
async def federation_install_bot(
    guild_id: int,
    payload: FederatedBotInstallRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-install",
        capacity=60,
        refill_per_minute=30,
    )
    try:
        installer_id = int(payload.installer_id)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "INSTALLER_INVALID"}) from None
    installer = await session.get(User, (installer_id, principal.origin))
    if installer is None or installer.account_type != "human":
        raise HTTPException(status_code=404, detail={"code": "INSTALLER_NOT_FOUND"})
    auth = AuthenticatedUser(
        user=installer,
        grant=cast(Any, None),
        access_token="",
        cookie_authenticated=False,
    )
    return await install_bot(
        EntityRef(f"{guild_id}@{settings.domain}"),
        payload.application_ref,
        payload.template_slug,
        auth,
        session,
        redis,
        snowflake,
        settings,
    )


async def _guild_bot_installation_payloads(
    session: AsyncSession, guild: Guild
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(BotInstallation, BotApplication, User)
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
                BotInstallation.guild_id == guild.id,
                BotInstallation.guild_domain == guild.origin_domain,
                BotInstallation.status != "revoked",
            )
            .order_by(func.lower(BotApplication.name), BotApplication.id)
        )
    ).all()
    return [
        {
            "id": str(installation.id),
            "application": application_payload(application, bot),
            "status": installation.status,
            "scopes": installation.granted_scopes,
            "intents": installation.granted_intents,
            "permissions": str(installation.granted_permissions),
            "e2ee_mode": installation.e2ee_mode,
            "installed_at": installation.installed_at.isoformat(),
        }
        for installation, application, bot in rows
    ]


@router.get("/guilds/{guild_ref}/integrations/bots")
async def list_guild_bot_integrations(
    guild_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, auth.user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    if guild_domain != settings.domain:
        upstream = await signed_request(
            session,
            settings,
            "GET",
            guild_domain,
            f"/_kaede/v1/guilds/{guild_id}/bot-installations",
            query={"user_id": str(auth.user.id)},
            request_timeout=15,
            max_response_bytes=512 * 1024,
        )
        if upstream.status_code != 200:
            raise HTTPException(
                status_code=upstream.status_code,
                detail={"code": "REMOTE_BOT_INSTALLATIONS_UNAVAILABLE"},
            )
        raw = decode_federation_response_json(upstream)
        if (
            not isinstance(raw, list)
            or len(raw) > 1000
            or any(not isinstance(item, dict) for item in raw)
        ):
            raise HTTPException(
                status_code=502, detail={"code": "REMOTE_BOT_INSTALLATIONS_INVALID"}
            )
        return [{str(key): value for key, value in item.items()} for item in raw]
    return await _guild_bot_installation_payloads(session, guild)


@federation_router.get("/_kaede/v1/guilds/{guild_id}/bot-installations")
async def federation_list_guild_bot_installations(
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
        redis, principal.origin, "bot-installation-list", capacity=120, refill_per_minute=60
    )
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail={"code": "USER_REF_INVALID"})
    user = await session.get(User, (int(user_id), principal.origin))
    guild = await session.get(Guild, (guild_id, settings.domain))
    if user is None or guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    return await _guild_bot_installation_payloads(session, guild)


async def _uninstall_bot_from_local_guild(
    guild: Guild,
    application_ref: EntityRef,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> None:
    permissions = await get_permissions(session, redis, guild, auth.user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    mutation_signer = await locked_guild_mutation_signer(session, guild)
    app_id, app_domain = application_ref.resolve(settings.domain)
    installation = await session.scalar(
        select(BotInstallation)
        .where(
            BotInstallation.application_id == app_id,
            BotInstallation.application_domain == app_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
        )
        .with_for_update()
    )
    if installation is None or installation.status == "revoked":
        return
    if (installation.bot_user_id, installation.bot_user_domain) == (
        guild.owner_id,
        guild.owner_domain,
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "OWNER_MUST_TRANSFER_OR_DELETE_GUILD"},
        )
    installation.status = "revoked"
    installation.revoked_at = datetime.now(UTC)
    installation.grant_revision += 1
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        mutation_signer,
        [installation],
    )
    member = await session.get(
        GuildMember,
        (
            guild.id,
            guild.origin_domain,
            installation.bot_user_id,
            installation.bot_user_domain,
        ),
    )
    removed_thread_members = []
    if member is not None:
        removed_thread_members = await cleanup_guild_member_threads(
            session,
            settings,
            guild,
            mutation_signer,
            [(installation.bot_user_id, installation.bot_user_domain)],
        )
        await clear_tracker_assignees(
            session,
            settings,
            guild,
            mutation_signer,
            [(installation.bot_user_id, installation.bot_user_domain)],
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            mutation_signer,
            "guild.member.remove",
            {
                "user": {
                    "id": str(installation.bot_user_id),
                    "origin_domain": installation.bot_user_domain,
                }
            },
            snapshot_required=True,
            pause_e2ee=False,
        )
        await session.delete(member)
    await session.commit()
    if member is not None:
        await wake_tracker_membership_cleanup(guild)
    else:
        await wake_queued_guild_federation(guild)
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
    await publish_guild_thread_member_cleanup(redis, guild, removed_thread_members)
    if member is not None:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_REMOVE",
            {
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "user_id": str(installation.bot_user_id),
                "user_domain": installation.bot_user_domain,
            },
        )


@router.delete(
    "/guilds/{guild_ref}/integrations/bots/{application_ref}",
    status_code=204,
)
async def uninstall_bot(
    guild_ref: EntityRef,
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != settings.domain:
        federation_response = await signed_request(
            session,
            settings,
            "DELETE",
            guild_domain,
            f"/_kaede/v1/guilds/{guild_id}/bot-install",
            payload={
                "installer_id": str(auth.user.id),
                "application_ref": str(application_ref),
            },
            request_timeout=15,
            max_response_bytes=64 * 1024,
        )
        if federation_response.status_code != 204:
            detail: dict[str, object] = {"code": "REMOTE_BOT_UNINSTALL_FAILED"}
            if federation_response.status_code in {403, 404, 409, 422, 429, 507}:
                raw = decode_federation_response_json(federation_response)
                if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                    detail = {str(key): value for key, value in raw["detail"].items()}
            raise HTTPException(status_code=federation_response.status_code, detail=detail)
        return Response(status_code=204)
    guild = await session.get(Guild, (guild_id, guild_domain), with_for_update=True)
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await _uninstall_bot_from_local_guild(guild, application_ref, auth, session, redis, settings)
    return Response(status_code=204)


@federation_router.delete(
    "/_kaede/v1/guilds/{guild_id}/bot-install",
    status_code=204,
)
async def federation_uninstall_bot(
    guild_id: int,
    payload: FederatedBotUninstallRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-uninstall", capacity=60, refill_per_minute=30
    )
    installer = await session.get(User, (int(payload.installer_id), principal.origin))
    if installer is None or installer.account_type != "human":
        raise HTTPException(status_code=404, detail={"code": "INSTALLER_NOT_FOUND"})
    guild = await session.get(Guild, (guild_id, settings.domain), with_for_update=True)
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    auth = AuthenticatedUser(
        user=installer, grant=cast(Any, None), access_token="", cookie_authenticated=False
    )
    await _uninstall_bot_from_local_guild(
        guild, payload.application_ref, auth, session, redis, settings
    )
    return Response(status_code=204)
