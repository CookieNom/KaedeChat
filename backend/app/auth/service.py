from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import pyotp
from redis.asyncio import Redis
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    decrypt_secret,
    encrypt_secret,
    new_mfa_ticket,
    new_one_time_token,
    new_refresh_token,
    recovery_code_hash,
    token_hash,
    token_key,
)
from app.auth.tokens import AccessGrant, AccessTokenStore
from app.core.settings import Settings
from app.db.models import OneTimeToken, RecoveryCode, Session, User

MFA_TICKET_TTL_SECONDS = 300
MFA_FAILURE_WINDOW_SECONDS = 900
MFA_TICKET_FAILURE_LIMIT = 5
MFA_ACCOUNT_FAILURE_LIMIT = 5
MFA_IP_FAILURE_LIMIT = 30
FAILURE_WINDOW_SCRIPT = """
local failures = redis.call('INCR', KEYS[1])
if failures == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if tonumber(ARGV[2]) > 0 and failures >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
end
return failures
"""
INCREMENT_WITH_EXPIRY_SCRIPT = """
local failures = redis.call('INCR', KEYS[1])
if failures == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return failures
"""


class InvalidTokenError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedSession:
    access_token: str
    refresh_token: str
    session_id: str


def credential_fingerprint(user: User) -> str:
    if not user.is_local or user.password_hash is None:
        raise ValueError("MFA tickets require a local user with password credentials")
    return hashlib.sha256(user.password_hash.encode("utf-8")).hexdigest()


async def create_session(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    user: User,
    *,
    device_name: str | None,
    user_agent: str | None,
    ip_address: str | None,
) -> IssuedSession:
    if not user.is_local:
        raise ValueError("sessions can only be issued to local users")
    now = datetime.now(UTC)
    refresh = new_refresh_token()
    session_id = secrets.token_urlsafe(24)
    record = Session(
        id=session_id,
        user_id=user.id,
        user_domain=user.origin_domain,
        user_is_local=True,
        refresh_token_hash=token_hash(refresh),
        device_name=device_name,
        user_agent=user_agent,
        ip_address=ip_address,
        expires_at=now + timedelta(days=settings.refresh_sliding_days),
        absolute_expires_at=now + timedelta(days=settings.refresh_absolute_days),
    )
    session.add(record)
    await session.flush()
    access = await AccessTokenStore(redis, settings.access_token_ttl_seconds).issue(
        AccessGrant(user.id, user.origin_domain, session_id)
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await AccessTokenStore(redis, settings.access_token_ttl_seconds).revoke_token(access)
        raise
    return IssuedSession(access, refresh, session_id)


async def rotate_refresh_token(
    session: AsyncSession, redis: Redis, settings: Settings, refresh_token: str
) -> IssuedSession:
    if not refresh_token.startswith("kc1_rt_"):
        raise InvalidTokenError
    digest = token_hash(refresh_token)
    record = await session.scalar(
        select(Session)
        .where(or_(Session.refresh_token_hash == digest, Session.previous_token_hash == digest))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if (
        record is None
        or record.revoked_at is not None
        or record.expires_at <= now
        or record.absolute_expires_at <= now
    ):
        raise InvalidTokenError
    store = AccessTokenStore(redis, settings.access_token_ttl_seconds)
    if record.previous_token_hash == digest:
        record.revoked_at = now
        await session.commit()
        await store.revoke_session(record.id)
        raise InvalidTokenError
    new_refresh = new_refresh_token()
    record.previous_token_hash = record.refresh_token_hash
    record.refresh_token_hash = token_hash(new_refresh)
    record.last_used_at = now
    record.expires_at = min(
        now + timedelta(days=settings.refresh_sliding_days), record.absolute_expires_at
    )
    await store.revoke_session(record.id)
    access = await store.issue(AccessGrant(record.user_id, record.user_domain, record.id))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await store.revoke_token(access)
        raise
    return IssuedSession(access, new_refresh, record.id)


async def revoke_user_sessions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    user: User,
    *,
    keep_session_id: str | None = None,
) -> None:
    now = datetime.now(UTC)
    conditions = [
        Session.user_id == user.id,
        Session.user_domain == user.origin_domain,
        Session.revoked_at.is_(None),
    ]
    if keep_session_id is not None:
        conditions.append(Session.id != keep_session_id)
    session_ids = list((await session.scalars(select(Session.id).where(*conditions))).all())
    await session.execute(update(Session).where(Session.id.in_(session_ids)).values(revoked_at=now))
    await session.commit()
    store = AccessTokenStore(redis, settings.access_token_ttl_seconds)
    for session_id in session_ids:
        await store.revoke_session(session_id)


async def create_one_time_token(
    session: AsyncSession,
    user: User,
    *,
    purpose: str,
    expires_in: timedelta,
    payload: dict[str, object] | None = None,
) -> tuple[str, OneTimeToken]:
    # Serialize issuance with consumption and make each purpose a single-active
    # credential. Otherwise an older password-reset/email-change link remains a
    # valid way to undo a later account-security action.
    locked_user = await session.scalar(
        select(User)
        .where(User.id == user.id, User.origin_domain == user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None:
        raise ValueError("one-time token user no longer exists")
    await session.execute(
        delete(OneTimeToken).where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.user_domain == user.origin_domain,
            OneTimeToken.purpose == purpose,
            OneTimeToken.consumed_at.is_(None),
        )
    )
    raw = new_one_time_token()
    record = OneTimeToken(
        id=secrets.token_urlsafe(24),
        user_id=user.id,
        user_domain=user.origin_domain,
        user_is_local=True,
        purpose=purpose,
        token_hash=token_hash(raw),
        payload=payload or {},
        expires_at=datetime.now(UTC) + expires_in,
    )
    session.add(record)
    # The API adds the encrypted delivery intent before committing.  Keeping
    # transaction ownership with the caller makes the credential and the email
    # that carries it an indivisible unit of work.
    await session.flush()
    return raw, record


async def consume_one_time_token(
    session: AsyncSession, raw: str, *, purpose: str
) -> tuple[OneTimeToken, User]:
    digest = token_hash(raw)
    candidate = await session.scalar(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == digest,
            OneTimeToken.purpose == purpose,
        )
    )
    if candidate is None:
        raise InvalidTokenError
    # Issuance takes the user lock before deleting old tokens. Take locks in the
    # same order so concurrent resend/consume operations cannot deadlock.
    user = await session.scalar(
        select(User)
        .where(User.id == candidate.user_id, User.origin_domain == candidate.user_domain)
        .with_for_update()
    )
    if user is None:
        raise InvalidTokenError
    record = await session.scalar(
        select(OneTimeToken)
        .where(
            OneTimeToken.token_hash == digest,
            OneTimeToken.purpose == purpose,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if record is None or record.consumed_at is not None or record.expires_at <= now:
        raise InvalidTokenError
    if (record.user_id, record.user_domain) != (user.id, user.origin_domain):
        raise InvalidTokenError
    record.consumed_at = now
    return record, user


def mfa_account_digest(user_id: int, user_domain: str) -> str:
    return hashlib.sha256(f"{user_id}@{user_domain}".encode()).hexdigest()


def mfa_ip_digest(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


def mfa_active_ticket_key(user_id: int, user_domain: str) -> str:
    return f"auth:mfa_active:{mfa_account_digest(user_id, user_domain)}"


def mfa_account_failure_keys(user_id: int, user_domain: str) -> tuple[str, str]:
    digest = mfa_account_digest(user_id, user_domain)
    return f"auth:mfa_fail:account:{digest}", f"auth:mfa_lock:account:{digest}"


def mfa_ip_failure_keys(ip: str) -> tuple[str, str]:
    digest = mfa_ip_digest(ip)
    return f"auth:mfa_fail:ip:{digest}", f"auth:mfa_lock:ip:{digest}"


def mfa_factor_fingerprint(user: User) -> str:
    material = user.totp_secret_encrypted or b"mfa-disabled"
    return hashlib.sha256(material).hexdigest()


def mfa_setup_key(user: User) -> str:
    return f"auth:mfa_setup:{user.origin_domain}:{user.id}"


async def store_mfa_setup(redis: Redis, user: User, session_id: str, secret: str) -> None:
    await redis.set(
        mfa_setup_key(user),
        json.dumps(
            {
                "secret": secret,
                "session_id": session_id,
                "credential_fingerprint": credential_fingerprint(user),
                "factor_fingerprint": mfa_factor_fingerprint(user),
            },
            separators=(",", ":"),
        ),
        ex=600,
    )


async def load_mfa_setup(redis: Redis, user: User, session_id: str) -> str | None:
    key = mfa_setup_key(user)
    value = await redis.get(key)
    if value is None:
        return None
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError
        secret = str(parsed["secret"])
        stored_session_id = str(parsed["session_id"])
        stored_credentials = str(parsed["credential_fingerprint"])
        stored_factor = str(parsed["factor_fingerprint"])
        valid = (
            bool(secret)
            and secrets.compare_digest(stored_session_id, session_id)
            and secrets.compare_digest(stored_credentials, credential_fingerprint(user))
            and secrets.compare_digest(stored_factor, mfa_factor_fingerprint(user))
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        valid = False
        secret = ""
    if not valid:
        await redis.delete(key)
        return None
    return secret


async def issue_mfa_ticket(redis: Redis, user: User) -> str:
    ticket = new_mfa_ticket()
    digest = token_key(ticket)
    active_key = mfa_active_ticket_key(user.id, user.origin_domain)
    previous_digest = await redis.get(active_key)
    await redis.set(
        f"auth:mfa_ticket:{digest}",
        json.dumps(
            {
                "user_id": user.id,
                "user_domain": user.origin_domain,
                "credential_fingerprint": credential_fingerprint(user),
            }
        ),
        ex=MFA_TICKET_TTL_SECONDS,
    )
    await redis.set(active_key, digest, ex=MFA_TICKET_TTL_SECONDS)
    if previous_digest is not None and not secrets.compare_digest(str(previous_digest), digest):
        await redis.delete(
            f"auth:mfa_ticket:{previous_digest}",
            f"auth:mfa_fail:ticket:{previous_digest}",
        )
    return ticket


async def invalidate_active_mfa_ticket(redis: Redis, user: User) -> None:
    active_key = mfa_active_ticket_key(user.id, user.origin_domain)
    digest = await redis.get(active_key)
    keys = [active_key]
    if digest is not None:
        keys.extend(
            (
                f"auth:mfa_ticket:{digest}",
                f"auth:mfa_fail:ticket:{digest}",
            )
        )
    await redis.delete(*keys)


async def consume_mfa_ticket(redis: Redis, ticket: str) -> tuple[int, str, str]:
    if not ticket.startswith("kc1_mfa_"):
        raise InvalidTokenError
    key = f"auth:mfa_ticket:{token_key(ticket)}"
    value = await redis.get(key)
    if value is None:
        raise InvalidTokenError
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError
        fingerprint = str(parsed["credential_fingerprint"])
        if len(fingerprint) != 64:
            raise ValueError
        user_id = int(parsed["user_id"])
        user_domain = str(parsed["user_domain"])
        active_digest = await redis.get(mfa_active_ticket_key(user_id, user_domain))
        if active_digest is None or not secrets.compare_digest(
            str(active_digest), token_key(ticket)
        ):
            await redis.delete(key)
            raise InvalidTokenError
        return user_id, user_domain, fingerprint
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        await redis.delete(key)
        raise InvalidTokenError from exc


async def claim_mfa_ticket(redis: Redis, ticket: str) -> bool:
    """Atomically make a successfully verified MFA ticket single-use."""
    digest = token_key(ticket)
    claimed = bool(await redis.delete(f"auth:mfa_ticket:{digest}"))
    if claimed:
        await redis.delete(f"auth:mfa_fail:ticket:{digest}")
    return claimed


async def mfa_ip_locked(redis: Redis, ip: str) -> bool:
    _, lock_key = mfa_ip_failure_keys(ip)
    return await redis.get(lock_key) is not None


async def mfa_attempt_locked(redis: Redis, user_id: int, user_domain: str, ip: str) -> bool:
    _, account_lock = mfa_account_failure_keys(user_id, user_domain)
    _, ip_lock = mfa_ip_failure_keys(ip)
    account_value, ip_value = await redis.mget(account_lock, ip_lock)
    return account_value is not None or ip_value is not None


async def record_mfa_ip_failure(redis: Redis, ip: str) -> int:
    failure_key, lock_key = mfa_ip_failure_keys(ip)
    result = await cast(
        Awaitable[object],
        redis.eval(
            FAILURE_WINDOW_SCRIPT,
            2,
            failure_key,
            lock_key,
            str(MFA_FAILURE_WINDOW_SECONDS),
            str(MFA_IP_FAILURE_LIMIT),
        ),
    )
    return int(cast(int | str, result))


async def record_mfa_verification_failure(
    redis: Redis, user_id: int, user_domain: str, ip: str
) -> tuple[int, int]:
    account_failure, account_lock = mfa_account_failure_keys(user_id, user_domain)
    result = await cast(
        Awaitable[object],
        redis.eval(
            FAILURE_WINDOW_SCRIPT,
            2,
            account_failure,
            account_lock,
            str(MFA_FAILURE_WINDOW_SECONDS),
            str(MFA_ACCOUNT_FAILURE_LIMIT),
        ),
    )
    account_failures = int(cast(int | str, result))
    ip_failures = await record_mfa_ip_failure(redis, ip)
    return account_failures, ip_failures


async def clear_mfa_account_failures(redis: Redis, user_id: int, user_domain: str) -> None:
    failure_key, lock_key = mfa_account_failure_keys(user_id, user_domain)
    await redis.delete(failure_key, lock_key)


async def record_mfa_ticket_failure(
    redis: Redis,
    ticket: str,
    *,
    user_id: int,
    user_domain: str,
    ip: str,
    max_attempts: int = MFA_TICKET_FAILURE_LIMIT,
) -> None:
    digest = token_key(ticket)
    failure_key = f"auth:mfa_fail:ticket:{digest}"
    result = await cast(
        Awaitable[object],
        redis.eval(
            INCREMENT_WITH_EXPIRY_SCRIPT,
            1,
            failure_key,
            str(MFA_TICKET_TTL_SECONDS),
        ),
    )
    failures = int(cast(int | str, result))
    await record_mfa_verification_failure(redis, user_id, user_domain, ip)
    if failures >= max_attempts:
        await redis.delete(f"auth:mfa_ticket:{digest}", failure_key)


async def verify_mfa_code(session: AsyncSession, settings: Settings, user: User, code: str) -> bool:
    normalized = code.replace(" ", "").replace("-", "").lower()
    if user.totp_secret_encrypted is not None and normalized.isdecimal():
        secret = decrypt_secret(
            user.totp_secret_encrypted,
            settings.secret_key_bytes,
            context=f"totp:{user.id}@{user.origin_domain}".encode(),
        )
        if pyotp.TOTP(secret).verify(normalized, valid_window=1):
            return True
    digest = recovery_code_hash(code)
    recovery = await session.scalar(
        select(RecoveryCode)
        .where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.user_domain == user.origin_domain,
            RecoveryCode.code_hash == digest,
            RecoveryCode.used_at.is_(None),
        )
        .with_for_update()
    )
    if recovery is None:
        return False
    recovery.used_at = datetime.now(UTC)
    await session.flush()
    return True


async def enable_totp(
    session: AsyncSession, settings: Settings, user: User, secret: str, codes: list[str]
) -> None:
    user.totp_secret_encrypted = encrypt_secret(
        secret,
        settings.secret_key_bytes,
        context=f"totp:{user.id}@{user.origin_domain}".encode(),
    )
    await session.execute(
        delete(RecoveryCode).where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.user_domain == user.origin_domain,
        )
    )
    session.add_all(
        RecoveryCode(
            user_id=user.id,
            user_domain=user.origin_domain,
            user_is_local=True,
            code_hash=recovery_code_hash(code),
        )
        for code in codes
    )
    await session.flush()
