from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.chat.e2ee import validate_channel_encryption_policy
from app.core.dm import group_dm_key
from app.core.json_limits import FEDERATION_JSON_LIMITS, validate_json_tree
from app.core.settings import DOMAIN_RE

POLICY_HELD_OUTBOX_PREFIX = "held by local federation block:"
POLICY_HELD_OUTBOX_DELAY = timedelta(days=36_500)
BLOCK_POLICY_ADVISORY_NAME = "kaede-instance-blocks"
SECURITY_CRITICAL_GUILD_EVENTS = frozenset(
    {
        "guild.access.revoked",
        "guild.instance_access.revoked",
        "guild.leave.request",
        "guild.media.delete.request",
        "guild.resync.required",
        "media.delete",
        "relationship.remove",
        "e2ee.device-list.changed",
        "e2ee.room-policy.changed",
    }
)
FEDERATION_CAPABILITIES = (
    "dm-history-page/1",
    "e2ee-mls/1",
    "e2ee-media/1",
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


def terminal_room_generation(envelope: dict[str, Any]) -> int:
    content = envelope.get("content")
    raw_generation = content.get("_terminal_generation") if isinstance(content, dict) else None
    if (
        not isinstance(raw_generation, str)
        or not raw_generation.isascii()
        or not raw_generation.isdecimal()
        or (len(raw_generation) > 1 and raw_generation.startswith("0"))
    ):
        raise ValueError("terminal-room generation is invalid")
    generation = int(raw_generation)
    if not 1 <= generation <= (1 << 63) - 1:
        raise ValueError("terminal-room generation is invalid")
    return generation


def terminal_room_event_ref(envelope: dict[str, Any]) -> tuple[str, int, str] | None:
    """Return the room named by an exact authoritative terminal-room event.

    These two event shapes are durable deletion controls rather than ordinary
    membership updates.  Keep this predicate deliberately strict because it
    is also used to grant old-timestamp acceptance and non-expiring delivery.
    Origin-signature, destination, and retained-route checks still run in the
    inbox processor; terminal truth must not depend on a stale owner/member
    projection at a destination that already lost room visibility.
    """

    if set(envelope) != {
        "event_id",
        "origin",
        "type",
        "ts",
        "actor",
        "context",
        "content",
        "signatures",
    }:
        return None
    event_type = envelope.get("type")
    origin = envelope.get("origin")
    content = envelope.get("content")
    context = envelope.get("context")
    signatures = envelope.get("signatures")
    if not isinstance(origin, str) or not origin or not isinstance(content, dict):
        return None
    if (
        origin != origin.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(origin)
        or not isinstance(signatures, dict)
        or set(signatures) != {origin}
        or not isinstance(signatures.get(origin), dict)
        or len(signatures[origin]) != 1
    ):
        return None

    if event_type == "guild.instance_access.revoked":
        actor = envelope.get("actor")
        if (
            not isinstance(context, dict)
            or set(context) != {"guild_id", "guild_domain"}
            or set(content) != {"target_domain", "reason", "_terminal_generation"}
            or context.get("guild_domain") != origin
            or content.get("reason") != "guild_deleted"
            or not isinstance(content.get("target_domain"), str)
            or not content["target_domain"]
            or content["target_domain"] != content["target_domain"].rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(content["target_domain"])
            or not isinstance(actor, dict)
            or set(actor) != {"id", "domain"}
            or not isinstance(actor.get("id"), str)
            or not actor["id"].isascii()
            or not actor["id"].isdecimal()
            or (len(actor["id"]) > 1 and actor["id"].startswith("0"))
            or int(actor["id"]) > (1 << 63) - 1
            or actor.get("domain") != origin
        ):
            return None
        raw_id = context.get("guild_id")
        room_kind = "guild"
    elif event_type == "dm.group.state":
        actor = envelope.get("actor")
        conversation = content.get("conversation")
        participants = content.get("participants")
        if (
            not isinstance(actor, dict)
            or set(actor) != {"id", "domain"}
            or not isinstance(actor.get("id"), str)
            or not actor["id"].isascii()
            or not actor["id"].isdecimal()
            or (len(actor["id"]) > 1 and actor["id"].startswith("0"))
            or not isinstance(actor.get("domain"), str)
            or not actor["domain"]
            or actor["domain"] != actor["domain"].rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(actor["domain"])
            or int(actor["id"]) > (1 << 63) - 1
            or not isinstance(conversation, dict)
            or set(content) != {"conversation", "participants", "_terminal_generation"}
            or set(conversation)
            != {
                "id",
                "origin_domain",
                "pair_key",
                "type",
                "authority_domain",
                "owner",
                "name",
                "state_version",
                "deleted",
                "encryption_policy",
            }
            or context != {}
            or conversation.get("deleted") is not True
            or conversation.get("type") != "group"
            or conversation.get("origin_domain") != origin
            or conversation.get("authority_domain") != origin
            or participants != []
        ):
            return None
        raw_id = conversation.get("id")
        room_kind = "group_dm"
    else:
        return None

    if (
        not isinstance(raw_id, str)
        or not raw_id.isascii()
        or not raw_id.isdecimal()
        or (len(raw_id) > 1 and raw_id.startswith("0"))
    ):
        return None
    room_id = int(raw_id)
    if not 0 <= room_id <= (1 << 63) - 1:
        return None
    try:
        terminal_room_generation(envelope)
    except ValueError:
        return None
    if room_kind == "group_dm":
        group_conversation = cast(dict[str, Any], conversation)
        owner = group_conversation.get("owner")
        name = group_conversation.get("name")
        state_version = group_conversation.get("state_version")
        encryption_policy = group_conversation.get("encryption_policy")
        if (
            group_conversation.get("pair_key") != group_dm_key(origin, room_id)
            or not isinstance(owner, dict)
            or set(owner) != {"id", "origin_domain"}
            or not isinstance(owner.get("id"), str)
            or not owner["id"].isascii()
            or not owner["id"].isdecimal()
            or (len(owner["id"]) > 1 and owner["id"].startswith("0"))
            or int(owner["id"]) > (1 << 63) - 1
            or not isinstance(owner.get("origin_domain"), str)
            or owner["origin_domain"] != owner["origin_domain"].rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(owner["origin_domain"])
            or (
                name is not None
                and (not isinstance(name, str) or not 1 <= len(name) <= 100 or name != name.strip())
            )
            or not isinstance(state_version, str)
            or not state_version.isascii()
            or not state_version.isdecimal()
            or (len(state_version) > 1 and state_version.startswith("0"))
            or not 1 <= int(state_version) <= (1 << 63) - 1
            or not isinstance(encryption_policy, dict)
            or set(encryption_policy)
            != {"mode", "state", "generation", "protocol", "suite", "group_id", "epoch"}
            or not isinstance(encryption_policy.get("generation"), str)
            or (
                encryption_policy.get("epoch") is not None
                and not isinstance(encryption_policy.get("epoch"), str)
            )
        ):
            return None
        try:
            validate_channel_encryption_policy(encryption_policy)
        except ValueError:
            return None
    return room_kind, room_id, origin


def durable_terminal_room_event(envelope: dict[str, Any]) -> bool:
    """Whether an envelope is an exact terminal-room deletion control."""

    return terminal_room_event_ref(envelope) is not None


def guild_media_delete_request_ref(
    envelope: dict[str, Any],
) -> tuple[int, str, int, str, int, str, int] | None:
    """Parse one exact authority-signed request to delete origin-owned media.

    A guild home can delete a message after the attachment's origin instance
    has left the guild.  The ordinary sequenced mutation is intentionally not
    disclosed to that former member, so this minimal removal-only control is
    addressed directly to the attachment home.  Its exact shape is used to
    grant durable delivery and old-timestamp acceptance; semantic authority is
    still bound to the recipient's retained guild/message/media route.
    """

    if (
        set(envelope)
        != {
            "event_id",
            "origin",
            "type",
            "ts",
            "actor",
            "context",
            "content",
            "signatures",
        }
        or envelope.get("type") != "guild.media.delete.request"
    ):
        return None
    origin = envelope.get("origin")
    actor = envelope.get("actor")
    context = envelope.get("context")
    content = envelope.get("content")
    signatures = envelope.get("signatures")
    if (
        not isinstance(origin, str)
        or origin != origin.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(origin)
        or not isinstance(actor, dict)
        or set(actor) != {"id", "domain"}
        or actor.get("domain") != origin
        or not isinstance(actor.get("id"), str)
        or not actor["id"].isascii()
        or not actor["id"].isdecimal()
        or (len(actor["id"]) > 1 and actor["id"].startswith("0"))
        or int(actor["id"]) > (1 << 63) - 1
        or context != {}
        or not isinstance(content, dict)
        or set(content) != {"guild", "message", "attachment", "deleted_at", "_deletion_generation"}
        or not isinstance(signatures, dict)
        or set(signatures) != {origin}
        or not isinstance(signatures.get(origin), dict)
        or len(signatures[origin]) != 1
    ):
        return None

    def exact_ref(raw: object, *, expected_domain: str | None = None) -> tuple[int, str] | None:
        if not isinstance(raw, dict) or set(raw) != {"id", "origin_domain"}:
            return None
        raw_id = raw.get("id")
        domain = raw.get("origin_domain")
        if (
            not isinstance(raw_id, str)
            or not raw_id.isascii()
            or not raw_id.isdecimal()
            or (len(raw_id) > 1 and raw_id.startswith("0"))
            or int(raw_id) > (1 << 63) - 1
            or not isinstance(domain, str)
            or domain != domain.rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(domain)
            or (expected_domain is not None and domain != expected_domain)
        ):
            return None
        return int(raw_id), domain

    guild_ref = exact_ref(content.get("guild"), expected_domain=origin)
    message_ref = exact_ref(content.get("message"), expected_domain=origin)
    attachment_ref = exact_ref(content.get("attachment"))
    raw_generation = content.get("_deletion_generation")
    raw_deleted_at = content.get("deleted_at")
    if (
        guild_ref is None
        or message_ref is None
        or attachment_ref is None
        or attachment_ref[1] == origin
        or not isinstance(raw_generation, str)
        or not raw_generation.isascii()
        or not raw_generation.isdecimal()
        or (len(raw_generation) > 1 and raw_generation.startswith("0"))
        or not 1 <= int(raw_generation) <= (1 << 63) - 1
        or not isinstance(raw_deleted_at, str)
    ):
        return None
    try:
        deleted_at = datetime.fromisoformat(raw_deleted_at)
    except ValueError:
        return None
    if deleted_at.tzinfo is None or deleted_at.isoformat() != raw_deleted_at:
        return None
    return (
        guild_ref[0],
        guild_ref[1],
        message_ref[0],
        message_ref[1],
        attachment_ref[0],
        attachment_ref[1],
        int(raw_generation),
    )


def durable_guild_media_delete_request(envelope: dict[str, Any]) -> bool:
    return guild_media_delete_request_ref(envelope) is not None


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
