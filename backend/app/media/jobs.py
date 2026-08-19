from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.bot_models import AbuseReport
from app.db.models import (
    Attachment,
    RemoteMediaCache,
    RemoteMediaOrphan,
    RemoteMediaTombstone,
)
from app.media.digest_revocation import (
    DIGEST_REVOCATION_STATUSES,
    try_lock_asset_digest,
    valid_content_digest,
)
from app.media.photodna import (
    PhotoDNAFinding,
    PhotoDNAInputRejected,
    photodna_report,
    scan_image,
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
    original_object_key,
)
from app.media.storage import S3Storage, StorageError

log = structlog.get_logger()

# Official clients bound one upload attempt to at most 15 minutes (web), five
# minutes (desktop), or three minutes (mobile). Wait strictly longer than the
# largest bound so a PUT accepted just before presign expiry cannot complete
# after the staging marker is cleared at the timeout boundary.
STAGING_UPLOAD_COMPLETION_GRACE_SECONDS = 16 * 60


class TerminalCommitPreparationError(Exception):
    """Prevent a failed terminal outbox write from becoming a media retry state."""


async def delete_terminal_attachment_objects(
    storage: S3Storage, settings: Settings, attachment: Attachment
) -> bool:
    """Best-effort deletion used immediately and by the durable staging sweep.

    The terminal decision is committed before this runs, so none of these keys
    can be served even if object storage is temporarily unavailable. Keeping
    ``staging_object_key`` populated makes a failed deletion retryable.
    """

    objects: set[tuple[str, str]] = {(settings.media_attachments_bucket, attachment.object_key)}
    if attachment.staging_object_key is not None:
        objects.add((settings.media_attachments_bucket, attachment.staging_object_key))
    for raw in attachment.variants.values():
        if isinstance(raw, dict) and isinstance(raw.get("object_key"), str):
            objects.add((settings.media_derived_bucket, raw["object_key"]))

    complete = True
    for bucket, key in objects:
        try:
            await storage.delete(bucket, key)
        except StorageError:
            complete = False
            log.exception(
                "media_terminal_object_delete_failed",
                attachment_id=str(attachment.id),
                scan_status=attachment.scan_status,
                bucket=bucket,
                object_key=key,
            )
    return complete


def attachment_photodna_report(attachment: Attachment, finding: PhotoDNAFinding) -> AbuseReport:
    """Create a retry-stable metadata-only report for a local upload."""

    if attachment.detected_content_type is None or attachment.content_sha256 is None:
        raise RuntimeError("PhotoDNA report requires finalized media metadata")
    # Attachment IDs are minted by the same instance-wide snowflake lease as
    # user-created report IDs. Reusing one is retry-stable and collision-free.
    return photodna_report(
        report_id=attachment.id,
        attachment_ref=f"{attachment.id}@{attachment.origin_domain}",
        finding=finding,
        uploader_ref=f"{attachment.uploader_id}@{attachment.uploader_domain}",
        message_ref=(
            f"{attachment.message_id}@{attachment.message_domain}"
            if attachment.message_id is not None and attachment.message_domain is not None
            else None
        ),
        purpose=attachment.purpose,
        detected_content_type=attachment.detected_content_type,
        content_sha256=attachment.content_sha256,
    )


def image_derivatives_are_current(attachment: Attachment) -> bool:
    if attachment.detected_content_type not in IMAGE_TYPES:
        return True
    for name in ("thumbnail_128", "thumbnail_512", "thumbnail_1024"):
        raw = attachment.variants.get(name)
        if not isinstance(raw, dict) or raw.get("processing_version") != IMAGE_PIPELINE_VERSION:
            return False
    return True


def staging_upload_grace_elapsed(
    upload_expires_at: datetime | None,
    *,
    now: datetime,
) -> bool:
    if upload_expires_at is None:
        return True
    return upload_expires_at + timedelta(seconds=STAGING_UPLOAD_COMPLETION_GRACE_SECONDS) < now


async def process_attachment_record(
    session: AsyncSession,
    settings: Settings,
    attachment_id: int,
    origin_domain: str,
    *,
    before_terminal_commit: Callable[[Attachment], Awaitable[None]] | None = None,
) -> str:
    async def prepare_terminal_commit(attachment: Attachment) -> None:
        # Serialize the terminal transition with bind-time evidence checks and
        # digest-proof cleanup. This is needed even when a caller has no
        # projection callback: the terminal row itself is digest revocation
        # evidence observed by future asset binds.
        if valid_content_digest(attachment.content_sha256) and not await try_lock_asset_digest(
            session,
            attachment.content_sha256,
        ):
            # This path already owns the Attachment row. Never wait on a bind
            # that owns digest and may be waiting to replace this same row.
            raise TerminalCommitPreparationError("terminal digest fence is busy")
        if before_terminal_commit is None:
            return
        try:
            await before_terminal_commit(attachment)
        except Exception as exc:
            # The caller's outbox work belongs to the verdict transaction. Do
            # not let the ordinary media RuntimeError handler commit a partial
            # deleted/failed row when that preparation aborts.
            raise TerminalCommitPreparationError from exc

    async def reject_retained_terminal_digest(
        attachment: Attachment,
        storage: S3Storage,
    ) -> bool:
        """Fence a bind-capable clean transition against retained evidence."""

        digest = attachment.content_sha256
        if attachment.purpose == "attachment":
            return False
        if not valid_content_digest(digest):
            raise TerminalCommitPreparationError("clean public asset has no valid digest")
        # This lifecycle path already owns the Attachment row. A capability or
        # repair owns digest before taking Attachment, so waiting would form
        # the inverse cycle; rollback and let the media task retry instead.
        if not await try_lock_asset_digest(session, digest):
            raise TerminalCommitPreparationError("clean asset digest fence is busy")
        terminal_evidence = await session.scalar(
            select(Attachment.id)
            .where(
                Attachment.origin_domain == settings.domain,
                Attachment.content_sha256 == digest,
                Attachment.scan_status.in_(DIGEST_REVOCATION_STATUSES),
            )
            .limit(1)
        )
        if terminal_evidence is None:
            return False
        # The bytes passed their independent scan, so do not copy or disclose
        # the original moderation category. A neutral terminal state is the
        # durable propagation marker for the same local digest.
        attachment.scan_status = "rejected"
        if attachment.staging_object_key is None:
            attachment.staging_object_key = attachment.object_key
        await discard_attachment(session, settings, attachment)
        await prepare_terminal_commit(attachment)
        await session.commit()
        await delete_terminal_attachment_objects(storage, settings, attachment)
        return True

    # All media lifecycle paths use the advisory ref fence before taking an
    # Attachment row lock.  Metadata disclosure takes the same fence before
    # its recipient writes, so a verdict can never deadlock as row -> media
    # while disclosure is waiting media -> FK KEY SHARE.
    from app.media.tombstones import lock_media_tombstone_ref

    await lock_media_tombstone_ref(session, attachment_id, origin_domain)
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.id == attachment_id, Attachment.origin_domain == origin_domain)
        .with_for_update()
    )
    if attachment is None:
        return "missing"
    if attachment.deleted_at is not None:
        if (
            attachment.scan_status in {"infected", "quarantined", "rejected"}
            and before_terminal_commit is not None
        ):
            await prepare_terminal_commit(attachment)
            await session.commit()
            return attachment.scan_status
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
    if attachment.scan_status in {"infected", "quarantined", "rejected"}:
        if before_terminal_commit is not None:
            await prepare_terminal_commit(attachment)
            await session.commit()
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
            if attachment.staging_object_key is None:
                attachment.staging_object_key = attachment.object_key
            await discard_attachment(session, settings, attachment)
            await prepare_terminal_commit(attachment)
            await session.commit()
            await delete_terminal_attachment_objects(storage, settings, attachment)
            return "infected"
        if detected in IMAGE_TYPES:
            try:
                finding = await scan_image(data, settings)
            except PhotoDNAInputRejected:
                # The configured PhotoDNA decoder cannot safely inspect this
                # image. This is a terminal, fail-closed policy decision, not a
                # malware verdict, a positive PhotoDNA match, or an outage to
                # retry. The durable marker also prevents legacy reprocessing
                # from selecting the same deterministic failure forever.
                attachment.scan_status = "rejected"
                if attachment.staging_object_key is None:
                    attachment.staging_object_key = attachment.object_key
                await discard_attachment(session, settings, attachment)
                await prepare_terminal_commit(attachment)
                await session.commit()
                await delete_terminal_attachment_objects(storage, settings, attachment)
                return "rejected"
            if finding is not None:
                attachment.scan_status = "quarantined"
                # This nullable key doubles as the existing durable physical
                # deletion queue. Reprocessing may discover a match after the
                # original staging key has already been swept.
                if attachment.staging_object_key is None:
                    attachment.staging_object_key = attachment.object_key
                await discard_attachment(session, settings, attachment)
                if await session.get(AbuseReport, attachment.id) is None:
                    session.add(attachment_photodna_report(attachment, finding))
                await prepare_terminal_commit(attachment)
                # Commit the durable quarantine and metadata-only report before
                # deleting storage. A failed object deletion cannot republish
                # the staging key, and the normal staging sweep will retry it.
                await session.commit()
                # Keep the durable marker until the upload capability expires.
                # A client may rewrite its staging key after this immediate
                # deletion; the scheduled sweep performs the authoritative
                # post-expiry delete and only then clears the marker.
                await delete_terminal_attachment_objects(storage, settings, attachment)
                return "quarantined"
        # A bind-capable clean publication must be ordered against terminal
        # proof cleanup before any derivative/final-object work. When no proof
        # exists, retain the transaction-scoped digest fence through the clean
        # commit so cleanup cannot remove the last proof between check/commit.
        if await reject_retained_terminal_digest(attachment, storage):
            return "rejected"
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
        # Size, type, decoder, and digest validation failures are not malware
        # detections. Fail closed with the same neutral terminal policy used
        # for unscannable PhotoDNA input; reserve ``infected`` exclusively for
        # an affirmative ClamAV result.
        attachment.scan_status = "rejected"
        if attachment.staging_object_key is None:
            attachment.staging_object_key = attachment.object_key
        await discard_attachment(session, settings, attachment)
        await prepare_terminal_commit(attachment)
        await session.commit()
        await delete_terminal_attachment_objects(storage, settings, attachment)
        return "rejected"
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
        if attachment.staging_object_key is None:
            attachment.staging_object_key = original_object_key(
                attachment.origin_domain,
                attachment.id,
            )
        try:
            await storage.delete(settings.media_attachments_bucket, attachment.object_key)
        except StorageError:
            log.exception("media_orphan_delete_failed", attachment_id=str(attachment.id))
            # Commit expired/deleted state and quota release even when storage
            # is unavailable. The deterministic marker moves physical retry to
            # the fair staging sweep instead of pinning the pending prefix.
        await discard_attachment(session, settings, attachment)
    await session.commit()
    return len(rows)


async def sweep_staging_objects(
    session: AsyncSession, settings: Settings, *, limit: int = 100
) -> int:
    """Delete client-writable objects after expiry plus completion grace."""

    rows = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.staging_object_key.is_not(None),
                or_(
                    Attachment.upload_expires_at.is_(None),
                    Attachment.upload_expires_at
                    < func.now() - timedelta(seconds=STAGING_UPLOAD_COMPLETION_GRACE_SECONDS),
                ),
                Attachment.scan_status.in_(("clean", "encrypted"))
                | Attachment.deleted_at.is_not(None),
            )
            .order_by(Attachment.updated_at, Attachment.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not rows:
        return 0
    storage = S3Storage(settings)
    swept_at = datetime.now(UTC)
    removed = 0
    for attachment in rows:
        # Recheck the strict boundary after the row lock; a test fake or a
        # concurrently repaired timestamp must not clear the marker at grace
        # equality.
        if not staging_upload_grace_elapsed(attachment.upload_expires_at, now=swept_at):
            continue
        if attachment.deleted_at is not None or attachment.scan_status in {
            "infected",
            "quarantined",
            "rejected",
        }:
            if not await delete_terminal_attachment_objects(storage, settings, attachment):
                attachment.updated_at = swept_at
                continue
            attachment.variants = {}
            if attachment.finalized_at is None and attachment.deleted_at is not None:
                await session.delete(attachment)
            else:
                attachment.staging_object_key = None
            removed += 1
            continue
        staging_key = attachment.staging_object_key
        if staging_key is None:
            continue
        try:
            await storage.delete(settings.media_attachments_bucket, staging_key)
        except StorageError:
            attachment.updated_at = swept_at
            log.exception(
                "media_staging_gc_failed",
                attachment_id=str(attachment.id),
                staging_key=staging_key,
            )
            continue
        if attachment.finalized_at is None and attachment.deleted_at is not None:
            # Expired tickets have no durable message/source lifecycle. Once
            # their sole staging object is gone, remove the quota-released row
            # instead of retaining an unreachable database tombstone forever.
            await session.delete(attachment)
        else:
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
    if attachment is None:
        return "missing"
    already_deleted = attachment.deleted_at is not None
    storage = S3Storage(settings)
    now = datetime.now(UTC)
    if attachment.staging_object_key is None and attachment.upload_expires_at is not None:
        # Recover the deterministic key even for rows processed by an older
        # release that cleared it after one deletion.
        attachment.staging_object_key = original_object_key(origin_domain, attachment_id)
    objects: set[tuple[str, str]] = {(settings.media_attachments_bucket, attachment.object_key)}
    if attachment.staging_object_key is not None:
        objects.add((settings.media_attachments_bucket, attachment.staging_object_key))
    for raw in attachment.variants.values():
        if isinstance(raw, dict) and isinstance(raw.get("object_key"), str):
            objects.add((settings.media_derived_bucket, raw["object_key"]))
    for bucket, key in sorted(objects):
        await storage.delete(bucket, key)
    if not already_deleted:
        await discard_attachment(session, settings, attachment)
    # Clear only strictly after the fixed post-expiry completion grace. At the
    # equality boundary an official client's maximum-duration request can
    # still be completing, so the scheduled staging sweep must retry later.
    if staging_upload_grace_elapsed(attachment.upload_expires_at, now=now):
        attachment.staging_object_key = None
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
