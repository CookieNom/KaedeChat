from __future__ import annotations

import base64
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from app.api import auth as auth_api
from app.auth.schemas import RegisterRequest
from app.core.settings import Settings
from app.db.models import User

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings(
    *, email_backend: Literal["smtp", "mailtrap_api", "console", "disabled"] = "disabled"
) -> Settings:
    return Settings(
        domain="chat.example.com",
        environment="test",
        service_role="api",
        secret_key=VALID_KEY,
        database_url="postgresql+asyncpg://kaede:secret@postgres/kaede",
        dragonfly_url="redis://dragonfly:6379/0",
        email_backend=email_backend,
    )


@pytest.mark.asyncio
async def test_auth_configuration_reports_disabled_email_capabilities() -> None:
    config = await auth_api.auth_configuration(settings())
    assert config == {
        "email_required": False,
        "password_recovery_enabled": False,
        "gif_picker_enabled": False,
        "message_search_enabled": False,
        "e2ee_activation_enabled": True,
        "turnstile": {"enabled": False, "site_key": None},
    }


def test_email_less_user_does_not_require_verification() -> None:
    user = User(
        id=1,
        origin_domain="chat.example.com",
        is_local=True,
        username="maple",
        email=None,
        password_hash="hash",
    )
    assert not auth_api.email_verification_required(user, settings(email_backend="console"))


@pytest.mark.asyncio
async def test_disabled_email_registration_creates_no_delivery_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings()
    session = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    snowflake = MagicMock()
    snowflake.mint = AsyncMock(return_value=42)
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    issue_token = AsyncMock()
    wake_outbox = AsyncMock()
    monkeypatch.setattr(auth_api, "hash_submitted_password", AsyncMock(return_value="hash"))
    monkeypatch.setattr(auth_api, "create_one_time_token", issue_token)
    monkeypatch.setattr(auth_api, "wake_email_outbox", wake_outbox)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/register",
            "headers": [],
            "client": ("192.0.2.1", 1234),
        }
    )

    result = await auth_api.register(
        request,
        RegisterRequest(username="maple", password="long-enough-password"),
        session,
        snowflake,
        config,
        redis,
    )

    assert result["email_verification_required"] is False
    created_user = session.add.call_args_list[0].args[0]
    assert isinstance(created_user, User)
    assert created_user.email is None
    issue_token.assert_not_awaited()
    wake_outbox.assert_not_awaited()
    session.commit.assert_awaited_once()
