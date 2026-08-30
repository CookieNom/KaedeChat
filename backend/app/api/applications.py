from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ConfigDict, Field, HttpUrl, field_validator, model_validator
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
from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.auth.security import new_token, token_hash
from app.bots.application_contract import (
    DEVELOPER_TEAM_APPLICATION_LIMIT,
    SUPPORTED_APPLICATION_SCOPES,
    validate_application_https_url,
    validate_known_permission_mask,
)
from app.bots.auth import decode_urlsafe, encode_urlsafe, issue_bot_token, worker_assertion_message
from app.bots.command_contract import (
    CommandChoice,
    CommandDefinition,
    CommandOptionDefinition,
    CommandsPut,
    command_character_count,
)
from app.bots.developer_projection import (
    commit_developer_application_mutation,
    commit_developer_team_mutation,
)
from app.bots.directory_contract import (
    DirectoryDescriptionLocalizations,
    DirectoryExternalLinks,
    DirectoryMediaList,
    DirectorySupportedLocales,
    validate_directory_localizations,
)
from app.bots.dm_capability import usable_dm_capability
from app.bots.e2ee import revoke_bot_e2ee_access, revoke_bot_e2ee_devices
from app.bots.install_config import (
    GUILD_INSTALL,
    REQUIRED_USER_INSTALL_SCOPES,
    SUPPORTED_INSTALL_TYPES,
    USER_INSTALL,
    USER_INSTALL_CONTEXTS,
    USER_INSTALL_SCOPES,
    integration_types_config,
)
from app.bots.installations import (
    active_standard_installation_exists,
    cleanup_installation_roles,
    publish_deleted_installation_roles,
    qualified_channel_restrictions,
    queue_installation_gateway_events,
    usable_guild_installation,
)
from app.bots.target_contract import target_policy_allows
from app.bots.target_discovery import (
    queue_application_target_snapshot,
    queue_application_target_snapshots_for_refs,
    require_application_runtime_enabled,
    wake_application_target_deliveries,
)
from app.bots.worker_targets import worker_target_allowed
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import (
    guild_authority_owner,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import member_payload, role_payload, user_payload
from app.chat.permissions import get_permissions
from app.chat.thread_membership import (
    cleanup_guild_member_threads,
    publish_guild_thread_member_cleanup,
)
from app.core.bot_intents import SUPPORTED_BOT_INTENTS
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import Permission
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import DOMAIN_RE, Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef, WireSnowflake
from app.db.bot_models import (
    ApplicationCommand,
    BotApplication,
    BotApplicationTarget,
    BotCredential,
    BotDMCapability,
    BotInstallation,
    BotInstallTemplate,
    BotInstanceRule,
    BotWorker,
    DeveloperTeam,
    DeveloperTeamMember,
)
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Ban,
    Channel,
    Guild,
    GuildInstanceBan,
    GuildMember,
    InstanceBlock,
    MemberRole,
    Role,
    User,
)
from app.federation.actor_intents import worker_actor_runtime_revision
from app.federation.application_management import (
    application_management_dict_body,
    application_management_list_body,
    proxy_remote_application_management,
    require_application_management_empty,
)
from app.federation.client import signed_request
from app.federation.developer_management import (
    developer_management_dict_body,
    developer_management_list_body,
    proxy_remote_developer_management,
    require_developer_management_empty,
)
from app.federation.network import decode_federation_response_json
from app.federation.replication import profile_from_user
from app.federation.schemas import SnowflakeString
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)
from app.tracker.membership import clear_tracker_assignees, wake_tracker_membership_cleanup

__all__ = [
    "CommandChoice",
    "CommandDefinition",
    "CommandOptionDefinition",
    "CommandsPut",
    "command_character_count",
]

router = APIRouter(prefix="/api/v1", tags=["applications"])
federation_router = APIRouter(tags=["bot install federation"])

APPLICATION_CREATE_LIMIT = ClientRateLimit("application-create", 10, 3600)
BOT_INVITE_LIMIT = ClientRateLimit("bot-invite", 30, 60)
BOT_TOKEN_LIMIT = ClientRateLimit("bot-token", 30, 60)
BOT_CONTROL_LIMIT = ClientRateLimit("bot-control", 120, 60)
SLUG_CLEAN_RE = re.compile(r"[^a-z0-9_.]+")

SUPPORTED_SCOPES = SUPPORTED_APPLICATION_SCOPES
SUPPORTED_INTENTS = SUPPORTED_BOT_INTENTS
CONTROL_SCOPES = frozenset({"workers.manage", "commands.manage"})


def normalize_values(values: list[str], supported: frozenset[str], label: str) -> list[str]:
    normalized = list(dict.fromkeys(values))
    invalid = sorted(set(normalized) - supported)
    if invalid:
        raise ValueError(f"unsupported {label}: {', '.join(invalid)}")
    return normalized


def default_guild_contexts() -> list[Literal["guild"]]:
    return ["guild"]


def application_https_url(value: HttpUrl | None) -> HttpUrl | None:
    if value is not None:
        validate_application_https_url(str(value))
    return value


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


class ApplicationCreate(UnambiguousInputModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    support_url: HttpUrl | None = None
    privacy_url: HttpUrl | None = None
    terms_url: HttpUrl | None = None
    team_ref: EntityRef | None = None

    _valid_https_url = field_validator("support_url", "privacy_url", "terms_url")(
        application_https_url
    )


class DeveloperTeamCreate(UnambiguousInputModel):
    name: str = Field(min_length=1, max_length=100)


class DeveloperTeamMemberPut(UnambiguousInputModel):
    user_ref: EntityRef
    role: Literal["owner", "administrator", "developer", "security", "analyst", "support"]


class DeveloperTeamMemberPatch(UnambiguousInputModel):
    role: Literal["owner", "administrator", "developer", "security", "analyst", "support"]


class ApplicationPatch(UnambiguousInputModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    support_url: HttpUrl | None = None
    privacy_url: HttpUrl | None = None
    terms_url: HttpUrl | None = None
    directory_enabled: bool | None = None
    directory_summary: str | None = Field(default=None, max_length=200)
    directory_category: (
        Literal["entertainment", "games", "moderation", "productivity", "social", "utilities"]
        | None
    ) = None
    directory_tags: list[str] | None = Field(default=None, max_length=5)
    directory_media: DirectoryMediaList | None = None
    directory_external_links: DirectoryExternalLinks | None = None
    directory_supported_locales: DirectorySupportedLocales | None = None
    directory_description_localizations: DirectoryDescriptionLocalizations | None = None
    target_policy: Literal["open", "allowlist", "blocklist", "local_only"] | None = None
    default_scopes: list[str] | None = Field(default=None, max_length=64)
    default_intents: list[str] | None = Field(default=None, max_length=32)
    default_permissions: WireSnowflake | None = None
    supported_install_types: list[str] | None = Field(default=None, min_length=1, max_length=2)
    user_install_scopes: list[str] | None = Field(default=None, min_length=2, max_length=4)
    user_install_contexts: list[str] | None = Field(default=None, min_length=1, max_length=3)
    e2ee_modes: list[Literal["participant"]] | None = Field(default=None, max_length=1)

    _valid_https_url = field_validator("support_url", "privacy_url", "terms_url")(
        application_https_url
    )

    @field_validator("directory_tags")
    @classmethod
    def valid_directory_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = list(dict.fromkeys(item.strip().lower() for item in value))
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", item) for item in normalized):
            raise ValueError("directory tags must be lowercase slugs")
        return normalized

    @model_validator(mode="after")
    def coherent_directory_metadata(self) -> ApplicationPatch:
        required_fields = {
            "directory_enabled",
            "directory_tags",
            "directory_media",
            "directory_external_links",
            "directory_supported_locales",
            "directory_description_localizations",
        }
        if any(
            field in self.model_fields_set and getattr(self, field) is None
            for field in required_fields
        ):
            raise ValueError("non-null directory metadata must not be null")
        if (
            self.directory_supported_locales is not None
            and self.directory_description_localizations is not None
        ):
            validate_directory_localizations(
                list(self.directory_supported_locales),
                dict(self.directory_description_localizations),
            )
        return self

    @field_validator("default_permissions")
    @classmethod
    def valid_default_permissions(cls, value: int | None) -> int | None:
        if value is not None:
            validate_known_permission_mask(value, label="default permissions")
        return value

    @field_validator("default_scopes")
    @classmethod
    def valid_scopes(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_values(value, SUPPORTED_SCOPES, "scope")

    @field_validator("default_intents")
    @classmethod
    def valid_intents(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_values(value, SUPPORTED_INTENTS, "intent")

    @field_validator("supported_install_types")
    @classmethod
    def valid_install_types(cls, value: list[str] | None) -> list[str] | None:
        return (
            None
            if value is None
            else normalize_values(value, SUPPORTED_INSTALL_TYPES, "install type")
        )

    @field_validator("user_install_scopes")
    @classmethod
    def valid_user_install_scopes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        scopes = normalize_values(value, USER_INSTALL_SCOPES, "user-install scope")
        if not set(scopes) >= REQUIRED_USER_INSTALL_SCOPES:
            raise ValueError("user-install scopes must include command and response access")
        return scopes

    @field_validator("user_install_contexts")
    @classmethod
    def valid_user_install_contexts(cls, value: list[str] | None) -> list[str] | None:
        return (
            None
            if value is None
            else normalize_values(value, USER_INSTALL_CONTEXTS, "user-install context")
        )


class CredentialCreate(UnambiguousInputModel):
    label: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(
        default_factory=lambda: ["workers.manage", "commands.manage"], max_length=8
    )

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        return normalize_values(value, CONTROL_SCOPES, "control scope")


class WorkerCreate(UnambiguousInputModel):
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


class TemplateCreate(UnambiguousInputModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    scopes: list[str] = Field(default_factory=list, max_length=64)
    intents: list[str] = Field(default_factory=list, max_length=32)
    permissions: WireSnowflake = 0
    contexts: list[Literal["guild"]] = Field(
        default_factory=default_guild_contexts, min_length=1, max_length=1
    )
    e2ee_mode: Literal["disabled", "participant"] = "disabled"

    @field_validator("permissions")
    @classmethod
    def valid_permissions(cls, value: int) -> int:
        return validate_known_permission_mask(value, label="template permissions")

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        return normalize_values(value, SUPPORTED_SCOPES, "scope")

    @field_validator("intents")
    @classmethod
    def valid_intents(cls, value: list[str]) -> list[str]:
        return normalize_values(value, SUPPORTED_INTENTS, "intent")


def validate_command_set(payload: CommandsPut) -> None:
    keys = [(item.type, item.name) for item in payload.commands]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail={"code": "COMMAND_NAME_DUPLICATE"})
    if len(payload.model_dump_json().encode()) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail={"code": "COMMAND_SET_TOO_LARGE"})


def validate_command_install_types(
    payload: CommandsPut, supported_install_types: list[str]
) -> None:
    supported = set(supported_install_types)
    for command in payload.commands:
        unsupported = sorted(set(command.integration_types) - supported)
        if unsupported:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "COMMAND_INSTALL_TYPE_NOT_CONFIGURED",
                    "command": command.name,
                    "install_types": unsupported,
                },
            )


async def replace_application_commands(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    application: BotApplication,
    payload: CommandsPut,
    *,
    guild_ref: tuple[int, str] | None = None,
) -> dict[str, object]:
    """Atomically upsert one global or guild command scope."""

    validate_command_set(payload)
    validate_command_install_types(payload, application.supported_install_types)
    definitions = payload.commands
    if guild_ref is not None:
        normalized: list[CommandDefinition] = []
        for definition in definitions:
            if ("contexts" in definition.model_fields_set and definition.contexts != ["guild"]) or (
                "integration_types" in definition.model_fields_set
                and definition.integration_types != ["guild_install"]
            ):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "GUILD_COMMAND_CONTEXT_INVALID"},
                )
            normalized.append(
                definition.model_copy(
                    update={
                        "contexts": ["guild"],
                        "integration_types": ["guild_install"],
                    }
                )
            )
        definitions = normalized
    application.command_generation += 1
    scope_clause = (
        ApplicationCommand.guild_id.is_(None)
        if guild_ref is None
        else (
            (ApplicationCommand.guild_id == guild_ref[0])
            & (ApplicationCommand.guild_domain == guild_ref[1])
        )
    )
    existing = {
        (command.type, command.name): command
        for command in await session.scalars(
            select(ApplicationCommand).where(
                ApplicationCommand.application_id == application.id,
                ApplicationCommand.application_domain == application.origin_domain,
                scope_clause,
            )
        )
    }
    user_install_availability_changed = guild_ref is None and (
        any(
            command.state == "active" and USER_INSTALL in command.integration_types
            for command in existing.values()
        )
        != any(USER_INSTALL in definition.integration_types for definition in definitions)
    )
    stored: list[ApplicationCommand] = []
    for definition in definitions:
        command = existing.pop((definition.type, definition.name), None)
        if command is None:
            command = ApplicationCommand(
                id=await snowflake.mint(),
                application_id=application.id,
                application_domain=application.origin_domain,
                guild_id=guild_ref[0] if guild_ref is not None else None,
                guild_domain=guild_ref[1] if guild_ref is not None else None,
                name=definition.name,
                type=definition.type,
                definition={},
                generation=application.command_generation,
            )
            session.add(command)
        command.definition = definition.model_dump(mode="json")
        command.contexts = list(definition.contexts)
        command.integration_types = list(definition.integration_types)
        command.generation = application.command_generation
        command.state = "active"
        stored.append(command)
    if existing:
        await session.execute(
            delete(ApplicationCommand).where(
                ApplicationCommand.id.in_([command.id for command in existing.values()])
            )
        )
    if user_install_availability_changed:
        application.directory_approved = False
    await commit_developer_application_mutation(session, settings, application)
    return {
        "generation": str(application.command_generation),
        "commands": len(stored),
        "items": [
            {
                "id": str(command.id),
                "origin_domain": application.origin_domain,
                "ref": f"{command.id}@{application.origin_domain}",
                "application_ref": f"{application.id}@{application.origin_domain}",
                "guild_ref": (f"{guild_ref[0]}@{guild_ref[1]}" if guild_ref is not None else None),
                **command.definition,
            }
            for command in stored
        ],
    }


class FederatedGuildCommand(CommandDefinition):
    id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")


class FederatedGuildCommandsPut(UnambiguousInputModel):
    generation: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    commands: list[FederatedGuildCommand] = Field(max_length=130)

    @model_validator(mode="after")
    def valid_set(self) -> FederatedGuildCommandsPut:
        CommandsPut(commands=self.commands)
        ids = [command.id for command in self.commands]
        if len(ids) != len(set(ids)):
            raise ValueError("guild command IDs must be unique")
        if any(
            command.contexts != ["guild"] or command.integration_types != ["guild_install"]
            for command in self.commands
        ):
            raise ValueError("guild commands have fixed contexts")
        return self


class FederatedBotApplicationRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    installer_id: SnowflakeString
    application_ref: EntityRef

    @field_validator("application_ref")
    @classmethod
    def qualified_application_ref(cls, value: EntityRef) -> EntityRef:
        if value.domain is None:
            raise ValueError("federated bot application reference must be qualified")
        return value


class FederatedBotInstallRequest(FederatedBotApplicationRequest):
    template_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")


class FederatedBotUninstallRequest(FederatedBotApplicationRequest):
    pass


class BotChannelRestrictionsUpdate(UnambiguousInputModel):
    """Target-guild-owned installation channel ceiling.

    An empty set means that the installation is not further restricted beyond
    its managed role and live channel overwrites.  Concrete channel references
    are deliberately not part of portable application install templates: only
    the target guild authority can validate and own them.
    """

    channel_restrictions: list[EntityRef] = Field(max_length=500)


class FederatedBotRestrictionsUpdate(FederatedBotApplicationRequest):
    channel_restrictions: list[EntityRef] = Field(max_length=500)

    @field_validator("channel_restrictions")
    @classmethod
    def qualified_channel_refs(cls, value: list[EntityRef]) -> list[EntityRef]:
        if any(item.domain is None for item in value):
            raise ValueError("federated channel restriction references must be qualified")
        if len({str(item) for item in value}) != len(value):
            raise ValueError("channel restriction references must be unique")
        return value


class _FederatedBotInstallationResult(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    status: Literal["active", "suspended"]
    application_ref: EntityRef
    guild_ref: EntityRef
    channel_restrictions: list[EntityRef] = Field(max_length=500)
    grant_revision: SnowflakeString

    @field_validator("application_ref", "guild_ref")
    @classmethod
    def qualified_result_ref(cls, value: EntityRef) -> EntityRef:
        if value.domain is None:
            raise ValueError("federated bot install result references must be qualified")
        return value

    @field_validator("channel_restrictions")
    @classmethod
    def qualified_result_channel_refs(cls, value: list[EntityRef]) -> list[EntityRef]:
        if any(item.domain is None for item in value):
            raise ValueError("federated bot install result references must be qualified")
        if len({str(item) for item in value}) != len(value):
            raise ValueError("federated bot install result repeats a channel restriction")
        return value


class FederatedBotInstallResult(_FederatedBotInstallationResult):
    status: Literal["active"]


class FederatedBotRestrictionsResult(_FederatedBotInstallationResult):
    pass


class FederatedGuildBotInstallation(UnambiguousInputModel):
    """Strict peer response for one target-owned guild installation."""

    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    ref: EntityRef
    guild_ref: EntityRef
    application_ref: EntityRef
    application: dict[str, object]
    status: Literal["active", "suspended"]
    scopes: list[str] = Field(max_length=64)
    intents: list[str] = Field(max_length=32)
    permissions: SnowflakeString
    channel_restrictions: list[EntityRef] = Field(max_length=500)
    e2ee_mode: Literal["disabled", "participant"]
    grant_revision: SnowflakeString
    installed_at: datetime

    @model_validator(mode="after")
    def coherent_identity(self) -> FederatedGuildBotInstallation:
        refs = (self.ref, self.guild_ref, self.application_ref, *self.channel_restrictions)
        if any(item.domain is None for item in refs):
            raise ValueError("federated guild installation references must be qualified")
        if self.ref.id != int(self.id):
            raise ValueError("federated installation ID and reference disagree")
        if len({str(item) for item in self.channel_restrictions}) != len(self.channel_restrictions):
            raise ValueError("federated guild installation repeats a channel restriction")
        app_ref = self.application.get("ref")
        app_id = self.application.get("id")
        app_domain = self.application.get("origin_domain")
        if (
            app_ref != str(self.application_ref)
            or str(app_id) != str(self.application_ref.id)
            or app_domain != self.application_ref.domain
        ):
            raise ValueError("federated guild installation application identity disagrees")
        bot_user = self.application.get("bot_user")
        if not isinstance(bot_user, dict) or bot_user.get("origin_domain") != app_domain:
            raise ValueError("federated guild installation bot authority disagrees")
        return self


async def federated_human_installer(
    session: AsyncSession,
    principal: FederationPrincipal,
    installer_id: SnowflakeString,
    *,
    require_mutation_admission: bool = False,
) -> User:
    """Resolve one active remote human exactly bound to the signed peer."""

    installer = await session.get(User, (int(installer_id), principal.origin))
    if (
        installer is None
        or installer.is_local
        or installer.origin_domain != principal.origin
        or installer.account_type != "human"
        or installer.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "INSTALLER_NOT_FOUND"})
    if require_mutation_admission:
        await require_remote_user_creation_allowed(session, installer)
    return installer


class WorkerTokenRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    application_ref: EntityRef
    worker_id: int = Field(ge=0)
    audience: str = Field(min_length=1, max_length=2048)
    issued_at: int
    expires_at: int
    nonce: str = Field(min_length=16, max_length=128)
    signature: str = Field(min_length=86, max_length=88)
    dm_capability_grant_id: str | None = Field(
        default=None,
        pattern=r"^kbdg_[A-Za-z0-9_-]{43}$",
    )
    dm_capability_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def complete_dm_capability_binding(self) -> WorkerTokenRequest:
        if (self.dm_capability_grant_id is None) != (self.dm_capability_revision is None):
            raise ValueError("worker DM capability binding is incomplete")
        return self


async def authenticated_worker_assertion(
    payload: WorkerTokenRequest,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    *,
    expected_audience: str,
    replay_scope: str,
    local_application_only: bool = False,
    require_target_delegation: bool = False,
) -> tuple[BotWorker, BotApplication, User]:
    """Verify one short-lived worker proof against its application authority.

    Runtime targets may refresh a remote worker authorization. Application-home
    control surfaces set ``local_application_only`` so an instance cannot be
    tricked into acting as another application's control plane.
    """

    now = int(time.time())
    if (
        payload.audience != expected_audience
        or payload.expires_at - payload.issued_at > 60
        or not payload.issued_at - 60 <= now <= payload.expires_at
    ):
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    app_id, app_domain = payload.application_ref.resolve(settings.domain)
    if local_application_only and app_domain != settings.domain:
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    asserted_capability: BotDMCapability | None = None
    if payload.dm_capability_grant_id is not None:
        asserted_capability = await session.scalar(
            select(BotDMCapability).where(
                BotDMCapability.grant_id == payload.dm_capability_grant_id,
                BotDMCapability.revision == payload.dm_capability_revision,
                BotDMCapability.application_id == app_id,
                BotDMCapability.application_domain == app_domain,
                BotDMCapability.authority_domain == settings.domain,
                BotDMCapability.conversation_domain == settings.domain,
                usable_dm_capability(at=datetime.now(UTC)),
            )
        )
        if asserted_capability is None:
            raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    if app_domain != settings.domain:
        await refresh_remote_worker_authorization(
            session,
            settings,
            snowflake,
            app_id,
            app_domain,
            payload.worker_id,
            dm_capability_grant_id=payload.dm_capability_grant_id,
            dm_capability_revision=payload.dm_capability_revision,
        )
    worker_identity_filter = (
        BotWorker.id == payload.worker_id
        if app_domain == settings.domain
        else ((BotWorker.source_id == payload.worker_id) & (BotWorker.source_domain == app_domain))
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
                worker_identity_filter,
                BotWorker.application_id == app_id,
                BotWorker.application_domain == app_domain,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
            # Serialize token minting with durable runtime-control snapshots.
            # If mint wins, the snapshot revokes the new row; if the snapshot
            # wins, this transaction refreshes the complete authority state
            # before issuing a token.
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    worker, application, bot_user = row
    if asserted_capability is not None and (
        asserted_capability.bot_user_id != bot_user.id
        or asserted_capability.bot_user_domain != bot_user.origin_domain
    ):
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    if require_target_delegation and not worker_target_allowed(
        worker.target_domains,
        application_domain=application.origin_domain,
        target_domain=settings.domain,
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_TARGET_NOT_DELEGATED"})
    replay_digest = hashlib.sha256(payload.nonce.encode()).hexdigest()
    replay_key = (
        f"bot:assertion:{replay_scope}:{application.origin_domain}:"
        f"{worker.authority_id}:{replay_digest}"
    )
    if not await redis.set(replay_key, "1", ex=120, nx=True):
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_REPLAYED"})
    try:
        Ed25519PublicKey.from_public_bytes(worker.public_key).verify(
            decode_urlsafe(payload.signature, length=64),
            worker_assertion_message(
                str(payload.application_ref),
                worker.authority_id,
                payload.audience,
                payload.issued_at,
                payload.expires_at,
                payload.nonce,
                dm_capability_grant_id=payload.dm_capability_grant_id,
                dm_capability_revision=payload.dm_capability_revision,
            ),
        )
    except (InvalidSignature, ValueError):
        await redis.delete(replay_key)
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"}) from None
    return worker, application, bot_user


def qualified_application_ref(application: BotApplication) -> str:
    return f"{application.id}@{application.origin_domain}"


def application_resource_identity(
    application: BotApplication,
    resource_id: int,
) -> dict[str, str]:
    return {
        "id": str(resource_id),
        "ref": f"{resource_id}@{application.origin_domain}",
        "application_ref": qualified_application_ref(application),
    }


def application_payload(application: BotApplication, bot: User) -> dict[str, object]:
    return {
        "id": str(application.id),
        "origin_domain": application.origin_domain,
        "ref": qualified_application_ref(application),
        "team_ref": f"{application.team_id}@{application.team_domain}",
        "name": application.name,
        "description": application.description,
        "directory_enabled": application.directory_enabled,
        "directory_approved": application.directory_approved,
        "directory_summary": application.directory_summary,
        "directory_category": application.directory_category,
        "directory_tags": list(application.directory_tags),
        "directory_collections": list(application.directory_collections),
        "directory_media": list(application.directory_media or []),
        "directory_external_links": list(application.directory_external_links or []),
        "directory_supported_locales": list(application.directory_supported_locales or []),
        "directory_description_localizations": dict(
            application.directory_description_localizations or {}
        ),
        "icon_hash": application.icon_hash,
        "banner_hash": application.banner_hash,
        "support_url": application.support_url,
        "privacy_url": application.privacy_url,
        "terms_url": application.terms_url,
        "status": application.status,
        "target_policy": application.target_policy,
        "default_scopes": application.default_scopes,
        "default_intents": application.default_intents,
        "default_permissions": str(application.default_permissions),
        "supported_install_types": list(application.supported_install_types),
        "user_install_scopes": list(application.user_install_scopes),
        "user_install_contexts": list(application.user_install_contexts),
        "integration_types_config": integration_types_config(
            supported_install_types=application.supported_install_types,
            guild_scopes=application.default_scopes,
            guild_permissions=application.default_permissions,
            user_scopes=application.user_install_scopes,
            user_contexts=application.user_install_contexts,
        ),
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
    session: AsyncSession,
    settings: Settings,
    auth: AuthenticatedUser,
    ref: EntityRef,
    *,
    for_update: bool = False,
) -> tuple[BotApplication, DeveloperTeamMember, User]:
    app_id, app_domain = ref.resolve(settings.domain)
    statement = (
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
    if for_update:
        statement = statement.with_for_update(of=BotApplication)
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    return row[0], row[1], row[2]


async def control_application(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    ref: EntityRef,
    required_scope: str,
    *,
    for_update: bool = False,
) -> tuple[BotApplication, BotCredential]:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("BotControl "):
        raise HTTPException(status_code=401, detail={"code": "BOT_CONTROL_AUTH_REQUIRED"})
    raw = authorization.removeprefix("BotControl ")
    if not raw.startswith("kb1_ctl_") or len(raw) > 160:
        raise HTTPException(status_code=401, detail={"code": "BOT_CONTROL_TOKEN_INVALID"})
    app_id, app_domain = ref.resolve(settings.domain)
    now = datetime.now(UTC)
    statement = (
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
    if for_update:
        statement = statement.with_for_update(of=BotApplication)
    row = (await session.execute(statement)).one_or_none()
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


async def require_team_application_capacity(
    session: AsyncSession,
    team: DeveloperTeam,
) -> None:
    """Serialize app creation and enforce Discord's per-team capacity."""

    locked_team = await session.scalar(
        select(DeveloperTeam)
        .where(
            DeveloperTeam.id == team.id,
            DeveloperTeam.origin_domain == team.origin_domain,
        )
        .with_for_update()
    )
    if locked_team is None:
        raise HTTPException(status_code=404, detail={"code": "DEVELOPER_TEAM_NOT_FOUND"})
    application_count = int(
        await session.scalar(
            select(func.count(BotApplication.id)).where(
                BotApplication.team_id == team.id,
                BotApplication.team_domain == team.origin_domain,
                BotApplication.status != "deleted",
            )
        )
        or 0
    )
    if application_count >= DEVELOPER_TEAM_APPLICATION_LIMIT:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DEVELOPER_TEAM_APPLICATION_LIMIT_REACHED",
                "limit": DEVELOPER_TEAM_APPLICATION_LIMIT,
            },
        )


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
    remote = await proxy_remote_developer_management(
        session,
        settings,
        team_ref,
        auth.user,
        "member.list",
    )
    if remote is not None:
        return cast(list[dict[str, object]], developer_management_list_body(remote))
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
    user_id, user_domain = payload.user_ref.resolve(settings.domain)
    user = await session.get(User, (user_id, user_domain))
    if user is None or user.account_type != "human" or user.disabled_at is not None:
        raise HTTPException(status_code=404, detail={"code": "TEAM_MEMBER_NOT_FOUND"})
    remote = await proxy_remote_developer_management(
        session,
        settings,
        team_ref,
        auth.user,
        "member.add",
        {
            "data": {
                **payload.model_dump(mode="json"),
                "user_ref": f"{user.id}@{user.origin_domain}",
            },
            "target": profile_from_user(user),
        },
    )
    if remote is not None:
        return developer_management_dict_body(remote)
    team, actor = await managed_team(session, settings, auth, team_ref)
    if team.personal:
        raise HTTPException(status_code=409, detail={"code": "PERSONAL_TEAM_MEMBERS_IMMUTABLE"})
    require_team_role(actor, "owner", "administrator")
    if payload.role == "owner" and actor.role != "owner":
        raise HTTPException(status_code=403, detail={"code": "TEAM_OWNER_REQUIRED"})
    member = await session.get(
        DeveloperTeamMember, (team.id, team.origin_domain, user.id, user.origin_domain)
    )
    if member is None:
        member = DeveloperTeamMember(
            team_id=team.id,
            team_domain=team.origin_domain,
            user_id=user.id,
            user_domain=user.origin_domain,
            user_is_local=user.is_local,
            role=payload.role,
        )
        session.add(member)
    else:
        if member.role == "owner" and payload.role != "owner":
            await _preserve_team_owner(session, member)
        member.role = payload.role
    await commit_developer_team_mutation(session, settings, team)
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
    user_id, user_domain = user_ref.resolve(settings.domain)
    remote = await proxy_remote_developer_management(
        session,
        settings,
        team_ref,
        auth.user,
        "member.update",
        {
            "user_ref": f"{user_id}@{user_domain}",
            "data": payload.model_dump(mode="json"),
        },
    )
    if remote is not None:
        return developer_management_dict_body(remote)
    team, actor = await managed_team(session, settings, auth, team_ref)
    if team.personal:
        raise HTTPException(status_code=409, detail={"code": "PERSONAL_TEAM_MEMBERS_IMMUTABLE"})
    require_team_role(actor, "owner", "administrator")
    if payload.role == "owner" and actor.role != "owner":
        raise HTTPException(status_code=403, detail={"code": "TEAM_OWNER_REQUIRED"})
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
    await commit_developer_team_mutation(session, settings, team)
    return {"user": user_payload(user), "role": member.role}


@router.delete("/developer-teams/{team_ref}/members/{user_ref}", status_code=204)
async def remove_developer_team_member(
    team_ref: EntityRef,
    user_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    user_id, user_domain = user_ref.resolve(settings.domain)
    remote = await proxy_remote_developer_management(
        session,
        settings,
        team_ref,
        auth.user,
        "member.remove",
        {"user_ref": f"{user_id}@{user_domain}"},
    )
    if remote is not None:
        require_developer_management_empty(remote)
        return Response(status_code=204)
    team, actor = await managed_team(session, settings, auth, team_ref)
    if team.personal:
        raise HTTPException(status_code=409, detail={"code": "PERSONAL_TEAM_MEMBERS_IMMUTABLE"})
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
    removed_user = await session.get(User, (member.user_id, member.user_domain))
    if removed_user is None:
        raise RuntimeError("developer team member identity is unavailable")
    if member.role == "owner" and actor.role != "owner":
        raise HTTPException(status_code=403, detail={"code": "TEAM_OWNER_REQUIRED"})
    await _preserve_team_owner(session, member)
    await session.delete(member)
    await commit_developer_team_mutation(
        session,
        settings,
        team,
        revoked_members=(removed_user,),
    )
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
    if auth.user.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "LOCAL_HUMAN_ACCOUNT_REQUIRED"})
    await enforce_keyed_rate_limit(
        redis,
        response,
        APPLICATION_CREATE_LIMIT,
        identity=f"{auth.user.origin_domain}:{auth.user.id}",
    )
    if payload.team_ref is not None:
        team_id, team_domain = payload.team_ref.resolve(settings.domain)
        remote = await proxy_remote_developer_management(
            session,
            settings,
            payload.team_ref,
            auth.user,
            "application.create",
            {
                "data": {
                    **payload.model_dump(mode="json"),
                    "team_ref": f"{team_id}@{team_domain}",
                }
            },
        )
        if remote is not None:
            return developer_management_dict_body(remote)
    selected_team: DeveloperTeam
    selected_team_member: DeveloperTeamMember
    if payload.team_ref is not None:
        selected_team, selected_team_member = await managed_team(
            session, settings, auth, payload.team_ref
        )
        require_team_role(selected_team_member, "owner", "administrator", "developer")
    else:
        selected_team, selected_team_member = await ensure_personal_developer_team(
            session, settings, auth, snowflake
        )
    await require_team_application_capacity(session, selected_team)
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
        terms_url=str(payload.terms_url) if payload.terms_url else None,
    )
    # BotApplication's composite foreign key is scalar-only, so SQLAlchemy has
    # no relationship edge from which to infer INSERT ordering. Persist the bot
    # identity first; the following projection queries may otherwise autoflush
    # the application before its referenced user exists.
    session.add(bot)
    await session.flush()
    session.add(application)
    await commit_developer_team_mutation(session, settings, selected_team)
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
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "application.get"
    )
    if remote is not None:
        return application_management_dict_body(remote)
    app, _, bot = await managed_application(session, settings, auth, application_ref)
    return application_payload(app, bot)


@router.get("/applications/{application_ref}/directory-preview")
async def get_application_directory_preview(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "application.directory_preview",
    )
    if remote is not None:
        return application_management_dict_body(remote)
    app, _, _ = await managed_application(session, settings, auth, application_ref)
    from app.api.application_directory import directory_preview_response

    return await directory_preview_response(session, settings, app)


@router.patch("/applications/{application_ref}")
async def patch_application(
    application_ref: EntityRef,
    payload: ApplicationPatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "application.update",
        {"data": payload.model_dump(mode="json", exclude_unset=True)},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    app, member, bot = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
    require_team_role(member, "owner", "administrator")
    values = payload.model_dump(exclude_unset=True)
    if "directory_media" in values and payload.directory_media is not None:
        values["directory_media"] = [
            item.model_dump(mode="json") for item in payload.directory_media
        ]
    if "directory_external_links" in values and payload.directory_external_links is not None:
        values["directory_external_links"] = [
            item.model_dump(mode="json") for item in payload.directory_external_links
        ]
    supported_locales = cast(
        list[str], values.get("directory_supported_locales", app.directory_supported_locales)
    )
    description_localizations = cast(
        dict[str, str],
        values.get(
            "directory_description_localizations",
            app.directory_description_localizations,
        ),
    )
    try:
        validate_directory_localizations(supported_locales, description_localizations)
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail={"code": "APPLICATION_DIRECTORY_LOCALIZATIONS_INVALID"},
        ) from None
    from app.api.application_directory import directory_media_assets_valid

    if not await directory_media_assets_valid(
        session,
        app,
        values.get("directory_media", app.directory_media),
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "APPLICATION_DIRECTORY_MEDIA_INVALID"},
        )
    configured_install_types = cast(
        list[str], values.get("supported_install_types", app.supported_install_types)
    )
    configured_default_scopes = cast(list[str], values.get("default_scopes", app.default_scopes))
    configured_default_intents = cast(list[str], values.get("default_intents", app.default_intents))
    configured_user_scopes = cast(
        list[str], values.get("user_install_scopes", app.user_install_scopes)
    )
    if USER_INSTALL in configured_install_types and (
        not set(configured_user_scopes) <= set(configured_default_scopes)
        or "interactions" not in configured_default_intents
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "USER_INSTALL_EXCEEDS_APPLICATION"},
        )
    if supported := values.get("supported_install_types"):
        registered_install_types = list(
            await session.scalars(
                select(ApplicationCommand.integration_types).where(
                    ApplicationCommand.application_id == app.id,
                    ApplicationCommand.application_domain == app.origin_domain,
                    ApplicationCommand.state == "active",
                )
            )
        )
        configured = set(cast(list[str], supported))
        if any(set(types) - configured for types in registered_install_types):
            raise HTTPException(
                status_code=409,
                detail={"code": "APPLICATION_INSTALL_TYPE_IN_USE"},
            )
    for key in ("support_url", "privacy_url", "terms_url"):
        if key in values and values[key] is not None:
            values[key] = str(values[key])
    if values.get("directory_enabled", app.directory_enabled):
        from app.api.application_directory import directory_readiness_errors

        readiness_errors = await directory_readiness_errors(session, app, values=values)
        if readiness_errors:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "APPLICATION_DIRECTORY_NOT_READY",
                    "missing": readiness_errors,
                },
            )
    from app.api.application_directory import directory_patch_requires_reapproval

    if directory_patch_requires_reapproval(app, values):
        app.directory_approved = False
    for key, value in values.items():
        setattr(app, key, value)
    if "name" in values:
        bot.display_name = cast(str, values["name"]).strip()
        bot.profile_version += 1
    app.manifest_generation += 1
    await commit_developer_application_mutation(session, settings, app)
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
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "credential.create",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        return application_management_dict_body(remote)
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
    return {
        **application_resource_identity(app, credential.id),
        "token": raw,
        "token_hint": credential.token_hint,
    }


@router.get("/applications/{application_ref}/credentials")
async def list_credentials(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "credential.list"
    )
    if remote is not None:
        return cast(list[dict[str, object]], application_management_list_body(remote))
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
            **application_resource_identity(app, row.id),
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
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "credential.revoke",
        {"resource_id": credential_id},
    )
    if remote is not None:
        require_application_management_empty(remote)
        return Response(status_code=204)
    app, member, _ = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
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
        await commit_developer_application_mutation(session, settings, app)
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
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "worker.create",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    app, member, _ = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
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
    app.manifest_generation += 1
    await commit_developer_application_mutation(session, settings, app)
    return {
        **application_resource_identity(app, worker.id),
        "name": worker.name,
        "public_key": payload.public_key,
    }


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
        request,
        session,
        settings,
        application_ref,
        "workers.manage",
        for_update=True,
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
    app.manifest_generation += 1
    await commit_developer_application_mutation(session, settings, app)
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
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "command.replace",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    app, member, _ = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
    require_team_role(member, "owner", "administrator", "developer")
    return await replace_application_commands(session, settings, snowflake, app, payload)


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
        request,
        session,
        settings,
        application_ref,
        "commands.manage",
        for_update=True,
    )
    await enforce_keyed_rate_limit(
        redis, response, BOT_CONTROL_LIMIT, identity=f"credential:{credential.id}"
    )
    return await replace_application_commands(session, settings, snowflake, app, payload)


@router.get("/applications/{application_ref}/commands")
async def get_commands(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "command.list"
    )
    if remote is not None:
        return cast(list[dict[str, Any]], application_management_list_body(remote))
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


def federated_guild_command_payload(
    result: dict[str, object],
) -> dict[str, object]:
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("stored command result is missing items")
    commands: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise RuntimeError("stored command result contains an invalid item")
        commands.append(
            {
                key: value
                for key, value in raw.items()
                if key not in {"origin_domain", "ref", "application_ref", "guild_ref"}
            }
        )
    return {"generation": result["generation"], "commands": commands}


async def sync_remote_guild_commands(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    guild: Guild,
    result: dict[str, object],
) -> None:
    if guild.origin_domain == settings.domain:
        return
    upstream = await signed_request(
        session,
        settings,
        "PUT",
        guild.origin_domain,
        f"/_kaede/v1/applications/{application.id}/guilds/{guild.id}/commands",
        payload=federated_guild_command_payload(result),
        request_timeout=15,
        max_response_bytes=64 * 1024,
    )
    if upstream.status_code == 200:
        return
    detail: dict[str, object] = {"code": "REMOTE_GUILD_COMMAND_SYNC_FAILED"}
    if upstream.status_code in {400, 403, 404, 409, 422, 429}:
        raw = decode_federation_response_json(upstream)
        if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
            detail = {str(key): value for key, value in raw["detail"].items()}
    raise HTTPException(status_code=upstream.status_code, detail=detail)


async def application_guild_command_scope(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
) -> Guild:
    guild = await session.get(Guild, guild_ref.resolve(settings.domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


async def require_local_guild_command_installation(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    guild: Guild,
) -> None:
    if guild.origin_domain != settings.domain:
        return
    installation = await session.scalar(
        select(BotInstallation.id).where(
            BotInstallation.application_id == application.id,
            BotInstallation.application_domain == application.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            usable_guild_installation(),
            BotInstallation.granted_scopes.contains(["applications.commands"]),
        )
    )
    if installation is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})


@router.put("/applications/{application_ref}/guilds/{guild_ref}/commands")
async def put_guild_commands(
    application_ref: EntityRef,
    guild_ref: EntityRef,
    payload: CommandsPut,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "guild_command.replace",
        {
            "guild_ref": f"{guild_id}@{guild_domain}",
            "data": payload.model_dump(mode="json"),
        },
    )
    if remote is not None:
        return application_management_dict_body(remote)
    application, member, _ = await managed_application(
        session,
        settings,
        auth,
        application_ref,
        for_update=True,
    )
    require_team_role(member, "owner", "administrator", "developer")
    guild = await application_guild_command_scope(session, settings, guild_ref)
    await require_local_guild_command_installation(session, settings, application, guild)
    result = await replace_application_commands(
        session,
        settings,
        snowflake,
        application,
        payload,
        guild_ref=(guild.id, guild.origin_domain),
    )
    await sync_remote_guild_commands(session, settings, application, guild, result)
    return result


@router.put("/bot-control/applications/{application_ref}/guilds/{guild_ref}/commands")
async def control_put_guild_commands(
    application_ref: EntityRef,
    guild_ref: EntityRef,
    payload: CommandsPut,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    application, credential = await control_application(
        request,
        session,
        settings,
        application_ref,
        "commands.manage",
        for_update=True,
    )
    await enforce_keyed_rate_limit(
        redis,
        response,
        BOT_CONTROL_LIMIT,
        identity=f"credential:{credential.id}",
    )
    guild = await application_guild_command_scope(session, settings, guild_ref)
    await require_local_guild_command_installation(session, settings, application, guild)
    result = await replace_application_commands(
        session,
        settings,
        snowflake,
        application,
        payload,
        guild_ref=(guild.id, guild.origin_domain),
    )
    await sync_remote_guild_commands(session, settings, application, guild, result)
    return result


@router.get("/applications/{application_ref}/guilds/{guild_ref}/commands")
async def get_guild_commands(
    application_ref: EntityRef,
    guild_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "guild_command.list",
        {"guild_ref": f"{guild_id}@{guild_domain}"},
    )
    if remote is not None:
        return cast(list[dict[str, object]], application_management_list_body(remote))
    application, _, _ = await managed_application(session, settings, auth, application_ref)
    guild = await application_guild_command_scope(session, settings, guild_ref)
    rows = list(
        await session.scalars(
            select(ApplicationCommand)
            .where(
                ApplicationCommand.application_id == application.id,
                ApplicationCommand.application_domain == application.origin_domain,
                ApplicationCommand.guild_id == guild.id,
                ApplicationCommand.guild_domain == guild.origin_domain,
                ApplicationCommand.state == "active",
            )
            .order_by(ApplicationCommand.type, ApplicationCommand.name)
        )
    )
    return [
        {
            "id": str(row.id),
            "origin_domain": application.origin_domain,
            "ref": f"{row.id}@{application.origin_domain}",
            "application_ref": f"{application.id}@{application.origin_domain}",
            "guild_ref": f"{guild.id}@{guild.origin_domain}",
            **row.definition,
        }
        for row in rows
    ]


def federated_command_definition(command: FederatedGuildCommand) -> dict[str, object]:
    return command.model_dump(mode="json", exclude={"id"})


async def materialize_federated_guild_commands(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    application: BotApplication,
    guild: Guild,
    payload: FederatedGuildCommandsPut,
) -> list[ApplicationCommand]:
    existing = list(
        await session.scalars(
            select(ApplicationCommand)
            .where(
                ApplicationCommand.application_id == application.id,
                ApplicationCommand.application_domain == application.origin_domain,
                ApplicationCommand.guild_id == guild.id,
                ApplicationCommand.guild_domain == guild.origin_domain,
            )
            .with_for_update()
        )
    )
    generation = int(payload.generation)
    current_generation = max((row.generation for row in existing), default=0)
    if generation < current_generation:
        raise HTTPException(status_code=409, detail={"code": "COMMAND_GENERATION_STALE"})
    by_source = {
        row.source_id: row
        for row in existing
        if row.source_id is not None and row.source_domain == application.origin_domain
    }
    incoming_ids = {int(command.id) for command in payload.commands}
    if generation == current_generation and existing:
        existing_projection = {
            row.source_id: row.definition
            for row in existing
            if row.source_id is not None and row.source_domain == application.origin_domain
        }
        incoming_projection = {
            int(command.id): federated_command_definition(command) for command in payload.commands
        }
        if existing_projection != incoming_projection:
            raise HTTPException(
                status_code=409,
                detail={"code": "COMMAND_GENERATION_CONFLICT"},
            )
        return existing
    stored: list[ApplicationCommand] = []
    for incoming in payload.commands:
        source_id = int(incoming.id)
        command = by_source.get(source_id)
        if command is None:
            collision = await session.scalar(
                select(ApplicationCommand.id).where(
                    ApplicationCommand.source_id == source_id,
                    ApplicationCommand.source_domain == application.origin_domain,
                )
            )
            if collision is not None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "APPLICATION_COMMAND_ID_CONFLICT"},
                )
            command = ApplicationCommand(
                id=await snowflake.mint(),
                source_id=source_id,
                source_domain=application.origin_domain,
                application_id=application.id,
                application_domain=application.origin_domain,
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                name=incoming.name,
                type=incoming.type,
                definition={},
                generation=generation,
            )
            session.add(command)
        command.name = incoming.name
        command.type = incoming.type
        command.definition = federated_command_definition(incoming)
        command.contexts = ["guild"]
        command.integration_types = ["guild_install"]
        command.generation = generation
        command.state = "active"
        stored.append(command)
    for obsolete in existing:
        if obsolete.source_id not in incoming_ids:
            await session.delete(obsolete)
    application.command_generation = max(application.command_generation, generation)
    await session.commit()
    return stored


@federation_router.put("/_kaede/v1/applications/{application_id}/guilds/{guild_id}/commands")
async def federation_put_guild_commands(
    application_id: int,
    guild_id: int,
    payload: FederatedGuildCommandsPut,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced or principal.origin == settings.domain:
        raise HTTPException(status_code=403, detail={"code": "FEDERATION_FORBIDDEN"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-guild-command-put",
        capacity=120,
        refill_per_minute=60,
    )
    guild = await session.get(Guild, (guild_id, settings.domain))
    application = await session.get(BotApplication, (application_id, principal.origin))
    if guild is None or application is None or application.status != "active":
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    installation = await session.scalar(
        select(BotInstallation.id).where(
            BotInstallation.application_id == application.id,
            BotInstallation.application_domain == application.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            usable_guild_installation(),
            BotInstallation.granted_scopes.contains(["applications.commands"]),
        )
    )
    if installation is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    stored = await materialize_federated_guild_commands(
        session,
        snowflake,
        application,
        guild,
        payload,
    )
    return {
        "generation": payload.generation,
        "commands": len(stored),
        "items": [
            {
                "id": str(command.authority_id),
                "origin_domain": application.origin_domain,
                "ref": f"{command.authority_id}@{application.origin_domain}",
                **command.definition,
            }
            for command in stored
        ],
    }


@router.post("/applications/{application_ref}/install-templates", status_code=201)
async def create_template(
    application_ref: EntityRef,
    payload: TemplateCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "template.create",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    app, member, _ = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
    require_team_role(member, "owner", "administrator", "developer")
    user_only_consent_shell_invalid = GUILD_INSTALL not in app.supported_install_types and (
        bool(payload.scopes)
        or bool(payload.intents)
        or payload.permissions != 0
        or payload.e2ee_mode != "disabled"
    )
    if (
        user_only_consent_shell_invalid
        or not set(payload.scopes).issubset(set(app.default_scopes))
        or not set(payload.intents).issubset(set(app.default_intents))
        or (payload.e2ee_mode != "disabled" and payload.e2ee_mode not in app.e2ee_modes)
    ):
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
    app.manifest_generation += 1
    await commit_developer_application_mutation(session, settings, app)
    return {
        **application_resource_identity(app, template.id),
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
        application_id, application_domain = application_ref.resolve(settings.domain)
        federation_response = await signed_request(
            session,
            settings,
            "POST",
            guild_domain,
            f"/_kaede/v1/guilds/{guild_id}/bot-install",
            payload={
                "installer_id": str(auth.user.id),
                "application_ref": f"{application_id}@{application_domain}",
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
        try:
            result = FederatedBotInstallResult.model_validate(raw)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail={"code": "REMOTE_BOT_INSTALL_INVALID"},
            ) from None
        if (
            result.application_ref.resolve(settings.domain) != (application_id, application_domain)
            or result.guild_ref.resolve(settings.domain) != (guild_id, guild_domain)
            or any(item.domain != guild_domain for item in result.channel_restrictions)
        ):
            raise HTTPException(status_code=502, detail={"code": "REMOTE_BOT_INSTALL_INVALID"})
        return result.model_dump(mode="json")
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, auth.user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    app_id, app_domain = application_ref.resolve(settings.domain)
    remote_manifest = (
        await fetch_bot_manifest(session, settings, app_id, app_domain, template_slug)
        if app_domain != settings.domain
        else None
    )
    # Serialize the authorization decision and every following membership,
    # role, and installation mutation with kicks/bans/instance bans.  The
    # preliminary permission check above avoids fetching an untrusted remote
    # manifest for an unauthorized caller; permissions are re-evaluated after
    # the lock in case they changed while federation I/O was in flight.  Only
    # the signed manifest fetch happens before the lock: materialization waits
    # for the locked permission recheck so unauthorized or stale requests cannot
    # write remote application state, while no network I/O holds the guild lock.
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
    mutation_signer = await guild_authority_owner(session, settings, guild, for_update=True)
    if remote_manifest is not None:
        await materialize_remote_manifest(session, remote_manifest, settings, snowflake)
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
    if (
        GUILD_INSTALL not in app.supported_install_types
        or bot.account_type != "bot"
        or bot.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    if await session.get(InstanceBlock, app.origin_domain) is not None:
        raise HTTPException(status_code=403, detail={"code": "BOT_INSTANCE_BLOCKED"})
    await require_application_runtime_enabled(session, settings, app)
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
    paused_channels = (
        await revoke_bot_e2ee_access(
            session,
            redis,
            settings,
            installation_ids=(existing.id,),
        )
        if existing is not None
        else []
    )
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
            temporary=False,
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
    # BotInstallation's composite role foreign key is scalar-only, so the ORM
    # cannot infer that this role must be inserted first. Persist the role before
    # guild mutation locking flushes the rest of the pending installation state.
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
    if existing is not None:
        installation.bot_user_id = bot.id
        installation.bot_user_domain = bot.origin_domain
        installation.role_id = role.id
        installation.role_domain = role.origin_domain
        installation.installer_id = auth.user.id
        installation.installer_domain = auth.user.origin_domain
        installation.granted_scopes = template.scopes
        installation.granted_intents = template.intents
        installation.granted_permissions = template.permissions
        installation.e2ee_mode = template.e2ee_mode
        installation.grant_revision += 1
        installation.status = "active"
        installation.revoked_at = None
    await session.flush(objects=[role])
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
    if existing is None:
        session.add(installation)
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
        {
            "user": profile_from_user(bot),
            "joined_at": member.joined_at.isoformat(),
            "temporary": False,
        },
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
    queue_installation_gateway_events(
        session,
        installation,
        "CREATE" if existing is None else "UPDATE",
    )
    target_destination = await queue_application_target_snapshot(
        session,
        settings,
        app,
        bot,
        force=True,
    )
    # Shifting retained roles invokes the database-managed ``updated_at``
    # expression.  Materialize their gateway projections while the transaction
    # is still active; reading ``resource_version`` after commit would otherwise
    # attempt implicit async I/O and raise MissingGreenlet.
    await materialize_updated_at(session, *existing_roles)
    rendered_existing_roles: list[dict[str, object]] = []
    for existing_role in existing_roles:
        rendered_existing_roles.append(role_payload(existing_role))
    rendered_role = role_payload(role)
    rendered_member = member_payload(member, bot, [role.id])
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    await wake_application_target_deliveries(
        {target_destination} if target_destination is not None else set()
    )
    await wake_queued_guild_federation(guild)
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
    for rendered_existing_role in rendered_existing_roles:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_ROLE_UPDATE",
            rendered_existing_role,
        )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_ROLE_CREATE", rendered_role
    )
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_MEMBER_ADD",
        rendered_member,
    )
    return {
        "id": str(installation.id),
        "status": installation.status,
        "application_ref": f"{app.id}@{app.origin_domain}",
        "guild_ref": f"{guild.id}@{guild.origin_domain}",
        "channel_restrictions": list(
            qualified_channel_restrictions(
                installation.channel_restrictions or [],
                authority_domain=installation.guild_domain,
            )
        ),
        "grant_revision": str(installation.grant_revision),
    }


@router.post("/bot-workers/targets")
async def discover_bot_worker_targets(
    payload: WorkerTokenRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return authority-attested runtime domains without installation rosters."""

    await enforce_keyed_rate_limit(
        redis,
        response,
        BOT_TOKEN_LIMIT,
        identity=f"target-discovery:{payload.application_ref}",
    )
    worker, application, _ = await authenticated_worker_assertion(
        payload,
        session,
        redis,
        snowflake,
        settings,
        expected_audience=f"https://{settings.domain}/api/v1/bot-workers/targets",
        replay_scope="target-discovery",
        local_application_only=True,
    )
    query = (
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            or_(
                BotApplicationTarget.guild_installations > 0,
                BotApplicationTarget.user_installations > 0,
            ),
        )
        .order_by(BotApplicationTarget.target_domain)
    )
    if worker.target_domains:
        query = query.where(BotApplicationTarget.target_domain.in_(worker.target_domains))
    rules = {
        rule.target_domain: rule.effect
        for rule in await session.scalars(
            select(BotInstanceRule).where(
                BotInstanceRule.application_id == application.id,
                BotInstanceRule.application_domain == application.origin_domain,
            )
        )
    }
    targets = [
        target
        for target in await session.scalars(query)
        if target_policy_allows(
            application.target_policy,
            rules,
            target.target_domain,
        )
    ]
    return {
        "application_ref": f"{application.id}@{application.origin_domain}",
        "targets": [
            {
                "domain": target.target_domain,
                "origin": f"https://{target.target_domain}",
                "generation": str(target.generation),
                "install_types": [
                    install_type
                    for install_type, count in (
                        ("guild_install", target.guild_installations),
                        ("user_install", target.user_installations),
                    )
                    if count > 0
                ],
            }
            for target in targets
        ],
        "poll_after_seconds": 30,
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
    worker, app, bot_user = await authenticated_worker_assertion(
        payload,
        session,
        redis,
        snowflake,
        settings,
        expected_audience=expected_audience,
        replay_scope="runtime-token",
        require_target_delegation=False,
    )
    application_home = app.origin_domain == settings.domain
    capability: BotDMCapability | None = None
    if payload.dm_capability_grant_id is not None:
        capability = await session.scalar(
            select(BotDMCapability)
            .where(
                BotDMCapability.grant_id == payload.dm_capability_grant_id,
                BotDMCapability.revision == payload.dm_capability_revision,
                BotDMCapability.application_id == app.id,
                BotDMCapability.application_domain == app.origin_domain,
                BotDMCapability.bot_user_id == bot_user.id,
                BotDMCapability.bot_user_domain == bot_user.origin_domain,
                BotDMCapability.authority_domain == settings.domain,
                BotDMCapability.conversation_domain == settings.domain,
                BotDMCapability.conversation_id.is_not(None),
                usable_dm_capability(at=datetime.now(UTC)),
            )
            .with_for_update()
        )
        if capability is None:
            raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"})
    elif not application_home:
        if not worker_target_allowed(
            worker.target_domains,
            application_domain=app.origin_domain,
            target_domain=settings.domain,
        ):
            raise HTTPException(status_code=403, detail={"code": "BOT_TARGET_NOT_DELEGATED"})
        if not await session.scalar(
            select(
                active_standard_installation_exists(
                    application_id=app.id,
                    application_domain=app.origin_domain,
                    bot_user_id=bot_user.id,
                    bot_user_domain=bot_user.origin_domain,
                    current_instance_domain=settings.domain,
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
        target_domain=settings.domain,
        dm_capability=capability,
    )
    runtime_target = await session.get(
        BotApplicationTarget,
        (app.id, app.origin_domain, settings.domain),
    )
    runtime_revision = worker_actor_runtime_revision(
        app,
        worker,
        runtime_target,
        target_domain=settings.domain,
    )
    await session.commit()
    return {
        "access_token": raw,
        "token_type": "Bot",
        "expires_in": max(1, int((token.expires_at - datetime.now(UTC)).total_seconds())),
        "dpop_thumbprint": thumbprint,
        "bot_user_ref": f"{bot_user.id}@{bot_user.origin_domain}",
        **runtime_revision,
    }


class InstanceRulePut(UnambiguousInputModel):
    effect: Literal["allow", "deny"]


@router.get("/applications/{application_ref}/instance-rules")
async def list_instance_rules(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, str]]:
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "instance_rule.list"
    )
    if remote is not None:
        return cast(list[dict[str, str]], application_management_list_body(remote))
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
    return [
        {
            "application_ref": qualified_application_ref(app),
            "target_domain": row.target_domain,
            "effect": row.effect,
        }
        for row in rules
    ]


@router.put("/applications/{application_ref}/instance-rules/{target_domain}")
async def put_instance_rule(
    application_ref: EntityRef,
    target_domain: str,
    payload: InstanceRulePut,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "instance_rule.put",
        {
            "target_domain": target_domain,
            "data": payload.model_dump(mode="json"),
        },
    )
    if remote is not None:
        return cast(dict[str, str], application_management_dict_body(remote))
    app, member, _ = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
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
    await commit_developer_application_mutation(session, settings, app)
    return {
        "application_ref": qualified_application_ref(app),
        "target_domain": target_domain,
        "effect": payload.effect,
    }


@router.delete("/applications/{application_ref}/instance-rules/{target_domain}", status_code=204)
async def delete_instance_rule(
    application_ref: EntityRef,
    target_domain: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "instance_rule.delete",
        {"target_domain": target_domain},
    )
    if remote is not None:
        require_application_management_empty(remote)
        return Response(status_code=204)
    app, member, _ = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
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
    await commit_developer_application_mutation(session, settings, app)
    return Response(status_code=204)


@router.get("/applications/{application_ref}/workers")
async def list_workers(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "worker.list"
    )
    if remote is not None:
        return cast(list[dict[str, object]], application_management_list_body(remote))
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
            **application_resource_identity(app, row.authority_id),
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
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "worker.revoke",
        {"resource_id": worker_id},
    )
    if remote is not None:
        require_application_management_empty(remote)
        return Response(status_code=204)
    app, member, _ = await managed_application(
        session, settings, auth, application_ref, for_update=True
    )
    require_team_role(member, "owner", "administrator", "security")
    worker = await session.get(BotWorker, worker_id, with_for_update=True)
    if worker is None or (worker.application_id, worker.application_domain) != (
        app.id,
        app.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    bot, paused_channels = await revoke_bot_e2ee_devices(
        session,
        redis,
        settings,
        application_ref=(app.id, app.origin_domain),
        worker_ids=(worker.id,),
    )
    worker.revoked_at = datetime.now(UTC)
    worker.generation += 1
    app.manifest_generation += 1
    app.revocation_generation += 1
    if bot is not None:
        from app.api.bot_e2ee import (
            _publish_bot_device_generation_change,
            _queue_bot_device_generation_change,
        )

        paused_channels, device_destinations = await _queue_bot_device_generation_change(
            session,
            settings,
            app,
            bot,
            already_paused=paused_channels,
        )
    await commit_developer_application_mutation(session, settings, app)
    if bot is not None:
        await _publish_bot_device_generation_change(
            session,
            redis,
            settings,
            paused_channels,
            device_destinations,
        )
    else:
        await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    return Response(status_code=204)


@router.get("/applications/{application_ref}/install-templates")
async def list_templates(
    application_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "template.list"
    )
    if remote is not None:
        return cast(list[dict[str, object]], application_management_list_body(remote))
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
            **application_resource_identity(app, row.authority_id),
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
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "installation.list"
    )
    if remote is not None:
        return cast(list[dict[str, object]], application_management_list_body(remote))
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
            "ref": f"{row.id}@{row.guild_domain}",
            "application_ref": qualified_application_ref(app),
            "guild_ref": f"{row.guild_id}@{row.guild_domain}",
            "status": row.status,
            "scopes": row.granted_scopes,
            "intents": row.granted_intents,
            "permissions": str(row.granted_permissions),
            "channel_restrictions": list(
                qualified_channel_restrictions(
                    row.channel_restrictions or [],
                    authority_domain=row.guild_domain,
                )
            ),
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
    installer = await federated_human_installer(
        session,
        principal,
        payload.installer_id,
        require_mutation_admission=True,
    )
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
                BotInstallation.revoked_at.is_(None),
            )
            .order_by(func.lower(BotApplication.name), BotApplication.id)
        )
    ).all()
    return [
        {
            "id": str(installation.id),
            "ref": f"{installation.id}@{guild.origin_domain}",
            "guild_ref": f"{guild.id}@{guild.origin_domain}",
            "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
            "application": application_payload(application, bot),
            "status": installation.status,
            "scopes": installation.granted_scopes,
            "intents": installation.granted_intents,
            "permissions": str(installation.granted_permissions),
            "channel_restrictions": list(
                qualified_channel_restrictions(
                    installation.channel_restrictions or [],
                    authority_domain=installation.guild_domain,
                )
            ),
            "e2ee_mode": installation.e2ee_mode,
            "grant_revision": str(installation.grant_revision),
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
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    if guild_domain != settings.domain:
        member = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, auth.user.id, auth.user.origin_domain),
        )
        if member is None:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
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
        if not isinstance(raw, list) or len(raw) > 1000:
            raise HTTPException(
                status_code=502, detail={"code": "REMOTE_BOT_INSTALLATIONS_INVALID"}
            )
        try:
            parsed = [FederatedGuildBotInstallation.model_validate(item) for item in raw]
        except ValueError:
            raise HTTPException(
                status_code=502, detail={"code": "REMOTE_BOT_INSTALLATIONS_INVALID"}
            ) from None
        expected_guild_ref = f"{guild_id}@{guild_domain}"
        if (
            any(
                str(item.guild_ref) != expected_guild_ref
                or item.ref.domain != guild_domain
                or any(ref.domain != guild_domain for ref in item.channel_restrictions)
                for item in parsed
            )
            or len({item.id for item in parsed}) != len(parsed)
            or len({str(item.ref) for item in parsed}) != len(parsed)
            or len({str(item.application_ref) for item in parsed}) != len(parsed)
        ):
            raise HTTPException(
                status_code=502, detail={"code": "REMOTE_BOT_INSTALLATIONS_INVALID"}
            )
        return [item.model_dump(mode="json") for item in parsed]
    permissions = await get_permissions(session, redis, guild, auth.user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    return await _guild_bot_installation_payloads(session, guild)


@federation_router.get("/_kaede/v1/guilds/{guild_id}/bot-installations")
async def federation_list_guild_bot_installations(
    guild_id: int,
    user_id: SnowflakeString,
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
    user = await federated_human_installer(session, principal, user_id)
    guild = await session.get(Guild, (guild_id, settings.domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    return await _guild_bot_installation_payloads(session, guild)


def _bot_restrictions_result(installation: BotInstallation) -> dict[str, object]:
    return {
        "id": str(installation.id),
        "status": installation.status,
        "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
        "guild_ref": f"{installation.guild_id}@{installation.guild_domain}",
        "channel_restrictions": list(
            qualified_channel_restrictions(
                installation.channel_restrictions or [],
                authority_domain=installation.guild_domain,
            )
        ),
        "grant_revision": str(installation.grant_revision),
    }


async def _canonical_installation_channel_restrictions(
    session: AsyncSession,
    guild: Guild,
    requested: list[EntityRef],
) -> list[str]:
    resolved = [item.resolve(guild.origin_domain) for item in requested]
    if any(domain != guild.origin_domain for _, domain in resolved):
        raise HTTPException(
            status_code=422,
            detail={"code": "CHANNEL_RESTRICTION_WRONG_AUTHORITY"},
        )
    if len(set(resolved)) != len(resolved):
        raise HTTPException(
            status_code=422,
            detail={"code": "CHANNEL_RESTRICTION_DUPLICATE"},
        )
    channel_ids = {channel_id for channel_id, _ in resolved}
    if not channel_ids:
        return []
    found_ids = set(
        await session.scalars(
            select(Channel.id).where(
                Channel.id.in_(channel_ids),
                Channel.origin_domain == guild.origin_domain,
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.unavailable.is_(False),
            )
        )
    )
    if found_ids != channel_ids:
        raise HTTPException(
            status_code=422,
            detail={"code": "CHANNEL_RESTRICTION_INVALID"},
        )
    return [f"{channel_id}@{guild.origin_domain}" for channel_id in sorted(found_ids)]


async def _update_local_bot_channel_restrictions(
    guild: Guild,
    application_ref: EntityRef,
    requested: list[EntityRef],
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict[str, object]:
    permissions = await get_permissions(session, redis, guild, auth.user)
    if not permissions & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    app_id, app_domain = application_ref.resolve(settings.domain)
    row = (
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
                BotInstallation.application_id == app_id,
                BotInstallation.application_domain == app_domain,
                BotInstallation.guild_id == guild.id,
                BotInstallation.guild_domain == guild.origin_domain,
                BotInstallation.status != "revoked",
                BotInstallation.revoked_at.is_(None),
            )
            .with_for_update(of=BotInstallation)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_INSTALLATION_NOT_FOUND"})
    installation, application, bot = row
    normalized = await _canonical_installation_channel_restrictions(session, guild, requested)
    current = list(
        qualified_channel_restrictions(
            installation.channel_restrictions or [],
            authority_domain=installation.guild_domain,
        )
    )
    if current == normalized:
        return _bot_restrictions_result(installation)

    # A grant revision is a capability fence. Revoke derived DM/E2EE grants and
    # evict active media sessions before publishing the narrowed (or widened)
    # replacement so no worker can keep using the previous revision.
    paused_channels = await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        installation_ids=(installation.id,),
    )
    installation.channel_restrictions = normalized
    installation.grant_revision += 1
    queue_installation_gateway_events(session, installation, "UPDATE")
    target_destination = await queue_application_target_snapshot(
        session,
        settings,
        application,
        bot,
        force=True,
    )
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    await wake_application_target_deliveries(
        {target_destination} if target_destination is not None else set()
    )
    return _bot_restrictions_result(installation)


@router.patch("/guilds/{guild_ref}/integrations/bots/{application_ref}")
async def update_bot_channel_restrictions(
    guild_ref: EntityRef,
    application_ref: EntityRef,
    payload: BotChannelRestrictionsUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != settings.domain:
        application_id, application_domain = application_ref.resolve(settings.domain)
        requested = [
            f"{item.id}@{item.domain or guild_domain}" for item in payload.channel_restrictions
        ]
        federation_response = await signed_request(
            session,
            settings,
            "PATCH",
            guild_domain,
            f"/_kaede/v1/guilds/{guild_id}/bot-install",
            payload={
                "installer_id": str(auth.user.id),
                "application_ref": f"{application_id}@{application_domain}",
                "channel_restrictions": requested,
            },
            request_timeout=15,
            max_response_bytes=64 * 1024,
        )
        if federation_response.status_code != 200:
            detail: dict[str, object] = {"code": "REMOTE_BOT_RESTRICTIONS_FAILED"}
            if federation_response.status_code in {403, 404, 409, 422, 429, 507}:
                raw = decode_federation_response_json(federation_response)
                if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                    detail = {str(key): value for key, value in raw["detail"].items()}
            raise HTTPException(status_code=federation_response.status_code, detail=detail)
        try:
            result = FederatedBotRestrictionsResult.model_validate(
                decode_federation_response_json(federation_response)
            )
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail={"code": "REMOTE_BOT_RESTRICTIONS_INVALID"},
            ) from None
        expected_restrictions = [
            EntityRef(item)
            for item in sorted(requested, key=lambda item: int(item.partition("@")[0]))
        ]
        if (
            result.application_ref.resolve(settings.domain) != (application_id, application_domain)
            or result.guild_ref.resolve(settings.domain) != (guild_id, guild_domain)
            or result.channel_restrictions != expected_restrictions
            or any(item.domain != guild_domain for item in result.channel_restrictions)
        ):
            raise HTTPException(
                status_code=502,
                detail={"code": "REMOTE_BOT_RESTRICTIONS_INVALID"},
            )
        return result.model_dump(mode="json")

    guild = await session.get(Guild, (guild_id, guild_domain), with_for_update=True)
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return await _update_local_bot_channel_restrictions(
        guild,
        application_ref,
        payload.channel_restrictions,
        auth,
        session,
        redis,
        settings,
    )


@federation_router.patch("/_kaede/v1/guilds/{guild_id}/bot-install")
async def federation_update_bot_channel_restrictions(
    guild_id: int,
    payload: FederatedBotRestrictionsUpdate,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-restrictions", capacity=120, refill_per_minute=60
    )
    installer = await federated_human_installer(
        session,
        principal,
        payload.installer_id,
        require_mutation_admission=True,
    )
    guild = await session.get(Guild, (guild_id, settings.domain), with_for_update=True)
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    auth = AuthenticatedUser(
        user=installer,
        grant=cast(Any, None),
        access_token="",
        cookie_authenticated=False,
    )
    return await _update_local_bot_channel_restrictions(
        guild,
        payload.application_ref,
        payload.channel_restrictions,
        auth,
        session,
        redis,
        settings,
    )


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
    mutation_signer = await guild_authority_owner(session, settings, guild, for_update=True)
    app_id, app_domain = application_ref.resolve(settings.domain)
    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == app_id,
            BotApplication.origin_domain == app_domain,
        )
        .with_for_update()
    )
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
    paused_channels = await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        installation_ids=(installation.id,),
    )
    installation.status = "revoked"
    installation.revoked_at = datetime.now(UTC)
    installation.grant_revision += 1
    queue_installation_gateway_events(session, installation, "DELETE")
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
            e2ee_policy_channels=paused_channels,
            pause_e2ee=False,
        )
        await session.delete(member)
    target_destinations: set[str] = set()
    target_snapshot_queued = False
    if application is not None and (
        application.bot_user_id,
        application.bot_user_domain,
    ) == (installation.bot_user_id, installation.bot_user_domain):
        application_bot = await session.get(
            User,
            (application.bot_user_id, application.bot_user_domain),
        )
        if application_bot is not None and application_bot.account_type == "bot":
            target_destinations = await queue_application_target_snapshots_for_refs(
                session,
                settings,
                {(installation.application_id, installation.application_domain)},
                force=True,
            )
            target_snapshot_queued = True
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    if target_snapshot_queued:
        await wake_application_target_deliveries(target_destinations)
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
        application_id, application_domain = application_ref.resolve(settings.domain)
        federation_response = await signed_request(
            session,
            settings,
            "DELETE",
            guild_domain,
            f"/_kaede/v1/guilds/{guild_id}/bot-install",
            payload={
                "installer_id": str(auth.user.id),
                "application_ref": f"{application_id}@{application_domain}",
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
    installer = await federated_human_installer(session, principal, payload.installer_id)
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
