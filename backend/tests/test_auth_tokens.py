from __future__ import annotations

import base64
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pyotp
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api.auth as auth_api
from app.api.dependencies import AuthenticatedUser
from app.auth.schemas import MfaCodeRequest
from app.auth.service import (
    FAILURE_WINDOW_SCRIPT,
    INCREMENT_WITH_EXPIRY_SCRIPT,
    MFA_ACCOUNT_FAILURE_LIMIT,
    MFA_IP_FAILURE_LIMIT,
    InvalidTokenError,
    claim_mfa_ticket,
    clear_mfa_account_failures,
    consume_mfa_ticket,
    issue_mfa_ticket,
    load_mfa_setup,
    mfa_account_failure_keys,
    mfa_attempt_locked,
    mfa_ip_locked,
    record_mfa_ip_failure,
    record_mfa_ticket_failure,
    record_mfa_verification_failure,
    store_mfa_setup,
)
from app.auth.tokens import (
    LOGIN_ADMIT_SCRIPT,
    LOGIN_FAILURE_SCRIPT,
    AccessGrant,
    AccessTokenStore,
    LoginLimiter,
)
from app.core.settings import Settings
from app.db.models import User


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        del kwargs
        self.values[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *args: str) -> int | list[int]:
        if script == LOGIN_ADMIT_SCRIPT:
            assert numkeys == 2
            return 1
        if script == LOGIN_FAILURE_SCRIPT:
            assert numkeys == 4
            account_fail_key, ip_fail_key, account_lock_key, ip_lock_key = args
            account_fails = await self.incr(account_fail_key)
            ip_fails = await self.incr(ip_fail_key)
            if account_fails >= 5:
                self.values[account_lock_key] = "1"
            if ip_fails >= 30:
                self.values[ip_lock_key] = "1"
            return [account_fails, ip_fails]
        if script == FAILURE_WINDOW_SCRIPT:
            assert numkeys == 2
            failure_key, lock_key, _ttl, limit = args
            failures = await self.incr(failure_key)
            if failures >= int(limit):
                self.values[lock_key] = "1"
            return failures
        if script == INCREMENT_WITH_EXPIRY_SCRIPT:
            assert numkeys == 1
            failure_key, _ttl = args
            return await self.incr(failure_key)
        assert numkeys == 2
        access_key, session_key, grant, digest, _ttl = args
        self.values[access_key] = grant
        self.sets.setdefault(session_key, set()).add(digest)
        return 1

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def sadd(self, key: str, value: str) -> int:
        before = len(self.sets.setdefault(key, set()))
        self.sets[key].add(value)
        return len(self.sets[key]) - before

    async def smembers(self, key: str) -> set[str]:
        return self.sets.get(key, set())

    async def expire(self, key: str, ttl: int) -> bool:
        del key, ttl
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(self.values.pop(key, None) is not None)
            removed += int(self.sets.pop(key, None) is not None)
        return removed

    async def publish(self, channel: str, value: str) -> int:
        self.published.append((channel, value))
        return 1

    async def mget(self, *keys: str) -> list[str | None]:
        return [self.values.get(key) for key in keys]

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value


def auth_settings() -> Settings:
    secret_key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    return Settings(
        domain="alpha.test",
        environment="test",
        secret_key=secret_key,
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
    )


def authenticated_user(user: User) -> AuthenticatedUser:
    return AuthenticatedUser(
        user=user,
        grant=AccessGrant(user.id, user.origin_domain, "session-one"),
        access_token="kc1_at_test",
        cookie_authenticated=False,
    )


def source_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/mfa/enable",
            "headers": [],
            "client": ("192.0.2.10", 54321),
        }
    )


@pytest.mark.asyncio
async def test_access_token_issue_lookup_and_session_revocation() -> None:
    redis = FakeRedis()
    store = AccessTokenStore(redis, 900)  # type: ignore[arg-type]
    grant = AccessGrant(42, "alpha.test", "session-one")
    token = await store.issue(grant)
    assert await store.get(token) == grant
    await store.revoke_session("session-one")
    assert await store.get(token) is None
    assert redis.published == [("auth:revoke:session-one", "revoked")]


@pytest.mark.asyncio
async def test_login_limiter_locks_after_five_account_failures() -> None:
    redis = FakeRedis()
    limiter = LoginLimiter(redis)  # type: ignore[arg-type]
    assert await limiter.admit("account", "127.0.0.1")
    for _ in range(5):
        await limiter.failure("account", "127.0.0.1")
    assert await limiter.is_locked("account", "127.0.0.1")
    await limiter.success("account")
    assert not await limiter.is_locked("account", "127.0.0.1")


@pytest.mark.asyncio
async def test_mfa_ticket_is_bound_and_claimed_once() -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    ticket = await issue_mfa_ticket(redis, user)  # type: ignore[arg-type]
    user_id, domain, fingerprint = await consume_mfa_ticket(  # type: ignore[arg-type]
        redis, ticket
    )
    assert (user_id, domain) == (42, "alpha.test")
    assert len(fingerprint) == 64
    assert await claim_mfa_ticket(redis, ticket)  # type: ignore[arg-type]
    assert not await claim_mfa_ticket(redis, ticket)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mfa_ticket_is_destroyed_after_five_failures() -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    ticket = await issue_mfa_ticket(redis, user)  # type: ignore[arg-type]
    for _ in range(5):
        await record_mfa_ticket_failure(  # type: ignore[arg-type]
            redis,
            ticket,
            user_id=user.id,
            user_domain=user.origin_domain,
            ip="192.0.2.10",
        )
    with pytest.raises(RuntimeError):
        await consume_mfa_ticket(redis, ticket)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_new_mfa_ticket_invalidates_the_previous_ticket() -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    first = await issue_mfa_ticket(redis, user)  # type: ignore[arg-type]
    second = await issue_mfa_ticket(redis, user)  # type: ignore[arg-type]

    with pytest.raises(InvalidTokenError):
        await consume_mfa_ticket(redis, first)  # type: ignore[arg-type]
    assert (await consume_mfa_ticket(redis, second))[0] == user.id  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mfa_account_failures_survive_new_ticket_issuance() -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    ip = "192.0.2.10"
    first = await issue_mfa_ticket(redis, user)  # type: ignore[arg-type]
    for _ in range(MFA_ACCOUNT_FAILURE_LIMIT - 2):
        await record_mfa_ticket_failure(  # type: ignore[arg-type]
            redis,
            first,
            user_id=user.id,
            user_domain=user.origin_domain,
            ip=ip,
        )
    second = await issue_mfa_ticket(redis, user)  # type: ignore[arg-type]
    for _ in range(2):
        await record_mfa_ticket_failure(  # type: ignore[arg-type]
            redis,
            second,
            user_id=user.id,
            user_domain=user.origin_domain,
            ip=ip,
        )

    assert await mfa_attempt_locked(redis, user.id, user.origin_domain, ip)  # type: ignore[arg-type]
    await clear_mfa_account_failures(redis, user.id, user.origin_domain)  # type: ignore[arg-type]
    assert not await mfa_attempt_locked(redis, user.id, user.origin_domain, ip)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_invalid_mfa_tickets_are_limited_by_source_ip() -> None:
    redis = FakeRedis()
    ip = "192.0.2.11"
    for _ in range(MFA_IP_FAILURE_LIMIT):
        await record_mfa_ip_failure(redis, ip)  # type: ignore[arg-type]
    assert await mfa_ip_locked(redis, ip)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_pending_mfa_setup_is_bound_to_session_and_credential_state() -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    await store_mfa_setup(redis, user, "session-one", "BASE32SECRET")  # type: ignore[arg-type]
    assert (await load_mfa_setup(redis, user, "session-one")) == "BASE32SECRET"  # type: ignore[arg-type]
    assert await load_mfa_setup(redis, user, "session-two") is None  # type: ignore[arg-type]

    await store_mfa_setup(redis, user, "session-one", "BASE32SECRET")  # type: ignore[arg-type]
    user.password_hash = "changed-password-hash"
    assert await load_mfa_setup(redis, user, "session-one") is None  # type: ignore[arg-type]

    user.password_hash = "encoded-password-hash"
    await store_mfa_setup(redis, user, "session-one", "BASE32SECRET")  # type: ignore[arg-type]
    user.totp_secret_encrypted = b"changed-factor"
    assert await load_mfa_setup(redis, user, "session-one") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mfa_enable_limits_invalid_codes_by_account() -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    accepted_codes = {totp.at(now + offset) for offset in (-30, 0, 30)}
    invalid_code = next(
        candidate
        for candidate in ("000000", "111111", "222222", "333333")
        if candidate not in accepted_codes
    )
    await store_mfa_setup(redis, user, "session-one", secret)  # type: ignore[arg-type]
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[user, "session-one"] * (MFA_ACCOUNT_FAILURE_LIMIT + 1))

    for _ in range(MFA_ACCOUNT_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as exc_info:
            await auth_api.enable_mfa(
                MfaCodeRequest(code=invalid_code),
                source_request(),
                authenticated_user(user),
                session,  # type: ignore[arg-type]
                redis,  # type: ignore[arg-type]
                auth_settings(),
            )
        assert exc_info.value.status_code == 400

    assert await mfa_attempt_locked(  # type: ignore[arg-type]
        redis, user.id, user.origin_domain, "192.0.2.10"
    )
    with pytest.raises(HTTPException) as exc_info:
        await auth_api.enable_mfa(
            MfaCodeRequest(code=invalid_code),
            source_request(),
            authenticated_user(user),
            session,  # type: ignore[arg-type]
            redis,  # type: ignore[arg-type]
            auth_settings(),
        )
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_mfa_enable_honors_the_source_ip_lock() -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    for _ in range(MFA_IP_FAILURE_LIMIT):
        await record_mfa_ip_failure(redis, "192.0.2.10")  # type: ignore[arg-type]
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[user, "session-one"])

    with pytest.raises(HTTPException) as exc_info:
        await auth_api.enable_mfa(
            MfaCodeRequest(code="000000"),
            source_request(),
            authenticated_user(user),
            session,  # type: ignore[arg-type]
            redis,  # type: ignore[arg-type]
            auth_settings(),
        )

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_mfa_enable_clears_account_failures_and_pending_setup_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    user = User(
        id=42,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="encoded-password-hash",
    )
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    await store_mfa_setup(redis, user, "session-one", secret)  # type: ignore[arg-type]
    await record_mfa_verification_failure(  # type: ignore[arg-type]
        redis, user.id, user.origin_domain, "192.0.2.10"
    )
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[user, "session-one"])
    monkeypatch.setattr(auth_api, "enable_totp", AsyncMock())
    monkeypatch.setattr(auth_api, "revoke_user_sessions", AsyncMock())
    monkeypatch.setattr(auth_api, "invalidate_active_mfa_ticket", AsyncMock())

    response = await auth_api.enable_mfa(
        MfaCodeRequest(code=code),
        source_request(),
        authenticated_user(user),
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        auth_settings(),
    )

    failure_key, lock_key = mfa_account_failure_keys(user.id, user.origin_domain)
    assert response["status"] == "enabled"
    assert failure_key not in redis.values
    assert lock_key not in redis.values
    assert auth_api.mfa_setup_key(user) not in redis.values
