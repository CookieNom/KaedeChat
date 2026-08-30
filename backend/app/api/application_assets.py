from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.applications import managed_application, require_team_role
from app.api.dependencies import (
    AuthenticatedUser,
    get_session,
    get_snowflake,
    require_user,
)
from app.bots.auth import BotPrincipal, require_application_home_bot
from app.bots.developer_projection import commit_developer_application_mutation
from app.bots.directory_contract import (
    DIRECTORY_MEDIA_LIMIT,
    append_directory_image,
    remove_directory_image,
)
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, WireSnowflake
from app.db.bot_models import ApplicationAsset, ApplicationEmoji, BotApplication
from app.db.materialization import materialize_updated_at
from app.db.models import Attachment, User
from app.federation.application_management import (
    application_management_dict_body,
    application_management_list_body,
    proxy_remote_application_management,
    require_application_management_empty,
)
from app.media.payloads import attachment_payload
from app.media.schemas import UploadTicketRequest
from app.media.service import (
    attachment_variant_is_animated,
    bind_asset,
    create_upload_ticket,
    finalize_attachment,
    is_federated_human_authority_upload,
    require_image_type,
    ticket_payload,
)
from app.tasks import media_local_purge, media_process

router = APIRouter(prefix="/api/v1", tags=["application assets"])
APPLICATION_ASSET_LIMIT = 300
APPLICATION_STORE_ASSET_LIMIT = 5
APPLICATION_EMOJI_LIMIT = 2_000
APPLICATION_EMOJI_MAX_BYTES = 256 * 1024
DIRECTORY_ASSET_KINDS = frozenset({"icon", "cover", "store"})


class ApplicationAssetCommit(UnambiguousInputModel):
    attachment_id: WireSnowflake
    kind: Literal["icon", "cover", "store", "achievement", "activity", "other"]
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("asset name must not be blank")
        return cleaned


class ApplicationAssetPatch(UnambiguousInputModel):
    kind: Literal["icon", "cover", "store", "achievement", "activity", "other"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("asset name must not be blank")
        return cleaned

    @model_validator(mode="after")
    def not_empty(self) -> ApplicationAssetPatch:
        if not self.model_fields_set:
            raise ValueError("at least one asset field is required")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"application asset {field_name} cannot be null")
        return self


class ApplicationEmojiCommit(UnambiguousInputModel):
    attachment_id: WireSnowflake
    name: str = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")


class ApplicationEmojiPatch(UnambiguousInputModel):
    """Discord-compatible application emoji edit payload.

    Availability is authority-managed state, not a developer-controlled field.
    Forbid unknown fields so an obsolete client cannot appear to toggle it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")


@dataclass(frozen=True, slots=True)
class AppAccess:
    application: BotApplication
    actor: User


def _asset_binding(application: BotApplication, asset_id: int) -> str:
    return f"application:{application.origin_domain}:{application.id}:asset:{asset_id}"


def directory_asset_change_requires_reapproval(
    current_kind: str | None,
    next_kind: str | None,
    *,
    changed: bool = True,
) -> bool:
    return changed and bool({current_kind, next_kind} & DIRECTORY_ASSET_KINDS)


def _directory_media(application: BotApplication) -> list[dict[str, object]]:
    return list(application.directory_media or [])


def _require_directory_media_capacity(application: BotApplication) -> None:
    if len(_directory_media(application)) >= DIRECTORY_MEDIA_LIMIT:
        raise HTTPException(
            status_code=409,
            detail={"code": "APPLICATION_STORE_ASSET_LIMIT_REACHED"},
        )


def _emoji_binding(application: BotApplication, emoji_id: int) -> str:
    return f"application:{application.origin_domain}:{application.id}:emoji:{emoji_id}"


def _upload_binding(application: BotApplication, purpose: str, attachment_id: int) -> str:
    return (
        f"application_upload:{application.origin_domain}:{application.id}:{purpose}:{attachment_id}"
    )


def _federated_application_upload(access: AppAccess, settings: Settings) -> bool:
    return (
        access.application.origin_domain == settings.domain
        and is_federated_human_authority_upload(access.actor, settings)
    )


def _require_upload_binding(
    attachment: Attachment,
    application: BotApplication,
    purpose: str,
) -> None:
    if attachment.asset_binding != _upload_binding(application, purpose, attachment.id):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})


def asset_payload(asset: ApplicationAsset) -> dict[str, object]:
    return {
        "id": str(asset.id),
        "ref": f"{asset.id}@{asset.application_domain}",
        "application_ref": f"{asset.application_id}@{asset.application_domain}",
        "kind": asset.kind,
        "name": asset.name,
        "media_hash": asset.media_hash,
        "content_type": asset.content_type,
        "width": asset.width,
        "height": asset.height,
        "version": asset.version,
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
    }


def emoji_payload(emoji: ApplicationEmoji) -> dict[str, object]:
    return {
        "id": str(emoji.id),
        "ref": f"{emoji.id}@{emoji.application_domain}",
        "application_ref": f"{emoji.application_id}@{emoji.application_domain}",
        "name": emoji.name,
        "media_hash": emoji.media_hash,
        "animated": emoji.animated,
        "available": emoji.available,
        "creator_id": str(emoji.creator_id),
        "creator_domain": emoji.creator_domain,
        "version": emoji.version,
        "created_at": emoji.created_at.isoformat(),
        "updated_at": emoji.updated_at.isoformat(),
    }


async def _developer_access(
    session: AsyncSession,
    settings: Settings,
    auth: AuthenticatedUser,
    application_ref: EntityRef,
) -> AppAccess:
    application, member, _ = await managed_application(session, settings, auth, application_ref)
    require_team_role(member, "owner", "administrator", "developer")
    return AppAccess(application, auth.user)


def _bot_access(principal: BotPrincipal, scope: str) -> AppAccess:
    principal.require_scope(scope)
    return AppAccess(principal.application, principal.user)


def _bot_write_access(principal: BotPrincipal, settings: Settings, scope: str) -> AppAccess:
    access = _bot_access(principal, scope)
    if access.application.origin_domain != settings.domain:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_HOME_INSTANCE_REQUIRED",
                "message": (
                    "Application assets and emoji can only be changed on the "
                    "application's home instance."
                ),
                "home_domain": access.application.origin_domain,
            },
        )
    return access


async def _locked_access(session: AsyncSession, access: AppAccess) -> AppAccess:
    """Serialize authoritative media mutations and manifest generation bumps."""

    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == access.application.id,
            BotApplication.origin_domain == access.application.origin_domain,
        )
        .with_for_update()
    )
    if application is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "APPLICATION_NOT_FOUND",
                "message": "Application not found.",
            },
        )
    return AppAccess(application, access.actor)


async def _asset(
    session: AsyncSession, access: AppAccess, asset_id: int, *, for_update: bool = False
) -> ApplicationAsset:
    statement = select(ApplicationAsset).where(
        ApplicationAsset.id == asset_id,
        ApplicationAsset.application_id == access.application.id,
        ApplicationAsset.application_domain == access.application.origin_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await session.scalar(statement)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "APPLICATION_ASSET_NOT_FOUND",
                "message": "Application asset not found.",
            },
        )
    return result


async def _emoji(
    session: AsyncSession, access: AppAccess, emoji_id: int, *, for_update: bool = False
) -> ApplicationEmoji:
    statement = select(ApplicationEmoji).where(
        ApplicationEmoji.id == emoji_id,
        ApplicationEmoji.application_id == access.application.id,
        ApplicationEmoji.application_domain == access.application.origin_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await session.scalar(statement)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "APPLICATION_EMOJI_NOT_FOUND",
                "message": "Application emoji not found.",
            },
        )
    return result


async def _ticket(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    access: AppAccess,
    payload: UploadTicketRequest,
    *,
    purpose: Literal["application_asset", "application_emoji"],
) -> dict[str, object]:
    require_image_type(payload.content_type)
    limit = (
        APPLICATION_EMOJI_MAX_BYTES
        if purpose == "application_emoji"
        else settings.media_max_attachment_bytes
    )
    if payload.size > limit:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "APPLICATION_MEDIA_TOO_LARGE",
                "message": f"Application media cannot exceed {limit} bytes.",
                "max_bytes": limit,
            },
        )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        access.actor,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose=purpose,
        federated_application_upload=_federated_application_upload(access, settings),
    )
    attachment.asset_binding = _upload_binding(
        access.application,
        purpose,
        attachment.id,
    )
    await session.commit()
    return {
        **ticket_payload(attachment, upload_url),
        "application_ref": (f"{access.application.id}@{access.application.origin_domain}"),
    }


async def _commit_asset(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    response: Response,
    access: AppAccess,
    payload: ApplicationAssetCommit,
) -> dict[str, object]:
    access = await _locked_access(session, access)
    duplicate = await session.scalar(
        select(ApplicationAsset.id).where(
            ApplicationAsset.application_id == access.application.id,
            ApplicationAsset.application_domain == access.application.origin_domain,
            ApplicationAsset.kind == payload.kind,
            ApplicationAsset.name == payload.name,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_ASSET_NAME_TAKEN",
                "message": "An asset of that kind already uses this name.",
            },
        )
    count = await session.scalar(
        select(func.count())
        .select_from(ApplicationAsset)
        .where(
            ApplicationAsset.application_id == access.application.id,
            ApplicationAsset.application_domain == access.application.origin_domain,
        )
    )
    if int(count or 0) >= APPLICATION_ASSET_LIMIT:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_ASSET_LIMIT_REACHED",
                "message": (f"An application can have at most {APPLICATION_ASSET_LIMIT} assets."),
            },
        )
    if payload.kind == "store":
        _require_directory_media_capacity(access.application)
        store_count = await session.scalar(
            select(func.count())
            .select_from(ApplicationAsset)
            .where(
                ApplicationAsset.application_id == access.application.id,
                ApplicationAsset.application_domain == access.application.origin_domain,
                ApplicationAsset.kind == "store",
            )
        )
        if int(store_count or 0) >= APPLICATION_STORE_ASSET_LIMIT:
            raise HTTPException(
                status_code=409,
                detail={"code": "APPLICATION_STORE_ASSET_LIMIT_REACHED"},
            )
    attachment = await finalize_attachment(
        session,
        settings,
        access.actor,
        int(payload.attachment_id),
        required_purpose="application_asset",
        federated_application_upload=_federated_application_upload(access, settings),
    )
    _require_upload_binding(attachment, access.application, "application_asset")
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return {
            "status": "processing",
            "application_ref": (f"{access.application.id}@{access.application.origin_domain}"),
            "attachment": attachment_payload(attachment),
        }
    if attachment.content_sha256 is None:
        raise RuntimeError("clean application asset is missing its content digest")
    require_image_type(attachment.detected_content_type)
    attachment.asset_binding = None
    asset = ApplicationAsset(
        id=await snowflake.mint(),
        application_id=access.application.id,
        application_domain=access.application.origin_domain,
        kind=payload.kind,
        name=payload.name,
        media_hash=attachment.content_sha256,
        object_key=attachment.object_key,
        content_type=attachment.detected_content_type or attachment.content_type,
        width=attachment.width,
        height=attachment.height,
    )
    session.add(asset)
    previous = await bind_asset(
        session,
        attachment,
        _asset_binding(access.application, asset.id),
    )
    if payload.kind == "icon":
        access.application.icon_hash = attachment.content_sha256
    elif payload.kind == "cover":
        access.application.banner_hash = attachment.content_sha256
    elif payload.kind == "store":
        access.application.directory_media = append_directory_image(
            _directory_media(access.application),
            asset.id,
        )
    if directory_asset_change_requires_reapproval(None, payload.kind):
        access.application.directory_approved = False
    access.application.manifest_generation += 1
    await commit_developer_application_mutation(session, settings, access.application)
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return asset_payload(asset)


async def _delete_asset(
    session: AsyncSession,
    settings: Settings,
    access: AppAccess,
    asset_id: int,
) -> None:
    access = await _locked_access(session, access)
    asset = await _asset(session, access, asset_id, for_update=True)
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == _asset_binding(access.application, asset.id))
        .with_for_update()
    )
    if asset.kind == "icon" and access.application.icon_hash == asset.media_hash:
        access.application.icon_hash = None
    elif asset.kind == "cover" and access.application.banner_hash == asset.media_hash:
        access.application.banner_hash = None
    if asset.kind == "store":
        access.application.directory_media = remove_directory_image(
            _directory_media(access.application),
            asset.id,
        )
    if directory_asset_change_requires_reapproval(asset.kind, None):
        access.application.directory_approved = False
    if attachment is not None:
        attachment.asset_binding = None
    access.application.manifest_generation += 1
    await session.delete(asset)
    await commit_developer_application_mutation(session, settings, access.application)
    if attachment is not None:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)


async def _patch_asset(
    session: AsyncSession,
    settings: Settings,
    access: AppAccess,
    asset_id: int,
    payload: ApplicationAssetPatch,
) -> dict[str, object]:
    access = await _locked_access(session, access)
    asset = await _asset(session, access, asset_id, for_update=True)
    next_kind = payload.kind or asset.kind
    next_name = payload.name or asset.name
    if next_kind == "store" and asset.kind != "store":
        _require_directory_media_capacity(access.application)
        store_count = await session.scalar(
            select(func.count())
            .select_from(ApplicationAsset)
            .where(
                ApplicationAsset.application_id == access.application.id,
                ApplicationAsset.application_domain == access.application.origin_domain,
                ApplicationAsset.kind == "store",
            )
        )
        if int(store_count or 0) >= APPLICATION_STORE_ASSET_LIMIT:
            raise HTTPException(
                status_code=409,
                detail={"code": "APPLICATION_STORE_ASSET_LIMIT_REACHED"},
            )
    duplicate = await session.scalar(
        select(ApplicationAsset.id).where(
            ApplicationAsset.application_id == access.application.id,
            ApplicationAsset.application_domain == access.application.origin_domain,
            ApplicationAsset.kind == next_kind,
            ApplicationAsset.name == next_name,
            ApplicationAsset.id != asset.id,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_ASSET_NAME_TAKEN",
                "message": "An asset of that kind already uses this name.",
            },
        )
    if (
        asset.kind == "icon"
        and next_kind != "icon"
        and access.application.icon_hash == asset.media_hash
    ):
        access.application.icon_hash = None
    if next_kind == "icon":
        access.application.icon_hash = asset.media_hash
    if (
        asset.kind == "cover"
        and next_kind != "cover"
        and access.application.banner_hash == asset.media_hash
    ):
        access.application.banner_hash = None
    if next_kind == "cover":
        access.application.banner_hash = asset.media_hash
    if asset.kind != "store" and next_kind == "store":
        access.application.directory_media = append_directory_image(
            _directory_media(access.application),
            asset.id,
        )
    elif asset.kind == "store" and next_kind != "store":
        access.application.directory_media = remove_directory_image(
            _directory_media(access.application),
            asset.id,
        )
    if directory_asset_change_requires_reapproval(
        asset.kind,
        next_kind,
        changed=next_kind != asset.kind or next_name != asset.name,
    ):
        access.application.directory_approved = False
    asset.kind = next_kind
    asset.name = next_name
    asset.version += 1
    access.application.manifest_generation += 1
    await materialize_updated_at(session, asset)
    await commit_developer_application_mutation(session, settings, access.application)
    return asset_payload(asset)


async def _commit_emoji(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    response: Response,
    access: AppAccess,
    payload: ApplicationEmojiCommit,
) -> dict[str, object]:
    access = await _locked_access(session, access)
    duplicate = await session.scalar(
        select(ApplicationEmoji.id).where(
            ApplicationEmoji.application_id == access.application.id,
            ApplicationEmoji.application_domain == access.application.origin_domain,
            ApplicationEmoji.name_casefold == payload.name.casefold(),
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_EMOJI_NAME_TAKEN",
                "message": "An application emoji already uses that name.",
            },
        )
    count = await session.scalar(
        select(func.count())
        .select_from(ApplicationEmoji)
        .where(
            ApplicationEmoji.application_id == access.application.id,
            ApplicationEmoji.application_domain == access.application.origin_domain,
        )
    )
    if int(count or 0) >= APPLICATION_EMOJI_LIMIT:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_EMOJI_LIMIT_REACHED",
                "message": f"An application can have at most {APPLICATION_EMOJI_LIMIT} emoji.",
            },
        )
    attachment = await finalize_attachment(
        session,
        settings,
        access.actor,
        int(payload.attachment_id),
        required_purpose="application_emoji",
        federated_application_upload=_federated_application_upload(access, settings),
    )
    _require_upload_binding(attachment, access.application, "application_emoji")
    if attachment.size > APPLICATION_EMOJI_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "APPLICATION_MEDIA_TOO_LARGE",
                "max_bytes": APPLICATION_EMOJI_MAX_BYTES,
            },
        )
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return {
            "status": "processing",
            "application_ref": (f"{access.application.id}@{access.application.origin_domain}"),
            "attachment": attachment_payload(attachment),
        }
    if attachment.content_sha256 is None:
        raise RuntimeError("clean application emoji is missing its content digest")
    require_image_type(attachment.detected_content_type)
    attachment.asset_binding = None
    emoji = ApplicationEmoji(
        id=await snowflake.mint(),
        application_id=access.application.id,
        application_domain=access.application.origin_domain,
        name=payload.name,
        name_casefold=payload.name.casefold(),
        media_hash=attachment.content_sha256,
        object_key=attachment.object_key,
        animated=attachment_variant_is_animated(attachment, "thumbnail_128"),
        creator_id=access.actor.id,
        creator_domain=access.actor.origin_domain,
    )
    session.add(emoji)
    previous = await bind_asset(
        session,
        attachment,
        _emoji_binding(access.application, emoji.id),
    )
    access.application.manifest_generation += 1
    await commit_developer_application_mutation(session, settings, access.application)
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return emoji_payload(emoji)


async def _patch_emoji(
    session: AsyncSession,
    settings: Settings,
    access: AppAccess,
    emoji_id: int,
    payload: ApplicationEmojiPatch,
) -> dict[str, object]:
    access = await _locked_access(session, access)
    emoji = await _emoji(session, access, emoji_id, for_update=True)
    duplicate = await session.scalar(
        select(ApplicationEmoji.id).where(
            ApplicationEmoji.application_id == access.application.id,
            ApplicationEmoji.application_domain == access.application.origin_domain,
            ApplicationEmoji.name_casefold == payload.name.casefold(),
            ApplicationEmoji.id != emoji.id,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_EMOJI_NAME_TAKEN",
                "message": "An application emoji already uses that name.",
            },
        )
    emoji.name = payload.name
    emoji.name_casefold = payload.name.casefold()
    emoji.version += 1
    access.application.manifest_generation += 1
    await materialize_updated_at(session, emoji)
    await commit_developer_application_mutation(session, settings, access.application)
    return emoji_payload(emoji)


async def _delete_emoji(
    session: AsyncSession,
    settings: Settings,
    access: AppAccess,
    emoji_id: int,
) -> None:
    access = await _locked_access(session, access)
    emoji = await _emoji(session, access, emoji_id, for_update=True)
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == _emoji_binding(access.application, emoji.id))
        .with_for_update()
    )
    if attachment is not None:
        attachment.asset_binding = None
    access.application.manifest_generation += 1
    await session.delete(emoji)
    await commit_developer_application_mutation(session, settings, access.application)
    if attachment is not None:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)


async def _list_assets(session: AsyncSession, access: AppAccess) -> list[dict[str, object]]:
    rows = list(
        await session.scalars(
            select(ApplicationAsset)
            .where(
                ApplicationAsset.application_id == access.application.id,
                ApplicationAsset.application_domain == access.application.origin_domain,
            )
            .order_by(ApplicationAsset.kind, ApplicationAsset.name, ApplicationAsset.id)
        )
    )
    return [asset_payload(item) for item in rows]


async def _list_emojis(session: AsyncSession, access: AppAccess) -> list[dict[str, object]]:
    rows = list(
        await session.scalars(
            select(ApplicationEmoji)
            .where(
                ApplicationEmoji.application_id == access.application.id,
                ApplicationEmoji.application_domain == access.application.origin_domain,
            )
            .order_by(ApplicationEmoji.name_casefold, ApplicationEmoji.id)
        )
    )
    return [emoji_payload(item) for item in rows]


@router.get("/applications/{application_ref}/assets")
async def list_application_assets(
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "asset.list"
    )
    if remote is not None:
        return cast(list[dict[str, object]], application_management_list_body(remote))
    return await _list_assets(
        session, await _developer_access(session, settings, auth, application_ref)
    )


@router.post("/applications/{application_ref}/assets/tickets", status_code=201)
async def create_application_asset_ticket(
    application_ref: EntityRef,
    payload: UploadTicketRequest,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "asset.ticket",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    return await _ticket(
        session,
        settings,
        snowflake,
        await _developer_access(session, settings, auth, application_ref),
        payload,
        purpose="application_asset",
    )


@router.get("/applications/{application_ref}/assets/{asset_id}")
async def get_application_asset(
    application_ref: EntityRef,
    asset_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "asset.get",
        {"resource_id": asset_id},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    access = await _developer_access(session, settings, auth, application_ref)
    return asset_payload(await _asset(session, access, asset_id))


@router.patch("/applications/{application_ref}/assets/{asset_id}")
async def patch_application_asset(
    application_ref: EntityRef,
    asset_id: int,
    payload: ApplicationAssetPatch,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "asset.update",
        {
            "resource_id": asset_id,
            "data": payload.model_dump(mode="json", exclude_unset=True),
        },
    )
    if remote is not None:
        return application_management_dict_body(remote)
    return await _patch_asset(
        session,
        settings,
        await _developer_access(session, settings, auth, application_ref),
        asset_id,
        payload,
    )


@router.post("/applications/{application_ref}/assets", status_code=201)
async def create_application_asset(
    application_ref: EntityRef,
    payload: ApplicationAssetCommit,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "asset.create",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        response.status_code = remote.status_code
        return application_management_dict_body(remote)
    return await _commit_asset(
        session,
        settings,
        snowflake,
        response,
        await _developer_access(session, settings, auth, application_ref),
        payload,
    )


@router.delete("/applications/{application_ref}/assets/{asset_id}", status_code=204)
async def delete_application_asset(
    application_ref: EntityRef,
    asset_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "asset.delete",
        {"resource_id": asset_id},
    )
    if remote is not None:
        require_application_management_empty(remote)
        return Response(status_code=204)
    await _delete_asset(
        session,
        settings,
        await _developer_access(session, settings, auth, application_ref),
        asset_id,
    )
    return Response(status_code=204)


@router.get("/applications/{application_ref}/emojis")
async def list_application_emojis(
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    remote = await proxy_remote_application_management(
        session, settings, application_ref, auth.user, "emoji.list"
    )
    if remote is not None:
        return cast(list[dict[str, object]], application_management_list_body(remote))
    return await _list_emojis(
        session, await _developer_access(session, settings, auth, application_ref)
    )


@router.get("/applications/{application_ref}/emojis/{emoji_id}")
async def get_application_emoji(
    application_ref: EntityRef,
    emoji_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "emoji.get",
        {"resource_id": emoji_id},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    access = await _developer_access(session, settings, auth, application_ref)
    return emoji_payload(await _emoji(session, access, emoji_id))


@router.post("/applications/{application_ref}/emojis/tickets", status_code=201)
async def create_application_emoji_ticket(
    application_ref: EntityRef,
    payload: UploadTicketRequest,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "emoji.ticket",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        return application_management_dict_body(remote)
    return await _ticket(
        session,
        settings,
        snowflake,
        await _developer_access(session, settings, auth, application_ref),
        payload,
        purpose="application_emoji",
    )


@router.post("/applications/{application_ref}/emojis", status_code=201)
async def create_application_emoji(
    application_ref: EntityRef,
    payload: ApplicationEmojiCommit,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "emoji.create",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        response.status_code = remote.status_code
        return application_management_dict_body(remote)
    return await _commit_emoji(
        session,
        settings,
        snowflake,
        response,
        await _developer_access(session, settings, auth, application_ref),
        payload,
    )


@router.patch("/applications/{application_ref}/emojis/{emoji_id}")
async def patch_application_emoji(
    application_ref: EntityRef,
    emoji_id: int,
    payload: ApplicationEmojiPatch,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "emoji.update",
        {
            "resource_id": emoji_id,
            "data": payload.model_dump(mode="json"),
        },
    )
    if remote is not None:
        return application_management_dict_body(remote)
    return await _patch_emoji(
        session,
        settings,
        await _developer_access(session, settings, auth, application_ref),
        emoji_id,
        payload,
    )


@router.delete("/applications/{application_ref}/emojis/{emoji_id}", status_code=204)
async def delete_application_emoji(
    application_ref: EntityRef,
    emoji_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    remote = await proxy_remote_application_management(
        session,
        settings,
        application_ref,
        auth.user,
        "emoji.delete",
        {"resource_id": emoji_id},
    )
    if remote is not None:
        require_application_management_empty(remote)
        return Response(status_code=204)
    await _delete_emoji(
        session,
        settings,
        await _developer_access(session, settings, auth, application_ref),
        emoji_id,
    )
    return Response(status_code=204)


@router.get("/bots/applications/@me/assets")
async def bot_list_application_assets(
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    return await _list_assets(session, _bot_access(principal, "applications.assets.manage"))


@router.post("/bots/applications/@me/assets/tickets", status_code=201)
async def bot_create_application_asset_ticket(
    payload: UploadTicketRequest,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _ticket(
        session,
        settings,
        snowflake,
        _bot_write_access(principal, settings, "applications.assets.manage"),
        payload,
        purpose="application_asset",
    )


@router.get("/bots/applications/@me/assets/{asset_id}")
async def bot_get_application_asset(
    asset_id: int,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    access = _bot_access(principal, "applications.assets.manage")
    return asset_payload(await _asset(session, access, asset_id))


@router.patch("/bots/applications/@me/assets/{asset_id}")
async def bot_patch_application_asset(
    asset_id: int,
    payload: ApplicationAssetPatch,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _patch_asset(
        session,
        settings,
        _bot_write_access(principal, settings, "applications.assets.manage"),
        asset_id,
        payload,
    )


@router.post("/bots/applications/@me/assets", status_code=201)
async def bot_create_application_asset(
    payload: ApplicationAssetCommit,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _commit_asset(
        session,
        settings,
        snowflake,
        response,
        _bot_write_access(principal, settings, "applications.assets.manage"),
        payload,
    )


@router.delete("/bots/applications/@me/assets/{asset_id}", status_code=204)
async def bot_delete_application_asset(
    asset_id: int,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await _delete_asset(
        session,
        settings,
        _bot_write_access(principal, settings, "applications.assets.manage"),
        asset_id,
    )
    return Response(status_code=204)


@router.get("/bots/applications/@me/emojis")
async def bot_list_application_emojis(
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    return await _list_emojis(session, _bot_access(principal, "applications.emojis.manage"))


@router.get("/bots/applications/@me/emojis/{emoji_id}")
async def bot_get_application_emoji(
    emoji_id: int,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    access = _bot_access(principal, "applications.emojis.manage")
    return emoji_payload(await _emoji(session, access, emoji_id))


@router.post("/bots/applications/@me/emojis/tickets", status_code=201)
async def bot_create_application_emoji_ticket(
    payload: UploadTicketRequest,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _ticket(
        session,
        settings,
        snowflake,
        _bot_write_access(principal, settings, "applications.emojis.manage"),
        payload,
        purpose="application_emoji",
    )


@router.post("/bots/applications/@me/emojis", status_code=201)
async def bot_create_application_emoji(
    payload: ApplicationEmojiCommit,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _commit_emoji(
        session,
        settings,
        snowflake,
        response,
        _bot_write_access(principal, settings, "applications.emojis.manage"),
        payload,
    )


@router.patch("/bots/applications/@me/emojis/{emoji_id}")
async def bot_patch_application_emoji(
    emoji_id: int,
    payload: ApplicationEmojiPatch,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _patch_emoji(
        session,
        settings,
        _bot_write_access(principal, settings, "applications.emojis.manage"),
        emoji_id,
        payload,
    )


@router.delete("/bots/applications/@me/emojis/{emoji_id}", status_code=204)
async def bot_delete_application_emoji(
    emoji_id: int,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await _delete_emoji(
        session,
        settings,
        _bot_write_access(principal, settings, "applications.emojis.manage"),
        emoji_id,
    )
    return Response(status_code=204)
