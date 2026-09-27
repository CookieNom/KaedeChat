from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.db.bot_models import BotInstallation
from app.db.models import Attachment, User
from app.media import service


def bot_user() -> User:
    return User(
        id=40,
        origin_domain="apps.example",
        is_local=False,
        account_type="bot",
        username="media_bot",
        password_hash=None,
        profile_resolved=True,
        federation_introduced_by_domain="apps.example",
    )


def installation() -> BotInstallation:
    return BotInstallation(
        id=50,
        application_id=20,
        application_domain="apps.example",
        guild_id=30,
        guild_domain="local.example",
        bot_user_id=40,
        bot_user_domain="apps.example",
        installer_id=1,
        installer_domain="local.example",
        granted_scopes=["attachments.read", "attachments.write"],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=1,
        media_bytes_used=11,
        media_pending_bytes=0,
        status="active",
    )


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        domain="local.example",
        media_max_attachment_bytes=1024,
        media_inflight_limit=4,
        media_inflight_quota_bytes=1024,
        media_user_quota_bytes=4096,
        media_daily_upload_quota_bytes=0,
        media_upload_ttl_seconds=300,
        media_attachments_bucket="attachments",
    )


@pytest.mark.parametrize(
    "quota,daily,remaining,error_code",
    [
        (0, 0, None, None),
        (16, 0, None, None),
        (15, 0, None, "USER_STORAGE_QUOTA_EXCEEDED"),
        (0, 5, 5, None),
        (0, 4, None, "DAILY_UPLOAD_QUOTA_EXCEEDED"),
        (0, 5, None, "DAILY_UPLOAD_QUOTA_EXCEEDED"),
    ],
)
@pytest.mark.asyncio
async def test_bot_ticket_uses_installation_ledger_without_local_user_usage(
    monkeypatch: pytest.MonkeyPatch,
    quota: int,
    daily: int,
    remaining: int | None,
    error_code: str | None,
) -> None:
    ledger = installation()
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[ledger, 0, remaining]),
        add=Mock(),
        flush=AsyncMock(),
    )
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=60))

    class Storage:
        def __init__(self, _: object):
            pass

        def presign(self, *_: object, **__: object) -> str:
            return "https://storage.example/upload"

    monkeypatch.setattr(service, "S3Storage", Storage)

    configured = settings()
    configured.media_user_quota_bytes = quota
    configured.media_daily_upload_quota_bytes = daily

    async def upload():
        return await service.create_upload_ticket(
            session,
            configured,
            snowflake,
            bot_user(),
            filename="photo.png",
            content_type="image/png",
            size=5,
            bot_installation=ledger,
        )

    if error_code is not None:
        with pytest.raises(HTTPException) as error:
            await upload()
        assert error.value.detail == {"code": error_code}
        snowflake.mint.assert_not_awaited()
        return
    attachment, upload_url = await upload()

    assert attachment.bot_installation_id == ledger.id
    assert attachment.uploader_domain == "apps.example"
    assert ledger.media_pending_bytes == 5
    assert ledger.media_bytes_used == 11
    assert upload_url == "https://storage.example/upload"
    session.add.assert_called_once_with(attachment)


@pytest.mark.parametrize("quota", [0, 16, 15])
@pytest.mark.asyncio
async def test_finalizing_bot_media_moves_bytes_within_installation_ledger(
    monkeypatch: pytest.MonkeyPatch,
    quota: int,
) -> None:
    ledger = installation()
    ledger.media_pending_bytes = 5
    attachment = Attachment(
        id=60,
        origin_domain="local.example",
        uploader_id=40,
        uploader_domain="apps.example",
        bot_installation_id=ledger.id,
        filename="photo.png",
        content_type="image/png",
        size=5,
        object_key="local.example/60/staging/original",
        staging_object_key="local.example/60/staging/original",
        purpose="attachment",
        scan_status="pending",
        encryption_mode="plaintext",
        upload_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        variants={},
    )
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[attachment, ledger]))

    class Storage:
        def __init__(self, _: object):
            pass

        async def head(self, *_: object) -> SimpleNamespace:
            return SimpleNamespace(size=5, content_type="image/png")

    monkeypatch.setattr(service, "S3Storage", Storage)

    configured = settings()
    configured.media_user_quota_bytes = quota
    if quota == 15:
        with pytest.raises(HTTPException) as error:
            await service.finalize_attachment(session, configured, bot_user(), attachment.id)
        assert error.value.detail == {"code": "USER_STORAGE_QUOTA_EXCEEDED"}
        assert ledger.media_pending_bytes == 5
        assert ledger.media_bytes_used == 11
        return
    finalized = await service.finalize_attachment(session, configured, bot_user(), attachment.id)

    assert finalized is attachment
    assert finalized.finalized_at is not None
    assert ledger.media_pending_bytes == 0
    assert ledger.media_bytes_used == 16


@pytest.mark.asyncio
async def test_discarding_bot_media_releases_only_installation_quota() -> None:
    ledger = installation()
    ledger.media_pending_bytes = 5
    attachment = Attachment(
        id=60,
        origin_domain="local.example",
        uploader_id=40,
        uploader_domain="apps.example",
        bot_installation_id=ledger.id,
        filename="photo.png",
        content_type="image/png",
        size=5,
        object_key="local.example/60/staging/original",
        purpose="attachment",
        scan_status="pending",
        encryption_mode="plaintext",
        variants={},
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=ledger))

    await service.discard_attachment(session, settings(), attachment)

    assert ledger.media_pending_bytes == 0
    assert ledger.media_bytes_used == 11
    assert attachment.deleted_at is not None
