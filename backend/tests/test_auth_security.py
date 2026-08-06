import base64
import threading

import pytest
from anyio import CapacityLimiter
from cryptography.exceptions import InvalidTag

import app.auth.security as security
from app.auth.security import (
    ARGON2_MAX_CONCURRENCY,
    PasswordHashBusy,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    hash_password_async,
    new_access_token,
    new_mfa_ticket,
    new_refresh_token,
    recovery_code,
    recovery_code_hash,
    token_hash,
    verify_password,
    verify_password_async,
)


def test_password_hash_uses_locked_argon2id_parameters() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert "m=65536,t=3,p=1" in encoded
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)
    assert not verify_password("anything", None)


@pytest.mark.asyncio
async def test_async_password_helpers_use_the_bounded_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fake_hash(password: str) -> str:
        worker_threads.append(threading.get_ident())
        return f"hashed:{password}"

    def fake_verify(password: str, password_hash: str | None) -> bool:
        worker_threads.append(threading.get_ident())
        return password_hash == f"hashed:{password}"

    monkeypatch.setattr(security, "hash_password", fake_hash)
    monkeypatch.setattr(security, "verify_password", fake_verify)

    encoded = await hash_password_async("lantern")
    assert await verify_password_async("lantern", encoded)
    assert ARGON2_MAX_CONCURRENCY == 1
    assert worker_threads
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)


@pytest.mark.asyncio
async def test_password_work_rejects_when_the_bounded_admission_queue_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = CapacityLimiter(1)
    holder = object()
    limiter.acquire_on_behalf_of_nowait(holder)
    monkeypatch.setattr(security, "ARGON2_ADMISSION_LIMITER", limiter)
    try:
        with pytest.raises(PasswordHashBusy):
            await verify_password_async("lantern", None)
    finally:
        limiter.release_on_behalf_of(holder)


def test_opaque_tokens_have_scannable_prefixes_and_hash_deterministically() -> None:
    access = new_access_token()
    refresh = new_refresh_token()
    ticket = new_mfa_ticket()
    assert access.startswith("kc1_at_")
    assert refresh.startswith("kc1_rt_")
    assert ticket.startswith("kc1_mfa_")
    assert len(token_hash(access)) == 32
    assert token_hash(access) == token_hash(access)


def test_encrypted_secret_is_bound_to_user_context() -> None:
    key = bytes(range(32))
    encrypted = encrypt_secret("totp-secret", key, context=b"totp:1@alpha.test")
    assert decrypt_secret(encrypted, key, context=b"totp:1@alpha.test") == "totp-secret"
    with pytest.raises(InvalidTag):
        decrypt_secret(encrypted, key, context=b"totp:2@alpha.test")


def test_recovery_codes_are_readable_but_stored_as_hashes() -> None:
    code = recovery_code()
    assert len(code.split("-")) == 4
    assert recovery_code_hash(code) == recovery_code_hash(code.upper())
    assert base64.b16encode(recovery_code_hash(code)) != code.encode()
