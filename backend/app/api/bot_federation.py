from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_redis, get_session
from app.bots.application_contract import (
    SUPPORTED_APPLICATION_SCOPES,
    canonical_application_manifest_projection,
    validate_application_https_url,
    validate_application_icon_hash,
    validate_application_install_contract,
    validate_known_permission_mask,
)
from app.bots.command_contract import (
    CommandDefinition,
    CommandsPut,
)
from app.bots.developer_projection import manifest_team_placeholder_name
from app.bots.dm_capability import (
    FederationDomain,
    PositiveDecimal,
    QualifiedRef,
    require_stored_capability_runtime,
    stored_bot_dm_capability_payload,
    usable_dm_capability,
)
from app.bots.install_config import (
    DEFAULT_USER_INSTALL_SCOPES,
    USER_INSTALL,
)
from app.bots.installations import usable_user_installation
from app.bots.projection_locking import (
    bot_application_identity_owner,
    lock_bot_projection_identities,
)
from app.bots.runtime_control import promote_application_runtime_highwater
from app.bots.target_contract import target_policy_allows
from app.bots.worker_targets import worker_target_allowed
from app.core.base64url import decode_base64url, encode_base64url
from app.core.bot_intents import SUPPORTED_BOT_INTENTS
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import DOMAIN_RE, Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import (
    ApplicationCommand,
    ApplicationEmoji,
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotInstallTemplate,
    BotInstanceRule,
    BotUserInstallation,
    BotWorker,
    DeveloperTeam,
)
from app.db.models import User
from app.federation.client import signed_request
from app.federation.events import build_envelope
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.replication import profile_from_user, upsert_remote_user
from app.federation.schemas import RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    validated_event_envelope,
)

router = APIRouter(tags=["bot federation"])
BOT_MANIFEST_CAPABILITY = "bot-direct-auth/1"
BOT_MANIFEST_EVENT = "bot.application.manifest"
BOT_RUNTIME_MANIFEST_EVENT = "bot.application.runtime-manifest"
LOCAL_APPLICATION_REFRESH_HOLDS = frozenset({"review_required", "suspended", "deleting", "deleted"})
MAX_SIGNED_INTEGER = 2**63 - 1


def validated_decimal(value: str, *, allow_zero: bool = False) -> str:
    if not value.isascii() or not value.isdecimal() or (len(value) > 1 and value.startswith("0")):
        raise ValueError("signed manifest integers must be canonical decimal strings")
    number = int(value)
    if number > MAX_SIGNED_INTEGER or (number == 0 and not allow_zero):
        raise ValueError("signed manifest integer is outside the supported range")
    return value


def validated_https_url(value: str | None) -> str | None:
    return validate_application_https_url(value)


def activate_remote_application_if_permitted(
    application: BotApplication,
    *,
    created: bool,
) -> None:
    """Apply remote liveness without overriding an instance administrator hold."""

    if created or application.status not in LOCAL_APPLICATION_REFRESH_HOLDS:
        application.status = "active"


def restore_remote_worker_if_new(worker: BotWorker, *, created: bool) -> None:
    """Never interpret remote refresh as revocation relief granted by this instance."""

    if created:
        worker.revoked_at = None


def _canonical_unordered(values: Iterable[str]) -> tuple[str, ...]:
    """Compare protocol set fields without giving their wire order meaning."""

    return tuple(sorted(values))


async def remote_manifest_child[
    RemoteManifestChild: (BotWorker, BotInstallTemplate, ApplicationCommand)
](
    session: AsyncSession,
    model: type[RemoteManifestChild],
    *,
    source_id: int,
    source_domain: str,
    application_id: int,
) -> RemoteManifestChild | None:
    """Resolve a federated child by authority identity, adopting legacy rows.

    Child-table primary keys are local database identities. A signed manifest's
    snowflake is namespaced by its authority and must therefore live in the
    source reference, even when its numeric value happens not to collide yet.
    """

    row = await session.scalar(
        select(model).where(
            model.source_id == source_id,
            model.source_domain == source_domain,
        )
    )
    if row is not None:
        if row.application_id != application_id or row.application_domain != source_domain:
            raise FederationNetworkError("bot manifest child authority is mismatched")
        return row

    # Upgrade backfills source refs for existing mirrors. Keep this narrow
    # adoption for rolling deployments and test fixtures created before that
    # migration; a row owned by another application remains an ordinary local
    # collision and is never overwritten.
    legacy = await session.get(model, source_id)
    if (
        legacy is not None
        and legacy.application_id == application_id
        and legacy.application_domain == source_domain
        and legacy.source_id is None
        and legacy.source_domain is None
    ):
        legacy.source_id = source_id
        legacy.source_domain = source_domain
        return legacy
    return None


class StrictModel(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")


def default_manifest_install_types() -> list[Literal["guild_install", "user_install"]]:
    return ["guild_install"]


def default_manifest_user_contexts() -> list[Literal["guild", "bot_dm", "private_channel"]]:
    return ["guild", "bot_dm", "private_channel"]


class ManifestApplication(StrictModel):
    id: str
    origin_domain: str = Field(min_length=1, max_length=253)
    team_id: str
    team_domain: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    icon_hash: str | None = Field(default=None, max_length=128)
    support_url: str | None = Field(default=None, max_length=2048)
    privacy_url: str | None = Field(default=None, max_length=2048)
    status: Literal["active"]
    target_policy: Literal["open", "allowlist", "blocklist", "local_only"]
    default_scopes: list[str] = Field(max_length=64)
    default_intents: list[str] = Field(max_length=32)
    default_permissions: str
    supported_install_types: list[Literal["guild_install", "user_install"]] = Field(
        default_factory=default_manifest_install_types, min_length=1, max_length=2
    )
    user_install_scopes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_USER_INSTALL_SCOPES), min_length=2, max_length=4
    )
    user_install_contexts: list[Literal["guild", "bot_dm", "private_channel"]] = Field(
        default_factory=default_manifest_user_contexts,
        min_length=1,
        max_length=3,
    )
    e2ee_modes: list[Literal["participant"]] = Field(max_length=1)
    manifest_generation: str
    command_generation: str
    bot_user: RemoteUserProfile

    @field_validator("id", "team_id", "manifest_generation", "command_generation")
    @classmethod
    def valid_positive_decimal(cls, value: str) -> str:
        return validated_decimal(value)

    @field_validator("default_permissions")
    @classmethod
    def valid_permissions(cls, value: str) -> str:
        validated_decimal(value, allow_zero=True)
        validate_known_permission_mask(int(value), label="default permissions")
        return value

    @field_validator("origin_domain", "team_domain")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        if not DOMAIN_RE.fullmatch(value) or normalize_domain(value) != value:
            raise ValueError("application origin must be a canonical domain")
        return value

    @field_validator("icon_hash")
    @classmethod
    def valid_icon_hash(cls, value: str | None) -> str | None:
        return validate_application_icon_hash(value)

    @field_validator("support_url", "privacy_url")
    @classmethod
    def valid_application_url(cls, value: str | None) -> str | None:
        return validated_https_url(value)

    @model_validator(mode="after")
    def valid_install_config(self) -> ManifestApplication:
        if self.team_domain != self.origin_domain:
            raise ValueError("application team must belong to the application authority")
        if self.bot_user.origin_domain != self.origin_domain or self.bot_user.account_type != "bot":
            raise ValueError("application bot must belong to the application authority")
        validate_application_install_contract(
            default_scopes=self.default_scopes,
            default_intents=self.default_intents,
            supported_install_types=self.supported_install_types,
            user_install_scopes=self.user_install_scopes,
            user_install_contexts=self.user_install_contexts,
            e2ee_modes=self.e2ee_modes,
        )
        return self


class ManifestTemplate(StrictModel):
    id: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    scopes: list[str] = Field(max_length=64)
    intents: list[str] = Field(max_length=32)
    permissions: str
    contexts: list[Literal["guild"]] = Field(min_length=1, max_length=1)
    e2ee_mode: Literal["disabled", "participant"]
    generation: str

    @field_validator("id", "generation")
    @classmethod
    def valid_positive_decimal(cls, value: str) -> str:
        return validated_decimal(value)

    @field_validator("permissions")
    @classmethod
    def valid_permissions(cls, value: str) -> str:
        validated_decimal(value, allow_zero=True)
        validate_known_permission_mask(int(value), label="template permissions")
        return value

    @model_validator(mode="after")
    def valid_grants(self) -> ManifestTemplate:
        if (
            len(self.scopes) != len(set(self.scopes))
            or not set(self.scopes) <= SUPPORTED_APPLICATION_SCOPES
        ):
            raise ValueError("template scopes are invalid")
        if (
            len(self.intents) != len(set(self.intents))
            or not set(self.intents) <= SUPPORTED_BOT_INTENTS
        ):
            raise ValueError("template intents are invalid")
        return self


class ManifestWorker(StrictModel):
    id: str
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=43, max_length=44)
    scopes: list[str] = Field(max_length=64)
    intents: list[str] = Field(max_length=32)
    target_domains: list[str] = Field(max_length=100)
    generation: str
    expires_at: str | None = None

    @field_validator("id", "generation")
    @classmethod
    def valid_positive_decimal(cls, value: str) -> str:
        return validated_decimal(value)

    @model_validator(mode="after")
    def valid_grants(self) -> ManifestWorker:
        if (
            len(self.scopes) != len(set(self.scopes))
            or not set(self.scopes) <= SUPPORTED_APPLICATION_SCOPES
        ):
            raise ValueError("worker scopes are invalid")
        if (
            len(self.intents) != len(set(self.intents))
            or not set(self.intents) <= SUPPORTED_BOT_INTENTS
        ):
            raise ValueError("worker intents are invalid")
        if len(self.target_domains) != len(set(self.target_domains)) or any(
            not DOMAIN_RE.fullmatch(domain) or normalize_domain(domain) != domain
            for domain in self.target_domains
        ):
            raise ValueError("worker target domains must be unique canonical domains")
        if self.expires_at is not None:
            try:
                expires_at = datetime.fromisoformat(self.expires_at)
            except ValueError as exc:
                raise ValueError("worker expiry must be an ISO timestamp") from exc
            if expires_at.tzinfo is None:
                raise ValueError("worker expiry must include a timezone")
        return self


class ManifestCommand(CommandDefinition):
    id: str
    contexts: list[Literal["guild", "bot_dm", "private_channel"]] = Field(
        min_length=1, max_length=3
    )
    integration_types: list[Literal["guild_install", "user_install"]] = Field(
        min_length=1, max_length=2
    )

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validated_decimal(value)


class ManifestApplicationEmoji(StrictModel):
    id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    name: str = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")
    media_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    animated: bool = False
    available: bool = True
    version: str = Field(pattern=r"^[1-9][0-9]{0,18}$")


class WorkerDMCapabilityDelegation(StrictModel):
    """A short-lived A authorization for one worker at capability authority C."""

    grant_id: str = Field(pattern=r"^kbdg_[A-Za-z0-9_-]{43}$")
    revision: PositiveDecimal
    authority_domain: FederationDomain
    conversation_ref: QualifiedRef
    expires_at_ms: PositiveDecimal

    @model_validator(mode="after")
    def valid_authority_binding(self) -> WorkerDMCapabilityDelegation:
        if EntityRef(self.conversation_ref).domain != self.authority_domain:
            raise ValueError("worker DM delegation conversation authority is invalid")
        if datetime.fromtimestamp(int(self.expires_at_ms) / 1000, tz=UTC) <= datetime.now(UTC):
            raise ValueError("worker DM delegation is expired")
        return self


class WorkerAuthorization(StrictModel):
    application_id: str
    application_domain: str = Field(min_length=1, max_length=253)
    bot_user_id: str
    worker: ManifestWorker
    manifest_generation: str
    # Optional while older homes roll forward.  New homes always send this so
    # an installed mirror can refresh command definitions independently from
    # application-profile/asset changes.
    command_generation: str | None = None
    revocation_generation: str
    dm_capability: WorkerDMCapabilityDelegation | None = None

    @field_validator(
        "application_id",
        "bot_user_id",
        "manifest_generation",
        "revocation_generation",
    )
    @classmethod
    def valid_positive_decimal(cls, value: str) -> str:
        return validated_decimal(value)

    @field_validator("command_generation")
    @classmethod
    def valid_optional_generation(cls, value: str | None) -> str | None:
        return None if value is None else validated_decimal(value)

    @field_validator("application_domain")
    @classmethod
    def valid_application_domain(cls, value: str) -> str:
        if not DOMAIN_RE.fullmatch(value) or normalize_domain(value) != value:
            raise ValueError("worker authorization domain must be canonical")
        return value


class BotManifest(StrictModel):
    application: ManifestApplication
    template: ManifestTemplate
    workers: list[ManifestWorker] = Field(max_length=100)
    commands: list[ManifestCommand] = Field(max_length=130)
    # Defaults preserve compatibility with instances that predate application
    # emoji federation. The home instance remains authoritative for the media;
    # mirrors only need this signed metadata to validate bot-authored tokens.
    emojis: list[ManifestApplicationEmoji] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="after")
    def valid_command_set(self) -> BotManifest:
        # Reuse the local Discord-compatible per-type/depth contract so a
        # signed remote manifest cannot materialize definitions that the home
        # would reject at registration time.
        CommandsPut(commands=self.commands)
        keys = [(command.type, command.name) for command in self.commands]
        if len(keys) != len(set(keys)):
            raise ValueError("bot manifest command names must be unique per type")
        if len({command.id for command in self.commands}) != len(self.commands):
            raise ValueError("bot manifest command IDs must be unique")
        supported = set(self.application.supported_install_types)
        if any(set(command.integration_types) - supported for command in self.commands):
            raise ValueError(
                "bot manifest command install types must be configured by the application"
            )
        if (
            self.template.slug != "user-install"
            and "guild_install" not in self.application.supported_install_types
            and (
                self.template.scopes
                or self.template.intents
                or int(self.template.permissions) != 0
                or self.template.e2ee_mode != "disabled"
            )
        ):
            raise ValueError("bot manifest advertises an unsupported guild install")
        if not set(self.template.scopes) <= set(self.application.default_scopes) or not set(
            self.template.intents
        ) <= set(self.application.default_intents):
            raise ValueError("bot manifest template exceeds the application grant")
        if (
            self.template.e2ee_mode != "disabled"
            and self.template.e2ee_mode not in self.application.e2ee_modes
        ):
            raise ValueError("bot manifest template exceeds the application E2EE modes")
        if any(
            set(worker.scopes) - set(self.application.default_scopes)
            or set(worker.intents) - set(self.application.default_intents)
            for worker in self.workers
        ):
            raise ValueError("bot manifest worker exceeds the application runtime grant")
        return self


class BotRuntimeManifest(StrictModel):
    """A target-bound runtime projection that does not imply an installation.

    A first-contact DM authority needs the application identity and worker
    verification keys, but it must not fabricate a guild/user installation or
    depend on the application supporting user installs.  The application home
    signs this deliberately smaller projection for the authenticated target.
    """

    target_domain: FederationDomain
    application: ManifestApplication
    revocation_generation: PositiveDecimal
    workers: list[ManifestWorker] = Field(max_length=100)

    @model_validator(mode="after")
    def valid_runtime_grants(self) -> BotRuntimeManifest:
        if len({worker.id for worker in self.workers}) != len(self.workers):
            raise ValueError("runtime manifest worker IDs must be unique")
        if any(
            set(worker.scopes) - set(self.application.default_scopes)
            or set(worker.intents) - set(self.application.default_intents)
            for worker in self.workers
        ):
            raise ValueError("runtime manifest worker exceeds the application runtime grant")
        return self


def enabled_bot_identity(user: User) -> bool:
    """Return whether a user may act as an exported bot identity."""

    return user.account_type == "bot" and user.disabled_at is None


def manifest_application_payload(application: BotApplication, bot: User) -> dict[str, object]:
    return {
        "id": str(application.id),
        "origin_domain": application.origin_domain,
        "team_id": str(application.team_id),
        "team_domain": application.team_domain,
        "name": application.name,
        "description": application.description,
        "icon_hash": application.icon_hash,
        "support_url": application.support_url,
        "privacy_url": application.privacy_url,
        "status": "active",
        "target_policy": application.target_policy,
        "default_scopes": application.default_scopes,
        "default_intents": application.default_intents,
        "default_permissions": str(application.default_permissions),
        "supported_install_types": application.supported_install_types,
        "user_install_scopes": application.user_install_scopes,
        "user_install_contexts": application.user_install_contexts,
        "e2ee_modes": application.e2ee_modes,
        "manifest_generation": str(application.manifest_generation),
        "command_generation": str(application.command_generation),
        "bot_user": profile_from_user(bot),
    }


def manifest_worker_payload(worker: BotWorker) -> dict[str, object]:
    return {
        "id": str(worker.id),
        "name": worker.name,
        "public_key": encode_base64url(worker.public_key),
        "scopes": worker.scopes,
        "intents": worker.intents,
        "target_domains": worker.target_domains,
        "generation": str(worker.generation),
        "expires_at": worker.expires_at.isoformat() if worker.expires_at else None,
    }


async def local_manifest(
    session: AsyncSession,
    application_id: int,
    template_slug: str | None,
    settings: Settings,
) -> tuple[BotManifest, User]:
    if template_slug is None:
        application_row = (
            await session.execute(
                select(BotApplication, User)
                .join(
                    User,
                    (User.id == BotApplication.bot_user_id)
                    & (User.origin_domain == BotApplication.bot_user_domain),
                )
                .where(
                    BotApplication.id == application_id,
                    BotApplication.origin_domain == settings.domain,
                    BotApplication.status == "active",
                    User.account_type == "bot",
                    User.disabled_at.is_(None),
                )
            )
        ).one_or_none()
        if application_row is None:
            raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
        application, bot = application_row
        if USER_INSTALL not in application.supported_install_types:
            raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
        user_command = await session.scalar(
            select(ApplicationCommand.id)
            .where(
                ApplicationCommand.application_id == application.id,
                ApplicationCommand.application_domain == application.origin_domain,
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.state == "active",
                ApplicationCommand.integration_types.contains(["user_install"]),
            )
            .limit(1)
        )
        if user_command is None:
            raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
        template: BotInstallTemplate | ManifestTemplate = ManifestTemplate(
            id=str(application.id),
            slug="user-install",
            name="User install",
            description=None,
            scopes=list(application.user_install_scopes),
            intents=["interactions"],
            permissions="0",
            contexts=["guild"],
            e2ee_mode="disabled",
            generation=str(application.manifest_generation),
        )
    else:
        template_row = (
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
                    BotApplication.id == application_id,
                    BotApplication.origin_domain == settings.domain,
                    BotApplication.status == "active",
                    BotInstallTemplate.slug == template_slug,
                    BotInstallTemplate.active.is_(True),
                    User.account_type == "bot",
                    User.disabled_at.is_(None),
                )
            )
        ).one_or_none()
        if template_row is None:
            raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
        application, template, bot = template_row
    if not enabled_bot_identity(bot):
        # Keep a runtime fence in addition to the SQL predicate.  This protects
        # callers using an already-populated ORM identity and makes it
        # impossible for a disabled home bot to export fresh worker material.
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    workers = list(
        await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == application.id,
                BotWorker.application_domain == application.origin_domain,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
            )
            .order_by(BotWorker.id)
            .limit(100)
        )
    )
    commands = list(
        await session.scalars(
            select(ApplicationCommand)
            .where(
                ApplicationCommand.application_id == application.id,
                ApplicationCommand.application_domain == application.origin_domain,
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.state == "active",
            )
            .order_by(ApplicationCommand.type, ApplicationCommand.name)
            .limit(130)
        )
    )
    emojis = list(
        await session.scalars(
            select(ApplicationEmoji)
            .where(
                ApplicationEmoji.application_id == application.id,
                ApplicationEmoji.application_domain == application.origin_domain,
            )
            .order_by(ApplicationEmoji.id)
            .limit(2_000)
        )
    )
    manifest = BotManifest.model_validate(
        {
            "application": manifest_application_payload(application, bot),
            "template": {
                "id": str(template.id),
                "slug": template.slug,
                "name": template.name,
                "description": template.description,
                "scopes": template.scopes,
                "intents": template.intents,
                "permissions": str(template.permissions),
                "contexts": template.contexts,
                "e2ee_mode": template.e2ee_mode,
                "generation": str(template.generation),
            },
            "workers": [manifest_worker_payload(worker) for worker in workers],
            "commands": [{"id": str(command.id), **command.definition} for command in commands],
            "emojis": [
                {
                    "id": str(emoji.id),
                    "name": emoji.name,
                    "media_hash": emoji.media_hash,
                    "animated": emoji.animated,
                    "available": emoji.available,
                    "version": str(emoji.version),
                }
                for emoji in emojis
            ],
        }
    )
    return manifest, bot


async def local_runtime_manifest(
    session: AsyncSession,
    application_id: int,
    target_domain: str,
    settings: Settings,
) -> tuple[BotRuntimeManifest, User]:
    """Build an A-authoritative projection without inventing an install.

    Unlike :func:`local_manifest`, this path intentionally has no template or
    command prerequisite.  It is usable only by the authenticated target named
    in the signed content and remains subject to the application's target
    policy.
    """

    application_row = (
        await session.execute(
            select(BotApplication, User)
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == application_id,
                BotApplication.origin_domain == settings.domain,
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
        )
    ).one_or_none()
    if application_row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_RUNTIME_NOT_FOUND"})
    application, bot = application_row
    if not enabled_bot_identity(bot):
        raise HTTPException(status_code=404, detail={"code": "BOT_RUNTIME_NOT_FOUND"})
    rules = {
        rule.target_domain: rule.effect
        for rule in await session.scalars(
            select(BotInstanceRule).where(
                BotInstanceRule.application_id == application.id,
                BotInstanceRule.application_domain == application.origin_domain,
            )
        )
    }
    if not target_policy_allows(application.target_policy, rules, target_domain):
        raise HTTPException(status_code=404, detail={"code": "BOT_RUNTIME_NOT_FOUND"})
    workers = list(
        await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == application.id,
                BotWorker.application_domain == application.origin_domain,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
            )
            .order_by(BotWorker.id)
            .limit(100)
        )
    )
    return (
        BotRuntimeManifest.model_validate(
            {
                "target_domain": target_domain,
                "application": manifest_application_payload(application, bot),
                "revocation_generation": str(application.revocation_generation),
                "workers": [manifest_worker_payload(worker) for worker in workers],
            }
        ),
        bot,
    )


@router.get("/_kaede/v1/applications/{application_id}/manifest")
async def federation_bot_manifest(
    application_id: int,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    template: Annotated[str | None, Query(min_length=2, max_length=64)] = None,
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-manifest",
        capacity=300,
        refill_per_minute=300,
    )
    manifest, bot = await local_manifest(session, application_id, template, settings)
    rules = {
        rule.target_domain: rule.effect
        for rule in await session.scalars(
            select(BotInstanceRule).where(
                BotInstanceRule.application_id == application_id,
                BotInstanceRule.application_domain == settings.domain,
            )
        )
    }
    policy = manifest.application.target_policy
    if not target_policy_allows(policy, rules, principal.origin):
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    return await build_envelope(
        session,
        settings,
        BOT_MANIFEST_EVENT,
        bot,
        manifest.model_dump(mode="json"),
    )


@router.get("/_kaede/v1/applications/{application_id}/runtime-manifest")
async def federation_bot_runtime_manifest(
    application_id: int,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-runtime-manifest",
        capacity=300,
        refill_per_minute=300,
    )
    manifest, bot = await local_runtime_manifest(
        session,
        application_id,
        principal.origin,
        settings,
    )
    return await build_envelope(
        session,
        settings,
        BOT_RUNTIME_MANIFEST_EVENT,
        bot,
        manifest.model_dump(mode="json"),
    )


@router.get("/_kaede/v1/applications/{application_id}/workers/{worker_id}/authorization")
async def federation_worker_authorization(
    application_id: int,
    worker_id: int,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    dm_capability_grant_id: Annotated[
        str | None,
        Query(pattern=r"^kbdg_[A-Za-z0-9_-]{43}$"),
    ] = None,
    dm_capability_revision: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-worker-authorization", capacity=600, refill_per_minute=600
    )
    if (dm_capability_grant_id is None) != (dm_capability_revision is None):
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    row = (
        await session.execute(
            select(BotApplication, BotWorker, User)
            .join(
                BotWorker,
                (BotWorker.application_id == BotApplication.id)
                & (BotWorker.application_domain == BotApplication.origin_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == application_id,
                BotApplication.origin_domain == settings.domain,
                BotApplication.status == "active",
                BotWorker.id == worker_id,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    application, worker, bot = row
    if not enabled_bot_identity(bot):
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    rules = {
        rule.target_domain: rule.effect
        for rule in await session.scalars(
            select(BotInstanceRule).where(
                BotInstanceRule.application_id == application.id,
                BotInstanceRule.application_domain == application.origin_domain,
            )
        )
    }
    if not target_policy_allows(application.target_policy, rules, principal.origin):
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    if not worker_target_allowed(
        worker.target_domains,
        application_domain=application.origin_domain,
        target_domain=principal.origin,
    ):
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    capability_delegation: WorkerDMCapabilityDelegation | None = None
    if dm_capability_grant_id is not None and dm_capability_revision is not None:
        capability = await session.scalar(
            select(BotDMCapability).where(
                BotDMCapability.grant_id == dm_capability_grant_id,
                BotDMCapability.revision == dm_capability_revision,
                BotDMCapability.application_id == application.id,
                BotDMCapability.application_domain == application.origin_domain,
                BotDMCapability.bot_user_id == bot.id,
                BotDMCapability.bot_user_domain == bot.origin_domain,
                BotDMCapability.authority_domain == principal.origin,
                BotDMCapability.conversation_domain == principal.origin,
                BotDMCapability.conversation_id.is_not(None),
                usable_dm_capability(at=datetime.now(UTC)),
            )
        )
        if capability is None:
            raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
        try:
            capability_payload = stored_bot_dm_capability_payload(capability)
            await require_stored_capability_runtime(
                session,
                settings,
                capability_payload,
            )
        except (FederationNetworkError, PermissionError, ValueError) as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "BOT_WORKER_NOT_FOUND"},
            ) from exc
        capability_delegation = WorkerDMCapabilityDelegation(
            grant_id=capability.grant_id,
            revision=str(capability.revision),
            authority_domain=capability.authority_domain,
            conversation_ref=(f"{capability.conversation_id}@{capability.conversation_domain}"),
            expires_at_ms=str(int(capability.expires_at.timestamp() * 1000)),
        )
    authorization = WorkerAuthorization.model_validate(
        {
            "application_id": str(application.id),
            "application_domain": application.origin_domain,
            "bot_user_id": str(bot.id),
            "worker": {
                "id": str(worker.id),
                "name": worker.name,
                "public_key": encode_base64url(worker.public_key),
                "scopes": worker.scopes,
                "intents": worker.intents,
                "target_domains": worker.target_domains,
                "generation": str(worker.generation),
                "expires_at": worker.expires_at.isoformat() if worker.expires_at else None,
            },
            "manifest_generation": str(application.manifest_generation),
            "command_generation": str(application.command_generation),
            "revocation_generation": str(application.revocation_generation),
            "dm_capability": (
                capability_delegation.model_dump(mode="json")
                if capability_delegation is not None
                else None
            ),
        }
    )
    return await build_envelope(
        session, settings, "bot.worker.authorization", bot, authorization.model_dump(mode="json")
    )


def decode_manifest_worker_public_key(worker: ManifestWorker) -> bytes:
    try:
        public_key = decode_base64url(worker.public_key, size=32)
    except ValueError as exc:
        raise ValueError("worker public key is invalid") from exc
    return public_key


@dataclass(frozen=True, slots=True)
class ValidatedWorkerAuthorization:
    authorization: WorkerAuthorization
    worker: ManifestWorker
    public_key: bytes
    application: BotApplication
    manifest_generation: int
    command_generation: int
    revocation_generation: int
    dm_capability: WorkerDMCapabilityDelegation | None


async def validated_worker_authorization(
    session: AsyncSession,
    settings: Settings,
    raw: object,
    *,
    application_id: int,
    application_domain: str,
    worker_id: int,
    dm_capability_grant_id: str | None = None,
    dm_capability_revision: int | None = None,
) -> ValidatedWorkerAuthorization:
    envelope = await validated_event_envelope(session, settings, application_domain, raw)
    if envelope.type != "bot.worker.authorization":
        raise ValueError("worker authorization has the wrong type")
    authorization = WorkerAuthorization.model_validate(envelope.content)
    remote_worker = authorization.worker
    identity = (
        int(authorization.application_id),
        authorization.application_domain,
        int(authorization.bot_user_id),
        envelope.actor.domain,
        int(remote_worker.id),
    )
    if identity != (
        application_id,
        application_domain,
        int(envelope.actor.id),
        application_domain,
        worker_id,
    ):
        raise ValueError("worker authorization identity mismatch")
    delegation = authorization.dm_capability
    if dm_capability_grant_id is None:
        if delegation is not None or dm_capability_revision is not None:
            raise ValueError("worker authorization added an unexpected DM delegation")
    elif (
        dm_capability_revision is None
        or delegation is None
        or delegation.grant_id != dm_capability_grant_id
        or int(delegation.revision) != dm_capability_revision
        or delegation.authority_domain != settings.domain
    ):
        raise ValueError("worker authorization DM delegation identity mismatch")
    application = await session.get(BotApplication, (application_id, application_domain))
    if application is None:
        raise ValueError("worker authorization has no installed application")
    manifest_generation = int(authorization.manifest_generation)
    command_generation = (
        int(authorization.command_generation)
        if authorization.command_generation is not None
        else application.command_generation
    )
    revocation_generation = int(authorization.revocation_generation)
    if (
        manifest_generation < application.manifest_generation
        or command_generation < application.command_generation
        or revocation_generation < application.revocation_generation
    ):
        raise ValueError("worker authorization rolls back an installed generation")
    return ValidatedWorkerAuthorization(
        authorization=authorization,
        worker=remote_worker,
        public_key=decode_manifest_worker_public_key(remote_worker),
        application=application,
        manifest_generation=manifest_generation,
        command_generation=command_generation,
        revocation_generation=revocation_generation,
        dm_capability=delegation,
    )


async def worker_refresh_manifest(
    session: AsyncSession,
    settings: Settings,
    *,
    application_id: int,
    application_domain: str,
) -> tuple[BotManifest, bool]:
    template_slug = await session.scalar(
        select(BotInstallTemplate.slug)
        .where(
            BotInstallTemplate.application_id == application_id,
            BotInstallTemplate.application_domain == application_domain,
            BotInstallTemplate.active.is_(True),
        )
        .order_by(BotInstallTemplate.id)
        .limit(1)
    )
    if template_slug is not None:
        return (
            await fetch_bot_manifest(
                session,
                settings,
                application_id,
                application_domain,
                template_slug,
            ),
            True,
        )
    user_installation_id = await session.scalar(
        select(BotUserInstallation.id)
        .where(
            BotUserInstallation.application_id == application_id,
            BotUserInstallation.application_domain == application_domain,
            usable_user_installation(current_instance_domain=settings.domain),
        )
        .limit(1)
    )
    if user_installation_id is None:
        raise ValueError("worker authorization has no active installation")
    return (
        await fetch_user_bot_manifest(
            session,
            settings,
            application_id,
            application_domain,
        ),
        False,
    )


async def refresh_manifest_for_worker_authorization(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    validated: ValidatedWorkerAuthorization,
) -> None:
    application = validated.application
    if (
        validated.manifest_generation == application.manifest_generation
        and validated.command_generation == application.command_generation
    ):
        return
    manifest, materialize_template = await worker_refresh_manifest(
        session,
        settings,
        application_id=application.id,
        application_domain=application.origin_domain,
    )
    if (
        int(manifest.application.manifest_generation) < validated.manifest_generation
        or int(manifest.application.command_generation) < validated.command_generation
    ):
        raise ValueError("remote manifest is older than worker authorization")
    await materialize_remote_manifest(
        session,
        manifest,
        settings,
        snowflake,
        materialize_template=materialize_template,
    )


def manifest_application_projection(
    application: BotApplication | ManifestApplication,
) -> tuple[object, ...]:
    """Return the canonical application fields governed by manifest_generation."""

    return canonical_application_manifest_projection(
        name=application.name,
        description=application.description,
        icon_hash=application.icon_hash,
        support_url=application.support_url,
        privacy_url=application.privacy_url,
        target_policy=application.target_policy,
        default_scopes=application.default_scopes,
        default_intents=application.default_intents,
        default_permissions=int(application.default_permissions),
        supported_install_types=application.supported_install_types,
        user_install_scopes=application.user_install_scopes,
        user_install_contexts=application.user_install_contexts,
        e2ee_modes=application.e2ee_modes,
    )


def manifest_template_projection(
    template: BotInstallTemplate | ManifestTemplate,
) -> tuple[object, ...]:
    """Return one canonical install-template generation."""

    return (
        template.slug,
        template.name,
        template.description,
        _canonical_unordered(template.scopes),
        _canonical_unordered(template.intents),
        int(template.permissions),
        _canonical_unordered(template.contexts),
        template.e2ee_mode,
    )


def _manifest_worker_expiry(worker: BotWorker | ManifestWorker) -> datetime | None:
    expires_at = worker.expires_at
    if expires_at is None or isinstance(expires_at, datetime):
        return expires_at
    return datetime.fromisoformat(expires_at)


def manifest_worker_projection(
    worker: BotWorker | ManifestWorker,
    *,
    public_key: bytes | None = None,
) -> tuple[object, ...]:
    """Return one canonical worker generation, excluding local revocation holds."""

    if isinstance(worker, BotWorker):
        resolved_public_key = worker.public_key
    else:
        resolved_public_key = public_key or decode_manifest_worker_public_key(worker)
    return (
        worker.name,
        resolved_public_key,
        _canonical_unordered(worker.scopes),
        _canonical_unordered(worker.intents),
        _canonical_unordered(worker.target_domains),
        _manifest_worker_expiry(worker),
    )


def _canonical_command_definition(definition: Mapping[str, object]) -> dict[str, object]:
    canonical = dict(definition)
    for field in ("contexts", "integration_types"):
        values = canonical.get(field)
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            canonical[field] = sorted(values)
    return canonical


def manifest_command_projection(
    command: ApplicationCommand | ManifestCommand,
) -> tuple[int, object, object, object, object, object]:
    """Return one canonical command definition while preserving option order."""

    if isinstance(command, ManifestCommand):
        authority_id = int(command.id)
        definition = command.model_dump(mode="json", exclude={"id"})
    else:
        authority_id = command.authority_id
        definition = command.definition
    return (
        authority_id,
        command.name,
        command.type,
        _canonical_command_definition(definition),
        _canonical_unordered(command.contexts),
        _canonical_unordered(command.integration_types),
    )


def manifest_emoji_projection(
    emoji: ApplicationEmoji | ManifestApplicationEmoji,
) -> tuple[object, ...]:
    """Return one canonical application-emoji version."""

    return (
        emoji.name,
        emoji.media_hash,
        emoji.animated,
        emoji.available,
    )


def projection_generation_advances(
    *,
    label: str,
    created: bool,
    current_generation: int,
    incoming_generation: int,
    stored_projection: Callable[[], object],
    incoming_projection: Callable[[], object],
) -> bool:
    """Enforce monotonic generations and reject same-generation equivocation."""

    if created:
        return True
    if incoming_generation < current_generation:
        raise FederationNetworkError(f"remote bot manifest rolls back {label} generation")
    if incoming_generation == current_generation:
        if stored_projection() != incoming_projection():
            raise FederationNetworkError(f"remote bot manifest equivocates at {label} generation")
        return False
    return True


def apply_manifest_worker(
    worker: BotWorker,
    remote_worker: ManifestWorker,
    public_key: bytes,
    *,
    created: bool,
    allow_stale: bool = False,
) -> None:
    incoming_generation = int(remote_worker.generation)
    if not created and allow_stale and incoming_generation < worker.generation:
        return
    if not projection_generation_advances(
        label="worker",
        created=created,
        current_generation=worker.generation,
        incoming_generation=incoming_generation,
        stored_projection=lambda: manifest_worker_projection(worker),
        incoming_projection=lambda: manifest_worker_projection(
            remote_worker, public_key=public_key
        ),
    ):
        return
    worker.name = remote_worker.name
    worker.public_key = public_key
    worker.scopes = remote_worker.scopes
    worker.intents = remote_worker.intents
    worker.target_domains = remote_worker.target_domains
    worker.generation = incoming_generation
    worker.expires_at = _manifest_worker_expiry(remote_worker)
    restore_remote_worker_if_new(worker, created=created)


async def apply_worker_authorization(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    validated: ValidatedWorkerAuthorization,
) -> None:
    application = validated.application
    remote_worker = validated.worker
    worker_id = int(remote_worker.id)
    worker = await remote_manifest_child(
        session,
        BotWorker,
        source_id=worker_id,
        source_domain=application.origin_domain,
        application_id=application.id,
    )
    created = worker is None
    if worker is None:
        worker = BotWorker(
            id=await snowflake.mint(),
            source_id=worker_id,
            source_domain=application.origin_domain,
            application_id=application.id,
            application_domain=application.origin_domain,
            name=remote_worker.name,
            public_key=validated.public_key,
        )
        session.add(worker)
    activate_remote_application_if_permitted(application, created=False)
    application.manifest_generation = max(
        application.manifest_generation, validated.manifest_generation
    )
    application.command_generation = max(
        application.command_generation, validated.command_generation
    )
    application.revocation_generation = validated.revocation_generation
    apply_manifest_worker(
        worker,
        remote_worker,
        validated.public_key,
        created=created,
        allow_stale=True,
    )
    await session.flush()


async def refresh_remote_worker_authorization(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    application_id: int,
    application_domain: str,
    worker_id: int,
    *,
    dm_capability_grant_id: str | None = None,
    dm_capability_revision: int | None = None,
) -> None:
    application_domain = normalize_domain(application_domain)
    if (dm_capability_grant_id is None) != (dm_capability_revision is None):
        raise FederationNetworkError("remote worker DM delegation is incomplete")
    if dm_capability_grant_id is not None and dm_capability_revision is not None:
        # A capability-only authority may have no guild/user installation from
        # which to materialize the application. Bootstrap from A only after
        # recovering the exact locally admitted B-signed grant.
        capability_row = await session.scalar(
            select(BotDMCapability)
            .where(
                BotDMCapability.grant_id == dm_capability_grant_id,
                BotDMCapability.revision == dm_capability_revision,
                BotDMCapability.application_id == application_id,
                BotDMCapability.application_domain == application_domain,
                BotDMCapability.authority_domain == settings.domain,
                usable_dm_capability(at=datetime.now(UTC)),
            )
            .with_for_update()
        )
        if capability_row is None:
            raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
        try:
            capability = stored_bot_dm_capability_payload(capability_row)
        except ValueError as exc:
            raise FederationNetworkError("stored bot DM capability proof is invalid") from exc
        bot_user_domain = capability.bot_user.domain
        if bot_user_domain is None:
            raise FederationNetworkError("stored bot DM capability bot authority is missing")
        await bootstrap_runtime_application_projection(
            session,
            settings,
            snowflake,
            application_id=application_id,
            application_domain=application_domain,
            bot_user_id=capability.bot_user.id,
            bot_user_domain=bot_user_domain,
            manifest_generation=int(capability.runtime_manifest_generation),
            revocation_generation=int(capability.runtime_revocation_generation),
            access_revocation_generation=int(capability.target_access_revocation_generation),
            runtime_snapshot_fingerprint=bytes.fromhex(capability.runtime_snapshot_fingerprint),
        )
        await require_stored_capability_runtime(
            session,
            settings,
            capability,
        )
    query = (
        {
            "dm_capability_grant_id": dm_capability_grant_id,
            "dm_capability_revision": str(dm_capability_revision),
        }
        if dm_capability_grant_id is not None and dm_capability_revision is not None
        else None
    )
    response = await signed_request(
        session,
        settings,
        "GET",
        application_domain,
        f"/_kaede/v1/applications/{application_id}/workers/{worker_id}/authorization",
        query=query,
        request_timeout=8,
        max_response_bytes=64 * 1024,
    )
    if response.status_code == 404:
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    if response.status_code != 200:
        raise FederationNetworkError("remote worker authorization failed")
    try:
        validated = await validated_worker_authorization(
            session,
            settings,
            decode_federation_response_json(response),
            application_id=application_id,
            application_domain=application_domain,
            worker_id=worker_id,
            dm_capability_grant_id=dm_capability_grant_id,
            dm_capability_revision=dm_capability_revision,
        )
        await refresh_manifest_for_worker_authorization(
            session,
            settings,
            snowflake,
            validated,
        )
        await apply_worker_authorization(session, snowflake, validated)
    except (TypeError, ValueError) as exc:
        raise FederationNetworkError("remote worker authorization is invalid") from exc


async def manifest_response_identity_matches(
    session: AsyncSession,
    application: ManifestApplication,
    *,
    application_id: int,
    application_domain: str,
    actor_id: int,
    actor_domain: str,
    expected_bot_user_id: int | None = None,
    expected_bot_user_domain: str | None = None,
) -> bool:
    """Bind every immutable manifest identity to its request and stored lineage."""

    bot_user_id = int(application.bot_user.id)
    bot_user_domain = application.bot_user.origin_domain
    if (
        int(application.id) != application_id
        or application.origin_domain != application_domain
        or application.team_domain != application_domain
        or bot_user_id != actor_id
        or bot_user_domain != actor_domain
        or (expected_bot_user_id is not None and bot_user_id != expected_bot_user_id)
        or (expected_bot_user_domain is not None and bot_user_domain != expected_bot_user_domain)
    ):
        return False
    stored = await session.get(BotApplication, (application_id, application_domain))
    return stored is None or (
        (stored.team_id, stored.team_domain) == (int(application.team_id), application.team_domain)
        and (stored.bot_user_id, stored.bot_user_domain) == (bot_user_id, bot_user_domain)
    )


async def fetch_bot_manifest(
    session: AsyncSession,
    settings: Settings,
    application_id: int,
    application_domain: str,
    template_slug: str | None,
) -> BotManifest:
    application_domain = normalize_domain(application_domain)
    response = await signed_request(
        session,
        settings,
        "GET",
        application_domain,
        f"/_kaede/v1/applications/{application_id}/manifest",
        query={"template": template_slug} if template_slug is not None else None,
        request_timeout=8,
        max_response_bytes=2 * 1024 * 1024,
    )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    if response.status_code != 200:
        raise FederationNetworkError("remote bot manifest request failed")
    raw = decode_federation_response_json(response)
    try:
        envelope = await validated_event_envelope(session, settings, application_domain, raw)
        if envelope.type != BOT_MANIFEST_EVENT:
            raise ValueError("manifest envelope has the wrong type")
        manifest = BotManifest.model_validate(envelope.content)
        if not await manifest_response_identity_matches(
            session,
            manifest.application,
            application_id=application_id,
            application_domain=application_domain,
            actor_id=int(envelope.actor.id),
            actor_domain=envelope.actor.domain,
        ) or (template_slug is not None and manifest.template.slug != template_slug):
            raise ValueError("manifest identity does not match the request")
        return manifest
    except (TypeError, ValueError) as exc:
        raise FederationNetworkError("remote bot manifest is invalid") from exc


async def fetch_user_bot_manifest(
    session: AsyncSession,
    settings: Settings,
    application_id: int,
    application_domain: str,
) -> BotManifest:
    """Fetch an application-home-signed manifest for a user installation."""

    application_domain = normalize_domain(application_domain)
    response = await signed_request(
        session,
        settings,
        "GET",
        application_domain,
        f"/_kaede/v1/applications/{application_id}/manifest",
        request_timeout=8,
        max_response_bytes=2 * 1024 * 1024,
    )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    if response.status_code != 200:
        raise FederationNetworkError("remote user-install manifest request failed")
    raw = decode_federation_response_json(response)
    try:
        envelope = await validated_event_envelope(session, settings, application_domain, raw)
        if envelope.type != BOT_MANIFEST_EVENT:
            raise ValueError("manifest envelope has the wrong type")
        manifest = BotManifest.model_validate(envelope.content)
        if (
            not await manifest_response_identity_matches(
                session,
                manifest.application,
                application_id=application_id,
                application_domain=application_domain,
                actor_id=int(envelope.actor.id),
                actor_domain=envelope.actor.domain,
            )
            or manifest.template.slug != "user-install"
            or not any("user_install" in command.integration_types for command in manifest.commands)
        ):
            raise ValueError("user-install manifest identity does not match the request")
        return manifest
    except (TypeError, ValueError) as exc:
        raise FederationNetworkError("remote user-install manifest is invalid") from exc


async def refresh_user_bot_application(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    application_id: int,
    application_domain: str,
) -> None:
    """Refresh the app-home half of a federated user-install authority proof."""

    manifest = await fetch_user_bot_manifest(
        session,
        settings,
        application_id,
        application_domain,
    )
    await materialize_remote_manifest(
        session,
        manifest,
        settings,
        snowflake,
        materialize_template=False,
    )


async def fetch_runtime_bot_manifest(
    session: AsyncSession,
    settings: Settings,
    *,
    application_id: int,
    application_domain: str,
    bot_user_id: int,
    bot_user_domain: str,
    manifest_generation: int,
    revocation_generation: int,
) -> BotRuntimeManifest:
    """Fetch the exact A-signed runtime projection already admitted at C."""

    application_domain = normalize_domain(application_domain)
    bot_user_domain = normalize_domain(bot_user_domain)
    response = await signed_request(
        session,
        settings,
        "GET",
        application_domain,
        f"/_kaede/v1/applications/{application_id}/runtime-manifest",
        request_timeout=8,
        max_response_bytes=512 * 1024,
    )
    if response.status_code != 200:
        raise FederationNetworkError("remote bot runtime manifest request failed")
    try:
        envelope = await validated_event_envelope(
            session,
            settings,
            application_domain,
            decode_federation_response_json(response),
        )
        if envelope.type != BOT_RUNTIME_MANIFEST_EVENT:
            raise ValueError("runtime manifest envelope has the wrong type")
        manifest = BotRuntimeManifest.model_validate(envelope.content)
        identity_matches = await manifest_response_identity_matches(
            session,
            manifest.application,
            application_id=application_id,
            application_domain=application_domain,
            actor_id=int(envelope.actor.id),
            actor_domain=envelope.actor.domain,
            expected_bot_user_id=bot_user_id,
            expected_bot_user_domain=bot_user_domain,
        )
        if not identity_matches or (
            manifest.target_domain,
            int(manifest.application.manifest_generation),
            int(manifest.revocation_generation),
        ) != (
            settings.domain,
            manifest_generation,
            revocation_generation,
        ):
            raise ValueError("runtime manifest identity or generation does not match the proof")
        return manifest
    except (TypeError, ValueError) as exc:
        raise FederationNetworkError("remote bot runtime manifest is invalid") from exc


def apply_manifest_application(
    application: BotApplication,
    manifest: BotManifest | BotRuntimeManifest,
    *,
    created: bool,
) -> bool:
    remote = manifest.application
    incoming_generation = int(remote.manifest_generation)
    if not projection_generation_advances(
        label="application",
        created=created,
        current_generation=application.manifest_generation or 1,
        incoming_generation=incoming_generation,
        stored_projection=lambda: manifest_application_projection(application),
        incoming_projection=lambda: manifest_application_projection(remote),
    ):
        return False
    application.name = remote.name
    application.description = remote.description
    application.icon_hash = remote.icon_hash
    application.support_url = remote.support_url
    application.privacy_url = remote.privacy_url
    activate_remote_application_if_permitted(application, created=created)
    application.target_policy = remote.target_policy
    application.default_scopes = remote.default_scopes
    application.default_intents = remote.default_intents
    application.default_permissions = int(remote.default_permissions)
    application.supported_install_types = list(remote.supported_install_types)
    application.user_install_scopes = list(remote.user_install_scopes)
    application.user_install_contexts = list(remote.user_install_contexts)
    application.e2ee_modes = list[str](remote.e2ee_modes)
    application.manifest_generation = incoming_generation
    return True


async def materialize_manifest_application(
    session: AsyncSession,
    manifest: BotManifest | BotRuntimeManifest,
    settings: Settings,
    *,
    app_id: int,
    domain: str,
) -> tuple[BotApplication, User, bool, bool]:
    profile = manifest.application.bot_user
    bot_id = int(profile.id)
    team_id = int(manifest.application.team_id)
    team_domain = manifest.application.team_domain
    if (
        int(manifest.application.id) != app_id
        or manifest.application.origin_domain != domain
        or team_domain != domain
        or profile.origin_domain != domain
    ):
        raise FederationNetworkError("bot manifest application identity is mismatched")
    await lock_bot_projection_identities(
        session,
        application_refs=((app_id, domain),),
        bot_user_refs=((bot_id, domain),),
        team_refs=((team_id, team_domain),),
    )
    bot_application = await bot_application_identity_owner(session, (bot_id, domain))
    if bot_application is not None and (
        bot_application.id,
        bot_application.origin_domain,
    ) != (app_id, domain):
        raise FederationNetworkError("bot manifest reuses another application's bot identity")
    application = await session.get(BotApplication, (app_id, domain))
    if application is not None and (
        (application.team_id, application.team_domain) != (team_id, team_domain)
        or (application.bot_user_id, application.bot_user_domain) != (bot_id, domain)
    ):
        raise FederationNetworkError("bot manifest changed immutable application identity")
    incoming_command_generation = int(manifest.application.command_generation)
    previous_command_generation = (
        application.command_generation or 1 if application is not None else 0
    )
    if application is not None and incoming_command_generation < previous_command_generation:
        raise FederationNetworkError("remote bot manifest rolls back command generation")
    existing_bot = await session.get(User, (bot_id, domain))
    if existing_bot is not None and existing_bot.account_type != "bot":
        raise FederationNetworkError("bot manifest reuses a human identity")
    bot = await upsert_remote_user(session, settings, profile)
    bot.account_type = "bot"
    await session.flush()
    team = await session.get(DeveloperTeam, (team_id, team_domain))
    if team is None:
        team = DeveloperTeam(
            id=team_id,
            origin_domain=team_domain,
            name=manifest_team_placeholder_name(team_id),
            personal=False,
            federation_revision=1,
            federation_metadata_fingerprint=None,
            federation_applications_fingerprint=None,
        )
        session.add(team)
        await session.flush()
    created = application is None
    if application is None:
        application = BotApplication(
            id=app_id,
            origin_domain=domain,
            team_id=team_id,
            team_domain=team_domain,
            bot_user_id=bot.id,
            bot_user_domain=bot.origin_domain,
            name=manifest.application.name,
        )
        session.add(application)
    manifest_advanced = apply_manifest_application(application, manifest, created=created)
    command_advanced = created or incoming_command_generation > previous_command_generation
    application.command_generation = incoming_command_generation
    await session.flush()
    return application, bot, manifest_advanced, command_advanced


async def materialize_manifest_template(
    session: AsyncSession,
    manifest: BotManifest,
    snowflake: SnowflakeGenerator,
    *,
    app_id: int,
    domain: str,
) -> BotInstallTemplate:
    source_id = int(manifest.template.id)
    template = await remote_manifest_child(
        session,
        BotInstallTemplate,
        source_id=source_id,
        source_domain=domain,
        application_id=app_id,
    )
    created = template is None
    if template is None:
        template = BotInstallTemplate(
            id=await snowflake.mint(),
            source_id=source_id,
            source_domain=domain,
            application_id=app_id,
            application_domain=domain,
            slug=manifest.template.slug,
            name=manifest.template.name,
        )
        session.add(template)
    incoming_generation = int(manifest.template.generation)
    if not projection_generation_advances(
        label="template",
        created=created,
        current_generation=template.generation,
        incoming_generation=incoming_generation,
        stored_projection=lambda: manifest_template_projection(template),
        incoming_projection=lambda: manifest_template_projection(manifest.template),
    ):
        return template
    template.slug = manifest.template.slug
    template.name = manifest.template.name
    template.description = manifest.template.description
    template.scopes = manifest.template.scopes
    template.intents = manifest.template.intents
    template.permissions = int(manifest.template.permissions)
    template.contexts = list[str](manifest.template.contexts)
    template.e2ee_mode = manifest.template.e2ee_mode
    template.generation = incoming_generation
    template.active = True
    return template


async def materialize_manifest_commands(
    session: AsyncSession,
    manifest: BotManifest,
    snowflake: SnowflakeGenerator,
    *,
    app_id: int,
    domain: str,
    generation_advanced: bool,
) -> None:
    stored_commands = list(
        await session.scalars(
            select(ApplicationCommand).where(
                ApplicationCommand.application_id == app_id,
                ApplicationCommand.application_domain == domain,
                ApplicationCommand.guild_id.is_(None),
            )
        )
    )
    if not generation_advanced:
        stored_projection = sorted(
            (
                manifest_command_projection(command)
                for command in stored_commands
                if command.state == "active"
            ),
            key=lambda item: item[0],
        )
        incoming_projection = sorted(
            (manifest_command_projection(command) for command in manifest.commands),
            key=lambda item: item[0],
        )
        if stored_projection != incoming_projection:
            raise FederationNetworkError("remote bot manifest equivocates at command generation")
        return
    source_ids: set[int] = set()
    for remote_command in manifest.commands:
        source_id = int(remote_command.id)
        source_ids.add(source_id)
        command = await remote_manifest_child(
            session,
            ApplicationCommand,
            source_id=source_id,
            source_domain=domain,
            application_id=app_id,
        )
        definition = remote_command.model_dump(mode="json", exclude={"id"})
        if command is None:
            command = ApplicationCommand(
                id=await snowflake.mint(),
                source_id=source_id,
                source_domain=domain,
                application_id=app_id,
                application_domain=domain,
                name=remote_command.name,
                type=remote_command.type,
                definition=definition,
            )
            session.add(command)
        command.name = remote_command.name
        command.type = remote_command.type
        command.definition = definition
        command.contexts = list(remote_command.contexts)
        command.integration_types = list(remote_command.integration_types)
        command.generation = int(manifest.application.command_generation)
        command.state = "active"
    for command in stored_commands:
        if command.authority_id not in source_ids:
            command.state = "superseded"


def apply_manifest_emoji(
    emoji: ApplicationEmoji,
    remote: ManifestApplicationEmoji,
    bot: User,
) -> None:
    emoji.name = remote.name
    emoji.name_casefold = remote.name.casefold()
    emoji.media_hash = remote.media_hash
    emoji.object_key = None
    emoji.animated = remote.animated
    emoji.available = remote.available
    emoji.creator_id = bot.id
    emoji.creator_domain = bot.origin_domain
    emoji.version = int(remote.version)


async def materialize_manifest_emojis(
    session: AsyncSession,
    manifest: BotManifest,
    bot: User,
    *,
    app_id: int,
    domain: str,
    manifest_generation_advanced: bool,
) -> None:
    source_ids: set[int] = set()
    for remote_emoji in manifest.emojis:
        emoji_id = int(remote_emoji.id)
        source_ids.add(emoji_id)
        emoji = await session.get(ApplicationEmoji, (emoji_id, domain))
        if emoji is not None and emoji.application_id != app_id:
            raise FederationNetworkError("bot manifest reuses an application emoji identity")
        created = emoji is None
        if emoji is None:
            emoji = ApplicationEmoji(
                id=emoji_id,
                application_id=app_id,
                application_domain=domain,
                name=remote_emoji.name,
                name_casefold=remote_emoji.name.casefold(),
                media_hash=remote_emoji.media_hash,
                object_key=None,
                animated=remote_emoji.animated,
                available=remote_emoji.available,
                creator_id=bot.id,
                creator_domain=bot.origin_domain,
                version=int(remote_emoji.version),
            )
            session.add(emoji)
        elif not projection_generation_advances(
            label="application emoji",
            created=False,
            current_generation=emoji.version,
            incoming_generation=int(remote_emoji.version),
            stored_projection=partial(manifest_emoji_projection, emoji),
            incoming_projection=partial(manifest_emoji_projection, remote_emoji),
        ):
            continue
        if created:
            continue
        apply_manifest_emoji(emoji, remote_emoji, bot)
    if not manifest_generation_advanced:
        return
    for emoji in await session.scalars(
        select(ApplicationEmoji).where(
            ApplicationEmoji.application_id == app_id,
            ApplicationEmoji.application_domain == domain,
        )
    ):
        if emoji.id not in source_ids:
            await session.delete(emoji)


async def materialize_manifest_workers(
    session: AsyncSession,
    manifest: BotManifest | BotRuntimeManifest,
    snowflake: SnowflakeGenerator,
    *,
    app_id: int,
    domain: str,
    manifest_generation_advanced: bool,
) -> None:
    source_ids: set[int] = set()
    for remote_worker in manifest.workers:
        source_id = int(remote_worker.id)
        source_ids.add(source_id)
        try:
            public_key = decode_manifest_worker_public_key(remote_worker)
        except ValueError as exc:
            raise FederationNetworkError(str(exc)) from exc
        worker = await remote_manifest_child(
            session,
            BotWorker,
            source_id=source_id,
            source_domain=domain,
            application_id=app_id,
        )
        created = worker is None
        if worker is None:
            worker = BotWorker(
                id=await snowflake.mint(),
                source_id=source_id,
                source_domain=domain,
                application_id=app_id,
                application_domain=domain,
                name=remote_worker.name,
                public_key=public_key,
            )
            session.add(worker)
        apply_manifest_worker(worker, remote_worker, public_key, created=created)
    if not manifest_generation_advanced:
        return
    for worker in await session.scalars(
        select(BotWorker)
        .where(
            BotWorker.application_id == app_id,
            BotWorker.application_domain == domain,
            BotWorker.revoked_at.is_(None),
        )
        .with_for_update()
    ):
        if worker.authority_id not in source_ids:
            worker.revoked_at = datetime.now(UTC)


async def materialize_runtime_bot_manifest(
    session: AsyncSession,
    manifest: BotRuntimeManifest,
    settings: Settings,
    snowflake: SnowflakeGenerator,
) -> tuple[BotApplication, User, BotApplicationTarget]:
    """Materialize only the A-owned runtime identity needed by a DM target."""

    app_id = int(manifest.application.id)
    domain = normalize_domain(manifest.application.origin_domain)
    incoming_revocation_generation = int(manifest.revocation_generation)
    if domain == settings.domain or manifest.target_domain != settings.domain:
        raise ValueError("runtime manifest materializer received the wrong authority")
    application, bot, manifest_advanced, _command_advanced = await materialize_manifest_application(
        session,
        manifest,
        settings,
        app_id=app_id,
        domain=domain,
    )
    if incoming_revocation_generation < application.revocation_generation:
        raise FederationNetworkError("remote bot runtime manifest rolls back revocation state")
    application.revocation_generation = incoming_revocation_generation
    await materialize_manifest_workers(
        session,
        manifest,
        snowflake,
        app_id=app_id,
        domain=domain,
        manifest_generation_advanced=manifest_advanced,
    )
    await session.flush()
    target = await promote_application_runtime_highwater(
        session,
        application,
        target_domain=settings.domain,
    )
    if (
        target is None
        or application.status != "active"
        or application.manifest_generation != int(manifest.application.manifest_generation)
        or application.revocation_generation != incoming_revocation_generation
        or target.runtime_manifest_generation != application.manifest_generation
        or target.runtime_revocation_generation != application.revocation_generation
        or target.runtime_status != "active"
        or target.runtime_target_allowed is not True
        or target.runtime_fingerprint is None
    ):
        raise FederationNetworkError("runtime manifest has no exact admitted target proof")
    return application, bot, target


def exact_runtime_target_projection(
    target: BotApplicationTarget | None,
    *,
    manifest_generation: int,
    revocation_generation: int,
    access_revocation_generation: int,
    runtime_snapshot_fingerprint: bytes,
) -> bool:
    return bool(
        target is not None
        and target.runtime_manifest_generation == manifest_generation
        and target.runtime_revocation_generation == revocation_generation
        and target.runtime_access_revocation_generation == access_revocation_generation
        and target.runtime_status == "active"
        and target.runtime_target_allowed is True
        and target.runtime_fingerprint == runtime_snapshot_fingerprint
    )


async def current_runtime_application_projection(
    session: AsyncSession,
    settings: Settings,
    *,
    application_id: int,
    application_domain: str,
    bot_user_id: int,
    bot_user_domain: str,
    manifest_generation: int,
    revocation_generation: int,
    access_revocation_generation: int,
    runtime_snapshot_fingerprint: bytes,
) -> tuple[BotApplication, User, BotApplicationTarget] | None:
    """Return an exact established projection without contacting A."""

    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == application_id,
            BotApplication.origin_domain == application_domain,
        )
        .with_for_update()
    )
    if application is None:
        return None
    if (
        (application.bot_user_id, application.bot_user_domain) != (bot_user_id, bot_user_domain)
        or application.status != "active"
        or application.manifest_generation > manifest_generation
        or application.revocation_generation > revocation_generation
    ):
        raise FederationNetworkError("runtime application projection conflicts with the proof")
    if (
        application.manifest_generation != manifest_generation
        or application.revocation_generation != revocation_generation
    ):
        return None
    bot = await session.get(User, (bot_user_id, bot_user_domain))
    if (
        bot is None
        or bot.account_type != "bot"
        or bot.disabled_at is not None
        or bot.is_local != (application_domain == settings.domain)
    ):
        raise FederationNetworkError("runtime application bot projection is invalid")
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application_id,
            BotApplicationTarget.application_domain == application_domain,
            BotApplicationTarget.target_domain == settings.domain,
        )
        .with_for_update()
    )
    if target is not None and exact_runtime_target_projection(
        target,
        manifest_generation=manifest_generation,
        revocation_generation=revocation_generation,
        access_revocation_generation=access_revocation_generation,
        runtime_snapshot_fingerprint=runtime_snapshot_fingerprint,
    ):
        return application, bot, target
    # A runtime proof may have arrived before the complete manifest and stayed
    # in the bounded pending ledger.  Promote it locally before considering a
    # network refresh.
    target = await promote_application_runtime_highwater(
        session,
        application,
        target_domain=settings.domain,
    )
    if target is not None and exact_runtime_target_projection(
        target,
        manifest_generation=manifest_generation,
        revocation_generation=revocation_generation,
        access_revocation_generation=access_revocation_generation,
        runtime_snapshot_fingerprint=runtime_snapshot_fingerprint,
    ):
        return application, bot, target
    if target is not None:
        raise FederationNetworkError("runtime target projection conflicts with the proof")
    return None


async def bootstrap_runtime_application_projection(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    *,
    application_id: int,
    application_domain: str,
    bot_user_id: int,
    bot_user_domain: str,
    manifest_generation: int,
    revocation_generation: int,
    access_revocation_generation: int,
    runtime_snapshot_fingerprint: bytes,
) -> tuple[BotApplication, User, BotApplicationTarget]:
    """Fetch and promote the exact full projection for a pending A proof.

    The caller must first durably validate/store the A runtime proof for this
    target.  Materialization fails when that pending proof is absent, expired,
    denied, or no longer matches A's current signed manifest generations.
    """

    application_domain = normalize_domain(application_domain)
    bot_user_domain = normalize_domain(bot_user_domain)
    if access_revocation_generation < 0 or len(runtime_snapshot_fingerprint) != 32:
        raise ValueError("runtime application proof lineage is invalid")
    current = await current_runtime_application_projection(
        session,
        settings,
        application_id=application_id,
        application_domain=application_domain,
        bot_user_id=bot_user_id,
        bot_user_domain=bot_user_domain,
        manifest_generation=manifest_generation,
        revocation_generation=revocation_generation,
        access_revocation_generation=access_revocation_generation,
        runtime_snapshot_fingerprint=runtime_snapshot_fingerprint,
    )
    if current is not None:
        return current
    if application_domain == settings.domain:
        raise ValueError("local runtime application projection is unavailable")
    manifest = await fetch_runtime_bot_manifest(
        session,
        settings,
        application_id=application_id,
        application_domain=application_domain,
        bot_user_id=bot_user_id,
        bot_user_domain=bot_user_domain,
        manifest_generation=manifest_generation,
        revocation_generation=revocation_generation,
    )
    projected = await materialize_runtime_bot_manifest(
        session,
        manifest,
        settings,
        snowflake,
    )
    if not exact_runtime_target_projection(
        projected[2],
        manifest_generation=manifest_generation,
        revocation_generation=revocation_generation,
        access_revocation_generation=access_revocation_generation,
        runtime_snapshot_fingerprint=runtime_snapshot_fingerprint,
    ):
        raise FederationNetworkError("runtime manifest did not promote the exact proof")
    return projected


async def materialize_remote_manifest(
    session: AsyncSession,
    manifest: BotManifest,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    *,
    materialize_template: bool = True,
) -> tuple[BotApplication, BotInstallTemplate | None, User]:
    app_id = int(manifest.application.id)
    domain = normalize_domain(manifest.application.origin_domain)
    if domain == settings.domain:
        raise ValueError("remote manifest materializer received a local application")
    application, bot, manifest_advanced, command_advanced = await materialize_manifest_application(
        session,
        manifest,
        settings,
        app_id=app_id,
        domain=domain,
    )
    template = (
        await materialize_manifest_template(
            session,
            manifest,
            snowflake,
            app_id=app_id,
            domain=domain,
        )
        if materialize_template
        else None
    )
    await materialize_manifest_commands(
        session,
        manifest,
        snowflake,
        app_id=app_id,
        domain=domain,
        generation_advanced=command_advanced,
    )
    await materialize_manifest_emojis(
        session,
        manifest,
        bot,
        app_id=app_id,
        domain=domain,
        manifest_generation_advanced=manifest_advanced,
    )
    await materialize_manifest_workers(
        session,
        manifest,
        snowflake,
        app_id=app_id,
        domain=domain,
        manifest_generation_advanced=manifest_advanced,
    )
    return application, template, bot
