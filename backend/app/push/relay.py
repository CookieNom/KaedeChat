from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decrypt_secret, encrypt_secret
from app.core.base64url import decode_base64url, encode_base64url
from app.core.settings import Settings
from app.federation.security import verify_federation_document

OPAQUE_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43}$"  # noqa: S105 - validation regex
RELAY_SUBSCRIPTION_PATTERN = r"^kps_[A-Za-z0-9_-]{32,59}$"
RELAY_GRANT_TTL_SECONDS = 300
RELAY_SUBSCRIPTION_DAYS = 90
PUSH_WAKE_TTL_SECONDS = 600
PUSH_RELAY_TOKEN_CONTEXT = b"kaede-push-relay-provider-v1"
PUSH_WAKE_SECRET_CONTEXT = b"kaede-push-wake-secret-v1"


def opaque_token() -> str:
    return secrets.token_urlsafe(32)


def subscription_id() -> str:
    return f"kps_{secrets.token_urlsafe(30)}"


def secret_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def stable_wake_identifier(
    settings: Settings,
    *,
    purpose: str,
    device_id: str,
    message_id: int,
    message_domain: str,
    kind: str,
) -> str:
    """Return a private, deterministic idempotency key for one device event."""

    canonical = (
        f"push-wake-v2\n{purpose}\n{device_id}\n{message_id}\n{message_domain}\n{kind}"
    ).encode()
    digest = hmac.digest(settings.secret_key_bytes, canonical, "sha256")
    return encode_base64url(digest)


def wake_mac(
    secret: str,
    *,
    route_id: str,
    event_token: str,
    delivery_id: str,
    expires_at: int,
) -> str:
    canonical = f"2\n{route_id}\n{event_token}\n{delivery_id}\n{expires_at}".encode("ascii")
    key = decode_base64url(secret)
    return encode_base64url(hmac.digest(key, canonical, "sha256"))


def encrypt_wake_secret(value: str, settings: Settings) -> bytes:
    return encrypt_secret(value, settings.secret_key_bytes, context=PUSH_WAKE_SECRET_CONTEXT)


def decrypt_wake_secret(value: bytes, settings: Settings) -> str:
    return decrypt_secret(value, settings.secret_key_bytes, context=PUSH_WAKE_SECRET_CONTEXT)


def encrypt_provider_token(value: str, settings: Settings) -> bytes:
    return encrypt_secret(value, settings.secret_key_bytes, context=PUSH_RELAY_TOKEN_CONTEXT)


def decrypt_provider_token(value: bytes, settings: Settings) -> str:
    return decrypt_secret(value, settings.secret_key_bytes, context=PUSH_RELAY_TOKEN_CONTEXT)


async def verify_push_document(
    session: AsyncSession,
    settings: Settings,
    document: dict[str, Any],
    *,
    expected_origin: str,
    expected_type: str,
) -> None:
    await verify_federation_document(
        session,
        settings,
        document,
        expected_origin=expected_origin,
        expected_type=expected_type,
        maximum_age=RELAY_GRANT_TTL_SECONDS,
    )


def utc_from_epoch(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)
