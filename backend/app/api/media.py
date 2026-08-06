from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from anyio import CapacityLimiter, WouldBlock
from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import local_guild
from app.chat.channel_access import load_channel_access
from app.chat.events import publish_dispatch, user_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import user_payload
from app.chat.permissions import require_permissions
from app.core.permission_contract import required_permissions
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference, Snowflake
from app.db.models import (
    Attachment,
    Emoji,
    Message,
    RemoteMediaCache,
    RemoteMediaTombstone,
    User,
)
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError, normalize_domain
from app.media.processing import (
    IMAGE_TYPES,
    MediaValidationError,
    clamav_scan,
    content_digest,
    normalize_declared_type,
    sniff_content_type,
    validate_detected_type,
)
from app.media.schemas import (
    AssetCommitRequest,
    AssetKind,
    EmojiCommitRequest,
    GuildAssetKind,
    UploadTicketRequest,
)
from app.media.service import (
    attachment_payload,
    bind_asset,
    create_upload_ticket,
    finalize_attachment,
)
from app.media.storage import S3Storage, StorageError
from app.tasks import media_cache_gc, media_local_purge, media_process

router = APIRouter(tags=["media"])
REMOTE_MEDIA_FETCH_CONCURRENCY = 8
PRIVATE_MEDIA_CAPABILITY_SECONDS = 60
remote_media_fetch_limiter = CapacityLimiter(REMOTE_MEDIA_FETCH_CONCURRENCY)


@asynccontextmanager
async def remote_media_fetch_admission(
    redis: Redis,
    response: Response,
    *,
    user_id: int,
    user_domain: str,
) -> AsyncIterator[None]:
    """Bound distinct remote cache misses without penalizing cache hits."""

    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["remote_media_fetch"],
        user_id=user_id,
        user_domain=user_domain,
    )
    try:
        remote_media_fetch_limiter.acquire_nowait()
    except WouldBlock:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "REMOTE_MEDIA_BUSY", "retry_after_ms": 1_000},
            headers={"Retry-After": "1"},
        ) from None
    try:
        yield
    finally:
        remote_media_fetch_limiter.release()


def copy_rate_limit_headers(source: Response, destination: Response) -> None:
    for name, value in source.headers.items():
        if name.lower().startswith("x-ratelimit-"):
            destination.headers[name] = value


def require_image_type(content_type: str | None) -> None:
    if content_type not in IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "IMAGE_ASSET_TYPE_REQUIRED"},
        )


def ticket_payload(attachment: Attachment, upload_url: str) -> dict[str, object]:
    return {
        **attachment_payload(attachment),
        "upload_url": upload_url,
        "upload_method": "PUT",
        "expires_at": (
            attachment.upload_expires_at.isoformat()
            if attachment.upload_expires_at is not None
            else None
        ),
    }


@router.post(
    "/api/v1/channels/{channel_id}/attachments",
    status_code=status.HTTP_201_CREATED,
)
async def create_channel_attachment_ticket(
    channel_id: EntityRef,
    payload: UploadTicketRequest,
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
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None:
        await require_permissions(
            session,
            redis,
            access.guild,
            auth.user,
            required_permissions("attachment.create"),
            channel=access.channel,
        )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        auth.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.post("/api/v1/users/@me/assets/{kind}", status_code=status.HTTP_201_CREATED)
async def create_user_asset_ticket(
    kind: AssetKind,
    payload: UploadTicketRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_image_type(payload.content_type)
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        auth.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose=kind,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.put("/api/v1/users/@me/assets/{kind}")
async def commit_user_asset(
    kind: AssetKind,
    payload: AssetCommitRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED"})
    attachment = await finalize_attachment(
        session, settings, user, int(payload.attachment_id), required_purpose=kind
    )
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return attachment_payload(attachment)
    if attachment.content_sha256 is None:
        raise RuntimeError("clean media is missing its content digest")
    require_image_type(attachment.detected_content_type)
    previous = await bind_asset(
        session,
        attachment,
        f"user:{settings.domain}:{user.id}:{kind}",
    )
    field = "avatar_hash" if kind == "avatar" else "banner_hash"
    setattr(user, field, attachment.content_sha256)
    user.profile_version += 1
    await session.commit()
    rendered = attachment_payload(attachment)
    await publish_dispatch(
        redis,
        user_topic(settings.domain, user.id),
        "USER_UPDATE",
        user_payload(user),
    )
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return rendered


@router.post("/api/v1/guilds/{guild_id}/assets/{kind}", status_code=201)
async def create_guild_asset_ticket(
    guild_id: EntityRef,
    kind: GuildAssetKind,
    payload: UploadTicketRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_image_type(payload.content_type)
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.asset.manage")
    )
    purpose = "guild_icon" if kind == "icon" else "guild_banner"
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        auth.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose=purpose,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.put("/api/v1/guilds/{guild_id}/assets/{kind}")
async def commit_guild_asset(
    guild_id: EntityRef,
    kind: GuildAssetKind,
    payload: AssetCommitRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.asset.manage")
    )
    purpose = "guild_icon" if kind == "icon" else "guild_banner"
    attachment = await finalize_attachment(
        session, settings, auth.user, int(payload.attachment_id), required_purpose=purpose
    )
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return attachment_payload(attachment)
    if attachment.content_sha256 is None:
        raise RuntimeError("clean media is missing its content digest")
    require_image_type(attachment.detected_content_type)
    previous = await bind_asset(
        session,
        attachment,
        f"guild:{guild.origin_domain}:{guild.id}:{kind}",
    )
    field = "icon_hash" if kind == "icon" else "banner_hash"
    setattr(guild, field, attachment.content_sha256)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.update",
        {"guild": {"id": str(guild.id), field: attachment.content_sha256}},
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return attachment_payload(attachment)


@router.post("/api/v1/guilds/{guild_id}/emojis/tickets", status_code=201)
async def create_emoji_ticket(
    guild_id: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_image_type(payload.content_type)
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.emoji.manage")
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        auth.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="emoji",
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.post("/api/v1/guilds/{guild_id}/emojis", status_code=201)
async def create_emoji(
    guild_id: EntityRef,
    payload: EmojiCommitRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.emoji.manage")
    )
    attachment = await finalize_attachment(
        session, settings, auth.user, int(payload.attachment_id), required_purpose="emoji"
    )
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return attachment_payload(attachment)
    require_image_type(attachment.detected_content_type)
    # The bounded query avoids a table-wide count on a hostile repeated request.
    existing = list(
        await session.scalars(
            select(Emoji)
            .where(Emoji.guild_id == guild.id, Emoji.guild_domain == guild.origin_domain)
            .limit(settings.media_emoji_limit)
        )
    )
    if len(existing) >= settings.media_emoji_limit:
        raise HTTPException(status_code=409, detail={"code": "EMOJI_LIMIT_REACHED"})
    if any(item.name.casefold() == payload.name.casefold() for item in existing):
        raise HTTPException(status_code=409, detail={"code": "EMOJI_NAME_TAKEN"})
    variant = attachment.variants.get("thumbnail_128")
    object_key = variant.get("object_key") if isinstance(variant, dict) else None
    if not isinstance(object_key, str):
        raise RuntimeError("clean emoji media is missing its object key")
    emoji = Emoji(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name=payload.name,
        object_key=object_key,
        animated=attachment.detected_content_type in {"image/gif", "image/webp"},
        creator_id=auth.user.id,
        creator_domain=auth.user.origin_domain,
    )
    await bind_asset(
        session,
        attachment,
        f"emoji:{guild.origin_domain}:{emoji.id}",
    )
    session.add(emoji)
    await session.commit()
    return {
        "id": str(emoji.id),
        "origin_domain": emoji.origin_domain,
        "guild_id": str(emoji.guild_id),
        "name": emoji.name,
        "animated": emoji.animated,
        "media_hash": attachment.content_sha256,
    }


@router.delete("/api/v1/guilds/{guild_id}/emojis/{emoji_id}", status_code=204)
async def delete_emoji(
    guild_id: EntityRef,
    emoji_id: Snowflake,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.emoji.manage")
    )
    emoji = await session.get(Emoji, (int(emoji_id), settings.domain))
    if emoji is None or (emoji.guild_id, emoji.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "EMOJI_NOT_FOUND"})
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == f"emoji:{emoji.origin_domain}:{emoji.id}")
        .with_for_update()
    )
    await session.delete(emoji)
    if attachment is not None:
        attachment.asset_binding = None
    await session.commit()
    if attachment is not None:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    return Response(status_code=204)


@router.get("/api/v1/attachments/{attachment_id}")
async def get_attachment_status(
    attachment_id: Snowflake,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    attachment = await session.get(Attachment, (int(attachment_id), settings.domain))
    if attachment is None or (attachment.uploader_id, attachment.uploader_domain) != (
        auth.user.id,
        auth.user.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    return attachment_payload(attachment)


def select_variant(
    settings: Settings, attachment: Attachment, variant: str
) -> tuple[str, str, str]:
    if attachment.scan_status != "clean" or attachment.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_AVAILABLE"})
    if variant == "original":
        return settings.media_attachments_bucket, attachment.object_key, attachment.filename
    raw = attachment.variants.get(variant)
    if not isinstance(raw, dict) or not isinstance(raw.get("object_key"), str):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    return settings.media_derived_bucket, raw["object_key"], attachment.filename


def redirect_to_object(
    settings: Settings, attachment: Attachment, variant: str, *, public: bool
) -> RedirectResponse:
    bucket, key, filename = select_variant(settings, attachment, variant)
    try:
        url = S3Storage(settings).presign(
            "GET",
            bucket,
            key,
            expires=604_800 if public else PRIVATE_MEDIA_CAPABILITY_SECONDS,
            download_name=(
                filename
                if not public and attachment.purpose == "attachment" and variant == "original"
                else None
            ),
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"}) from exc
    response = RedirectResponse(url, status_code=302)
    response.headers["Cache-Control"] = (
        "public, max-age=518400, immutable" if public else "private, no-store"
    )
    if not public:
        response.headers["Vary"] = "Authorization, Cookie"
    return response


@router.get("/media/assets/{content_hash}/{variant}")
async def public_asset(
    content_hash: str = Path(pattern=r"^[0-9a-f]{64}$"),
    variant: str = Path(pattern=r"^(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    attachment = await session.scalar(
        select(Attachment).where(
            Attachment.origin_domain == settings.domain,
            Attachment.content_sha256 == content_hash,
            Attachment.purpose != "attachment",
            Attachment.scan_status == "clean",
            Attachment.deleted_at.is_(None),
        )
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    return redirect_to_object(settings, attachment, variant, public=True)


async def cache_remote_media(
    session: AsyncSession,
    settings: Settings,
    *,
    origin_domain: str,
    attachment_id: int,
    variant: str,
) -> RemoteMediaCache:
    if await session.get(RemoteMediaTombstone, (origin_domain, attachment_id)) is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    try:
        remote = await signed_request(
            session,
            settings,
            "GET",
            origin_domain,
            f"/_kaede/v1/media/{attachment_id}/{variant}",
            max_response_bytes=settings.media_max_attachment_bytes,
        )
    except (FederationNetworkError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail={"code": "REMOTE_MEDIA_UNAVAILABLE"}) from exc
    if remote.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    if remote.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "REMOTE_MEDIA_UNAVAILABLE"})

    body = remote.content
    try:
        scan_status = await clamav_scan(body, settings)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "REMOTE_MEDIA_UNAVAILABLE"}) from exc
    if scan_status != "clean":
        raise HTTPException(status_code=422, detail={"code": "REMOTE_MEDIA_REJECTED"})

    detected_type = sniff_content_type(body)
    try:
        declared_type = normalize_declared_type(
            remote.headers.get("Content-Type", "application/octet-stream")
        )
        validate_detected_type(declared_type, detected_type)
    except MediaValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": "REMOTE_MEDIA_REJECTED"}) from exc

    cache_key = f"{origin_domain}/{attachment_id}/{variant}"
    try:
        await S3Storage(settings).put(
            settings.media_remote_cache_bucket,
            cache_key,
            body,
            detected_type,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"}) from exc

    stored_at = datetime.now(UTC)
    expires_at = stored_at + timedelta(days=settings.media_remote_cache_ttl_days)
    digest = content_digest(body)
    await session.execute(
        pg_insert(RemoteMediaCache)
        .values(
            origin_domain=origin_domain,
            attachment_id=attachment_id,
            variant=variant,
            object_key=cache_key,
            size=len(body),
            content_type=detected_type,
            content_sha256=digest,
            scan_status="clean",
            last_accessed_at=stored_at,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=["origin_domain", "attachment_id", "variant"],
            set_={
                "object_key": cache_key,
                "size": len(body),
                "content_type": detected_type,
                "content_sha256": digest,
                "scan_status": "clean",
                "last_accessed_at": stored_at,
                "expires_at": expires_at,
            },
        )
    )
    await session.commit()
    await enqueue_best_effort(media_cache_gc)
    cached = await session.get(RemoteMediaCache, (origin_domain, attachment_id, variant))
    if cached is None:
        raise RuntimeError("remote media cache write did not converge")
    return cached


@router.get("/media/{origin_domain}/{attachment_id}/{variant}")
async def authorized_attachment(
    origin_domain: str,
    attachment_id: Snowflake,
    response_status: Response,
    variant: str = Path(pattern=r"^(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$"),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    try:
        origin_domain = normalize_domain(origin_domain)
    except FederationNetworkError:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"}) from None
    attachment = await session.get(Attachment, (int(attachment_id), origin_domain))
    if (
        attachment is None
        or attachment.deleted_at is not None
        or attachment.message_id is None
        or attachment.message_domain is None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    message = await session.get(Message, (attachment.message_id, attachment.message_domain))
    if message is None or message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    access = await load_channel_access(
        session,
        settings,
        auth.user,
        EntityReference(message.channel_id, message.channel_domain),
    )
    if access.guild is not None:
        await require_permissions(
            session,
            redis,
            access.guild,
            auth.user,
            required_permissions("message.list"),
            channel=access.channel,
        )
    attachment.updated_at = datetime.now(UTC)
    if origin_domain == settings.domain:
        return redirect_to_object(settings, attachment, variant, public=False)

    remote_attachment_id = int(attachment_id)
    if await session.get(RemoteMediaTombstone, (origin_domain, remote_attachment_id)) is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"kaede-remote-media:{origin_domain}:{remote_attachment_id}", 0
                )
            )
        )
    )
    if (
        await session.get(
            RemoteMediaTombstone,
            (origin_domain, remote_attachment_id),
            populate_existing=True,
        )
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    cached = await session.get(RemoteMediaCache, (origin_domain, remote_attachment_id, variant))
    checked_at = datetime.now(UTC)
    if cached is not None and (cached.expires_at <= checked_at or cached.scan_status != "clean"):
        cached = None
    if cached is None:
        async with remote_media_fetch_admission(
            redis,
            response_status,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
        ):
            cached = await cache_remote_media(
                session,
                settings,
                origin_domain=origin_domain,
                attachment_id=remote_attachment_id,
                variant=variant,
            )
    if cached is None:
        raise RuntimeError("remote media cache write did not converge")
    cached.last_accessed_at = datetime.now(UTC)
    try:
        url = S3Storage(settings).presign(
            "GET",
            settings.media_remote_cache_bucket,
            cached.object_key,
            expires=PRIVATE_MEDIA_CAPABILITY_SECONDS,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"}) from exc
    await session.commit()
    response = RedirectResponse(url, status_code=302)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie"
    copy_rate_limit_headers(response_status, response)
    return response
