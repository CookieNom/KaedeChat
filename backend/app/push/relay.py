from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decrypt_secret, encrypt_secret
from app.core.federation import sign_envelope, verify_envelope
from app.core.settings import Settings
from app.db.models import PeerKey
from app.federation.network import FederationNetworkError, ensure_peer, normalize_domain
from app.federation.security import self_private_key

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
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def wake_mac(
    secret: str,
    *,
    route_id: str,
    event_token: str,
    delivery_id: str,
    expires_at: int,
) -> str:
    canonical = f"2\n{route_id}\n{event_token}\n{delivery_id}\n{expires_at}".encode("ascii")
    key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    return base64.urlsafe_b64encode(hmac.digest(key, canonical, "sha256")).decode().rstrip("=")


def encrypt_wake_secret(value: str, settings: Settings) -> bytes:
    return encrypt_secret(value, settings.secret_key_bytes, context=PUSH_WAKE_SECRET_CONTEXT)


def decrypt_wake_secret(value: bytes, settings: Settings) -> str:
    return decrypt_secret(value, settings.secret_key_bytes, context=PUSH_WAKE_SECRET_CONTEXT)


def encrypt_provider_token(value: str, settings: Settings) -> bytes:
    return encrypt_secret(value, settings.secret_key_bytes, context=PUSH_RELAY_TOKEN_CONTEXT)


def decrypt_provider_token(value: bytes, settings: Settings) -> str:
    return decrypt_secret(value, settings.secret_key_bytes, context=PUSH_RELAY_TOKEN_CONTEXT)


async def signed_push_document(
    session: AsyncSession,
    settings: Settings,
    document: dict[str, Any],
) -> dict[str, Any]:
    key_id, private_key = await self_private_key(session, settings)
    signed = {**document, "origin": settings.domain}
    signed["signatures"] = {settings.domain: {key_id: sign_envelope(signed, private_key)}}
    return signed


async def verify_push_document(
    session: AsyncSession,
    settings: Settings,
    document: dict[str, Any],
    *,
    expected_origin: str,
    expected_type: str,
) -> None:
    try:
        origin = normalize_domain(str(document["origin"]))
        document_type = str(document["type"])
        issued_at = int(document["issued_at"])
        expires_at = int(document["expires_at"])
        signatures = document["signatures"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("signed push document is malformed") from exc
    if origin != normalize_domain(expected_origin) or document_type != expected_type:
        raise ValueError("signed push document has the wrong authority or type")
    now = int(time.time())
    if issued_at > now + settings.federation_clock_skew_seconds or expires_at < now:
        raise ValueError("signed push document has expired")
    if expires_at - issued_at > RELAY_GRANT_TTL_SECONDS:
        raise ValueError("signed push document lifetime is too long")
    if not isinstance(signatures, dict) or not isinstance(signatures.get(origin), dict):
        raise ValueError("signed push document has no authority signature")

    async def verify_cached() -> tuple[bool, bool]:
        missing = False
        for key_id, encoded in signatures[origin].items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                continue
            key = await session.get(PeerKey, (origin, key_id))
            if key is None or key.expired_at is not None:
                missing = True
                continue
            try:
                signature = base64.b64decode(encoded, validate=True)
                public_key = Ed25519PublicKey.from_public_bytes(key.public_key)
            except (TypeError, ValueError):
                continue
            if verify_envelope(document, signature, public_key):
                return True, missing
        return False, missing

    verified, refresh = await verify_cached()
    if not verified and refresh:
        try:
            await ensure_peer(session, settings, origin, force=True)
        except FederationNetworkError as exc:
            raise ValueError("push authority keys are unavailable") from exc
        verified, _ = await verify_cached()
    if not verified:
        raise ValueError("signed push document signature is invalid")


def utc_from_epoch(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)
