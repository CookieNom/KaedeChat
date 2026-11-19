from __future__ import annotations

import base64
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.requests import Request

from app.api import auth as auth_api
from app.auth.schemas import RegisterRequest
from app.auth.turnstile import REGISTER_ACTION
from app.core.settings import Settings
from app.db.models import EmailOutbox, User

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
DERIVED_PASSWORD = "A" * 43
AUTH_SALT = base64.urlsafe_b64encode(bytes(16)).decode().rstrip("=")
VAULT_SALT = base64.urlsafe_b64encode(bytes([1]) * 16).decode().rstrip("=")
PASSWORD_KDF = {
    "version": 2,
    "algorithm": "PBKDF2-SHA256",
    "iterations": 600_000,
    "auth_salt": AUTH_SALT,
    "vault_salt": VAULT_SALT,
}
REGISTER_URL = "/api/v1/auth/register"
CONSENT_URL = "/api/v1/auth/native-registration-consent"
LEGACY_REGISTRATION = {
    "username": "maple",
    "password": DERIVED_PASSWORD,
    "password_kdf": PASSWORD_KDF,
}


def settings(
    *,
    email_backend: Literal["smtp", "mailtrap_api", "console", "disabled"] = "disabled",
    legal_contact_email: str | None = None,
) -> Settings:
    return Settings(
        domain="chat.example.com",
        environment="test",
        service_role="api",
        secret_key=VALID_KEY,
        database_url="postgresql+asyncpg://kaede:secret@postgres/kaede",
        dragonfly_url="redis://dragonfly:6379/0",
        email_backend=email_backend,
        legal_contact_email=legal_contact_email,
    )


@pytest.mark.asyncio
async def test_auth_configuration_reports_disabled_email_capabilities() -> None:
    config = await auth_api.auth_configuration(settings())
    assert config["email_required"] is False
    assert config["password_recovery_enabled"] is False
    assert config["legal_agreement_required"] is False


@pytest.mark.asyncio
async def test_published_policies_require_terms_agreement() -> None:
    config = settings(legal_contact_email="legal@chat.example.com")
    assert (await auth_api.auth_configuration(config))["legal_agreement_required"] is True
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/register",
            "headers": [],
            "client": ("192.0.2.1", 1234),
        }
    )

    with pytest.raises(HTTPException) as caught:
        await auth_api.register(
            request,
            RegisterRequest(username="maple", password=DERIVED_PASSWORD, password_kdf=PASSWORD_KDF),
            MagicMock(),
            MagicMock(),
            config,
            redis,
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "TERMS_AGREEMENT_REQUIRED"


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
        RegisterRequest(
            accepted_terms=True,
            username="maple",
            password=DERIVED_PASSWORD,
            password_kdf=PASSWORD_KDF,
        ),
        session,
        snowflake,
        config,
        redis,
    )

    assert result["email_verification_required"] is False
    created_user = next(
        call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], User)
    )
    assert isinstance(created_user, User)
    assert created_user.email is None
    issue_token.assert_not_awaited()
    wake_outbox.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.fixture
def registration_client(monkeypatch):
    config = settings(legal_contact_email="legal@chat.example.com")
    config.turnstile_site_key = "0x4AAAAAAExampleSiteKey"
    config.turnstile_secret = SecretStr("example-provider-secret")
    session = AsyncMock()
    session.add = MagicMock()
    snowflake = AsyncMock()
    snowflake.mint.return_value = 42
    redis = AsyncMock()
    values = {}

    def store(key, value, **kwargs):
        # Simulate requests sufficiently far apart to pass the short IP admission window.
        values[key] = value
        return True

    redis.set.side_effect = store
    redis.getdel.side_effect = lambda key: values.pop(key, None)
    verify = AsyncMock(return_value=True)
    monkeypatch.setattr(auth_api, "verify_turnstile_token", verify)
    monkeypatch.setattr(auth_api, "hash_submitted_password", AsyncMock(return_value="hash"))
    app = FastAPI()
    app.include_router(auth_api.router)
    app.dependency_overrides[auth_api.get_settings] = lambda: config
    app.dependency_overrides[auth_api.get_session] = lambda: session
    app.dependency_overrides[auth_api.get_snowflake] = lambda: snowflake
    app.dependency_overrides[auth_api.get_redis] = lambda: redis
    with TestClient(app, base_url="https://chat.example.com") as client:
        yield client, config, session, verify


def test_only_legacy_mobile_needs_native_challenge_for_consent(registration_client):
    client, config, _, _ = registration_client
    config_url = "/api/v1/auth/config"
    legacy_headers = {"X-Kaede-Client": "mobile"}
    assert client.get(config_url, headers=legacy_headers).json()["turnstile"]["enabled"] is True
    modern = client.get(config_url, headers=legacy_headers, params={"native_registration": 2})
    assert modern.json()["turnstile"]["enabled"] is False
    assert client.get(config_url).json()["turnstile"]["enabled"] is False

    challenge = client.get(
        "/api/v1/auth/native-challenge",
        params={"action": REGISTER_ACTION, "request_id": "released-mobile-104"},
    )
    assert challenge.status_code == 200
    assert CONSENT_URL in challenge.text
    assert "released-mobile-104" in challenge.text
    assert challenge.headers["cache-control"] == "no-store"

    config.legal_contact_email = None
    assert client.get(config_url, headers=legacy_headers).json()["turnstile"]["enabled"] is False

    config.legal_contact_email = "legal@chat.example.com"
    config.turnstile_enabled = True
    modern_page = client.get(
        "/api/v1/auth/native-challenge",
        params={
            "action": REGISTER_ACTION,
            "request_id": "modern-mobile-108",
            "native_registration": 2,
        },
    )
    assert modern_page.status_code == 200
    assert CONSENT_URL not in modern_page.text
    assert config.turnstile_site_key in modern_page.text


@pytest.mark.parametrize(
    ("turnstile", "terms_field"), [(False, {}), (True, {}), (True, {"accepted_terms": True})]
)
def test_native_registration_consumes_consent_once(registration_client, turnstile, terms_field):
    client, config, session, verify = registration_client
    config.turnstile_enabled = turnstile
    consent = {"accepted_terms": True}
    if turnstile:
        consent["turnstile_token"] = "provider-token"
    issued = client.post(CONSENT_URL, json=consent)
    assert issued.is_success, issued.text
    receipt = issued.json()["receipt"]
    assert receipt.startswith("kaede-reg-v1.") and len(receipt) <= 2048
    registration = {**LEGACY_REGISTRATION, **terms_field, "turnstile_token": receipt}
    result = client.post(REGISTER_URL, json=registration)
    assert result.status_code == 201, result.text
    assert result.json()["email_verification_required"] is False
    replay = client.post(REGISTER_URL, json=registration)
    assert replay.status_code == 403
    assert replay.json()["detail"]["code"] == "TURNSTILE_INVALID"
    session.commit.assert_awaited_once()
    if turnstile:
        verify.assert_awaited_once()
        assert verify.await_args.args[1] == "provider-token"
        assert verify.await_args.kwargs["action"] == REGISTER_ACTION
    else:
        verify.assert_not_awaited()


def test_native_registration_requires_real_consent_and_configured_challenge(registration_client):
    client, config, session, verify = registration_client
    for payload in ({}, {"accepted_terms": False}, {"accepted_terms": "true"}):
        assert client.post(CONSENT_URL, json=payload).status_code == 422
    receipt = client.post(CONSENT_URL, json={"accepted_terms": True}).json()["receipt"]
    refused = {**LEGACY_REGISTRATION, "accepted_terms": False, "turnstile_token": receipt}
    assert client.post(REGISTER_URL, json=refused).status_code == 422

    config.turnstile_enabled = True
    raw = client.post(
        REGISTER_URL, json={**LEGACY_REGISTRATION, "turnstile_token": "provider-token"}
    )
    assert raw.status_code == 422
    assert raw.json()["detail"]["code"] == "TERMS_AGREEMENT_REQUIRED"
    rejected = client.post(REGISTER_URL, json={**LEGACY_REGISTRATION, "turnstile_token": receipt})
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "TURNSTILE_INVALID"
    assert client.post(CONSENT_URL, json={"accepted_terms": True}).status_code == 403
    verify.return_value = False
    assert (
        client.post(
            CONSENT_URL, json={"accepted_terms": True, "turnstile_token": "failed-token"}
        ).status_code
        == 403
    )
    session.commit.assert_not_awaited()


def test_legacy_registration_preserves_email_verification(registration_client, monkeypatch):
    client, config, session, _ = registration_client
    config.email_backend = "console"
    monkeypatch.setattr(auth_api, "wake_email_outbox", AsyncMock())
    receipt = client.post(CONSENT_URL, json={"accepted_terms": True}).json()["receipt"]
    registration = {**LEGACY_REGISTRATION, "turnstile_token": receipt}
    missing = client.post(REGISTER_URL, json=registration)
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "EMAIL_REQUIRED"

    response = client.post(REGISTER_URL, json={**registration, "email": "maple@chat.example.com"})
    assert response.status_code == 201
    assert response.json()["email_verification_required"] is True
    assert any(isinstance(call.args[0], EmailOutbox) for call in session.add.call_args_list)
    session.commit.assert_awaited_once()


def test_modern_registration_still_accepts_terms_and_provider_token(registration_client):
    client, config, session, verify = registration_client
    config.turnstile_enabled = True
    response = client.post(
        REGISTER_URL,
        json={**LEGACY_REGISTRATION, "accepted_terms": True, "turnstile_token": "provider-token"},
    )
    assert response.status_code == 201
    verify.assert_awaited_once()
    assert verify.await_args.args[1] == "provider-token"
    session.commit.assert_awaited_once()
