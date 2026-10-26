from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Attachment, Message
from app.safety import client_api


def inputs():
    return SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")), SimpleNamespace(
        domain="home.test"
    )


@pytest.mark.asyncio
async def test_unknown_remote_history_is_purged_reversibly_not_confirmed_removed():
    auth, settings = inputs()
    session = AsyncMock()
    session.get.return_value = None
    session.scalar.return_value = None
    result = await client_api.content_status(
        client_api.CachedContent(
            message_refs=["20@remote.test"], attachment_refs=["21@remote.test"]
        ),
        auth,
        session,
        settings,
    )
    assert [item["state"] for item in result["items"]] == ["unavailable", "unavailable"]
    session.scalar.return_value = "removed"
    result = await client_api.content_status(
        client_api.CachedContent(message_refs=["20@remote.test"]), auth, session, settings
    )
    assert result["items"][0]["state"] == "removed"


@pytest.mark.asyncio
async def test_leaving_and_rejoining_a_room_does_not_permanently_revoke_content(monkeypatch):
    auth, settings = inputs()
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(deleted_at=None, safety_state=None)
    session.scalar.return_value = None
    readable = AsyncMock(return_value=False)
    monkeypatch.setattr(client_api, "readable_message", readable)
    payload = client_api.CachedContent(message_refs=["20@home.test"])
    result = await client_api.content_status(payload, auth, session, settings)
    assert result["items"][0]["state"] == "unavailable"
    readable.return_value = True
    result = await client_api.content_status(payload, auth, session, settings)
    assert result["items"][0]["state"] == "released"


@pytest.mark.asyncio
async def test_other_users_live_public_assets_remain_accessible(monkeypatch):
    auth, settings = inputs()
    asset = SimpleNamespace(
        id=21,
        origin_domain="home.test",
        uploader_id=2,
        uploader_domain="home.test",
        message_id=None,
        asset_binding="emoji:home.test:22",
        purpose="emoji",
        scan_status="clean",
        deleted_at=None,
        content_sha256="a" * 64,
    )
    session = AsyncMock()

    async def get(model, _key):
        assert model is not Message
        return asset if model is Attachment else None

    session.get.side_effect = get
    session.scalar.return_value = None
    payload = client_api.CachedContent(attachment_refs=["21@home.test"])
    result = await client_api.content_status(payload, auth, session, settings)
    assert result["items"][0]["state"] == "released"
    asset.asset_binding = None
    result = await client_api.content_status(payload, auth, session, settings)
    assert result["items"][0]["state"] == "unavailable"


@pytest.mark.asyncio
async def test_confirmed_verdict_outlives_missing_replica_and_membership():
    auth, settings = inputs()
    session = AsyncMock()
    session.get.return_value = None
    session.scalar.side_effect = ["confirmed", None]
    result = await client_api.content_status(
        client_api.CachedContent(attachment_refs=["21@remote.test"]), auth, session, settings
    )
    assert result["items"][0]["state"] == "removed"


@pytest.mark.asyncio
async def test_inaccessible_confirmed_message_returns_no_case_details(monkeypatch):
    auth, settings = inputs()
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        safety_state="removed", safety_reason="private review explanation"
    )
    session.scalar.return_value = None
    monkeypatch.setattr(client_api, "readable_message", AsyncMock(return_value=False))
    result = await client_api.content_status(
        client_api.CachedContent(message_refs=["20@remote.test"]), auth, session, settings
    )
    assert result["items"] == [
        {
            "message_refs": ["20@remote.test"],
            "attachment_refs": [],
            "state": "removed",
            "reason": "This content is no longer available.",
        }
    ]
