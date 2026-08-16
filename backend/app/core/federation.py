from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.json_limits import FEDERATION_JSON_LIMITS, validate_json_tree

POLICY_HELD_OUTBOX_PREFIX = "held by local federation block:"
POLICY_HELD_OUTBOX_DELAY = timedelta(days=36_500)
BLOCK_POLICY_ADVISORY_NAME = "kaede-instance-blocks"
SECURITY_CRITICAL_GUILD_EVENTS = frozenset(
    {
        "guild.access.revoked",
        "guild.instance_access.revoked",
        "guild.leave.request",
        "guild.resync.required",
        "relationship.remove",
    }
)
FEDERATION_CAPABILITIES = (
    "dm-history-page/1",
    "e2ee-transport/1",
    "bot-direct-auth/1",
    "group-dm/1",
    "guild-history-sync/1",
    "guild-history-sync/2",
    "member-self-moderation/1",
    "message-search/1",
    "presence/1",
    "profile-by-ref/1",
    "request-nonce/1",
)


def block_covers_domain(block_domain: str, include_subdomains: bool, destination: str) -> bool:
    """Return whether one normalized block rule covers a normalized peer."""

    return destination == block_domain or (
        include_subdomains and destination.endswith(f".{block_domain}")
    )


def federation_policy_holds_event(level: str, event_type: str) -> bool:
    """Return whether a local block must prevent this outbound delivery."""

    return level == "suspend" or (level == "silence" and event_type.startswith("guild."))


def policy_held_retry_at(now: datetime | None = None) -> datetime:
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return current + POLICY_HELD_OUTBOX_DELAY


def canonical_query(query: str) -> str:
    return urlencode(sorted(parse_qsl(query, keep_blank_values=True)))


def canonical_request_target(path: str, query: str = "") -> str:
    normalized = canonical_query(query)
    return f"{path}?{normalized}" if normalized else path


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_json(value: dict[str, Any]) -> bytes:
    validate_json_tree(value, limits=FEDERATION_JSON_LIMITS, label="federation JSON")
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SigningInput:
    method: str
    request_target: str
    origin: str
    destination: str
    timestamp: int
    content_hash: str
    nonce: str | None = None

    def canonical_bytes(self) -> bytes:
        value: dict[str, Any] = {
            "content_sha256": self.content_hash,
            "destination": self.destination,
            "method": self.method.upper(),
            "origin": self.origin,
            "request_target": self.request_target,
            "ts": self.timestamp,
        }
        if self.nonce is not None:
            value["nonce"] = self.nonce
        return canonical_json(value)


def sign_request(signing_input: SigningInput, private_key: Ed25519PrivateKey) -> bytes:
    return private_key.sign(signing_input.canonical_bytes())


def verify_request(
    signing_input: SigningInput, signature: bytes, public_key: Ed25519PublicKey
) -> bool:
    try:
        public_key.verify(signature, signing_input.canonical_bytes())
    except InvalidSignature:
        return False
    return True


def envelope_signing_bytes(envelope: dict[str, Any]) -> bytes:
    return canonical_json({key: value for key, value in envelope.items() if key != "signatures"})


def sign_envelope(envelope: dict[str, Any], private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.sign(envelope_signing_bytes(envelope))).decode("ascii")


def verify_envelope(
    envelope: dict[str, Any], signature: bytes, public_key: Ed25519PublicKey
) -> bool:
    try:
        public_key.verify(signature, envelope_signing_bytes(envelope))
    except InvalidSignature:
        return False
    return True
