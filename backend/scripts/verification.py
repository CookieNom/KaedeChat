"""Consistent, actionable failures for operator-run verification commands."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Protocol

MAX_FAILURE_MESSAGE_LENGTH = 2_000
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_QUOTED_FIELD = re.compile(
    r"""(?ix)
    (?P<prefix>
      ["']?
      (?:
        access[_-]?token|refresh[_-]?token|mfa[_-]?ticket|webhook[_-]?token|
        (?:api|client|proxy|admin|private|push)?[_-]?(?:token|secret|password|credential)|
        authorization|private[_-]?key
      )
      ["']?\s*[:=]\s*
    )
    (?P<quote>["'])(?P<value>.*?)(?P=quote)
    """,
)
_SENSITIVE_UNQUOTED_FIELD = re.compile(
    r"""(?ix)
    (?P<prefix>
      \b
      (?:
        access[_-]?token|refresh[_-]?token|mfa[_-]?ticket|webhook[_-]?token|
        (?:api|client|proxy|admin|private|push)?[_-]?(?:token|secret|password|credential)|
        authorization|private[_-]?key
      )
      \s*=\s*
    )
    [^\s,;&}\]]+
    """,
)
_BEARER_CREDENTIAL = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
_CREDENTIAL_URL = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_KAEDE_TOKEN = re.compile(r"\bkc1_(?:at|rt|mfa|ot)_[A-Za-z0-9._~-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

PASSWORD_KDF_VERSION = 2
PASSWORD_KDF_ALGORITHM = "PBKDF2-SHA256"  # noqa: S105 - protocol label
PASSWORD_KDF_ITERATIONS = 600_000


def authentication_secret(password: str, domain: str, auth_salt: bytes) -> str:
    """Derive the exact v2 authentication secret used by verification clients."""

    if len(auth_salt) != 16:
        raise ValueError("verification password authentication salt must be 16 bytes")
    bound_salt = f"kaede-password-kdf-v2\0auth\0{domain}\0".encode() + auth_salt
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bound_salt,
        PASSWORD_KDF_ITERATIONS,
        dklen=32,
    )
    return base64.urlsafe_b64encode(derived).decode().rstrip("=")


def password_kdf_metadata(
    auth_salt: bytes,
    *,
    vault_salt: bytes | None = None,
) -> dict[str, Any]:
    """Render the strict v2 registration/reset metadata for a test credential."""

    if len(auth_salt) != 16:
        raise ValueError("verification password authentication salt must be 16 bytes")
    if vault_salt is not None and len(vault_salt) != 16:
        raise ValueError("verification password vault salt must be 16 bytes")
    payload: dict[str, Any] = {
        "version": PASSWORD_KDF_VERSION,
        "algorithm": PASSWORD_KDF_ALGORITHM,
        "iterations": PASSWORD_KDF_ITERATIONS,
        "auth_salt": base64.urlsafe_b64encode(auth_salt).decode().rstrip("="),
    }
    if vault_salt is not None:
        payload["vault_salt"] = base64.urlsafe_b64encode(vault_salt).decode().rstrip("=")
    return payload


def _sanitize_failure_message(message: str) -> str:
    safe = _PRIVATE_KEY_BLOCK.sub("[private key redacted]", message)
    safe = _SENSITIVE_QUOTED_FIELD.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[redacted]{match.group('quote')}"
        ),
        safe,
    )
    safe = _SENSITIVE_UNQUOTED_FIELD.sub(lambda match: f"{match.group('prefix')}[redacted]", safe)
    safe = _BEARER_CREDENTIAL.sub("Bearer [redacted]", safe)
    safe = _CREDENTIAL_URL.sub(r"\1[credentials-redacted]@", safe)
    safe = _KAEDE_TOKEN.sub("[Kaede token redacted]", safe)
    safe = _JWT.sub("[token redacted]", safe)
    if len(safe) > MAX_FAILURE_MESSAGE_LENGTH:
        safe = f"{safe[:MAX_FAILURE_MESSAGE_LENGTH]}… [output truncated]"
    return safe


class VerificationFailure(RuntimeError):
    """A verification invariant failed in a way an operator can act on."""

    def __init__(self, message: str) -> None:
        super().__init__(_sanitize_failure_message(message))


class JsonReceiver(Protocol):
    def receive_json(self) -> object: ...


def receive_dispatch(
    receiver: JsonReceiver,
    expected_type: str,
    *,
    max_frames: int = 10,
) -> dict[str, Any]:
    """Receive an expected dispatch while tolerating bounded interleaving."""

    observed: list[str] = []
    for _ in range(max_frames):
        frame = receiver.receive_json()
        if not isinstance(frame, dict):
            observed.append(type(frame).__name__)
            continue
        event_type = frame.get("t")
        observed.append(str(event_type) if event_type is not None else f"op={frame.get('op')!r}")
        if event_type == expected_type:
            return frame
    raise VerificationFailure(
        f"{expected_type} dispatch missing after {max_frames} frames; observed {observed!r}"
    )


def require(condition: object, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def failure_message(suite: str, error: VerificationFailure, rerun: str) -> str:
    return (
        f"{suite} verification failed: {error}\n"
        "Inspect the failed service's preceding logs, correct the reported invariant, "
        f"then rerun `{rerun}`."
    )
