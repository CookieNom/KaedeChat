from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from app.api import auth as auth_api
from app.auth.schemas import VerificationResendRequest
from app.core.settings import Settings
from app.db.models import User
from app.email.backends import OutboundEmail
from app.email.outbox import (
    RETRY_DELAYS,
    EmailPayloadError,
    decrypt_email_payload,
    encrypt_email_payload,
    retry_delay,
)
from app.tasks import purge_unverified_accounts_in_session

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings() -> Settings:
    return Settings(
        domain="chat.example.com",
        environment="test",
        service_role="api",
        secret_key=VALID_KEY,
        database_url="postgresql+asyncpg://kaede:secret@postgres/kaede",
        dragonfly_url="redis://dragonfly:6379/0",
        app_url="https://chat.example.com",
    )


def test_email_payload_round_trip_is_authenticated_and_opaque() -> None:
    config = settings()
    message = OutboundEmail(
        to="maple@example.com",
        subject="Verify your account",
        text="https://chat.example.com/verify#token=kc1_ot_highly-secret",
    )
    encrypted = encrypt_email_payload(config, "outbox-id", "token-id", message)

    assert b"maple@example.com" not in encrypted
    assert b"kc1_ot_highly-secret" not in encrypted
    assert decrypt_email_payload(config, "outbox-id", "token-id", encrypted) == message


def test_email_payload_rejects_tampering_and_context_swaps() -> None:
    config = settings()
    message = OutboundEmail(to="maple@example.com", subject="Verify", text="secret link")
    encrypted = encrypt_email_payload(config, "outbox-id", "token-id", message)
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])

    with pytest.raises(EmailPayloadError):
        decrypt_email_payload(config, "outbox-id", "token-id", tampered)
    with pytest.raises(EmailPayloadError):
        decrypt_email_payload(config, "different-outbox", "token-id", encrypted)
    with pytest.raises(EmailPayloadError):
        decrypt_email_payload(config, "outbox-id", "different-token", encrypted)


def test_retry_backoff_is_bounded() -> None:
    assert retry_delay(0) == RETRY_DELAYS[0]
    assert retry_delay(1) == RETRY_DELAYS[0]
    assert retry_delay(2) == RETRY_DELAYS[1]
    assert retry_delay(10_000) == RETRY_DELAYS[-1]


def test_retry_schedule_fits_normal_token_lifetimes() -> None:
    now = datetime.now(UTC)
    assert now + retry_delay(1) < now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_resend_stays_uniform_when_candidate_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings()
    session = AsyncMock()
    redis = AsyncMock()
    redis.set.return_value = True
    session.scalar.return_value = User(
        id=1,
        origin_domain=config.domain,
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="irrelevant",
    )
    issue = AsyncMock(side_effect=ValueError("one-time token user no longer exists"))
    monkeypatch.setattr(auth_api, "create_one_time_token", issue)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/verify-email/resend",
            "headers": [],
            "client": ("192.0.2.1", 1234),
        }
    )

    response = await auth_api.resend_verification_email(
        VerificationResendRequest(email="maple@example.com"),
        request,
        session,
        redis,
        config,
    )

    assert response == {"status": "accepted"}
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_unverified_purge_candidates_use_nonblocking_user_locks() -> None:
    session = AsyncMock()
    empty_result = MagicMock()
    empty_result.tuples.return_value = []
    session.execute.return_value = empty_result

    assert await purge_unverified_accounts_in_session(session, settings()) == 0

    statement = session.execute.await_args.args[0]
    assert statement._for_update_arg is not None
    assert statement._for_update_arg.skip_locked is True
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_unverified_accounts_are_not_purged_when_email_is_disabled() -> None:
    session = AsyncMock()
    config = settings()
    config.email_backend = "disabled"

    assert await purge_unverified_accounts_in_session(session, config) == 0

    session.execute.assert_not_awaited()
    session.commit.assert_awaited_once()
