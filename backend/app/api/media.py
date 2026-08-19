from __future__ import annotations

import hashlib
import secrets
import tempfile
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path as FilePath
from typing import cast

import anyio
from anyio import CapacityLimiter, WouldBlock
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import local_guild
from app.chat.channel_access import load_channel_access
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import emoji_payload, guild_payload, user_payload
from app.chat.permissions import require_permissions
from app.core.metrics import increment_metric
from app.core.permission_contract import required_permissions
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference, Snowflake
from app.db.bot_models import AbuseReport
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    Emoji,
    Guild,
    GuildMember,
    MediaTombstoneSource,
    Message,
    RemoteMediaCache,
    RemoteMediaOrphan,
    RemoteMediaTombstone,
    TerminalRoomDeletion,
    User,
)
from app.federation.client import signed_request, signed_stream_request
from app.federation.dm_history import history_media_capability_status, history_media_path
from app.federation.network import FederationNetworkError, normalize_domain
from app.federation.relationships import queue_friend_profile_updates
from app.media.digest_revocation import (
    DIGEST_REVOCATION_STATUSES,
    lock_asset_digest,
    valid_content_digest,
)
from app.media.photodna import PhotoDNAInputRejected, photodna_report_values, scan_image
from app.media.processing import (
    IMAGE_TYPES,
    MediaValidationError,
    clamav_scan_file,
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
from app.tasks import federation_deliver, media_cache_gc, media_local_purge, media_process

router = APIRouter(tags=["media"])
REMOTE_MEDIA_UPLOAD_RESERVATION_SECONDS = 5 * 60
REMOTE_MEDIA_FETCH_CONCURRENCY = 8
REMOTE_MEDIA_FETCH_DEADLINE_SECONDS = 60
PRIVATE_MEDIA_CAPABILITY_SECONDS = 60
PUBLIC_MEDIA_CAPABILITY_SECONDS = 5 * 60
PUBLIC_MEDIA_REDIRECT_CACHE_SECONDS = 60
remote_media_fetch_limiter = CapacityLimiter(REMOTE_MEDIA_FETCH_CONCURRENCY)
REMOTE_MEDIA_RESERVATION_TTL_MS = 300_000
REMOTE_MEDIA_RESERVE_LUA = """
local function cleanup(zset_key, hash_key, total_key, now)
  local total = tonumber(redis.call('GET', total_key) or '0')
  local expired = redis.call('ZRANGEBYSCORE', zset_key, '-inf', now)
  for _, token in ipairs(expired) do
    local weight = tonumber(redis.call('HGET', hash_key, token) or '0')
    total = math.max(0, total - weight)
    redis.call('HDEL', hash_key, token)
    redis.call('ZREM', zset_key, token)
  end
  if total == 0 then redis.call('DEL', total_key) else redis.call('SET', total_key, total) end
  return total
end
local now = tonumber(ARGV[3])
local global_total = cleanup(KEYS[1], KEYS[2], KEYS[3], now)
local origin_total = cleanup(KEYS[4], KEYS[5], KEYS[6], now)
local weight = tonumber(ARGV[2])
if global_total + weight > tonumber(ARGV[5]) or origin_total + weight > tonumber(ARGV[4]) then
  return {0, global_total, origin_total}
end
local expires = tonumber(ARGV[3]) + tonumber(ARGV[6])
redis.call('HSET', KEYS[2], ARGV[1], weight)
redis.call('ZADD', KEYS[1], expires, ARGV[1])
redis.call('SET', KEYS[3], global_total + weight, 'PX', tonumber(ARGV[6]) * 2)
redis.call('HSET', KEYS[5], ARGV[1], weight)
redis.call('ZADD', KEYS[4], expires, ARGV[1])
redis.call('SET', KEYS[6], origin_total + weight, 'PX', tonumber(ARGV[6]) * 2)
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[6]) * 2)
redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[6]) * 2)
redis.call('PEXPIRE', KEYS[4], tonumber(ARGV[6]) * 2)
redis.call('PEXPIRE', KEYS[5], tonumber(ARGV[6]) * 2)
return {1, global_total + weight, origin_total + weight}
"""
REMOTE_MEDIA_RELEASE_LUA = """
local function release(zset_key, hash_key, total_key, token)
  local weight = tonumber(redis.call('HGET', hash_key, token) or '0')
  if weight == 0 then return end
  local total = math.max(0, tonumber(redis.call('GET', total_key) or '0') - weight)
  redis.call('HDEL', hash_key, token)
  redis.call('ZREM', zset_key, token)
  if total == 0 then
    redis.call('DEL', total_key)
  else
    redis.call('SET', total_key, total, 'PX', ARGV[2])
  end
end
release(KEYS[1], KEYS[2], KEYS[3], ARGV[1])
release(KEYS[4], KEYS[5], KEYS[6], ARGV[1])
return 1
"""
REMOTE_MEDIA_CACHE_LOCK_SECONDS = 300
REMOTE_MEDIA_CACHE_UNLOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@asynccontextmanager
async def remote_media_fetch_admission(
    redis: Redis,
    response: Response,
    *,
    user_id: int,
    user_domain: str,
    origin_domain: str,
    settings: Settings,
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
    token = secrets.token_urlsafe(18)
    global_prefix = "federation:remote-media:inflight"
    origin_prefix = f"{global_prefix}:{origin_domain}"
    keys = (
        f"{global_prefix}:leases",
        f"{global_prefix}:weights",
        f"{global_prefix}:bytes",
        f"{origin_prefix}:leases",
        f"{origin_prefix}:weights",
        f"{origin_prefix}:bytes",
    )
    reserved = False
    try:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        result = await cast(
            Awaitable[object],
            redis.eval(
                REMOTE_MEDIA_RESERVE_LUA,
                len(keys),
                *keys,
                token,
                str(settings.media_max_attachment_bytes),
                str(now_ms),
                str(settings.federation_remote_media_inflight_bytes_per_origin),
                str(settings.federation_remote_media_inflight_bytes_total),
                str(REMOTE_MEDIA_RESERVATION_TTL_MS),
            ),
        )
        if not isinstance(result, (list, tuple)) or not result or int(result[0]) != 1:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "REMOTE_MEDIA_BUSY", "retry_after_ms": 1_000},
                headers={"Retry-After": "1"},
            )
        reserved = True
        yield
    finally:
        if reserved:
            with suppress(Exception):
                await cast(
                    Awaitable[object],
                    redis.eval(
                        REMOTE_MEDIA_RELEASE_LUA,
                        len(keys),
                        *keys,
                        token,
                        str(REMOTE_MEDIA_RESERVATION_TTL_MS * 2),
                    ),
                )
        remote_media_fetch_limiter.release()


@asynccontextmanager
async def remote_media_cache_key_lock(
    redis: Redis,
    origin_domain: str,
    attachment_id: int,
    variant: str,
) -> AsyncIterator[None]:
    """Serialize one cache identity across workers with a crash-safe lease."""

    key = f"federation:remote-media:cache-lock:{origin_domain}:{attachment_id}:{variant}"
    token = secrets.token_urlsafe(18)
    accepted = await redis.set(
        key,
        token,
        ex=REMOTE_MEDIA_CACHE_LOCK_SECONDS,
        nx=True,
    )
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "REMOTE_MEDIA_BUSY", "retry_after_ms": 1_000},
            headers={"Retry-After": "1"},
        )
    try:
        yield
    finally:
        with suppress(Exception):
            await cast(
                Awaitable[object],
                redis.eval(REMOTE_MEDIA_CACHE_UNLOCK_LUA, 1, key, token),
            )


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
    expected_mode = "e2ee" if access.channel.encryption_mode == "e2ee" else "plaintext"
    if expected_mode == "e2ee" and access.channel.encryption_state != "active":
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
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        auth.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        encryption_mode=payload.encryption_mode,
        encryption_protocol=payload.encryption_protocol,
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
    destinations = await queue_friend_profile_updates(session, settings, user)
    await session.commit()
    rendered = attachment_payload(attachment)
    await publish_dispatch(
        redis,
        user_topic(settings.domain, user.id),
        "USER_UPDATE",
        user_payload(user),
    )
    for destination in destinations:
        await enqueue_best_effort(federation_deliver, destination)
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
    # PostgreSQL's on-update expression expires ``updated_at`` during the
    # flush.  Refresh before the synchronous payload serializer reads that
    # resource version; otherwise AsyncSession attempts an implicit lazy load
    # and raises MissingGreenlet after an otherwise successful upload/scan.
    await session.flush()
    await session.refresh(guild)
    rendered_guild = guild_payload(guild)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.update",
        {"guild": rendered_guild},
    )
    await session.commit()
    await session.refresh(guild)
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_UPDATE",
        guild_payload(guild),
    )
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
    if payload.size > settings.media_max_emoji_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "EMOJI_TOO_LARGE",
                "max_bytes": settings.media_max_emoji_bytes,
            },
        )
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
        media_hash=attachment.content_sha256,
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
    await session.flush()
    rendered = emoji_payload(emoji)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.emoji.create",
        {"emoji": rendered},
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_EMOJI_CREATE",
        rendered,
    )
    return rendered


@router.delete("/api/v1/guilds/{guild_id}/emojis/{emoji_id}", status_code=204)
async def delete_emoji(
    guild_id: EntityRef,
    emoji_id: Snowflake,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
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
    rendered = emoji_payload(emoji)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.emoji.delete",
        {"emoji": rendered},
    )
    await session.delete(emoji)
    if attachment is not None:
        attachment.asset_binding = None
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_EMOJI_DELETE",
        rendered,
    )
    if attachment is not None:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    return Response(status_code=204)


@router.get("/api/v1/users/@me/emojis")
async def available_emojis(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(Emoji, Guild.name)
            .join(
                Guild,
                (Guild.id == Emoji.guild_id) & (Guild.origin_domain == Emoji.guild_domain),
            )
            .join(
                GuildMember,
                (GuildMember.guild_id == Emoji.guild_id)
                & (GuildMember.guild_domain == Emoji.guild_domain),
            )
            .where(
                GuildMember.user_id == auth.user.id,
                GuildMember.user_domain == auth.user.origin_domain,
                Guild.unavailable.is_(False),
                Emoji.media_hash.is_not(None),
            )
            .order_by(Guild.name, Emoji.name, Emoji.id)
            .limit(5000)
        )
    ).tuples()
    return [{**emoji_payload(emoji), "guild_name": guild_name} for emoji, guild_name in rows]


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
    if attachment.scan_status not in {"clean", "encrypted"} or attachment.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_AVAILABLE"})
    if attachment.encryption_mode == "e2ee" and variant != "original":
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    if variant == "original":
        return settings.media_attachments_bucket, attachment.object_key, attachment.filename
    raw = attachment.variants.get(variant)
    if not isinstance(raw, dict) or not isinstance(raw.get("object_key"), str):
        # Older scanned images can predate generated derivatives. Falling back
        # to the bounded, clean original preserves compatibility without
        # trusting an unscanned or non-image payload as a preview.
        if (attachment.detected_content_type or attachment.content_type) in IMAGE_TYPES:
            return settings.media_attachments_bucket, attachment.object_key, attachment.filename
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
            expires=(
                PUBLIC_MEDIA_CAPABILITY_SECONDS if public else PRIVATE_MEDIA_CAPABILITY_SECONDS
            ),
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
        f"public, max-age={PUBLIC_MEDIA_REDIRECT_CACHE_SECONDS}, must-revalidate"
        if public
        else "private, no-store"
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
    # Public capabilities are authorized by digest, so serialize the entire
    # check-and-presign window with terminal verdicts, asset binding, and
    # proof cleanup for that digest. This endpoint owns no Attachment yet and
    # can therefore take the blocking fence in the global digest -> row order.
    await lock_asset_digest(session, content_hash)
    terminal_duplicate = aliased(Attachment)
    attachment = await session.scalar(
        select(Attachment)
        .where(
            Attachment.origin_domain == settings.domain,
            Attachment.content_sha256 == content_hash,
            Attachment.purpose != "attachment",
            Attachment.asset_binding.is_not(None),
            Attachment.scan_status == "clean",
            Attachment.deleted_at.is_(None),
            ~exists(
                select(terminal_duplicate.id).where(
                    terminal_duplicate.origin_domain == settings.domain,
                    terminal_duplicate.content_sha256 == content_hash,
                    terminal_duplicate.scan_status.in_(DIGEST_REVOCATION_STATUSES),
                )
            ),
        )
        .with_for_update(read=True)
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    if (
        await session.get(
            MediaTombstoneSource,
            (attachment.id, attachment.origin_domain),
        )
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    response = redirect_to_object(settings, attachment, variant, public=True)
    # Keep the shared Attachment lock through capability construction. A
    # reprocessing worker that discovers a terminal PhotoDNA verdict either
    # commits before this read (and is observed above), or waits until the
    # already-authorized capability has been minted.
    await session.commit()
    return response


@router.get("/media/emojis/{emoji_id}/{variant}")
async def public_emoji(
    emoji_id: Snowflake,
    variant: str = Path(pattern=r"^(original|thumbnail_128|thumbnail_512)$"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    emoji = await session.get(Emoji, (int(emoji_id), settings.domain))
    if emoji is None:
        raise HTTPException(status_code=404, detail={"code": "EMOJI_NOT_FOUND"})
    digest = emoji.media_hash
    if not valid_content_digest(digest):
        raise HTTPException(status_code=404, detail={"code": "EMOJI_NOT_FOUND"})
    # Emoji IDs do not expose the digest in the URL. Read the immutable hash,
    # take its fence before any Attachment lock, then revalidate the Emoji and
    # exact binding under the fence before minting a capability.
    await lock_asset_digest(session, digest)
    emoji = await session.scalar(
        select(Emoji)
        .where(
            Emoji.id == int(emoji_id),
            Emoji.origin_domain == settings.domain,
            Emoji.media_hash == digest,
        )
        .execution_options(populate_existing=True)
    )
    if emoji is None:
        raise HTTPException(status_code=404, detail={"code": "EMOJI_NOT_FOUND"})
    terminal_duplicate = aliased(Attachment)
    attachment = await session.scalar(
        select(Attachment)
        .where(
            Attachment.asset_binding == f"emoji:{emoji.origin_domain}:{emoji.id}",
            Attachment.origin_domain == settings.domain,
            Attachment.content_sha256 == digest,
            Attachment.scan_status == "clean",
            Attachment.deleted_at.is_(None),
            ~exists(
                select(terminal_duplicate.id).where(
                    terminal_duplicate.origin_domain == settings.domain,
                    terminal_duplicate.content_sha256 == Attachment.content_sha256,
                    terminal_duplicate.scan_status.in_(DIGEST_REVOCATION_STATUSES),
                )
            ),
        )
        .with_for_update(read=True)
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail={"code": "EMOJI_NOT_FOUND"})
    if (
        await session.get(
            MediaTombstoneSource,
            (attachment.id, attachment.origin_domain),
        )
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "EMOJI_NOT_FOUND"})
    response = redirect_to_object(settings, attachment, variant, public=True)
    await session.commit()
    return response


def remote_media_federation_query(
    dm_history_scope: tuple[tuple[int, str], tuple[int, str]] | None,
    requester: tuple[int, str] | None,
) -> dict[str, str] | None:
    """Bind remote DM media fetches to the exact requesting local user.

    Group-DM origins require this identity even for the ordinary live media
    path.  History scope is an additional binding, not the condition that
    decides whether the requester is sent.
    """

    query: dict[str, str] = {}
    if dm_history_scope is not None:
        query.update(
            {
                "conversation_id": str(dm_history_scope[0][0]),
                "conversation_domain": dm_history_scope[0][1],
                "message_id": str(dm_history_scope[1][0]),
                "message_domain": dm_history_scope[1][1],
            }
        )
    if requester is not None:
        query.update(
            {
                "requester_id": str(requester[0]),
                "requester_domain": requester[1],
            }
        )
    return query or None


async def known_photodna_match(
    session: AsyncSession,
    origin_domain: str,
    attachment_id: int,
) -> bool:
    """Return the durable attachment-wide decision, never a variant decision."""

    return (
        await session.scalar(
            select(AbuseReport.id)
            .where(
                AbuseReport.source == "photodna",
                AbuseReport.target_type == "attachment",
                AbuseReport.target_ref == f"{attachment_id}@{origin_domain}",
            )
            .limit(1)
        )
        is not None
    )


async def acquire_remote_photodna_lock(
    session: AsyncSession,
    origin_domain: str,
    attachment_id: int,
) -> None:
    """Serialize a positive decision with final cache admission."""

    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"kaede-remote-media-photodna:{origin_domain}:{attachment_id}", 0
                )
            )
        )
    )


async def final_remote_cache_for_capability(
    session: AsyncSession,
    *,
    origin_domain: str,
    attachment_id: int,
    variant: str,
    local_domain: str,
    message_ref: tuple[int, str] | None = None,
    conversation_ref: tuple[int, str] | None = None,
) -> RemoteMediaCache:
    """Order capability minting against authoritative delete and PhotoDNA.

    The remote-media budget lock is also the deletion/cache-row serialization
    fence. Reloading both the tombstone and cache row only after acquiring it
    prevents a stale identity-map value from minting a capability after a
    concurrently committed ``media.delete``. Keep the lock through the caller's
    presign and commit; that makes delete-before-capability ordering explicit.
    """

    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    if (
        await session.get(
            RemoteMediaTombstone,
            (origin_domain, attachment_id),
            populate_existing=True,
        )
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    await require_remote_media_binding_live(
        session,
        origin_domain=origin_domain,
        attachment_id=attachment_id,
        message_ref=message_ref,
        conversation_ref=conversation_ref,
        local_domain=local_domain,
    )
    if (
        await session.get(
            MediaTombstoneSource,
            (attachment_id, origin_domain),
            populate_existing=True,
        )
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    await acquire_remote_photodna_lock(session, origin_domain, attachment_id)
    await reject_known_photodna_match(
        session,
        origin_domain=origin_domain,
        attachment_id=attachment_id,
    )
    cached = await session.get(
        RemoteMediaCache,
        (origin_domain, attachment_id, variant),
        populate_existing=True,
        with_for_update=True,
    )
    checked_at = datetime.now(UTC)
    if cached is None or cached.expires_at <= checked_at or cached.scan_status != "clean":
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    return cached


async def require_remote_media_binding_live(
    session: AsyncSession,
    *,
    origin_domain: str,
    attachment_id: int,
    message_ref: tuple[int, str] | None,
    conversation_ref: tuple[int, str] | None,
    local_domain: str,
) -> None:
    """Revalidate the carrier after the remote-cache deletion fence."""

    attachment = await session.get(
        Attachment,
        (attachment_id, origin_domain),
        populate_existing=True,
    )
    if attachment is not None:
        if (
            attachment.deleted_at is not None
            or attachment.message_id is None
            or attachment.message_domain is None
            or (
                message_ref is not None
                and (attachment.message_id, attachment.message_domain) != message_ref
            )
        ):
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        message = await session.get(
            Message,
            (attachment.message_id, attachment.message_domain),
            populate_existing=True,
        )
        if message is None or message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        channel = await session.get(
            Channel,
            (message.channel_id, message.channel_domain),
            populate_existing=True,
        )
        if channel is None or channel.unavailable:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        room_ref = (
            ("guild", channel.guild_id, channel.guild_domain)
            if channel.guild_id is not None and channel.guild_domain is not None
            else None
        )
        conversation_ref = conversation_ref or (
            (channel.id, channel.origin_domain) if channel.guild_id is None else None
        )
    else:
        room_ref = None
        # Attachment-less access is supported only for an authenticated DM
        # authority history capability. Ordinary live media always has a
        # replicated Attachment carrier to revalidate.
        if conversation_ref is None or message_ref is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})

    if conversation_ref is not None:
        conversation = await session.get(
            DMConversation,
            conversation_ref,
            populate_existing=True,
        )
        conversation_channel = await session.get(
            Channel,
            conversation_ref,
            populate_existing=True,
        )
        if conversation is None or conversation_channel is None or conversation_channel.unavailable:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        if conversation.type == "group":
            room_ref = ("group_dm", conversation.id, conversation.origin_domain)

    if room_ref is not None:
        terminal = await session.get(
            TerminalRoomDeletion,
            (room_ref[0], room_ref[1], room_ref[2], local_domain),
        )
        if terminal is not None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})


async def retire_remote_media_variants(
    session: AsyncSession,
    *,
    origin_domain: str,
    attachment_id: int,
) -> None:
    """Make every previously cached representation unreachable and GC-able."""

    rows = list(
        await session.scalars(
            select(RemoteMediaCache)
            .where(
                RemoteMediaCache.origin_domain == origin_domain,
                RemoteMediaCache.attachment_id == attachment_id,
            )
            .with_for_update()
        )
    )
    for item in rows:
        await session.execute(
            pg_insert(RemoteMediaOrphan)
            .values(object_key=item.object_key, size=item.size)
            .on_conflict_do_nothing(index_elements=["object_key"])
        )
        await session.delete(item)
    if rows:
        await session.commit()
        await enqueue_best_effort(media_cache_gc)


async def reject_known_photodna_match(
    session: AsyncSession,
    *,
    origin_domain: str,
    attachment_id: int,
) -> None:
    """Block and retire every cached representation after one positive match."""

    if not await known_photodna_match(session, origin_domain, attachment_id):
        return
    await retire_remote_media_variants(
        session,
        origin_domain=origin_domain,
        attachment_id=attachment_id,
    )
    raise HTTPException(status_code=422, detail={"code": "REMOTE_MEDIA_REJECTED"})


async def cache_remote_media(
    session: AsyncSession,
    settings: Settings,
    *,
    redis: Redis | None = None,
    origin_domain: str,
    attachment_id: int,
    variant: str,
    dm_history_scope: tuple[tuple[int, str], tuple[int, str]] | None = None,
    requester: tuple[int, str] | None = None,
    encrypted_transport: bool = False,
    snowflake: SnowflakeGenerator | None = None,
    message_ref: tuple[int, str] | None = None,
    uploader_ref: tuple[int, str] | None = None,
) -> RemoteMediaCache:
    if (
        await session.get(RemoteMediaTombstone, (origin_domain, attachment_id)) is not None
        or await session.get(MediaTombstoneSource, (attachment_id, origin_domain)) is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    # A prior positive decision applies to every representation of the same
    # authoritative attachment, even if an origin later changes its declared
    # encryption mode. Retire variants cached before the match as well as
    # refusing another download.
    await reject_known_photodna_match(
        session,
        origin_domain=origin_domain,
        attachment_id=attachment_id,
    )
    with tempfile.NamedTemporaryFile(
        prefix="kaede-remote-media-", suffix=".spool", delete=False
    ) as temporary:
        temporary_path = FilePath(temporary.name)
    declared_type = "application/octet-stream"
    declared_size: int | None = None
    received = 0
    digest_builder = hashlib.sha256()
    prefix = bytearray()
    federation_query = remote_media_federation_query(dm_history_scope, requester)

    try:
        try:
            async with signed_stream_request(
                session,
                settings,
                "GET",
                origin_domain,
                f"/_kaede/v1/media/{attachment_id}/{variant}",
                query=federation_query,
                request_timeout=REMOTE_MEDIA_FETCH_DEADLINE_SECONDS,
                max_response_bytes=settings.media_max_attachment_bytes,
            ) as remote:
                if remote.status_code == 404:
                    raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
                if remote.status_code != 200:
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
                    )
                raw_length = remote.headers.get("Content-Length")
                if raw_length is not None:
                    declared_size = int(raw_length)
                declared_type = normalize_declared_type(
                    remote.headers.get("Content-Type", "application/octet-stream")
                )
                with anyio.fail_after(REMOTE_MEDIA_FETCH_DEADLINE_SECONDS):
                    async with await anyio.open_file(temporary_path, "wb") as destination:
                        async for chunk in remote.aiter_raw():
                            received += len(chunk)
                            if received > settings.media_max_attachment_bytes:
                                raise MediaValidationError(
                                    "remote media exceeded the configured size limit"
                                )
                            digest_builder.update(chunk)
                            if len(prefix) < 512:
                                prefix.extend(chunk[: 512 - len(prefix)])
                            await destination.write(chunk)
        except HTTPException:
            raise
        except (FederationNetworkError, RuntimeError, TimeoutError) as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
            ) from exc
        except MediaValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "REMOTE_MEDIA_REJECTED"},
            ) from exc

        if declared_size is not None and declared_size != received:
            raise HTTPException(
                status_code=503,
                detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
            )
        if encrypted_transport:
            if variant != "original" or declared_type != "application/octet-stream":
                raise HTTPException(status_code=422, detail={"code": "REMOTE_MEDIA_REJECTED"})
            detected_type = "application/octet-stream"
        else:
            try:
                scan_status = await clamav_scan_file(temporary_path, settings)
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
                ) from exc
            if scan_status != "clean":
                raise HTTPException(status_code=422, detail={"code": "REMOTE_MEDIA_REJECTED"})

            detected_type = sniff_content_type(bytes(prefix))
            try:
                validate_detected_type(declared_type, detected_type)
            except MediaValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "REMOTE_MEDIA_REJECTED"},
                ) from exc

            if detected_type in IMAGE_TYPES:
                try:
                    image_bytes = await anyio.Path(temporary_path).read_bytes()
                    finding = await scan_image(image_bytes, settings)
                except PhotoDNAInputRejected as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "REMOTE_MEDIA_REJECTED"},
                    ) from exc
                except RuntimeError as exc:
                    # Matching is fail-closed: bytes do not enter the local
                    # cache when the licensed generator or Microsoft service
                    # cannot return a trustworthy terminal decision.
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
                    ) from exc
                if finding is not None:
                    if snowflake is None:
                        raise RuntimeError(
                            "PhotoDNA remote scanning requires a snowflake generator"
                        )
                    attachment_ref = f"{attachment_id}@{origin_domain}"
                    report_values = photodna_report_values(
                        report_id=await snowflake.mint(),
                        attachment_ref=attachment_ref,
                        finding=finding,
                        uploader_ref=(
                            f"{uploader_ref[0]}@{uploader_ref[1]}"
                            if uploader_ref is not None
                            else None
                        ),
                        message_ref=(
                            f"{message_ref[0]}@{message_ref[1]}"
                            if message_ref is not None
                            else None
                        ),
                        detected_content_type=detected_type,
                        content_sha256=digest_builder.hexdigest(),
                        remote_variant=variant,
                    )
                    # A clean representation may be finishing in another API
                    # worker. Sharing this transaction lock with final cache
                    # admission ensures it either lands before this report and
                    # is retired, or sees the report and remains an orphan.
                    await acquire_remote_photodna_lock(
                        session,
                        origin_domain,
                        attachment_id,
                    )
                    await session.execute(
                        pg_insert(AbuseReport)
                        .values(**report_values)
                        .on_conflict_do_nothing(
                            index_elements=["source", "target_type", "target_ref"],
                            index_where=text("source = 'photodna'"),
                        )
                    )
                    await session.commit()
                    await increment_metric(redis, "media_photodna_matches")
                    await retire_remote_media_variants(
                        session,
                        origin_domain=origin_domain,
                        attachment_id=attachment_id,
                    )
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "REMOTE_MEDIA_REJECTED"},
                    )

        digest = digest_builder.hexdigest()
        cache_key = f"{origin_domain}/{attachment_id}/{variant}/{digest}"
        storage = S3Storage(settings)
        # Serialize reservations and eviction. Orphans remain charged until
        # their physical DELETE succeeds, so the ceiling reflects object-store
        # bytes rather than only currently referenced cache rows.
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended("kaede-remote-media-cache-budget", 0)
                )
            )
        )
        # Re-read the cache row only after taking the physical-budget lock.
        # A GC worker may have deleted a row (and scheduled its object for
        # deletion) while this fetch was downloading or waiting for the lock.
        # Using an identity-map value observed before the lock could otherwise
        # skip the PUT and recreate a row that points at the deleted object.
        existing = await session.get(
            RemoteMediaCache,
            (origin_domain, attachment_id, variant),
            populate_existing=True,
            with_for_update=True,
        )
        existing_key = existing.object_key if existing is not None else None
        existing_size = existing.size if existing is not None else None
        referenced_bytes = int(
            await session.scalar(select(func.coalesce(func.sum(RemoteMediaCache.size), 0))) or 0
        )
        orphan_bytes = int(
            await session.scalar(
                select(func.coalesce(func.sum(RemoteMediaOrphan.size), 0)).where(
                    ~exists(
                        select(RemoteMediaCache.object_key).where(
                            RemoteMediaCache.object_key == RemoteMediaOrphan.object_key
                        )
                    )
                )
            )
            or 0
        )
        reservation = await session.scalar(
            select(RemoteMediaOrphan)
            .where(RemoteMediaOrphan.object_key == cache_key)
            .with_for_update()
        )
        needs_new_object = cache_key != existing_key
        reservation_bytes = received if needs_new_object and reservation is None else 0
        if referenced_bytes + orphan_bytes + reservation_bytes > settings.media_remote_cache_bytes:
            await increment_metric(redis, "federation_remote_media_cache_quota_rejections")
            await enqueue_best_effort(media_cache_gc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "REMOTE_MEDIA_CACHE_FULL", "retry_after_ms": 1_000},
                headers={"Retry-After": "1"},
            )
        if needs_new_object:
            reservation_deadline = datetime.now(UTC) + timedelta(
                seconds=REMOTE_MEDIA_UPLOAD_RESERVATION_SECONDS
            )
            if reservation is None:
                session.add(
                    RemoteMediaOrphan(
                        object_key=cache_key,
                        size=received,
                        # The orphan row is first a crash-recovery reservation.
                        # Do not let the sweeper race the PUT/cache-row swap; a
                        # dead worker becomes collectible after this bounded lease.
                        next_retry_at=reservation_deadline,
                    )
                )
            else:
                if reservation.size != received:
                    raise RuntimeError("remote media reservation size changed")
                # A retry may have found an expired crash reservation. Refresh
                # it while holding both the budget advisory lock and row lock,
                # then commit before PUT so the sweeper cannot delete in flight.
                reservation.next_retry_at = reservation_deadline
                reservation.last_error = None
            # Commit the reservation before bytes are written. If this worker
            # dies during/after PUT, the deletion sweep still knows the exact
            # key and keeps its bytes inside the hard physical budget.
            await session.commit()
        try:
            if needs_new_object:
                await storage.put_file(
                    settings.media_remote_cache_bucket,
                    cache_key,
                    temporary_path,
                    size=received,
                    sha256=digest,
                    content_type=detected_type,
                )
            await session.scalar(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended("kaede-remote-media-cache-budget", 0)
                    )
                )
            )
            if (
                await session.get(
                    RemoteMediaTombstone,
                    (origin_domain, attachment_id),
                    populate_existing=True,
                )
                is not None
            ):
                raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
            if (
                await session.get(
                    MediaTombstoneSource,
                    (attachment_id, origin_domain),
                    populate_existing=True,
                )
                is not None
            ):
                raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
            await require_remote_media_binding_live(
                session,
                origin_domain=origin_domain,
                attachment_id=attachment_id,
                message_ref=message_ref,
                conversation_ref=(dm_history_scope[0] if dm_history_scope is not None else None),
                local_domain=settings.domain,
            )
            await acquire_remote_photodna_lock(session, origin_domain, attachment_id)
            await reject_known_photodna_match(
                session,
                origin_domain=origin_domain,
                attachment_id=attachment_id,
            )
            stored_at = datetime.now(UTC)
            expires_at = stored_at + timedelta(days=settings.media_remote_cache_ttl_days)
            if existing_key is not None and existing_key != cache_key:
                if existing_size is None:
                    raise RuntimeError("remote media cache entry changed during refresh")
                await session.execute(
                    pg_insert(RemoteMediaOrphan)
                    .values(object_key=existing_key, size=existing_size)
                    .on_conflict_do_nothing(index_elements=["object_key"])
                )
            await session.execute(
                pg_insert(RemoteMediaCache)
                .values(
                    origin_domain=origin_domain,
                    attachment_id=attachment_id,
                    variant=variant,
                    object_key=cache_key,
                    size=received,
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
                        "size": received,
                        "content_type": detected_type,
                        "content_sha256": digest,
                        "scan_status": "clean",
                        "last_accessed_at": stored_at,
                        "expires_at": expires_at,
                    },
                )
            )
            await session.execute(
                delete(RemoteMediaOrphan).where(RemoteMediaOrphan.object_key == cache_key)
            )
            await session.commit()
        except StorageError as exc:
            await session.rollback()
            await enqueue_best_effort(media_cache_gc)
            raise HTTPException(
                status_code=503,
                detail={"code": "MEDIA_STORAGE_UNAVAILABLE"},
            ) from exc
        except BaseException:
            await session.rollback()
            await enqueue_best_effort(media_cache_gc)
            raise

        if existing_key is not None and existing_key != cache_key:
            try:
                await storage.delete(settings.media_remote_cache_bucket, existing_key)
            except StorageError:
                pass
            else:
                await session.execute(
                    delete(RemoteMediaOrphan).where(RemoteMediaOrphan.object_key == existing_key)
                )
                await session.commit()
        await enqueue_best_effort(media_cache_gc)
        cached = await session.get(
            RemoteMediaCache,
            (origin_domain, attachment_id, variant),
            populate_existing=True,
        )
        if cached is None:
            raise RuntimeError("remote media cache write did not converge")
        return cached
    finally:
        with suppress(OSError):
            await anyio.Path(temporary_path).unlink()


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
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
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
        # Reprocessing holds Attachment FOR UPDATE while changing a formerly
        # clean image to a terminal PhotoDNA decision. Message deletion also
        # conflicts with the shared parent lock. Reload under both locks and
        # repeat authorization before minting so stale ORM state cannot race a
        # verdict or deletion commit.
        locked_attachment = await session.scalar(
            select(Attachment)
            .where(
                Attachment.id == int(attachment_id),
                Attachment.origin_domain == settings.domain,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if (
            locked_attachment is None
            or locked_attachment.deleted_at is not None
            or locked_attachment.message_id is None
            or locked_attachment.message_domain is None
        ):
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        if (
            await session.get(
                MediaTombstoneSource,
                (int(attachment_id), settings.domain),
                populate_existing=True,
            )
            is not None
        ):
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        locked_message = await session.scalar(
            select(Message)
            .where(
                Message.id == locked_attachment.message_id,
                Message.origin_domain == locked_attachment.message_domain,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if locked_message is None or locked_message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        locked_access = await load_channel_access(
            session,
            settings,
            auth.user,
            EntityReference(locked_message.channel_id, locked_message.channel_domain),
        )
        if locked_access.guild is not None:
            await require_permissions(
                session,
                redis,
                locked_access.guild,
                auth.user,
                required_permissions("message.list"),
                channel=locked_access.channel,
            )
        locked_attachment.updated_at = datetime.now(UTC)
        response = redirect_to_object(settings, locked_attachment, variant, public=False)
        await session.commit()
        return response

    remote_attachment_id = int(attachment_id)
    if (
        await session.get(RemoteMediaTombstone, (origin_domain, remote_attachment_id)) is not None
        or await session.get(
            MediaTombstoneSource,
            (remote_attachment_id, origin_domain),
        )
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    if (
        await session.get(
            RemoteMediaTombstone,
            (origin_domain, remote_attachment_id),
            populate_existing=True,
        )
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    await reject_known_photodna_match(
        session,
        origin_domain=origin_domain,
        attachment_id=remote_attachment_id,
    )
    cached = await session.get(RemoteMediaCache, (origin_domain, remote_attachment_id, variant))
    checked_at = datetime.now(UTC)
    if cached is not None and (cached.expires_at <= checked_at or cached.scan_status != "clean"):
        cached = None
    if cached is None:
        async with remote_media_cache_key_lock(
            redis,
            origin_domain,
            remote_attachment_id,
            variant,
        ):
            # The database transaction lock above is deliberately try-only;
            # the Redis lease persists across the durable object reservation
            # commit performed by cache_remote_media. Recheck after acquiring
            # the lease in case a prior worker filled the same cache key.
            cached = await session.get(
                RemoteMediaCache,
                (origin_domain, remote_attachment_id, variant),
                populate_existing=True,
            )
            checked_at = datetime.now(UTC)
            if cached is not None and (
                cached.expires_at <= checked_at or cached.scan_status != "clean"
            ):
                cached = None
            if cached is None:
                async with remote_media_fetch_admission(
                    redis,
                    response_status,
                    user_id=auth.user.id,
                    user_domain=auth.user.origin_domain,
                    origin_domain=origin_domain,
                    settings=settings,
                ):
                    cached = await cache_remote_media(
                        session,
                        settings,
                        redis=redis,
                        origin_domain=origin_domain,
                        attachment_id=remote_attachment_id,
                        variant=variant,
                        requester=(auth.user.id, auth.user.origin_domain),
                        encrypted_transport=attachment.encryption_mode == "e2ee",
                        snowflake=snowflake,
                        message_ref=(attachment.message_id, attachment.message_domain),
                        uploader_ref=(attachment.uploader_id, attachment.uploader_domain),
                    )
    if cached is None:
        raise RuntimeError("remote media cache write did not converge")
    # Recheck immediately before minting a capability so a match discovered
    # while this request was fetching another representation retires this row.
    # Holding the same transaction lock as the positive-decision writer makes
    # the order explicit: this capability either predates that decision, or
    # waits and observes it. It can never race past an in-flight report commit.
    cached = await final_remote_cache_for_capability(
        session,
        origin_domain=origin_domain,
        attachment_id=remote_attachment_id,
        variant=variant,
        local_domain=settings.domain,
        message_ref=(attachment.message_id, attachment.message_domain),
    )
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


@router.get("/api/v1/dms/{conversation_ref}/history-media/{message_ref}/{attachment_ref}/{variant}")
async def authorized_dm_history_media(
    conversation_ref: EntityRef,
    message_ref: EntityRef,
    attachment_ref: EntityRef,
    response_status: Response,
    expires: int = Query(ge=0),
    token: str = Query(min_length=40, max_length=48),
    variant: str = Path(pattern=r"^(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$"),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
) -> RedirectResponse:
    """Stream old authority-page media through the user's authenticated home."""

    conversation_key = conversation_ref.resolve(settings.domain)
    message_key = message_ref.resolve(settings.domain)
    attachment_key = attachment_ref.resolve(settings.domain)
    conversation = await session.get(DMConversation, conversation_key)
    capability_status = history_media_capability_status(
        settings,
        conversation_ref=conversation_key,
        message_ref=message_key,
        attachment_ref=attachment_key,
        variant=variant,
        expires=expires,
        token=token,
    )
    if (
        conversation is None
        or conversation.authority_domain == settings.domain
        or message_key[1] != attachment_key[1]
        or attachment_key[1] == settings.domain
        or capability_status == "invalid"
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    participant = await session.get(
        DMParticipant,
        (
            conversation.id,
            conversation.origin_domain,
            auth.user.id,
            auth.user.origin_domain,
        ),
    )
    if participant is None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})

    origin_domain = attachment_key[1]
    attachment_id = attachment_key[0]
    if await session.get(MediaTombstoneSource, (attachment_id, origin_domain)) is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    await reject_known_photodna_match(
        session,
        origin_domain=origin_domain,
        attachment_id=attachment_id,
    )
    try:
        authorization = await signed_request(
            session,
            settings,
            "GET",
            origin_domain,
            f"/_kaede/v1/media/{attachment_id}/{variant}/authorize",
            query={
                "conversation_id": str(conversation_key[0]),
                "conversation_domain": conversation_key[1],
                "message_id": str(message_key[0]),
                "message_domain": message_key[1],
                "requester_id": str(auth.user.id),
                "requester_domain": auth.user.origin_domain,
            },
            request_timeout=10,
            max_response_bytes=4_096,
        )
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
        ) from exc
    if authorization.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    if authorization.status_code != 204:
        raise HTTPException(status_code=503, detail={"code": "REMOTE_MEDIA_UNAVAILABLE"})
    if await session.get(RemoteMediaTombstone, (origin_domain, attachment_id)) is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    cached = await session.get(RemoteMediaCache, (origin_domain, attachment_id, variant))
    checked_at = datetime.now(UTC)
    if cached is not None and (cached.expires_at <= checked_at or cached.scan_status != "clean"):
        cached = None
    if cached is None:
        async with remote_media_cache_key_lock(redis, origin_domain, attachment_id, variant):
            cached = await session.get(
                RemoteMediaCache,
                (origin_domain, attachment_id, variant),
                populate_existing=True,
            )
            checked_at = datetime.now(UTC)
            if cached is not None and (
                cached.expires_at <= checked_at or cached.scan_status != "clean"
            ):
                cached = None
            if cached is None:
                async with remote_media_fetch_admission(
                    redis,
                    response_status,
                    user_id=auth.user.id,
                    user_domain=auth.user.origin_domain,
                    origin_domain=origin_domain,
                    settings=settings,
                ):
                    cached = await cache_remote_media(
                        session,
                        settings,
                        redis=redis,
                        origin_domain=origin_domain,
                        attachment_id=attachment_id,
                        variant=variant,
                        dm_history_scope=(conversation_key, message_key),
                        requester=(auth.user.id, auth.user.origin_domain),
                        encrypted_transport=(
                            authorization.headers.get("X-Kaede-Media-Encryption") == "e2ee"
                        ),
                        snowflake=snowflake,
                        message_ref=message_key,
                    )
    if cached is None:
        raise RuntimeError("remote history media cache write did not converge")
    cached = await final_remote_cache_for_capability(
        session,
        origin_domain=origin_domain,
        attachment_id=attachment_id,
        variant=variant,
        local_domain=settings.domain,
        message_ref=message_key,
        conversation_ref=conversation_key,
    )
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
    if capability_status == "renewable":
        # The caller may keep using the stale rendered URL: it is authenticated
        # and fully re-authorized above on every request. Content-Location also
        # exposes a newly short-lived, identically scoped path to clients that
        # choose to retain the renewal without adding another redirect hop.
        response.headers["Content-Location"] = history_media_path(
            settings,
            conversation_ref=conversation_key,
            message_ref=message_key,
            attachment_ref=attachment_key,
            variant=variant,
        )
    copy_rate_limit_headers(response_status, response)
    return response
