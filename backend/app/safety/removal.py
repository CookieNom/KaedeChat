"""Durable deletion receipts, independent of expiring attachment/cache rows."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import Attachment, RemoteMediaCache
from app.media.storage import S3Storage, StorageError
from app.safety.models import ChildSafetyCase

log = logging.getLogger(__name__)


def remember_local_objects(
    settings: Settings, case: ChildSafetyCase, attachment: Attachment
) -> None:
    if attachment.origin_domain != settings.domain:
        return
    objects = {(settings.media_attachments_bucket, attachment.object_key)}
    if attachment.staging_object_key:
        objects.add((settings.media_attachments_bucket, attachment.staging_object_key))
    for variant in (attachment.variants or {}).values():
        if isinstance(variant, dict) and isinstance(variant.get("object_key"), str):
            objects.add((settings.media_derived_bucket, variant["object_key"]))
    remember_objects(case, objects, attachment.upload_expires_at)


def remember_objects(
    case: ChildSafetyCase, objects: set[tuple[str, str]], upload_expires_at: datetime | None = None
) -> None:
    from app.media.jobs import STAGING_UPLOAD_COMPLETION_GRACE_SECONDS

    # Previously issued PUTs and in-flight workers must finish before the final check.
    not_before = max(datetime.now(UTC), upload_expires_at or datetime.now(UTC)) + timedelta(
        seconds=STAGING_UPLOAD_COMPLETION_GRACE_SECONDS
    )
    receipts = {(item["bucket"], item["key"]): dict(item) for item in case.removal_objects or []}
    for bucket, key in objects:
        if not key or key.startswith("evidence/"):
            raise StorageError("unsafe media removal key")
        receipts[(bucket, key)] = {
            "bucket": bucket,
            "key": key,
            "not_before": not_before.isoformat(),
        }
    case.removal_objects = list(receipts.values())
    case.removal_completed_at = None


async def remember_attachment_objects(
    session: AsyncSession, settings: Settings, case: ChildSafetyCase, attachment: Attachment
) -> None:
    if attachment.origin_domain == settings.domain:
        remember_local_objects(settings, case, attachment)
        return
    keys = await session.scalars(
        select(RemoteMediaCache.object_key).where(
            RemoteMediaCache.origin_domain == attachment.origin_domain,
            RemoteMediaCache.attachment_id == attachment.id,
        )
    )
    remember_objects(case, {(settings.media_remote_cache_bucket, key) for key in keys})


async def remove_case_objects(
    settings: Settings, case: ChildSafetyCase, *, deadline: float | None = None
) -> None:
    if case.status != "confirmed" or case.removal_completed_at is not None:
        return
    deadline = deadline if deadline is not None else monotonic() + 45
    now = datetime.now(UTC)
    storage = S3Storage(settings)
    receipts: list[dict[str, Any]] = [dict(item) for item in case.removal_objects or []]
    allowed_buckets = {
        settings.media_attachments_bucket,
        settings.media_derived_bucket,
        settings.media_remote_cache_bucket,
    }
    attempted = 0
    # Oldest attempts first so one unavailable object cannot starve a large case.
    for item in sorted(receipts, key=lambda entry: entry.get("checked_at", "")):
        if monotonic() >= deadline:
            break
        if item.get("verified_at") or attempted >= 20:
            continue
        attempted += 1
        item["checked_at"] = now.isoformat()
        try:
            if item["bucket"] not in allowed_buckets:
                raise StorageError("unknown media removal bucket")
            async with asyncio.timeout(min(15, max(0.1, deadline - monotonic()))):
                verified = await storage.purge_verified(item["bucket"], item["key"])
            if verified and now >= datetime.fromisoformat(item["not_before"]):
                item["verified_at"] = now.isoformat()
        except (StorageError, TimeoutError):
            log.warning("Safety media removal pending for case %s", case.id)
    case.removal_objects = receipts
    case.removal_checked_at = now
    if all(item.get("verified_at") for item in receipts):
        case.removal_completed_at = now


async def retry_case_removals(session: AsyncSession, settings: Settings) -> int:
    cases = list(
        await session.scalars(
            select(ChildSafetyCase)
            .where(
                ChildSafetyCase.status == "confirmed",
                ChildSafetyCase.removal_completed_at.is_(None),
            )
            .order_by(ChildSafetyCase.removal_checked_at.asc().nulls_first(), ChildSafetyCase.id)
            .with_for_update(skip_locked=True)
            .limit(10)
        )
    )
    deadline = monotonic() + 45
    attempted = 0
    for case in cases:
        if monotonic() >= deadline:
            break
        await remove_case_objects(settings, case, deadline=deadline)
        attempted += 1
    return attempted
