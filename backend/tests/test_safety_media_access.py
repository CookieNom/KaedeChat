from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from app.db.models import Attachment, Message, RemoteMediaCache, RemoteMediaTombstone
from app.safety.media_access import media_access, object_readable, public_object


def config():
    return SimpleNamespace(
        domain="home.test",
        proxy_secret=SecretStr("internal-test-hop"),
        media_attachments_bucket="attachments",
        media_derived_bucket="derived",
        media_remote_cache_bucket="remote",
    )


def attachment(**changes):
    return SimpleNamespace(
        **{
            "id": 1,
            "origin_domain": "home.test",
            "deleted_at": None,
            "scan_status": "clean",
            "message_id": None,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "path",
    [
        "/attachments/evidence/42/secret",
        "/attachments/%65vidence/42/secret",
        "/attachments/scanning/home.test/1/hash",
        "/attachments/reviewed/home.test/1/hash",
        "/attachments/home.test/1/staging/original",
        "/attachments/home.test/1/../original",
        "/attachments/home.test/1/%252e%252e/original",
        "/unknown/home.test/1/original",
        "/attachments/home.test/01/original",
        "/attachments/home.test/1/original?X-Amz-Signature=secret",
    ],
)
def test_private_or_ambiguous_object_paths_never_pass(path):
    with pytest.raises(ValueError):
        public_object(path, config())


@pytest.mark.asyncio
async def test_already_issued_read_is_rechecked_after_review_without_object_deletion():
    session = AsyncMock()
    row = attachment()
    session.scalars.return_value = [row]
    session.scalar.return_value = None
    target = public_object("/attachments/home.test/1/clean/hash/original", config())
    assert await object_readable(session, config(), target)
    # The same live object and URL must stop working as soon as review commits.
    session.scalar.return_value = 42
    assert not await object_readable(session, config(), target)
    session.scalar.return_value = None
    row.scan_status = "failed"
    assert not await object_readable(session, config(), target)
    session.scalars.return_value = []
    assert not await object_readable(session, config(), target)


@pytest.mark.asyncio
async def test_shared_object_remains_available_to_an_active_copy():
    session = AsyncMock()
    session.scalar.return_value = None
    session.scalars.return_value = [attachment(deleted_at=object()), attachment(id=2)]
    target = public_object("/attachments/home.test/1/clean/hash/original", config())
    assert await object_readable(session, config(), target)
    session.scalar.return_value = 42
    assert not await object_readable(session, config(), target)


@pytest.mark.asyncio
async def test_remote_cached_capability_denies_retired_or_replaced_bytes():
    session = AsyncMock()
    session.scalar.return_value = None
    cached = SimpleNamespace(
        object_key="remote.test/1/original/hash",
        scan_status="clean",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    async def get(model, _key):
        return cached if model is RemoteMediaCache else None

    session.get.side_effect = get
    target = public_object("/remote/remote.test/1/original/hash", config())
    assert await object_readable(session, config(), target)
    cached.object_key = "remote.test/1/original/new-hash"
    assert not await object_readable(session, config(), target)
    cached.object_key = target[1]
    cached.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not await object_readable(session, config(), target)
    cached.expires_at = datetime.now(UTC) + timedelta(hours=1)

    async def tombstoned(model, _key):
        return object() if model is RemoteMediaTombstone else cached

    session.get.side_effect = tombstoned
    assert not await object_readable(session, config(), target)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "withdrawal", ["attachment", "failed", "message", "pending_review", "removed"]
)
async def test_remote_cached_url_observes_replica_withdrawal_without_evidence_refs(withdrawal):
    session = AsyncMock()
    # A manual message report can have no attachment evidence linkage or tombstone.
    session.scalar.return_value = None
    cached = SimpleNamespace(
        object_key="remote.test/1/original/hash",
        scan_status="clean",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    replica = attachment(message_id=20, message_domain="remote.test")
    parent = SimpleNamespace(deleted_at=None, safety_state=None)

    async def get(model, _key):
        return {RemoteMediaCache: cached, Attachment: replica, Message: parent}.get(model)

    session.get.side_effect = get
    target = public_object("/remote/remote.test/1/original/hash", config())
    assert await object_readable(session, config(), target)
    if withdrawal == "attachment":
        replica.deleted_at = datetime.now(UTC)
    elif withdrawal == "failed":
        replica.scan_status = "failed"
    elif withdrawal == "message":
        parent.deleted_at = datetime.now(UTC)
    else:
        parent.safety_state = withdrawal
    assert not await object_readable(session, config(), target)


@pytest.mark.asyncio
async def test_regular_public_api_proxy_auth_cannot_call_the_gate():
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-kaede-proxy-secret", b"internal-test-hop")],
        }
    )
    session = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await media_access(request, session, config())
    assert error.value.status_code == 404
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
