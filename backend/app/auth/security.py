from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from anyio import CapacityLimiter, WouldBlock, to_thread
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

PASSWORD_HASH = PasswordHash((Argon2Hasher(memory_cost=65_536, time_cost=3, parallelism=1),))
DUMMY_PASSWORD_HASH = PASSWORD_HASH.hash("kaede-dummy-password-never-used")
# Each Argon2 invocation is configured to reserve 64 MiB.  Uvicorn already
# runs several API processes, so permit only one hash in each process at once
# instead of multiplying memory pressure through AnyIO's default thread pool.
ARGON2_MAX_CONCURRENCY = 1
ARGON2_CAPACITY_LIMITER = CapacityLimiter(ARGON2_MAX_CONCURRENCY)
ARGON2_MAX_ADMITTED = 4
ARGON2_ADMISSION_LIMITER = CapacityLimiter(ARGON2_MAX_ADMITTED)


class PasswordHashBusy(RuntimeError):
    """The bounded password-hash work queue is full."""


@asynccontextmanager
async def password_hash_admission() -> AsyncIterator[None]:
    """Reject excess Argon2 waiters instead of retaining an unbounded queue."""

    try:
        ARGON2_ADMISSION_LIMITER.acquire_nowait()
    except WouldBlock as exc:
        raise PasswordHashBusy from exc
    try:
        yield
    finally:
        ARGON2_ADMISSION_LIMITER.release()


def hash_password(password: str) -> str:
    return PASSWORD_HASH.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    candidate = password_hash or DUMMY_PASSWORD_HASH
    valid = PASSWORD_HASH.verify(password, candidate)
    return valid and password_hash is not None


async def hash_password_async(password: str) -> str:
    """Hash a password without blocking an API process's event loop."""

    async with password_hash_admission():
        return await to_thread.run_sync(
            hash_password,
            password,
            limiter=ARGON2_CAPACITY_LIMITER,
        )


async def verify_password_async(password: str, password_hash: str | None) -> bool:
    """Verify a password on the shared, memory-bounded Argon2 worker."""

    async with password_hash_admission():
        return await to_thread.run_sync(
            verify_password,
            password,
            password_hash,
            limiter=ARGON2_CAPACITY_LIMITER,
        )


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(prefix: str, *, bytes_count: int = 32) -> str:
    return f"{prefix}{secrets.token_urlsafe(bytes_count)}"


def new_access_token() -> str:
    return new_token("kc1_at_")


def new_refresh_token() -> str:
    return new_token("kc1_rt_")


def new_mfa_ticket() -> str:
    return new_token("kc1_mfa_")


def new_one_time_token() -> str:
    return new_token("kc1_ot_")


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes

    def encode(self) -> bytes:
        return self.nonce + self.ciphertext

    @classmethod
    def decode(cls, value: bytes) -> EncryptedSecret:
        if len(value) < 28:  # 12-byte nonce plus the 16-byte GCM authentication tag
            raise ValueError("encrypted secret is malformed")
        return cls(ciphertext=value[12:], nonce=value[:12])


def encrypt_secret(secret: str, key: bytes, *, context: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(key).encrypt(nonce, secret.encode("utf-8"), context)
    return EncryptedSecret(encrypted, nonce).encode()


def decrypt_secret(value: bytes, key: bytes, *, context: bytes) -> str:
    encrypted = EncryptedSecret.decode(value)
    return AESGCM(key).decrypt(encrypted.nonce, encrypted.ciphertext, context).decode("utf-8")


def recovery_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=").lower()
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"


def recovery_code_hash(code: str) -> bytes:
    return hmac.digest(b"kaede-recovery-v1", code.replace("-", "").lower().encode(), "sha256")
