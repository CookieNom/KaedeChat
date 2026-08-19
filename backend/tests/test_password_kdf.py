from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from app.api import auth as auth_api
from app.auth.schemas import PasswordKdfLookupRequest, PasswordResetRequest
from app.core.settings import Settings
from app.db.models import User
from scripts.verification import authentication_secret, password_kdf_metadata

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
AUTH_SALT = bytes(range(16))
VAULT_SALT = bytes(reversed(range(16)))


def test_verification_tooling_matches_the_cross_client_v2_vector() -> None:
    assert (
        authentication_secret(
            "correct horse battery staple",
            "kaede.example",
            AUTH_SALT,
        )
        == "-Z__QIBecQeJPG4vVovIPtt-Oct4ZE8zUSWu3oyMG3s"
    )
    assert password_kdf_metadata(AUTH_SALT, vault_salt=bytes(range(16, 32))) == {
        "version": 2,
        "algorithm": "PBKDF2-SHA256",
        "iterations": 600_000,
        "auth_salt": "AAECAwQFBgcICQoLDA0ODw",
        "vault_salt": "EBESExQVFhcYGRobHB0eHw",
    }


def settings() -> Settings:
    return Settings(
        domain="chat.example.com",
        environment="test",
        service_role="api",
        secret_key=VALID_KEY,
        database_url="postgresql+asyncpg://kaede:secret@postgres/kaede",
        dragonfly_url="redis://dragonfly:6379/0",
        email_backend="disabled",
    )


def request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "client": ("192.0.2.10", 443),
        }
    )


@pytest.mark.asyncio
async def test_kdf_lookup_is_stable_and_never_invalidates_the_account_vault() -> None:
    user = User(
        id=7,
        origin_domain="chat.example.com",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        password_kdf_version=2,
        password_auth_salt=AUTH_SALT,
        e2ee_vault_salt=VAULT_SALT,
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=user)
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)

    result = await auth_api.password_key_derivation(
        PasswordKdfLookupRequest(identifier="maple"),
        request("/api/v1/auth/key-derivation"),
        session,
        redis,
        settings(),
    )

    assert result["auth_salt"] == auth_api.encode_password_salt(AUTH_SALT)
    assert result["vault_salt"] == auth_api.encode_password_salt(VAULT_SALT)
    assert user.e2ee_vault_salt == VAULT_SALT
    session.commit.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_kdf_lookup_local_handle_alias_has_the_same_observable_pattern(
    known: bool,
) -> None:
    user = (
        User(
            id=7,
            origin_domain="chat.example.com",
            is_local=True,
            username="maple",
            account_type="human",
            password_hash="hash",
            password_kdf_version=2,
            password_auth_salt=AUTH_SALT,
            e2ee_vault_salt=VAULT_SALT,
        )
        if known
        else None
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=user)
    session.commit = AsyncMock()
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    configured = settings()

    bare = await auth_api.password_key_derivation(
        PasswordKdfLookupRequest(identifier="maple"),
        request("/api/v1/auth/key-derivation"),
        session,
        redis,
        configured,
    )
    composite = await auth_api.password_key_derivation(
        PasswordKdfLookupRequest(identifier="MAPLE@CHAT.EXAMPLE.COM"),
        request("/api/v1/auth/key-derivation"),
        session,
        redis,
        configured,
    )

    assert bare == composite
    assert redis.set.await_args_list[0].args[0] == redis.set.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_kdf_lookup_never_negotiates_legacy_or_discloses_its_vault_salt() -> None:
    user = User(
        id=7,
        origin_domain="chat.example.com",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="legacy-hash",
        e2ee_vault_salt=VAULT_SALT,
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=user)
    session.commit = AsyncMock()
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)

    result = await auth_api.password_key_derivation(
        PasswordKdfLookupRequest(identifier="maple"),
        request("/api/v1/auth/key-derivation"),
        session,
        redis,
        settings(),
    )

    assert result["version"] == 2
    assert result["algorithm"] == "PBKDF2-SHA256"
    assert result["vault_salt"] != auth_api.encode_password_salt(VAULT_SALT)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_reset_rotates_vault_salt_and_deletes_server_vault_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=7,
        origin_domain="chat.example.com",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="old",
        password_kdf_version=2,
        password_auth_salt=AUTH_SALT,
        e2ee_vault_salt=VAULT_SALT,
        e2ee_recovery_token_hash=b"r" * 32,
        e2ee_recovery_session_id="revoked-session",
        e2ee_recovery_generation=3,
        e2ee_recovery_expires_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    session = MagicMock()
    session.execute = AsyncMock()
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.eval = AsyncMock(return_value=1)
    monkeypatch.setattr(
        auth_api,
        "consume_one_time_token",
        AsyncMock(return_value=(SimpleNamespace(id="token"), user)),
    )
    monkeypatch.setattr(auth_api, "hash_submitted_password", AsyncMock(return_value="new"))
    revoke = AsyncMock()
    monkeypatch.setattr(auth_api, "revoke_user_sessions", revoke)
    encoded_auth_salt = auth_api.encode_password_salt(bytes([2]) * 16)
    payload = PasswordResetRequest(
        token="t" * 32,
        password="A" * 43,
        password_kdf={
            "version": 2,
            "algorithm": "PBKDF2-SHA256",
            "iterations": 600_000,
            "auth_salt": encoded_auth_salt,
        },
    )

    result = await auth_api.reset_password(payload, session, redis, settings())

    assert result == {
        "status": "password_updated",
        "account_ref": "7@chat.example.com",
    }
    assert user.password_hash == "new"
    assert user.password_auth_salt == bytes([2]) * 16
    assert user.e2ee_vault_salt != VAULT_SALT
    assert len(user.e2ee_vault_salt or b"") == 16
    assert user.e2ee_recovery_token_hash is None
    assert user.e2ee_recovery_session_id is None
    assert user.e2ee_recovery_generation is None
    assert user.e2ee_recovery_expires_at is None
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("DELETE FROM e2ee_account_vault_digests" in statement for statement in statements)
    assert any("DELETE FROM e2ee_account_vaults" in statement for statement in statements)
    reset_token = redis.set.await_args.args[1]
    redis.set.assert_awaited_once_with(
        auth_api.account_vault_lease_key(user.id, user.origin_domain),
        reset_token,
        ex=auth_api.ACCOUNT_VAULT_LEASE_TTL_SECONDS,
    )
    redis.eval.assert_awaited_once_with(
        auth_api.RELEASE_ACCOUNT_VAULT_LEASE,
        1,
        auth_api.account_vault_lease_key(user.id, user.origin_domain),
        reset_token,
    )
    revoke.assert_awaited_once_with(session, redis, settings(), user)
