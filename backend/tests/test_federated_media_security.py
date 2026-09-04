from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import HTTPException

import app.api.media as media_api
import app.media.jobs as media_jobs
from app.db.models import RemoteMediaCache, RemoteMediaOrphan
from app.media.photodna import PhotoDNAFinding, PhotoDNAMatchFlag
from app.media.storage import StorageError


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class DirectAsyncFile:
    """Test-only async facade; production uses AnyIO's worker-backed file API."""

    def __init__(self, path: Path, mode: str) -> None:
        self.handle = path.open(mode)

    async def __aenter__(self) -> DirectAsyncFile:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.handle.close()

    async def write(self, data: bytes) -> None:
        self.handle.write(data)


class DirectAsyncPath:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    async def unlink(self) -> None:
        os.unlink(self.path)

    async def exists(self) -> bool:
        return self.path.exists()


async def direct_open_file(path: Path, mode: str) -> DirectAsyncFile:
    return DirectAsyncFile(path, mode)


def install_direct_file_io(monkeypatch: pytest.MonkeyPatch) -> None:
    # The managed test host does not schedule AnyIO worker-thread jobs; this
    # facade keeps the test deterministic while exercising the chunked code.
    monkeypatch.setattr(media_api.anyio, "open_file", direct_open_file)
    monkeypatch.setattr(media_api.anyio, "Path", DirectAsyncPath)
    # These cache-pipeline tests use narrow session doubles and exercise the
    # spool/scan/reservation/swap phases. Capability revalidation has focused
    # coverage above, so keep that independent database walk out of the doubles.
    monkeypatch.setattr(media_api, "require_remote_media_binding_live", AsyncMock())


class CacheSession:
    def __init__(
        self,
        cached: object | None,
        *,
        total: int = 0,
        reservation: RemoteMediaOrphan | None = None,
        photodna_report: int | None = None,
        photodna_reports: list[int | None] | None = None,
        cached_variants: list[RemoteMediaCache] | None = None,
    ) -> None:
        self.cached = cached
        self.total = total
        self.reservation = reservation
        self.photodna_report = photodna_report
        self.photodna_reports = list(photodna_reports or [])
        self.cached_variants = cached_variants or []
        self.cache_gets = 0
        self.commits = 0
        self.executed = False
        self.statements: list[object] = []
        self.added: list[object] = []
        self.deleted: list[object] = []

    async def get(self, model: object, _key: object, **_kwargs: object) -> object | None:
        if model is media_api.RemoteMediaTombstone:
            return None
        if model is media_api.MediaTombstoneSource:
            return None
        if model is media_api.RemoteMediaCache:
            self.cache_gets += 1
            return self.cached if self.cache_gets > 1 else None
        if model is media_api.RemoteMediaOrphan:
            return None
        raise AssertionError("unexpected cache model lookup")

    async def scalar(self, statement: object) -> object:
        if "FROM abuse_reports" in str(statement):
            if self.photodna_reports:
                return self.photodna_reports.pop(0)
            return self.photodna_report
        if "FROM remote_media_orphans" in str(statement) and "sum(" not in str(statement):
            return self.reservation
        return self.total if "sum(remote_media_cache.size)" in str(statement) else None

    async def execute(self, statement: object) -> None:
        self.executed = True
        self.statements.append(statement)

    async def scalars(self, statement: object) -> list[RemoteMediaCache]:
        if "FROM remote_media_cache" not in str(statement):
            raise AssertionError("unexpected scalar collection query")
        return self.cached_variants

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def rollback(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def media_settings(*, max_bytes: int = 2048, cache_bytes: int = 8192) -> SimpleNamespace:
    return SimpleNamespace(
        domain="alpha.localhost",
        media_max_attachment_bytes=max_bytes,
        media_remote_cache_bytes=cache_bytes,
        media_remote_cache_bucket="remote-cache",
        media_remote_cache_ttl_days=30,
        photodna_enabled=False,
    )


def test_live_group_dm_media_query_carries_requester_without_history_scope() -> None:
    assert media_api.remote_media_federation_query(
        None,
        (42, "gamma.localhost"),
    ) == {
        "requester_id": "42",
        "requester_domain": "gamma.localhost",
    }


def test_group_dm_history_media_query_binds_scope_and_requester() -> None:
    assert media_api.remote_media_federation_query(
        ((30, "authority.localhost"), (20, "beta.localhost")),
        (42, "gamma.localhost"),
    ) == {
        "conversation_id": "30",
        "conversation_domain": "authority.localhost",
        "message_id": "20",
        "message_domain": "beta.localhost",
        "requester_id": "42",
        "requester_domain": "gamma.localhost",
    }


@pytest.mark.asyncio
async def test_final_remote_capability_recheck_observes_concurrent_tombstone() -> None:
    tombstone = SimpleNamespace(event_id="kcfe_delete")

    class TombstonedSession:
        def __init__(self) -> None:
            self.cache_lookup = AsyncMock()
            self.scalar = AsyncMock(return_value=None)

        async def get(self, model: object, _key: object, **_kwargs: object) -> object | None:
            if model is media_api.RemoteMediaTombstone:
                assert _kwargs.get("populate_existing") is True
                return tombstone
            if model is media_api.MediaTombstoneSource:
                return None
            if model is media_api.RemoteMediaCache:
                await self.cache_lookup()
                return None
            raise AssertionError("unexpected model lookup")

    session = TombstonedSession()

    with pytest.raises(HTTPException) as raised:
        await media_api.final_remote_cache_for_capability(
            cast(Any, session),
            origin_domain="beta.localhost",
            attachment_id=8,
            variant="original",
            local_domain="alpha.localhost",
        )

    assert raised.value.status_code == 404
    assert cast(dict[str, object], raised.value.detail)["code"] == "MEDIA_NOT_FOUND"
    session.scalar.assert_awaited_once()
    session.cache_lookup.assert_not_awaited()


async def test_known_remote_photodna_match_is_rejected_without_refetch() -> None:
    session = CacheSession(None, photodna_report=900)
    configured = media_settings()

    with pytest.raises(HTTPException) as raised:
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, configured),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="thumbnail_128",
        )

    assert raised.value.status_code == 422
    assert cast(dict[str, object], raised.value.detail)["code"] == "REMOTE_MEDIA_REJECTED"
    assert session.commits == 0


async def test_remote_media_not_yet_published_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        404,
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    session = CacheSession(None)

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        yield response

    monkeypatch.setattr(media_api, "signed_stream_request", stream)

    with pytest.raises(HTTPException) as raised:
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="thumbnail_512",
        )

    assert raised.value.status_code == 503
    assert cast(dict[str, object], raised.value.detail)["code"] == "REMOTE_MEDIA_BUSY"
    assert raised.value.headers == {"Retry-After": "1"}


async def test_photodna_match_retires_every_previously_clean_cached_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thumbnail = SimpleNamespace(object_key="beta/7/thumbnail_128/hash", size=123)
    poster = SimpleNamespace(object_key="beta/7/poster/hash", size=456)
    session = CacheSession(
        None,
        photodna_report=900,
        cached_variants=cast(list[RemoteMediaCache], [thumbnail, poster]),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(media_api, "enqueue_best_effort", enqueue)

    with pytest.raises(HTTPException) as raised:
        await media_api.reject_known_photodna_match(
            cast(Any, session),
            origin_domain="beta.localhost",
            attachment_id=7,
        )

    assert raised.value.status_code == 422
    assert cast(dict[str, object], raised.value.detail)["code"] == "REMOTE_MEDIA_REJECTED"
    assert session.deleted == [thumbnail, poster]
    assert session.commits == 1
    assert len(session.statements) == 2
    assert all("remote_media_orphans" in str(statement) for statement in session.statements)
    enqueue.assert_awaited_once_with(media_api.media_cache_gc)


async def test_remote_media_is_spooled_scanned_and_uploaded_without_a_body_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\x89PNG\r\n\x1a\n" + (b"a" * 700)
    digest = hashlib.sha256(body).hexdigest()
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(body))},
        stream=ChunkStream([body[:17], body[17:513], body[513:]]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    captured_path: Path | None = None
    uploads: list[tuple[str, int, str, bytes]] = []
    cached = SimpleNamespace(object_key=f"beta.localhost/7/original/{digest}")
    session = CacheSession(cached)
    install_direct_file_io(monkeypatch)
    signed_query: dict[str, str] | None = None

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        nonlocal signed_query
        signed_query = cast(dict[str, str] | None, _kwargs.get("query"))
        try:
            yield response
        finally:
            await response.aclose()

    async def scan(path: Path, _settings: object) -> str:
        nonlocal captured_path
        captured_path = path
        assert await anyio.Path(path).read_bytes() == body
        return "clean"

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def put_file(
            self,
            _bucket: str,
            key: str,
            path: Path,
            *,
            size: int,
            sha256: str,
            content_type: str,
        ) -> None:
            uploads.append((key, size, content_type, await anyio.Path(path).read_bytes()))
            assert sha256 == digest

        async def delete(self, _bucket: str, _key: str) -> None:
            raise AssertionError("a new cache entry has no superseded object")

    async def enqueue(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", scan)
    monkeypatch.setattr(media_api, "S3Storage", Storage)
    monkeypatch.setattr(media_api, "enqueue_best_effort", enqueue)

    with anyio.fail_after(10):
        result = await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="original",
            dm_history_scope=(
                (30, "authority.localhost"),
                (20, "beta.localhost"),
            ),
        )

    assert result is cached
    assert session.executed is True
    assert session.commits == 2
    assert len(session.added) == 1
    assert isinstance(session.added[0], media_api.RemoteMediaOrphan)
    reservation = cast(RemoteMediaOrphan, session.added[0])
    assert reservation.next_retry_at > media_jobs.datetime.now(media_jobs.UTC)
    assert uploads == [(f"beta.localhost/7/original/{digest}", len(body), "image/png", body)]
    assert captured_path is not None
    assert not await anyio.Path(captured_path).exists()
    assert signed_query == {
        "conversation_id": "30",
        "conversation_domain": "authority.localhost",
        "message_id": "20",
        "message_domain": "beta.localhost",
    }


async def test_remote_photodna_match_is_reported_and_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\x89PNG\r\n\x1a\n" + (b"safe-test-payload" * 40)
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(body))},
        stream=ChunkStream([body]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    session = CacheSession(None)
    redis = object()
    install_direct_file_io(monkeypatch)

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        try:
            yield response
        finally:
            await response.aclose()

    async def scan(data: bytes, _settings: object) -> PhotoDNAFinding:
        assert data == body
        return PhotoDNAFinding(
            tracking_id="tracking",
            flags=(PhotoDNAMatchFlag("Test", ("A1",), 12, "2600000"),),
        )

    class Snowflake:
        async def mint(self) -> int:
            return 900

    class Storage:
        def __init__(self, _settings: object) -> None:
            raise AssertionError("a matched remote image must never reach object storage")

    increment = AsyncMock()
    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", AsyncMock(return_value="clean"))
    monkeypatch.setattr(media_api, "scan_image", scan)
    monkeypatch.setattr(media_api, "S3Storage", Storage)
    monkeypatch.setattr(media_api, "increment_metric", increment)

    with pytest.raises(HTTPException) as raised:
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            redis=cast(Any, redis),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="original",
            snowflake=cast(Any, Snowflake()),
            message_ref=(8, "beta.localhost"),
        )

    assert cast(dict[str, object], raised.value.detail)["code"] == "REMOTE_MEDIA_REJECTED"
    assert session.commits == 1
    assert len(session.statements) == 1
    statement = session.statements[0]
    assert "ON CONFLICT" in str(statement)
    assert statement.compile().params["source"] == "photodna"  # type: ignore[attr-defined]
    assert statement.compile().params["target_ref"] == "7@beta.localhost"  # type: ignore[attr-defined]
    increment.assert_awaited_once_with(redis, "media_photodna_matches")


async def test_positive_decision_racing_final_cache_admission_leaves_only_an_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\x89PNG\r\n\x1a\n" + (b"safe-test-payload" * 40)
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(body))},
        stream=ChunkStream([body]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    # The first lookup precedes the download. The second happens under the
    # attachment-scoped advisory lock after object upload and observes the
    # positive report committed by a concurrent variant scan.
    session = CacheSession(None, photodna_reports=[None, 900])
    install_direct_file_io(monkeypatch)
    uploads: list[str] = []

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        try:
            yield response
        finally:
            await response.aclose()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def put_file(
            self,
            _bucket: str,
            key: str,
            _path: Path,
            **_kwargs: object,
        ) -> None:
            uploads.append(key)

    enqueue = AsyncMock()
    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", AsyncMock(return_value="clean"))
    monkeypatch.setattr(media_api, "scan_image", AsyncMock(return_value=None))
    monkeypatch.setattr(media_api, "S3Storage", Storage)
    monkeypatch.setattr(media_api, "enqueue_best_effort", enqueue)

    with pytest.raises(HTTPException) as raised:
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="thumbnail_128",
        )

    assert raised.value.status_code == 422
    assert cast(dict[str, object], raised.value.detail)["code"] == "REMOTE_MEDIA_REJECTED"
    assert len(uploads) == 1
    assert len(session.added) == 1
    assert isinstance(session.added[0], RemoteMediaOrphan)
    assert session.statements == []
    enqueue.assert_awaited_with(media_api.media_cache_gc)


async def test_remote_media_cache_ceiling_emits_operator_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\x89PNG\r\n\x1a\n" + (b"a" * 700)
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(body))},
        stream=ChunkStream([body]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    session = CacheSession(None, total=8192)
    redis = object()
    increment = AsyncMock()
    enqueue = AsyncMock()
    install_direct_file_io(monkeypatch)

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        try:
            yield response
        finally:
            await response.aclose()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", AsyncMock(return_value="clean"))
    monkeypatch.setattr(media_api, "S3Storage", Storage)
    monkeypatch.setattr(media_api, "increment_metric", increment)
    monkeypatch.setattr(media_api, "enqueue_best_effort", enqueue)

    with pytest.raises(HTTPException) as raised:
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            redis=cast(Any, redis),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="original",
        )

    assert cast(dict[str, object], raised.value.detail)["code"] == "REMOTE_MEDIA_CACHE_FULL"
    increment.assert_awaited_once_with(
        redis,
        "federation_remote_media_cache_quota_rejections",
    )
    enqueue.assert_awaited_once_with(media_api.media_cache_gc)


async def test_retry_refreshes_an_expired_orphan_reservation_before_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\x89PNG\r\n\x1a\n" + (b"r" * 700)
    digest = hashlib.sha256(body).hexdigest()
    key = f"beta.localhost/7/original/{digest}"
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(body))},
        stream=ChunkStream([body]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    before = media_jobs.datetime.now(media_jobs.UTC)
    reservation = RemoteMediaOrphan(
        object_key=key,
        size=len(body),
        attempts=1,
        last_error="prior worker stopped",
        next_retry_at=before - media_jobs.timedelta(minutes=1),
        created_at=before - media_jobs.timedelta(minutes=10),
    )
    cached = SimpleNamespace(object_key=key)
    session = CacheSession(cached, reservation=reservation)
    put_file = AsyncMock()
    install_direct_file_io(monkeypatch)

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        try:
            yield response
        finally:
            await response.aclose()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def put_file(self, *args: object, **kwargs: object) -> None:
            await put_file(*args, **kwargs)

        async def delete(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", AsyncMock(return_value="clean"))
    monkeypatch.setattr(media_api, "S3Storage", Storage)
    monkeypatch.setattr(media_api, "enqueue_best_effort", AsyncMock())

    assert (
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="original",
        )
        is cached
    )
    assert reservation.next_retry_at > before
    assert reservation.last_error is None
    assert session.commits == 2
    assert session.added == []
    put_file.assert_awaited_once()


async def test_refresh_reloads_cache_after_gc_budget_lock_before_reusing_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\x89PNG\r\n\x1a\n" + (b"g" * 700)
    digest = hashlib.sha256(body).hexdigest()
    key = f"beta.localhost/7/original/{digest}"
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(body))},
        stream=ChunkStream([body]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    before = media_jobs.datetime.now(media_jobs.UTC)
    reservation = RemoteMediaOrphan(
        object_key=key,
        size=len(body),
        attempts=1,
        last_error="GC took ownership of the old cache object",
        next_retry_at=before - media_jobs.timedelta(minutes=1),
        created_at=before - media_jobs.timedelta(minutes=10),
    )
    stale = SimpleNamespace(object_key=key, size=len(body))
    refreshed = SimpleNamespace(object_key=key, size=len(body))

    class GcRaceSession:
        def __init__(self) -> None:
            self.budget_locked = False
            self.cache_gets = 0
            self.prelock_cache_gets = 0
            self.commits = 0

        async def get(self, model: object, _key: object, **_kwargs: object) -> object | None:
            if model is media_api.RemoteMediaTombstone:
                return None
            if model is media_api.MediaTombstoneSource:
                return None
            if model is media_api.RemoteMediaCache:
                self.cache_gets += 1
                if self.cache_gets == 1:
                    if not self.budget_locked:
                        self.prelock_cache_gets += 1
                        return stale
                    # GC deleted the cache row before this worker acquired the
                    # budget lock. The old identity-map value must not be used.
                    return None
                return refreshed
            raise AssertionError("unexpected model lookup")

        async def scalar(self, statement: object) -> object:
            rendered = str(statement)
            if "pg_advisory_xact_lock" in rendered:
                self.budget_locked = True
                return True
            if "FROM remote_media_orphans" in rendered and "sum(" not in rendered:
                return reservation
            if "sum(remote_media_cache.size)" in rendered:
                return 0
            return None

        async def execute(self, _statement: object) -> None:
            return None

        def add(self, _value: object) -> None:
            raise AssertionError("the existing crash reservation should be refreshed")

        async def commit(self) -> None:
            self.commits += 1
            self.budget_locked = False

        async def rollback(self) -> None:
            return None

    session = GcRaceSession()
    put_file = AsyncMock()
    install_direct_file_io(monkeypatch)

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        try:
            yield response
        finally:
            await response.aclose()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def put_file(self, *args: object, **kwargs: object) -> None:
            await put_file(*args, **kwargs)

        async def delete(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", AsyncMock(return_value="clean"))
    monkeypatch.setattr(media_api, "S3Storage", Storage)
    monkeypatch.setattr(media_api, "enqueue_best_effort", AsyncMock())

    assert (
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="original",
        )
        is refreshed
    )
    assert session.prelock_cache_gets == 0
    assert session.cache_gets == 2
    assert session.commits == 2
    assert reservation.next_retry_at > before
    assert reservation.last_error is None
    put_file.assert_awaited_once()


async def test_failed_cache_swap_never_deletes_the_still_referenced_old_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"\x89PNG\r\n\x1a\n" + (b"b" * 700)
    response = httpx.Response(
        200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(body))},
        stream=ChunkStream([body]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    existing = SimpleNamespace(object_key="beta.localhost/7/original/old", size=128)

    class SwapSession:
        def __init__(self) -> None:
            self.scalar_values = iter([True, 128, 0, None, True, True])
            self.execute_count = 0
            self.commits = 0
            self.rollbacks = 0

        async def get(self, model: object, _key: object, **_kwargs: object) -> object | None:
            if model is media_api.RemoteMediaTombstone:
                return None
            if model is media_api.MediaTombstoneSource:
                return None
            if model is media_api.RemoteMediaCache:
                return existing
            if model is media_api.RemoteMediaOrphan:
                return None
            raise AssertionError("unexpected model lookup")

        async def scalar(self, _statement: object) -> object:
            if "FROM abuse_reports" in str(_statement):
                return None
            return next(self.scalar_values)

        async def execute(self, _statement: object) -> None:
            self.execute_count += 1
            if self.execute_count == 2:
                raise RuntimeError("simulated cache row failure")

        def add(self, _value: object) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = SwapSession()
    storage_delete = AsyncMock()
    install_direct_file_io(monkeypatch)

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        try:
            yield response
        finally:
            await response.aclose()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def put_file(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def delete(self, bucket: str, key: str) -> None:
            await storage_delete(bucket, key)

    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", AsyncMock(return_value="clean"))
    monkeypatch.setattr(media_api, "S3Storage", Storage)
    monkeypatch.setattr(media_api, "enqueue_best_effort", AsyncMock())

    with pytest.raises(RuntimeError, match="cache row failure"):
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings()),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="original",
        )

    assert session.commits == 1  # only the durable new-object reservation
    assert session.rollbacks == 1
    storage_delete.assert_not_awaited()


async def test_remote_media_stream_without_length_is_stopped_at_the_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200,
        headers={"Content-Type": "application/octet-stream"},
        stream=ChunkStream([b"a" * 10, b"b" * 10]),
        request=httpx.Request("GET", "https://beta.localhost/media"),
    )
    session = CacheSession(None)
    install_direct_file_io(monkeypatch)

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.Response]:
        try:
            yield response
        finally:
            await response.aclose()

    async def no_scan(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("an oversized stream reached the malware scanner")

    monkeypatch.setattr(media_api, "signed_stream_request", stream)
    monkeypatch.setattr(media_api, "clamav_scan_file", no_scan)

    with pytest.raises(HTTPException) as raised:
        await media_api.cache_remote_media(
            cast(Any, session),
            cast(Any, media_settings(max_bytes=16)),
            origin_domain="beta.localhost",
            attachment_id=7,
            variant="original",
        )

    assert raised.value.status_code == 422
    assert cast(dict[str, object], raised.value.detail)["code"] == "REMOTE_MEDIA_REJECTED"
    assert session.executed is False
    assert session.commits == 0


async def test_failed_physical_orphan_delete_remains_durable_for_retry() -> None:
    now = media_jobs.datetime.now(media_jobs.UTC)
    orphan = RemoteMediaOrphan(
        object_key="beta.example/7/original/deadbeef",
        size=128,
        attempts=0,
        next_retry_at=now,
        created_at=now,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[orphan]),
        scalar=AsyncMock(return_value=False),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    storage = SimpleNamespace(delete=AsyncMock(side_effect=StorageError("offline")))

    removed = await media_jobs.drain_remote_media_orphans(
        cast(Any, session),
        cast(Any, media_settings()),
        storage=cast(Any, storage),
    )

    assert removed == 0
    assert orphan.attempts == 1
    assert orphan.last_error is not None
    assert orphan.next_retry_at > now
    session.delete.assert_not_awaited()
    session.commit.assert_awaited_once()


async def test_cache_gc_evicts_below_low_water_when_cache_is_at_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = media_jobs.datetime.now(media_jobs.UTC)
    cached = RemoteMediaCache(
        origin_domain="beta.example",
        attachment_id=7,
        variant="original",
        object_key="beta.example/7/original/hash",
        size=20,
        content_type="image/png",
        scan_status="clean",
        last_accessed_at=now,
        expires_at=now + media_jobs.timedelta(days=1),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[True, 100, 0]),
        scalars=AsyncMock(side_effect=[[], [cached]]),
        execute=AsyncMock(),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    drain = AsyncMock(return_value=0)
    monkeypatch.setattr(media_jobs, "drain_remote_media_orphans", drain)
    monkeypatch.setattr(media_jobs, "S3Storage", lambda _settings: SimpleNamespace())

    removed = await media_jobs.enforce_remote_cache_limit(
        cast(Any, session),
        cast(Any, media_settings(cache_bytes=100)),
    )

    assert removed == 1
    session.delete.assert_awaited_once_with(cached)
    session.commit.assert_awaited_once()
    assert drain.await_count == 2
