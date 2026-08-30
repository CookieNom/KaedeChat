from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import (
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from redis.asyncio import Redis
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.bots.application_contract import validate_application_https_url
from app.bots.directory_contract import (
    DirectoryDescriptionLocalizations,
    DirectoryExternalLinks,
    DirectoryImageMedia,
    DirectoryMediaList,
    DirectorySupportedLocales,
    DirectoryYouTubeMedia,
    directory_image_asset_ids,
    validate_directory_localizations,
)
from app.bots.target_contract import target_policy_allows
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings, get_settings
from app.core.types import EntityRef
from app.db.bot_models import (
    ApplicationAsset,
    ApplicationCommand,
    BotApplication,
    BotInstallTemplate,
    BotInstanceRule,
    BotInteraction,
)
from app.db.models import User
from app.federation.client import signed_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.schemas import SnowflakeString
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)

router = APIRouter(prefix="/api/v1", tags=["application directory"])
federation_router = APIRouter(tags=["application directory federation"])

DirectoryCategory = Literal[
    "entertainment", "games", "moderation", "productivity", "social", "utilities"
]
DirectoryCollection = Literal["featured", "staff-picks", "new-and-noteworthy"]
DirectoryReadinessKey = Literal[
    "directory_enabled",
    "summary",
    "category",
    "tags",
    "description",
    "support_url",
    "privacy_url",
    "terms_url",
    "media",
    "external_links",
    "supported_locales",
    "description_localizations",
    "install_path",
    "user_install_command",
]
DIRECTORY_READINESS_KEYS: tuple[DirectoryReadinessKey, ...] = (
    "directory_enabled",
    "summary",
    "category",
    "tags",
    "description",
    "support_url",
    "privacy_url",
    "terms_url",
    "media",
    "external_links",
    "supported_locales",
    "description_localizations",
    "install_path",
    "user_install_command",
)
DIRECTORY_COLLECTIONS = (
    {
        "slug": "featured",
        "name": "Featured",
        "description": "Apps selected by this instance's directory team.",
    },
    {
        "slug": "staff-picks",
        "name": "Staff Picks",
        "description": "Apps recommended by this instance's staff.",
    },
    {
        "slug": "new-and-noteworthy",
        "name": "New & Noteworthy",
        "description": "Recently highlighted apps worth discovering.",
    },
)


class StrictDirectoryModel(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")


class DirectorySearch(StrictDirectoryModel):
    q: str | None = Field(default=None, max_length=100)
    category: DirectoryCategory | None = None
    tag: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    collection: DirectoryCollection | None = None
    limit: int = Field(default=25, ge=1, le=50)
    after: SnowflakeString | None = None


class DirectoryTemplate(StrictDirectoryModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(max_length=500)
    install_types: list[Literal["guild_install", "user_install"]] = Field(
        min_length=1, max_length=2
    )
    default_install_type: Literal["guild_install", "user_install"]

    @model_validator(mode="after")
    def coherent_install_types(self) -> DirectoryTemplate:
        if len(self.install_types) != len(set(self.install_types)):
            raise ValueError("directory install types must be unique")
        if self.default_install_type not in self.install_types:
            raise ValueError("default install type is not supported")
        return self


class DirectoryBotProfileApplication(StrictDirectoryModel):
    """Public Add App identity resolved by the bot account's authority."""

    bot_ref: str = Field(max_length=320)
    application_ref: str = Field(max_length=320)
    origin_domain: str = Field(max_length=253)
    name: str = Field(min_length=1, max_length=100)
    install_template: DirectoryTemplate
    directory_listed: bool

    @model_validator(mode="after")
    def coherent_identity(self) -> DirectoryBotProfileApplication:
        try:
            bot_ref = EntityRef(self.bot_ref)
            application_ref = EntityRef(self.application_ref)
        except ValueError:
            raise ValueError("bot profile application identity is invalid") from None
        if (
            bot_ref.domain is None
            or application_ref.domain is None
            or bot_ref.domain != self.origin_domain
            or application_ref.domain != self.origin_domain
        ):
            raise ValueError("bot profile application identity is inconsistent")
        return self


class DirectoryImage(StrictDirectoryModel):
    type: Literal["image"]
    asset_id: str = Field(pattern=r"^[1-9][0-9]*$")
    name: str = Field(min_length=1, max_length=100)
    media_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(pattern=r"^image/[a-z0-9.+-]+$", max_length=100)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def complete_dimensions(self) -> DirectoryImage:
        if (self.width is None) != (self.height is None):
            raise ValueError("directory image dimensions must be complete")
        return self


class DirectoryYouTubeVideo(StrictDirectoryModel):
    type: Literal["youtube"]
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")


DirectoryMedia = Annotated[
    DirectoryImage | DirectoryYouTubeVideo,
    Field(discriminator="type"),
]


class DirectoryCollectionMetadata(StrictDirectoryModel):
    slug: DirectoryCollection
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=300)


class DirectoryApplicationSummaryFields(StrictDirectoryModel):
    id: str = Field(pattern=r"^[1-9][0-9]*$")
    ref: str = Field(max_length=320)
    origin_domain: str = Field(max_length=253)
    name: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=200)
    category: DirectoryCategory
    tags: list[str] = Field(min_length=1, max_length=5)
    collections: list[DirectoryCollection] = Field(max_length=3)
    icon_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    banner_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool
    install_template: DirectoryTemplate
    user_install_supported: bool

    @field_validator("tags")
    @classmethod
    def canonical_tags(cls, value: list[str]) -> list[str]:
        if value != list(dict.fromkeys(value)) or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", item) for item in value
        ):
            raise ValueError("directory tags are not canonical")
        return value

    @field_validator("collections")
    @classmethod
    def unique_collections(cls, value: list[DirectoryCollection]) -> list[DirectoryCollection]:
        if len(value) != len(set(value)):
            raise ValueError("directory collections must be unique")
        return value

    @model_validator(mode="after")
    def coherent_identity(self) -> DirectoryApplicationSummaryFields:
        try:
            parsed = EntityRef(self.ref)
        except ValueError:
            raise ValueError("directory application reference is invalid") from None
        if (
            parsed.domain is None
            or str(parsed.id) != self.id
            or parsed.domain != self.origin_domain
        ):
            raise ValueError("directory application identity is inconsistent")
        if self.user_install_supported != ("user_install" in self.install_template.install_types):
            raise ValueError("directory user-install support is inconsistent")
        return self


class DirectoryApplicationSummary(DirectoryApplicationSummaryFields):
    @model_validator(mode="after")
    def publicly_approved(self) -> DirectoryApplicationSummary:
        if not self.verified:
            raise ValueError("directory application is not approved")
        return self


class DirectoryPopularCommand(StrictDirectoryModel):
    id: str = Field(pattern=r"^[1-9][0-9]*$")
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=100)


class DirectorySimilarApplication(StrictDirectoryModel):
    id: str = Field(pattern=r"^[1-9][0-9]*$")
    ref: str = Field(max_length=320)
    origin_domain: str = Field(max_length=253)
    name: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=200)
    category: DirectoryCategory
    tags: list[str] = Field(min_length=1, max_length=5)
    icon_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("tags")
    @classmethod
    def canonical_tags(cls, value: list[str]) -> list[str]:
        if value != list(dict.fromkeys(value)) or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", item) for item in value
        ):
            raise ValueError("directory tags are not canonical")
        return value

    @model_validator(mode="after")
    def coherent_identity(self) -> DirectorySimilarApplication:
        try:
            parsed = EntityRef(self.ref)
        except ValueError:
            raise ValueError("similar application reference is invalid") from None
        if (
            parsed.domain is None
            or str(parsed.id) != self.id
            or parsed.domain != self.origin_domain
        ):
            raise ValueError("similar application identity is inconsistent")
        return self


class DirectoryApplicationProduct(DirectoryApplicationSummaryFields):
    description: str = Field(min_length=1, max_length=1000)
    support_url: HttpUrl = Field(max_length=2048)
    privacy_policy_url: HttpUrl = Field(max_length=2048)
    terms_url: HttpUrl = Field(max_length=2048)
    media: list[DirectoryMedia] = Field(max_length=5)
    external_links: DirectoryExternalLinks
    supported_locales: DirectorySupportedLocales
    description_localizations: DirectoryDescriptionLocalizations
    popular_commands: list[DirectoryPopularCommand] = Field(max_length=5)
    similar_apps: list[DirectorySimilarApplication] = Field(max_length=3)

    @field_validator("support_url", "privacy_policy_url", "terms_url")
    @classmethod
    def safe_external_url(cls, value: HttpUrl) -> HttpUrl:
        validate_application_https_url(str(value))
        return value

    @model_validator(mode="after")
    def coherent_product_page(self) -> DirectoryApplicationProduct:
        validate_directory_localizations(
            list(self.supported_locales),
            dict(self.description_localizations),
        )
        media_ids = [
            (item.type, item.asset_id if isinstance(item, DirectoryImage) else item.video_id)
            for item in self.media
        ]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("directory media entries must be unique")
        command_ids = [item.id for item in self.popular_commands]
        command_names = [item.name for item in self.popular_commands]
        if len(command_ids) != len(set(command_ids)) or len(command_names) != len(
            set(command_names)
        ):
            raise ValueError("popular directory commands must be unique")
        similar_refs = [item.ref for item in self.similar_apps]
        if (
            len(similar_refs) != len(set(similar_refs))
            or self.ref in similar_refs
            or any(item.origin_domain != self.origin_domain for item in self.similar_apps)
        ):
            raise ValueError("similar directory applications are inconsistent")
        return self


class DirectoryApplication(DirectoryApplicationProduct):
    @model_validator(mode="after")
    def publicly_approved(self) -> DirectoryApplication:
        if not self.verified:
            raise ValueError("directory application is not approved")
        return self


class DirectoryPreviewApplication(StrictDirectoryModel):
    """Strict saved draft; public eligibility fields deliberately remain nullable."""

    id: str = Field(pattern=r"^[1-9][0-9]*$")
    ref: str = Field(max_length=320)
    origin_domain: str = Field(max_length=253)
    name: str = Field(min_length=1, max_length=100)
    summary: str | None = Field(default=None, min_length=1, max_length=200)
    category: DirectoryCategory | None = None
    tags: list[str] = Field(max_length=5)
    collections: list[DirectoryCollection] = Field(max_length=3)
    icon_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    banner_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool
    install_template: DirectoryTemplate | None
    user_install_supported: bool
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    support_url: HttpUrl | None = Field(default=None, max_length=2048)
    privacy_policy_url: HttpUrl | None = Field(default=None, max_length=2048)
    terms_url: HttpUrl | None = Field(default=None, max_length=2048)
    media: list[DirectoryMedia] = Field(max_length=5)
    external_links: DirectoryExternalLinks
    supported_locales: DirectorySupportedLocales
    description_localizations: DirectoryDescriptionLocalizations
    popular_commands: list[DirectoryPopularCommand] = Field(max_length=5)
    similar_apps: list[DirectorySimilarApplication] = Field(max_length=3)

    @field_validator("tags")
    @classmethod
    def canonical_tags(cls, value: list[str]) -> list[str]:
        if value != list(dict.fromkeys(value)) or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", item) for item in value
        ):
            raise ValueError("directory tags are not canonical")
        return value

    @field_validator("collections")
    @classmethod
    def unique_collections(cls, value: list[DirectoryCollection]) -> list[DirectoryCollection]:
        if len(value) != len(set(value)):
            raise ValueError("directory collections must be unique")
        return value

    @field_validator("support_url", "privacy_policy_url", "terms_url")
    @classmethod
    def safe_external_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None:
            validate_application_https_url(str(value))
        return value

    @model_validator(mode="after")
    def coherent_draft(self) -> DirectoryPreviewApplication:
        try:
            parsed = EntityRef(self.ref)
        except ValueError:
            raise ValueError("directory preview application reference is invalid") from None
        if (
            parsed.domain is None
            or str(parsed.id) != self.id
            or parsed.domain != self.origin_domain
        ):
            raise ValueError("directory preview application identity is inconsistent")
        if self.install_template is not None and self.user_install_supported != (
            "user_install" in self.install_template.install_types
        ):
            raise ValueError("directory preview user-install support is inconsistent")
        validate_directory_localizations(
            list(self.supported_locales),
            dict(self.description_localizations),
        )
        media_ids = [
            (item.type, item.asset_id if isinstance(item, DirectoryImage) else item.video_id)
            for item in self.media
        ]
        command_ids = [item.id for item in self.popular_commands]
        command_names = [item.name for item in self.popular_commands]
        similar_refs = [item.ref for item in self.similar_apps]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("directory preview media entries must be unique")
        if len(command_ids) != len(set(command_ids)) or len(command_names) != len(
            set(command_names)
        ):
            raise ValueError("popular directory preview commands must be unique")
        if (
            len(similar_refs) != len(set(similar_refs))
            or self.ref in similar_refs
            or any(item.origin_domain != self.origin_domain for item in self.similar_apps)
        ):
            raise ValueError("similar directory preview applications are inconsistent")
        return self


class DirectoryReadinessItem(StrictDirectoryModel):
    key: DirectoryReadinessKey
    ready: bool


class DirectoryReadiness(StrictDirectoryModel):
    status: Literal["incomplete", "ready_for_review", "approved"]
    ready: bool
    preview_available: bool
    missing: list[DirectoryReadinessKey] = Field(max_length=len(DIRECTORY_READINESS_KEYS))
    items: list[DirectoryReadinessItem] = Field(
        min_length=len(DIRECTORY_READINESS_KEYS),
        max_length=len(DIRECTORY_READINESS_KEYS),
    )

    @model_validator(mode="after")
    def coherent_checklist(self) -> DirectoryReadiness:
        keys = [item.key for item in self.items]
        missing = [item.key for item in self.items if not item.ready]
        if keys != list(DIRECTORY_READINESS_KEYS) or self.missing != missing:
            raise ValueError("directory readiness checklist is not canonical")
        if self.ready != (not missing):
            raise ValueError("directory readiness status is inconsistent")
        return self


class DirectoryPreviewResponse(StrictDirectoryModel):
    application_ref: str = Field(max_length=320)
    application: DirectoryPreviewApplication
    readiness: DirectoryReadiness

    @model_validator(mode="after")
    def coherent_preview(self) -> DirectoryPreviewResponse:
        try:
            ref = EntityRef(self.application_ref)
        except ValueError:
            raise ValueError("directory preview reference is invalid") from None
        if ref.domain is None:
            raise ValueError("directory preview reference is not qualified")
        if not self.readiness.preview_available:
            raise ValueError("directory preview availability is inconsistent")
        if (
            int(self.application.id),
            self.application.origin_domain,
        ) != (ref.id, ref.domain):
            raise ValueError("directory preview application identity is inconsistent")
        expected_status = (
            "approved"
            if self.readiness.ready and self.application.verified
            else "ready_for_review"
            if self.readiness.ready
            else "incomplete"
        )
        if self.readiness.status != expected_status:
            raise ValueError("directory preview status is inconsistent")
        return self


class DirectoryPage(StrictDirectoryModel):
    items: list[DirectoryApplicationSummary] = Field(max_length=50)
    next_cursor: str | None = Field(pattern=r"^[1-9][0-9]*$")
    collections: list[DirectoryCollectionMetadata] = Field(max_length=3)
    selected_collection: DirectoryCollection | None

    @field_validator("collections")
    @classmethod
    def canonical_collections(
        cls, value: list[DirectoryCollectionMetadata]
    ) -> list[DirectoryCollectionMetadata]:
        expected = [
            DirectoryCollectionMetadata.model_validate(item) for item in DIRECTORY_COLLECTIONS
        ]
        if value != expected:
            raise ValueError("directory collection catalog is not canonical")
        return value

    @model_validator(mode="after")
    def coherent_page(self) -> DirectoryPage:
        item_ids = [int(item.id) for item in self.items]
        if item_ids != sorted(set(item_ids)):
            raise ValueError("directory page items must be unique and ordered")
        if self.next_cursor is not None and (
            not self.items or self.next_cursor != self.items[-1].id
        ):
            raise ValueError("directory page cursor is inconsistent")
        return self


_DIRECTORY_MEDIA_ADAPTER = TypeAdapter(DirectoryMediaList)
_DIRECTORY_EXTERNAL_LINKS_ADAPTER = TypeAdapter(DirectoryExternalLinks)
_DIRECTORY_SUPPORTED_LOCALES_ADAPTER = TypeAdapter(DirectorySupportedLocales)
_DIRECTORY_DESCRIPTION_LOCALIZATIONS_ADAPTER = TypeAdapter(DirectoryDescriptionLocalizations)


def parsed_directory_media(raw: object) -> list[DirectoryImageMedia | DirectoryYouTubeMedia]:
    return _DIRECTORY_MEDIA_ADAPTER.validate_python(raw)


async def resolved_directory_media(
    session: AsyncSession,
    app: BotApplication,
    raw: object | None = None,
) -> (
    tuple[
        list[DirectoryImageMedia | DirectoryYouTubeMedia],
        dict[int, ApplicationAsset],
    ]
    | None
):
    try:
        media = parsed_directory_media(getattr(app, "directory_media", []) if raw is None else raw)
    except ValidationError:
        return None
    image_ids = directory_image_asset_ids(media)
    if not image_ids:
        return media, {}
    assets = list(
        await session.scalars(
            select(ApplicationAsset).where(
                ApplicationAsset.id.in_(image_ids),
                ApplicationAsset.application_id == app.id,
                ApplicationAsset.application_domain == app.origin_domain,
                ApplicationAsset.kind == "store",
            )
        )
    )
    by_id = {asset.id: asset for asset in assets}
    if set(by_id) != set(image_ids):
        return None
    return media, by_id


async def directory_media_assets_valid(
    session: AsyncSession,
    app: BotApplication,
    raw: object | None = None,
) -> bool:
    return await resolved_directory_media(session, app, raw) is not None


def directory_optional_metadata_errors(
    app: BotApplication,
    values: dict[str, object] | None = None,
) -> list[DirectoryReadinessKey]:
    pending = values or {}
    errors: list[DirectoryReadinessKey] = []
    try:
        _DIRECTORY_EXTERNAL_LINKS_ADAPTER.validate_python(
            pending.get(
                "directory_external_links",
                getattr(app, "directory_external_links", []),
            )
        )
    except ValidationError:
        errors.append("external_links")
    try:
        supported_locales = _DIRECTORY_SUPPORTED_LOCALES_ADAPTER.validate_python(
            pending.get(
                "directory_supported_locales",
                getattr(app, "directory_supported_locales", []),
            )
        )
    except ValidationError:
        errors.append("supported_locales")
        supported_locales = []
    try:
        descriptions = _DIRECTORY_DESCRIPTION_LOCALIZATIONS_ADAPTER.validate_python(
            pending.get(
                "directory_description_localizations",
                getattr(app, "directory_description_localizations", {}),
            )
        )
    except ValidationError:
        errors.append("description_localizations")
        descriptions = {}
    try:
        validate_directory_localizations(list(supported_locales), dict(descriptions))
    except ValueError:
        if "description_localizations" not in errors:
            errors.append("description_localizations")
    return errors


def directory_template_projection(
    app: BotApplication,
    template: BotInstallTemplate | None,
) -> dict[str, object] | None:
    install_types = list(app.supported_install_types)
    return (
        {
            "slug": template.slug,
            "name": template.name,
            "description": template.description,
            "install_types": install_types,
            "default_install_type": (
                "guild_install" if "guild_install" in install_types else "user_install"
            ),
        }
        if template is not None
        else None
    )


def directory_projection(
    app: BotApplication,
    template: BotInstallTemplate | None,
) -> dict[str, object]:
    template_payload = directory_template_projection(app, template)
    return {
        "id": str(app.id),
        "ref": f"{app.id}@{app.origin_domain}",
        "origin_domain": app.origin_domain,
        "name": app.name,
        "summary": app.directory_summary,
        "category": app.directory_category,
        "tags": list(app.directory_tags),
        "collections": list(app.directory_collections),
        "icon_hash": app.icon_hash,
        "banner_hash": app.banner_hash,
        "verified": app.directory_approved,
        "install_template": template_payload,
        "user_install_supported": "user_install" in app.supported_install_types,
    }


async def directory_readiness_errors(
    session: AsyncSession,
    app: BotApplication,
    *,
    values: dict[str, object] | None = None,
    template_available: bool | None = None,
) -> list[DirectoryReadinessKey]:
    """Return stable reasons an application cannot enter public discovery."""

    pending = values or {}
    required: tuple[tuple[DirectoryReadinessKey, object], ...] = (
        ("directory_enabled", pending.get("directory_enabled", app.directory_enabled)),
        ("summary", pending.get("directory_summary", app.directory_summary)),
        ("category", pending.get("directory_category", app.directory_category)),
        ("tags", pending.get("directory_tags", app.directory_tags)),
        ("description", pending.get("description", app.description)),
        ("support_url", pending.get("support_url", app.support_url)),
        ("privacy_url", pending.get("privacy_url", app.privacy_url)),
        ("terms_url", pending.get("terms_url", app.terms_url)),
    )
    errors = [
        name
        for name, value in required
        if not value or (isinstance(value, str) and not value.strip())
    ]
    if not await directory_media_assets_valid(
        session,
        app,
        pending.get("directory_media", getattr(app, "directory_media", [])),
    ):
        errors.append("media")
    errors.extend(directory_optional_metadata_errors(app, pending))
    if template_available is None:
        template_available = (
            await session.scalar(
                select(BotInstallTemplate.id).where(
                    BotInstallTemplate.application_id == app.id,
                    BotInstallTemplate.application_domain == app.origin_domain,
                    BotInstallTemplate.active.is_(True),
                )
            )
            is not None
        )
    if not template_available:
        errors.append("install_path")
    install_types = pending.get("supported_install_types", app.supported_install_types)
    if isinstance(install_types, list) and "user_install" in install_types:
        user_install_command = await session.scalar(
            select(ApplicationCommand.id)
            .where(
                ApplicationCommand.application_id == app.id,
                ApplicationCommand.application_domain == app.origin_domain,
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.state == "active",
                ApplicationCommand.integration_types.contains(["user_install"]),
            )
            .limit(1)
        )
        if user_install_command is None:
            errors.append("user_install_command")
    return errors


def directory_readiness_projection(
    app: BotApplication,
    errors: list[DirectoryReadinessKey],
    *,
    preview_available: bool,
) -> dict[str, object]:
    missing_set = set(errors)
    missing = [key for key in DIRECTORY_READINESS_KEYS if key in missing_set]
    ready = not missing
    status: Literal["incomplete", "ready_for_review", "approved"] = (
        "approved"
        if ready and app.directory_approved
        else "ready_for_review"
        if ready
        else "incomplete"
    )
    return DirectoryReadiness.model_validate(
        {
            "status": status,
            "ready": ready,
            "preview_available": preview_available,
            "missing": missing,
            "items": [
                {"key": key, "ready": key not in missing_set} for key in DIRECTORY_READINESS_KEYS
            ],
        }
    ).model_dump(mode="json")


def directory_target_allowed(
    app: BotApplication,
    *,
    target_domain: str,
    local_domain: str,
    rule: str | None,
) -> bool:
    if target_domain == local_domain:
        return True
    rules = {target_domain: rule} if rule is not None else {}
    return target_policy_allows(app.target_policy, rules, target_domain)


def directory_patch_requires_reapproval(
    app: BotApplication,
    values: dict[str, object],
) -> bool:
    if values.get("directory_enabled") is False:
        return True
    reviewed_fields = {
        "name",
        "description",
        "support_url",
        "privacy_url",
        "terms_url",
        "directory_summary",
        "directory_category",
        "directory_tags",
        "directory_media",
        "directory_external_links",
        "directory_supported_locales",
        "directory_description_localizations",
        "supported_install_types",
    }
    return any(
        field in values and values[field] != getattr(app, field) for field in reviewed_fields
    )


def directory_target_predicates(
    settings: Settings,
    target_domain: str,
) -> tuple[ColumnElement[bool], ...]:
    if target_domain == settings.domain:
        return ()
    rule_effect = (
        select(BotInstanceRule.effect)
        .where(
            BotInstanceRule.application_id == BotApplication.id,
            BotInstanceRule.application_domain == BotApplication.origin_domain,
            BotInstanceRule.target_domain == target_domain,
        )
        .correlate(BotApplication)
        .scalar_subquery()
    )
    return (
        BotApplication.target_policy != "local_only",
        or_(rule_effect.is_(None), rule_effect != "deny"),
        or_(BotApplication.target_policy != "allowlist", rule_effect == "allow"),
    )


def active_directory_template_id() -> ColumnElement[int]:
    """Return the stable first active install path for a public app surface."""

    return (
        select(func.min(BotInstallTemplate.id))
        .where(
            BotInstallTemplate.application_id == BotApplication.id,
            BotInstallTemplate.application_domain == BotApplication.origin_domain,
            BotInstallTemplate.active.is_(True),
        )
        .correlate(BotApplication)
        .scalar_subquery()
    )


async def directory_rows(
    session: AsyncSession,
    settings: Settings,
    search: DirectorySearch,
    *,
    target_domain: str,
    application_id: int | None = None,
) -> list[tuple[BotApplication, BotInstallTemplate]]:
    statement = (
        select(BotApplication, BotInstallTemplate)
        .join(
            User,
            (User.id == BotApplication.bot_user_id)
            & (User.origin_domain == BotApplication.bot_user_domain),
        )
        .join(
            BotInstallTemplate,
            BotInstallTemplate.id == active_directory_template_id(),
        )
        .where(
            BotApplication.origin_domain == settings.domain,
            BotApplication.status == "active",
            BotApplication.directory_enabled.is_(True),
            BotApplication.directory_approved.is_(True),
            User.account_type == "bot",
            User.disabled_at.is_(None),
        )
        .order_by(BotApplication.id)
    )
    if application_id is not None:
        statement = statement.where(BotApplication.id == application_id)
    if search.after is not None:
        statement = statement.where(BotApplication.id > int(search.after))
    if search.q:
        query = search.q.strip().lower()
        if query:
            escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            needle = f"%{escaped_query}%"
            statement = statement.where(
                or_(
                    BotApplication.name.ilike(needle, escape="\\"),
                    BotApplication.directory_summary.ilike(needle, escape="\\"),
                    BotApplication.directory_tags.contains([query]),
                )
            )
    if search.category:
        statement = statement.where(BotApplication.directory_category == search.category)
    if search.tag:
        statement = statement.where(BotApplication.directory_tags.contains([search.tag]))
    if search.collection:
        statement = statement.where(
            BotApplication.directory_collections.contains([search.collection])
        )
    statement = statement.where(*directory_target_predicates(settings, target_domain))
    statement = statement.limit(search.limit + 1)
    return list((await session.execute(statement)).tuples().all())


async def directory_bot_profile_application(
    session: AsyncSession,
    settings: Settings,
    bot_ref: tuple[int, str],
    *,
    target_domain: str,
) -> dict[str, object]:
    """Resolve a bot profile to its authority-owned Add App destination."""

    bot_id, bot_domain = bot_ref
    if bot_domain != settings.domain:
        raise ValueError("bot profile lookup must run on the bot authority")
    row = (
        await session.execute(
            select(BotApplication, BotInstallTemplate)
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .join(
                BotInstallTemplate,
                BotInstallTemplate.id == active_directory_template_id(),
            )
            .where(
                BotApplication.origin_domain == settings.domain,
                BotApplication.bot_user_id == bot_id,
                BotApplication.bot_user_domain == bot_domain,
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                *directory_target_predicates(settings, target_domain),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    application, template = row
    template_payload = directory_template_projection(application, template)
    if template_payload is None:  # The inner join above makes this an integrity assertion.
        raise RuntimeError("active application template disappeared during profile lookup")
    payload = {
        "bot_ref": f"{bot_id}@{bot_domain}",
        "application_ref": f"{application.id}@{application.origin_domain}",
        "origin_domain": application.origin_domain,
        "name": application.name,
        "install_template": template_payload,
        "directory_listed": bool(application.directory_enabled and application.directory_approved),
    }
    return DirectoryBotProfileApplication.model_validate(payload).model_dump(mode="json")


async def directory_page(
    session: AsyncSession,
    rows: list[tuple[BotApplication, BotInstallTemplate]],
    limit: int,
    collection: DirectoryCollection | None,
) -> dict[str, object]:
    del session
    visible = rows[:limit]
    payload = {
        "items": [directory_projection(app, template) for app, template in visible],
        "next_cursor": str(visible[-1][0].id) if len(rows) > limit and visible else None,
        "collections": list(DIRECTORY_COLLECTIONS),
        "selected_collection": collection,
    }
    return DirectoryPage.model_validate(payload).model_dump(mode="json")


async def directory_popular_commands(
    session: AsyncSession,
    app: BotApplication,
) -> list[dict[str, str]]:
    invocation_count = func.count(BotInteraction.id).label("invocation_count")
    rows = (
        await session.execute(
            select(ApplicationCommand, invocation_count)
            .outerjoin(
                BotInteraction,
                (BotInteraction.command_id == ApplicationCommand.id)
                & (BotInteraction.interaction_type == "command"),
            )
            .where(
                ApplicationCommand.application_id == app.id,
                ApplicationCommand.application_domain == app.origin_domain,
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.state == "active",
                ApplicationCommand.type == "chat_input",
            )
            .group_by(ApplicationCommand.id)
            .order_by(invocation_count.desc(), ApplicationCommand.name, ApplicationCommand.id)
            .limit(5)
        )
    ).all()
    commands: list[dict[str, str]] = []
    for command, _ in rows:
        description = command.definition.get("description")
        if isinstance(description, str):
            commands.append(
                {
                    "id": str(command.authority_id),
                    "name": command.name,
                    "description": description,
                }
            )
    return commands


async def directory_similar_applications(
    session: AsyncSession,
    settings: Settings,
    app: BotApplication,
    *,
    target_domain: str,
) -> list[dict[str, object]]:
    similarity: ColumnElement[Any] = case(
        (BotApplication.directory_category == app.directory_category, 10), else_=0
    )
    for tag in app.directory_tags:
        similarity += case((BotApplication.directory_tags.contains([tag]), 1), else_=0)
    rows = list(
        await session.scalars(
            select(BotApplication)
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.origin_domain == settings.domain,
                BotApplication.id != app.id,
                BotApplication.status == "active",
                BotApplication.directory_enabled.is_(True),
                BotApplication.directory_approved.is_(True),
                User.account_type == "bot",
                User.disabled_at.is_(None),
                similarity > 0,
                *directory_target_predicates(settings, target_domain),
            )
            .order_by(similarity.desc(), BotApplication.id)
            .limit(3)
        )
    )
    return [
        {
            "id": str(candidate.id),
            "ref": f"{candidate.id}@{candidate.origin_domain}",
            "origin_domain": candidate.origin_domain,
            "name": candidate.name,
            "summary": candidate.directory_summary,
            "category": candidate.directory_category,
            "tags": list(candidate.directory_tags),
            "icon_hash": candidate.icon_hash,
        }
        for candidate in rows
    ]


async def directory_detail_projection(
    session: AsyncSession,
    settings: Settings,
    app: BotApplication,
    template: BotInstallTemplate | None,
    *,
    target_domain: str,
    preview: bool = False,
) -> dict[str, object]:
    resolved = await resolved_directory_media(session, app)
    if resolved is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    stored_media, assets = resolved
    media: list[dict[str, object]] = []
    for item in stored_media:
        if isinstance(item, DirectoryImageMedia):
            asset = assets[int(item.asset_id)]
            media.append(
                {
                    "type": "image",
                    "asset_id": str(asset.id),
                    "name": asset.name,
                    "media_hash": asset.media_hash,
                    "content_type": asset.content_type,
                    "width": asset.width,
                    "height": asset.height,
                }
            )
        else:
            media.append({"type": "youtube", "video_id": item.video_id})
    payload = {
        **directory_projection(app, template),
        "description": app.description,
        "support_url": app.support_url,
        "privacy_policy_url": app.privacy_url,
        "terms_url": app.terms_url,
        "media": media,
        "external_links": list(app.directory_external_links),
        "supported_locales": list(app.directory_supported_locales),
        "description_localizations": dict(app.directory_description_localizations),
        "popular_commands": await directory_popular_commands(session, app),
        "similar_apps": await directory_similar_applications(
            session,
            settings,
            app,
            target_domain=target_domain,
        ),
    }
    try:
        if preview:
            return DirectoryPreviewApplication.model_validate(payload).model_dump(mode="json")
        return DirectoryApplication.model_validate(payload).model_dump(mode="json")
    except ValidationError:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"}) from None


async def directory_preview_response(
    session: AsyncSession,
    settings: Settings,
    app: BotApplication,
) -> dict[str, object]:
    template = await session.scalar(
        select(BotInstallTemplate)
        .where(
            BotInstallTemplate.application_id == app.id,
            BotInstallTemplate.application_domain == app.origin_domain,
            BotInstallTemplate.active.is_(True),
        )
        .order_by(BotInstallTemplate.id)
        .limit(1)
    )
    errors = await directory_readiness_errors(
        session,
        app,
        template_available=template is not None,
    )
    application = await directory_detail_projection(
        session,
        settings,
        app,
        template,
        target_domain=settings.domain,
        preview=True,
    )
    payload = {
        "application_ref": f"{app.id}@{app.origin_domain}",
        "application": application,
        "readiness": directory_readiness_projection(
            app,
            errors,
            preview_available=True,
        ),
    }
    return DirectoryPreviewResponse.model_validate(payload).model_dump(mode="json")


async def remote_directory_request(
    session: AsyncSession, settings: Settings, domain: str, path: str, payload: dict[str, object]
) -> object:
    try:
        response = await signed_request(session, settings, "POST", domain, path, payload=payload)
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
        response.raise_for_status()
        return decode_federation_response_json(response)
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503, detail={"code": "APPLICATION_DIRECTORY_UNAVAILABLE"}
        ) from None


def directory_query_domain(domain: str | None, local_domain: str) -> str:
    if domain is None:
        return local_domain
    try:
        return normalize_domain(domain)
    except FederationNetworkError:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_FEDERATION_DOMAIN"},
        ) from None


def validated_remote_directory_page(
    raw: object,
    *,
    domain: str,
    search: DirectorySearch,
) -> dict[str, object]:
    page = DirectoryPage.model_validate(raw)
    if (
        len(page.items) > search.limit
        or page.selected_collection != search.collection
        or (page.next_cursor is not None and len(page.items) != search.limit)
    ):
        raise ValueError("remote directory page does not match the request")
    after = int(search.after) if search.after is not None else 0
    query = search.q.strip().lower() if search.q is not None else ""
    for item in page.items:
        if item.origin_domain != domain or int(item.id) <= after:
            raise ValueError("remote directory item lineage is invalid")
        if search.category is not None and item.category != search.category:
            raise ValueError("remote directory category filter was not honored")
        if search.tag is not None and search.tag not in item.tags:
            raise ValueError("remote directory tag filter was not honored")
        if search.collection is not None and search.collection not in item.collections:
            raise ValueError("remote directory collection filter was not honored")
        if query and not (
            query in item.name.lower() or query in item.summary.lower() or query in item.tags
        ):
            raise ValueError("remote directory search filter was not honored")
    return page.model_dump(mode="json")


def validated_remote_directory_detail(
    raw: object,
    *,
    application_id: int,
    domain: str,
) -> dict[str, object]:
    application = DirectoryApplication.model_validate(raw)
    if (int(application.id), application.origin_domain) != (application_id, domain):
        raise ValueError("remote directory detail lineage is invalid")
    return application.model_dump(mode="json")


def validated_remote_bot_profile_application(
    raw: object,
    *,
    bot_id: int,
    domain: str,
) -> dict[str, object]:
    profile = DirectoryBotProfileApplication.model_validate(raw)
    parsed_bot = EntityRef(profile.bot_ref)
    if (
        parsed_bot.domain != domain
        or int(parsed_bot.id) != bot_id
        or profile.origin_domain != domain
    ):
        raise ValueError("remote bot profile application lineage is invalid")
    return profile.model_dump(mode="json")


@router.get("/application-directory")
async def list_application_directory(
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: DirectoryCategory | None = None,
    tag: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")] = None,
    collection: DirectoryCollection | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
    after: Annotated[int | None, Query(ge=1)] = None,
    domain: str | None = None,
) -> dict[str, object]:
    del auth
    search = DirectorySearch(
        q=q,
        category=category,
        tag=tag,
        collection=collection,
        limit=limit,
        after=str(after) if after is not None else None,
    )
    target = directory_query_domain(domain, settings.domain)
    if target != settings.domain:
        raw = await remote_directory_request(
            session,
            settings,
            target,
            "/_kaede/v1/application-directory/search",
            search.model_dump(mode="json"),
        )
        try:
            return validated_remote_directory_page(raw, domain=target, search=search)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail={"code": "APPLICATION_DIRECTORY_RESPONSE_INVALID"},
            ) from None
    rows = await directory_rows(session, settings, search, target_domain=target)
    return await directory_page(session, rows, limit, collection)


@router.get("/application-directory/bot-profiles/{bot_ref}")
async def get_bot_profile_application(
    bot_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    del auth
    bot_id, domain = bot_ref.resolve(settings.domain)
    if domain != settings.domain:
        raw = await remote_directory_request(
            session,
            settings,
            domain,
            "/_kaede/v1/application-directory/bot-profile",
            {"bot_id": str(bot_id)},
        )
        try:
            return validated_remote_bot_profile_application(
                raw,
                bot_id=bot_id,
                domain=domain,
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail={"code": "APPLICATION_DIRECTORY_RESPONSE_INVALID"},
            ) from None
    return await directory_bot_profile_application(
        session,
        settings,
        (bot_id, domain),
        target_domain=settings.domain,
    )


@router.get("/application-directory/{application_ref}")
async def get_application_directory_entry(
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    del auth
    app_id, domain = application_ref.resolve(settings.domain)
    if domain != settings.domain:
        raw = await remote_directory_request(
            session,
            settings,
            domain,
            "/_kaede/v1/application-directory/detail",
            {"application_id": str(app_id)},
        )
        try:
            return validated_remote_directory_detail(
                raw,
                application_id=app_id,
                domain=domain,
            )
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail={"code": "APPLICATION_DIRECTORY_RESPONSE_INVALID"},
            ) from None
    rows = await directory_rows(
        session,
        settings,
        DirectorySearch(limit=1),
        target_domain=settings.domain,
        application_id=app_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    return await directory_detail_projection(
        session,
        settings,
        *rows[0],
        target_domain=settings.domain,
    )


class DirectoryDetailRequest(StrictDirectoryModel):
    application_id: str = Field(pattern=r"^[1-9][0-9]*$")


class DirectoryBotProfileRequest(StrictDirectoryModel):
    bot_id: str = Field(pattern=r"^[1-9][0-9]*$")


@federation_router.post("/_kaede/v1/application-directory/search")
async def federation_search_application_directory(
    search: DirectorySearch,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "application-directory-search",
        capacity=120,
        refill_per_minute=120,
    )
    rows = await directory_rows(session, settings, search, target_domain=principal.origin)
    return await directory_page(session, rows, search.limit, search.collection)


@federation_router.post("/_kaede/v1/application-directory/bot-profile")
async def federation_get_bot_profile_application(
    payload: DirectoryBotProfileRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "application-directory-bot-profile",
        capacity=240,
        refill_per_minute=240,
    )
    return await directory_bot_profile_application(
        session,
        settings,
        (int(payload.bot_id), settings.domain),
        target_domain=principal.origin,
    )


@federation_router.post("/_kaede/v1/application-directory/detail")
async def federation_get_application_directory_entry(
    payload: DirectoryDetailRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "application-directory-detail",
        capacity=240,
        refill_per_minute=240,
    )
    rows = await directory_rows(
        session,
        settings,
        DirectorySearch(limit=1),
        target_domain=principal.origin,
        application_id=int(payload.application_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    return await directory_detail_projection(
        session,
        settings,
        *rows[0],
        target_domain=principal.origin,
    )
