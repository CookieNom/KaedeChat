from __future__ import annotations

import asyncio
import time

from app.core.settings import StorageSettings
from app.media.storage import S3Storage, StorageError


async def initialize() -> None:
    settings = StorageSettings()
    settings.validate_storage_transport()
    # Initialization only makes server-side requests to the storage endpoint.
    settings.media_public_base_url = settings.media_s3_endpoint
    storage = S3Storage(settings)
    buckets = (
        settings.media_attachments_bucket,
        settings.media_derived_bucket,
        settings.media_remote_cache_bucket,
    )
    deadline = time.monotonic() + settings.media_s3_init_timeout_seconds
    delay = 0.5
    while True:
        try:
            for bucket in buckets:
                await storage.ensure_bucket(
                    bucket,
                    create_if_missing=bool(settings.media_s3_create_buckets),
                )
            return
        except StorageError as exc:
            if not exc.retryable or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)


if __name__ == "__main__":
    asyncio.run(initialize())
