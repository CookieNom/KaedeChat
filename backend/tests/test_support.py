from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import support
from app.api.dependencies import get_redis, get_session
from app.auth.turnstile import TurnstileUnavailableError
from app.core.settings import Settings, get_settings
from app.email import outbox


@pytest.fixture
def support_client(monkeypatch):
    config = Settings(
        domain="chat.example.com",
        environment="test",
        service_role="api",
        secret_key=base64.urlsafe_b64encode(bytes(range(32))).decode(),
        database_url="postgresql+asyncpg://test:test@localhost/test",
        dragonfly_url="redis://localhost:6379/0",
        legal_contact_email="operator@example.com",
    )
    session = AsyncMock()
    session.add = MagicMock()
    redis = AsyncMock()
    redis.eval.return_value = [1, 2, 0, 1000]
    wake = AsyncMock()
    monkeypatch.setattr(support, "wake_email_outbox", wake)
    app = FastAPI()
    app.include_router(support.router)
    app.dependency_overrides[get_settings] = lambda: config
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_redis] = lambda: redis
    with TestClient(app) as client:
        yield client, config, session, redis, wake


PAYLOAD = {
    "email": "visitor@example.com",
    "reason": "Technical problem",
    "message": "Help me log in",
}


async def test_support_queues_encrypted_email_and_worker_delivers(support_client, monkeypatch):
    client, config, session, redis, wake = support_client
    response = client.post("/api/v1/support", json=PAYLOAD)
    assert response.status_code == 202
    session.commit.assert_awaited_once()
    wake.assert_awaited_once()
    assert redis.eval.await_count == 2
    record = session.add.call_args.args[0]
    assert record.one_time_token_id is None
    assert PAYLOAD["message"].encode() not in record.encrypted_payload
    message = outbox.decrypt_email_payload(config, record.id, None, record.encrypted_payload)
    assert message.to == config.legal_contact_email
    assert message.subject == "[Kaede support] Technical problem"
    assert PAYLOAD["email"] in message.text
    assert PAYLOAD["message"] in message.text
    monkeypatch.setattr(outbox, "_claim_batch", AsyncMock(return_value=([record], 0)))
    finish = AsyncMock()
    monkeypatch.setattr(outbox, "_finish_delivery", finish)
    backend = AsyncMock()
    result = await outbox.drain_email_outbox(MagicMock(), config, backend=backend)
    assert result["delivered"] == 1
    backend.send.assert_awaited_once_with(message)
    finish.assert_awaited_once()


@pytest.mark.parametrize("unavailable", ["recipient", "email"])
def test_support_unavailable_without_delivery_configuration(support_client, unavailable):
    client, config, session, _, _ = support_client
    if unavailable == "recipient":
        config.legal_contact_email = None
    else:
        config.email_backend = "disabled"
    assert client.get("/api/v1/support/config").json()["enabled"] is False
    assert client.post("/api/v1/support", json=PAYLOAD).status_code == 503
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [
        {"email": "bad-address"},
        {"reason": "made up"},
        {"message": "   "},
        {"message": "x" * 5001},
        {"to": "attacker@example.com"},
    ],
)
def test_support_rejects_invalid_input(support_client, invalid):
    client, _, session, _, _ = support_client
    assert client.post("/api/v1/support", json=PAYLOAD | invalid).status_code == 422
    session.add.assert_not_called()


def test_support_rejects_rate_limited_requests(support_client):
    client, _, session, redis, _ = support_client
    redis.eval.return_value = [0, 0, 1200000, 3600000]
    response = client.post("/api/v1/support", json=PAYLOAD)
    assert response.status_code == 429
    assert "retry-after" in response.headers
    session.add.assert_not_called()


@pytest.mark.parametrize("verification", [False, True, TurnstileUnavailableError()])
def test_support_enforces_configured_bot_check(support_client, monkeypatch, verification):
    client, config, session, _, _ = support_client
    config.turnstile_enabled = True
    config.turnstile_site_key = "test-site-key"
    verify = AsyncMock(return_value=verification)
    if isinstance(verification, Exception):
        verify.side_effect = verification
    monkeypatch.setattr(support, "verify_turnstile_token", verify)
    assert client.post("/api/v1/support", json=PAYLOAD).status_code == 400
    session.add.assert_not_called()
    response = client.post("/api/v1/support", json=PAYLOAD | {"turnstile_token": "test-token"})
    assert response.status_code == (
        503 if isinstance(verification, Exception) else 202 if verification else 400
    )
    assert verify.await_args.kwargs["action"] == "kaede-support"
    if verification is not True:
        session.add.assert_not_called()


def test_failed_commit_never_reports_success_or_wakes_worker(support_client):
    client, _, session, _, wake = support_client
    session.commit.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        client.post("/api/v1/support", json=PAYLOAD)
    wake.assert_not_awaited()


async def test_support_outbox_migration_preserves_pending_requests(postgres_schema, monkeypatch):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    migration = importlib.import_module("migrations.versions.7c2e9a1b4d60_support_email_outbox")
    await postgres_schema.execute(
        text(
            "CREATE TABLE email_outbox (id text PRIMARY KEY, "
            "one_time_token_id varchar(64) NOT NULL, status text NOT NULL)"
        )
    )

    def apply(connection, operation):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        operation()

    await postgres_schema.run_sync(apply, migration.upgrade)
    await postgres_schema.execute(
        text("INSERT INTO email_outbox VALUES ('support', NULL, 'pending')")
    )
    with pytest.raises(IntegrityError):
        async with postgres_schema.begin_nested():
            await postgres_schema.run_sync(apply, migration.downgrade)
    assert await postgres_schema.scalar(text("SELECT count(*) FROM email_outbox")) == 1
    await postgres_schema.execute(text("UPDATE email_outbox SET status = 'delivered'"))
    await postgres_schema.run_sync(apply, migration.downgrade)
    assert await postgres_schema.scalar(text("SELECT count(*) FROM email_outbox")) == 0
