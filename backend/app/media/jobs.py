from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import (
    Attachment,
    RemoteMediaCache,
    RemoteMediaOrphan,
    RemoteMediaTombstone,
)
from app.media.processing import (
    IMAGE_PIPELINE_VERSION,
    IMAGE_TYPES,
    VIDEO_TYPES,
    Derivative,
    MediaValidationError,
    clamav_scan,
    content_digest,
    image_derivatives,
    sniff_content_type,
    validate_detected_type,
    video_poster,
)
from app.media.service import (
    clean_object_key,
    derived_object_key,
    discard_attachment,
    expired_pending_attachments,
)
from app.media.storage import S3Storage, StorageError

log = structlog.get_logger()


def image_derivatives_are_current(attachment: Attachment) -> bool:
    if attachment.detected_content_type not in IMAGE_TYPES:
        return True
    for name in ("thumbnail_128", "thumbnail_512", "thumbnail_1024"):
        raw = attachment.variants.get(name)
        if not isinstance(raw, dict) or raw.get("processing_version") != IMAGE_PIPELINE_VERSION:
            return False
    return True


async def process_attachment_record(
    session: AsyncSession, settings: Settings, attachment_id: int, origin_domain: str
) -> str:
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.id == attachment_id, Attachment.origin_domain == origin_domain)
        .with_for_update()
    )
    if attachment is None or attachment.deleted_at is not None:
        return "missing"
    if attachment.finalized_at is None:
        return "pending_upload"
    if attachment.encryption_mode == "e2ee":
        if attachment.scan_status == "encrypted":
            return "encrypted"
        storage = S3Storage(settings)
        staging_key = attachment.object_key
        try:
            data = await storage.get(
                settings.media_attachments_bucket,
                staging_key,
                max_bytes=settings.media_max_attachment_bytes,
            )
            if len(data) != attachment.size:
                raise MediaValidationError("stored ciphertext size changed after finalization")
            digest = content_digest(data)
            final_key = clean_object_key(origin_domain, attachment_id, digest)
            await storage.put(
                settings.media_attachments_bucket,
                final_key,
                data,
                "application/octet-stream",
            )
            attachment.content_sha256 = digest
            attachment.object_key = final_key
            attachment.staging_object_key = staging_key
            attachment.scan_status = "encrypted"
            await session.commit()
            try:
                await storage.delete(settings.media_attachments_bucket, staging_key)
            except StorageError:
                log.exception(
                    "encrypted_media_staging_delete_failed",
                    attachment_id=str(attachment_id),
                    staging_key=staging_key,
                )
            return "encrypted"
        except (MediaValidationError, StorageError, RuntimeError):
            attachment.scan_status = "failed"
            await session.commit()
            log.exception("encrypted_media_processing_failed", attachment_id=str(attachment_id))
            raise
    reprocessing = attachment.scan_status == "clean"
    if reprocessing and image_derivatives_are_current(attachment):
        return "clean"
    if attachment.scan_status == "infected":
        return attachment.scan_status
    storage = S3Storage(settings)
    try:
        staging_key = attachment.object_key
        data = await storage.get(
            settings.media_attachments_bucket,
            staging_key,
            max_bytes=settings.media_max_attachment_bytes,
        )
        if len(data) != attachment.size:
            raise MediaValidationError("stored object size changed after finalization")
        detected = sniff_content_type(data)
        validate_detected_type(attachment.content_type, detected)
        digest = content_digest(data)
        if reprocessing and attachment.content_sha256 != digest:
            raise MediaValidationError("clean media content digest changed")
        attachment.detected_content_type = detected
        attachment.content_sha256 = digest
        scan = "clean" if reprocessing else await clamav_scan(data, settings)
        if scan == "infected":
            attachment.scan_status = "infected"
            await storage.delete(settings.media_attachments_bucket, staging_key)
            await discard_attachment(session, settings, attachment)
            await session.commit()
            return "infected"
        derivatives: list[Derivative] = []
        if detected in IMAGE_TYPES:
            derivatives, attachment.blurhash, attachment.perceptual_hash, width, height = (
                image_derivatives(data)
            )
            attachment.width = width
            attachment.height = height
        elif detected in VIDEO_TYPES:
            derivatives = [await video_poster(data, detected)]
        rendered_variants: dict[str, object] = {}
        for derivative in derivatives:
            key = derived_object_key(origin_domain, attachment_id, derivative.name)
            await storage.put(
                settings.media_derived_bucket,
                key,
                derivative.content,
                derivative.content_type,
            )
            rendered_variants[derivative.name] = {
                "object_key": key,
                "content_type": derivative.content_type,
                "size": len(derivative.content),
                "width": derivative.width,
                "height": derivative.height,
                "processing_version": IMAGE_PIPELINE_VERSION,
            }
        if reprocessing:
            attachment.variants = rendered_variants
            attachment.scan_status = "clean"
            await session.commit()
            return "clean"
        # A presigned staging PUT cannot be revoked portably across Garage, S3,
        # and other compatible providers. Copy the exact in-memory bytes that
        # passed validation to a key the client never received, then atomically
        # point the database at that immutable object.
        final_key = clean_object_key(origin_domain, attachment_id, digest)
        await storage.put(
            settings.media_attachments_bucket,
            final_key,
            data,
            detected,
        )
        attachment.variants = rendered_variants
        attachment.object_key = final_key
        attachment.scan_status = "clean"
        await session.commit()
        try:
            await storage.delete(settings.media_attachments_bucket, staging_key)
        except StorageError:
            # The staging credential expires independently. A leaked staging
            # object is never served and the retention sweep can remove it.
            log.exception(
                "media_staging_delete_failed",
                attachment_id=str(attachment_id),
                staging_key=staging_key,
            )
        return "clean"
    except MediaValidationError:
        if reprocessing:
            await session.rollback()
            log.exception("media_reprocessing_failed", attachment_id=str(attachment_id))
            raise
        attachment.scan_status = "infected"
        try:
            await storage.delete(settings.media_attachments_bucket, attachment.object_key)
        except StorageError:
            log.exception("media_invalid_object_delete_failed", attachment_id=str(attachment_id))
        await discard_attachment(session, settings, attachment)
        await session.commit()
        return "infected"
    except (StorageError, RuntimeError):
        if reprocessing:
            await session.rollback()
            log.exception("media_reprocessing_failed", attachment_id=str(attachment_id))
            raise
        attachment.scan_status = "failed"
        await session.commit()
        log.exception("media_processing_failed", attachment_id=str(attachment_id))
        raise


async def sweep_orphan_uploads(
    session: AsyncSession, settings: Settings, *, limit: int = 100
) -> int:
    rows = await expired_pending_attachments(session, limit=limit)
    if not rows:
        return 0
    storage = S3Storage(settings)
    for attachment in rows:
        try:
            await storage.delete(settings.media_attachments_bucket, attachment.object_key)
        except StorageError:
            log.exception("media_orphan_delete_failed", attachment_id=str(attachment.id))
            continue
        await discard_attachment(session, settings, attachment)
    await session.commit()
    return len(rows)


async def sweep_staging_objects(
    session: AsyncSession, settings: Settings, *, limit: int = 100
) -> int:
    """Retry deletion of client-writable objects after immutable promotion."""

    rows = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.staging_object_key.is_not(None),
                Attachment.upload_expires_at <= func.now(),
                (Attachment.scan_status == "clean") | Attachment.deleted_at.is_not(None),
            )
            .order_by(Attachment.updated_at, Attachment.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not rows:
        return 0
    storage = S3Storage(settings)
    removed = 0
    for attachment in rows:
        staging_key = attachment.staging_object_key
        if staging_key is None:
            continue
        try:
            await storage.delete(settings.media_attachments_bucket, staging_key)
        except StorageError:
            log.exception(
                "media_staging_gc_failed",
                attachment_id=str(attachment.id),
                staging_key=staging_key,
            )
            continue
        attachment.staging_object_key = None
        removed += 1
    await session.commit()
    return removed


async def enforce_remote_cache_limit(session: AsyncSession, settings: Settings) -> int:
    storage = S3Storage(settings)
    await drain_remote_media_orphans(session, settings, storage=storage)
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    now = datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(RemoteMediaCache)
            .where(
                or_(
                    RemoteMediaCache.expires_at <= now,
                    exists().where(
                        RemoteMediaTombstone.origin_domain == RemoteMediaCache.origin_domain,
                        RemoteMediaTombstone.attachment_id == RemoteMediaCache.attachment_id,
                    ),
                )
            )
            .order_by(RemoteMediaCache.last_accessed_at)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
    )
    total = int(
        await session.scalar(select(func.coalesce(func.sum(RemoteMediaCache.size), 0))) or 0
    )
    orphan_total = int(
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
    total_after_expiry = total + orphan_total - sum(item.size for item in rows)
    # Admission never permits the cache to cross the hard ceiling. Evict to a
    # low-water mark so a cache already at the ceiling actually makes room for
    # the request that scheduled this sweep instead of waiting for TTL expiry.
    low_water_bytes = settings.media_remote_cache_bytes * 9 // 10
    if total_after_expiry > low_water_bytes:
        overflow = list(
            await session.scalars(
                select(RemoteMediaCache)
                .where(
                    RemoteMediaCache.expires_at > now,
                    ~exists().where(
                        RemoteMediaTombstone.origin_domain == RemoteMediaCache.origin_domain,
                        RemoteMediaTombstone.attachment_id == RemoteMediaCache.attachment_id,
                    ),
                )
                .order_by(RemoteMediaCache.last_accessed_at)
                .limit(500)
                .with_for_update(skip_locked=True)
            )
        )
        for item in overflow:
            if total_after_expiry <= low_water_bytes:
                break
            rows.append(item)
            total_after_expiry -= item.size
    unique = {(item.origin_domain, item.attachment_id, item.variant): item for item in rows}
    for item in unique.values():
        await session.execute(
            pg_insert(RemoteMediaOrphan)
            .values(object_key=item.object_key, size=item.size)
            .on_conflict_do_nothing(index_elements=["object_key"])
        )
        await session.delete(item)
    await session.commit()
    await drain_remote_media_orphans(session, settings, storage=storage)
    return len(unique)


async def drain_remote_media_orphans(
    session: AsyncSession,
    settings: Settings,
    *,
    storage: S3Storage | None = None,
    limit: int = 500,
) -> int:
    """Retry physical deletions recorded before cache references are removed."""

    if not 1 <= limit <= 5_000:
        raise ValueError("remote media orphan batch must be between 1 and 5000")
    object_storage = storage or S3Storage(settings)
    now = datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(RemoteMediaOrphan)
            .where(RemoteMediaOrphan.next_retry_at <= now)
            .order_by(RemoteMediaOrphan.next_retry_at, RemoteMediaOrphan.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    removed = 0
    for item in rows:
        referenced = bool(
            await session.scalar(
                select(exists().where(RemoteMediaCache.object_key == item.object_key))
            )
        )
        if referenced:
            await session.delete(item)
            continue
        try:
            await object_storage.delete(settings.media_remote_cache_bucket, item.object_key)
        except StorageError:
            item.attempts += 1
            item.last_error = "object deletion failed; Kaede will retry"
            item.next_retry_at = now + timedelta(
                seconds=min(3600, 5 * (2 ** min(item.attempts, 9)))
            )
            log.exception("remote_media_orphan_delete_failed", object_key=item.object_key)
            continue
        await session.delete(item)
        removed += 1
    await session.commit()
    return removed


async def purge_remote_attachment_cache(
    session: AsyncSession,
    settings: Settings,
    origin_domain: str,
    attachment_id: int,
) -> int:
    """Delete every cached variant after an authenticated origin tombstone."""

    rows = list(
        await session.scalars(
            select(RemoteMediaCache)
            .where(
                RemoteMediaCache.origin_domain == origin_domain,
                RemoteMediaCache.attachment_id == attachment_id,
            )
            .with_for_update(skip_locked=True)
        )
    )
    storage = S3Storage(settings)
    for item in rows:
        await session.execute(
            pg_insert(RemoteMediaOrphan)
            .values(object_key=item.object_key, size=item.size)
            .on_conflict_do_nothing(index_elements=["object_key"])
        )
        await session.delete(item)
    await session.commit()
    await drain_remote_media_orphans(session, settings, storage=storage)
    return len(rows)


async def purge_local_attachment(
    session: AsyncSession,
    settings: Settings,
    attachment_id: int,
    origin_domain: str,
) -> str:
    attachment = await session.scalar(
        select(Attachment)
        .where(
            Attachment.id == attachment_id,
            Attachment.origin_domain == origin_domain,
        )
        .with_for_update()
    )
    if attachment is None or attachment.deleted_at is not None:
        return "missing"
    storage = S3Storage(settings)
    await storage.delete(settings.media_attachments_bucket, attachment.object_key)
    for raw in attachment.variants.values():
        if isinstance(raw, dict) and isinstance(raw.get("object_key"), str):
            await storage.delete(settings.media_derived_bucket, raw["object_key"])
    await discard_attachment(session, settings, attachment)
    await session.commit()
    return "deleted"


async def retention_sweep(session: AsyncSession, settings: Settings) -> int:
    if settings.media_retention_days is None:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=settings.media_retention_days)
    rows = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.finalized_at < cutoff,
                Attachment.message_id.is_(None),
                Attachment.purpose == "attachment",
                Attachment.deleted_at.is_(None),
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    )
    storage = S3Storage(settings)
    removed = 0
    for attachment in rows:
        try:
            await storage.delete(settings.media_attachments_bucket, attachment.object_key)
            for raw in attachment.variants.values():
                if isinstance(raw, dict) and isinstance(raw.get("object_key"), str):
                    await storage.delete(settings.media_derived_bucket, raw["object_key"])
        except StorageError:
            continue
        await discard_attachment(session, settings, attachment)
        removed += 1
    await session.commit()
    return removed


async def process_attachment_with_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    attachment_id: int,
    origin_domain: str,
) -> str:
    async with sessionmaker() as session:
        return await process_attachment_record(session, settings, attachment_id, origin_domain)
