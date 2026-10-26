from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.sax.saxutils import escape

import httpx
import pytest

from app.core.settings import Settings
from app.db.models import Attachment
from app.media.storage import S3Storage, StorageError
from app.safety import removal
from app.safety.models import ChildSafetyCase


def settings():
    return Settings(
        _env_file=None,
        domain="home.test",
        environment="test",
        secret_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        dragonfly_url="redis://localhost:6379/15",
        media_storage_backend="s3",
        media_s3_endpoint="https://storage.test",
        media_public_base_url="https://storage.test",
        media_s3_access_key="test-access-key-123",
        media_s3_secret_key="test-secret-key-123",
    )


def transport(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def listing(versions=(), markers=(), *, truncated=False, next_key="", next_version=""):
    entries = "".join(
        f"<{kind}><Key>{escape(key)}</Key><VersionId>{escape(version)}</VersionId></{kind}>"
        for kind, items in (("Version", versions), ("DeleteMarker", markers))
        for key, version in items
    )
    return (
        '<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"{entries}<IsTruncated>{str(truncated).lower()}</IsTruncated>"
        f"<NextKeyMarker>{escape(next_key)}</NextKeyMarker>"
        f"<NextVersionIdMarker>{escape(next_version)}</NextVersionIdMarker></ListVersionsResult>"
    )


@pytest.mark.asyncio
async def test_version_purge_preserves_hiding_marker_until_all_data_versions_are_gone(monkeypatch):
    key = "ordinary/file & name"
    versions = ["old+/=", "new"]
    markers = []
    removed = []
    signatures = {}

    def handler(request):
        params = request.url.params
        if request.method == "HEAD":
            return httpx.Response(404 if markers or not versions else 200)
        if request.method == "GET":
            return httpx.Response(
                200,
                text=listing(
                    [(key, version) for version in versions] + [(key + "-neighbor", "keep")],
                    [(key, marker) for marker in markers],
                ),
            )
        version = params.get("versionId")
        if version is None:
            markers.append("hidden")
        else:
            signatures[version] = request.headers["Authorization"]
            assert version != "keep"
            if version == "hidden":
                assert not versions
                markers.remove(version)
            else:
                versions.remove(version)
            removed.append(version)
        return httpx.Response(204)

    transport(monkeypatch, handler)
    storage = S3Storage(settings())
    assert not await storage.purge_verified("media", key)
    assert markers == ["hidden"] and not versions
    assert not await storage.purge_verified("media", key)
    assert await storage.purge_verified("media", key)
    assert removed == ["old+/=", "new", "hidden"]
    assert len(set(signatures.values())) == 3  # versionId is included in the signature.


@pytest.mark.asyncio
async def test_paginated_versions_are_exact_key_scoped(monkeypatch):
    deleted = []
    queries = []

    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "DELETE":
            deleted.append(request.url.params["versionId"])
            return httpx.Response(204)
        queries.append(dict(request.url.params))
        return httpx.Response(
            200,
            text=(
                listing(
                    markers=[("file", "hide")], truncated=True, next_key="file", next_version="hide"
                )
                if "key-marker" not in request.url.params
                else listing(versions=[("file", "old"), ("file-neighbor", "keep")])
            ),
        )

    transport(monkeypatch, handler)
    assert not await S3Storage(settings()).purge_verified("media", "file")
    assert queries[1]["key-marker"] == "file"
    assert queries[1]["version-id-marker"] == "hide"
    assert deleted == ["old"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["head-denied", "list-denied", "unsupported", "malformed", "locked"]
)
async def test_storage_uncertainty_never_verifies_removal(monkeypatch, failure):
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(403 if failure == "head-denied" else 404)
        if request.method == "DELETE":
            return httpx.Response(403)
        if failure == "locked":
            return httpx.Response(200, text=listing(versions=[("file", "locked")]))
        return httpx.Response(
            {"list-denied": 403, "unsupported": 501}.get(failure, 200), text="bad XML"
        )

    transport(monkeypatch, handler)
    with pytest.raises(StorageError):
        await S3Storage(settings()).purge_verified("media", "file")


@pytest.mark.asyncio
async def test_evidence_prefix_is_never_deleted_and_garage_requires_verified_absence(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(404)

    transport(monkeypatch, handler)
    storage = S3Storage(settings())
    with pytest.raises(StorageError):
        await storage.purge_verified("media", "evidence/case/file")
    assert calls == []
    storage.storage_backend = "garage"
    assert await storage.purge_verified("media", "file")
    assert calls == ["HEAD", "HEAD"]


@pytest.mark.asyncio
async def test_durable_receipts_retry_failed_objects_and_wait_out_uploads(monkeypatch):
    config = settings()
    now = datetime.now(UTC)
    case = ChildSafetyCase(id=1, status="confirmed", removal_objects=[])
    attachment = Attachment(
        origin_domain=config.domain,
        object_key="original",
        staging_object_key="upload",
        variants={"thumbnail": {"object_key": "thumb"}},
        upload_expires_at=now + timedelta(hours=1),
    )
    removal.remember_local_objects(config, case, attachment)
    attachment.variants = {}  # The normal cleanup job can forget the old rows/keys.
    attachment.staging_object_key = None
    assert {item["key"] for item in case.removal_objects} == {"original", "upload", "thumb"}
    purge = AsyncMock(return_value=True)
    monkeypatch.setattr(removal, "S3Storage", lambda _: SimpleNamespace(purge_verified=purge))
    await removal.remove_case_objects(config, case)
    assert case.removal_completed_at is None
    assert all(not item.get("verified_at") for item in case.removal_objects)
    for item in case.removal_objects:
        item["not_before"] = (now - timedelta(seconds=1)).isoformat()
    purge.side_effect = [StorageError("outage"), True, True]
    await removal.remove_case_objects(config, case)
    assert case.removal_completed_at is None
    assert sum(bool(item.get("verified_at")) for item in case.removal_objects) == 2
    purge.reset_mock(side_effect=True)
    await removal.remove_case_objects(config, case)
    assert purge.await_count == 1
    assert case.removal_completed_at is not None


@pytest.mark.asyncio
async def test_pending_allegations_never_trigger_deletion(monkeypatch):
    storage = AsyncMock()
    monkeypatch.setattr(removal, "S3Storage", storage)
    await removal.remove_case_objects(settings(), ChildSafetyCase(status="pending"))
    storage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        b'<!DOCTYPE a [<!ENTITY x "expanded">]><a>&x;</a>',
        "<!DOCTYPE a><a/>".encode("utf-16"),
        listing(versions=[("", "unknown-key")]).encode(),
        listing(versions=[("file", "")]).encode(),
    ],
)
async def test_unsafe_or_incomplete_version_listing_fails_closed(monkeypatch, document):
    def handler(request):
        return (
            httpx.Response(404)
            if request.method == "HEAD"
            else httpx.Response(200, content=document)
        )

    transport(monkeypatch, handler)
    with pytest.raises(StorageError):
        await S3Storage(settings()).purge_verified("media", "file")
