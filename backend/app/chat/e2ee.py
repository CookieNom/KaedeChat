from __future__ import annotations

import base64
import json
import re
from typing import Any, cast

from app.core.json_limits import JsonTreeLimits, validate_json_tree

MAX_E2EE_ENVELOPE_BYTES = 64 * 1024
MAX_E2EE_ENVELOPE_VERSION = (1 << 31) - 1
MAX_E2EE_ENVELOPE_DEPTH = 16
MAX_E2EE_ENVELOPE_NODES = 4096
MAX_E2EE_OBJECT_MEMBERS = 256
MAX_E2EE_ARRAY_MEMBERS = 1024
MAX_E2EE_KEY_BYTES = 256
E2EE_PROTOCOL_MLS_10 = "mls10"
E2EE_SUITE_MLS_128 = "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519"
E2EE_ROOM_STATES = frozenset(
    {"plaintext", "legacy", "proposed", "activating", "active", "rekeying", "failed"}
)
MLS_DEVICE_ID_RE = re.compile(r"ked_[A-Za-z0-9_-]{43}")


class MessageEncryptionPolicyError(ValueError):
    """A message body would violate the authoritative room policy."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _nonnegative_wire_integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative decimal integer")
    if isinstance(value, int):
        parsed = value
    elif (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and (len(value) == 1 or not value.startswith("0"))
    ):
        parsed = int(value)
    else:
        raise ValueError(f"{label} must be a non-negative decimal integer")
    if parsed < 0 or parsed > (1 << 63) - 1:
        raise ValueError(f"{label} is outside the database range")
    return parsed


def validate_channel_encryption_policy(value: object) -> dict[str, Any]:
    """Validate downgrade-sensitive room policy transported by federation."""

    if not isinstance(value, dict):
        raise ValueError("channel encryption policy must be an object")
    mode = value.get("mode", "plaintext")
    state = value.get("state", "plaintext")
    if mode not in {"plaintext", "e2ee"} or state not in E2EE_ROOM_STATES:
        raise ValueError("channel encryption policy mode or state is invalid")
    generation = _nonnegative_wire_integer(value.get("generation", "0"), "policy generation")
    protocol = value.get("protocol")
    suite = value.get("suite")
    group_id = value.get("group_id")
    epoch_raw = value.get("epoch")
    epoch = (
        _nonnegative_wire_integer(epoch_raw, "encryption epoch") if epoch_raw is not None else None
    )
    for field, maximum in ((protocol, 32), (suite, 96), (group_id, 128)):
        if field is not None and (not isinstance(field, str) or not 1 <= len(field) <= maximum):
            raise ValueError("channel encryption policy identifier is invalid")
    if generation == 0:
        if state not in {"plaintext", "legacy"}:
            raise ValueError("an activated encryption policy requires a positive generation")
        expected_mode = "e2ee" if state == "legacy" else "plaintext"
        if mode != expected_mode or any(
            item is not None for item in (protocol, suite, group_id, epoch)
        ):
            raise ValueError("legacy channel encryption policy is inconsistent")
    else:
        if state == "plaintext" or state == "legacy":
            raise ValueError("a generated encryption policy has an invalid state")
        if state in {"proposed", "failed"} and mode == "plaintext":
            if mode != "plaintext" or epoch is not None:
                raise ValueError("proposed channel encryption policy is inconsistent")
        elif mode != "e2ee" or epoch is None:
            raise ValueError("active channel encryption policy is inconsistent")
        if protocol != E2EE_PROTOCOL_MLS_10 or suite != E2EE_SUITE_MLS_128 or group_id is None:
            raise ValueError("channel encryption protocol is unsupported")
    return {
        "mode": mode,
        "state": state,
        "generation": generation,
        "protocol": protocol,
        "suite": suite,
        "group_id": group_id,
        "epoch": epoch,
    }


def channel_encryption_policy_payload(channel: Any) -> dict[str, object | None]:
    mode = getattr(channel, "encryption_mode", None) or "plaintext"
    state = getattr(channel, "encryption_state", None) or (
        "legacy" if mode == "e2ee" else "plaintext"
    )
    generation = getattr(channel, "encryption_policy_generation", None) or 0
    epoch = getattr(channel, "encryption_epoch", None)
    return {
        "mode": mode,
        "state": state,
        "generation": str(generation),
        "protocol": getattr(channel, "encryption_protocol", None),
        "suite": getattr(channel, "encryption_suite", None),
        "group_id": getattr(channel, "encryption_group_id", None),
        "epoch": str(epoch) if epoch is not None else None,
    }


def validate_channel_encryption_policy_transition(
    channel: Any,
    incoming: dict[str, Any],
    *,
    label: str,
) -> None:
    """Reject stale, equivocated, or downgrade policy state from an authority."""

    current = validate_channel_encryption_policy(channel_encryption_policy_payload(channel))
    incoming_generation = int(incoming["generation"])
    current_generation = int(current["generation"])
    if incoming_generation < current_generation:
        raise ValueError(f"{label} encryption policy generation regressed")
    if incoming_generation == current_generation and incoming != current:
        immutable_fields = ("protocol", "suite", "group_id")
        if any(incoming[field] != current[field] for field in immutable_fields):
            raise ValueError(f"{label} encryption policy generation was equivocated")
        allowed_same_generation = {
            ("proposed", "activating"),
            ("proposed", "active"),
            ("proposed", "failed"),
            ("activating", "active"),
            ("activating", "failed"),
            ("active", "rekeying"),
        }
        if (str(current["state"]), str(incoming["state"])) not in allowed_same_generation:
            raise ValueError(f"{label} encryption policy generation was equivocated")
    if current["mode"] == "e2ee" and incoming["mode"] != "e2ee":
        raise ValueError(f"{label} encryption policy attempted a downgrade")


E2EE_JSON_LIMITS = JsonTreeLimits(
    max_depth=MAX_E2EE_ENVELOPE_DEPTH,
    max_nodes=MAX_E2EE_ENVELOPE_NODES,
    max_object_members=MAX_E2EE_OBJECT_MEMBERS,
    max_array_members=MAX_E2EE_ARRAY_MEMBERS,
    max_key_bytes=MAX_E2EE_KEY_BYTES,
    max_string_bytes=MAX_E2EE_ENVELOPE_BYTES,
)


def validate_e2ee_envelope(value: object) -> dict[str, Any] | None:
    """Validate a versioned opaque client-encryption envelope.

    Version 1 remains bounded legacy opaque transport. Version 2 is the
    prescribed MLS 1.0 application envelope and receives strict public-context
    validation before storage or federation.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("encrypted message envelope must be an object")
    version = value.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 1 <= version <= MAX_E2EE_ENVELOPE_VERSION
    ):
        raise ValueError("encrypted message envelope requires a positive version")
    validate_json_tree(value, limits=E2EE_JSON_LIMITS, label="encrypted message envelope")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("encrypted message envelope is not valid JSON") from exc
    if len(encoded) > MAX_E2EE_ENVELOPE_BYTES:
        raise ValueError("encrypted message envelope is too large")
    # A JSON round-trip removes caller-owned aliases and normalizes tuples to
    # arrays before the object is persisted or relayed to another instance.
    normalized = json.loads(encoded)
    normalized = cast(dict[str, Any], normalized)
    if version == 2:
        _validate_mls_application_envelope(normalized)
    return normalized


def _canonical_base64url(value: object, label: str, *, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum * 2:
        raise ValueError(f"{label} is invalid")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not decoded or len(decoded) > maximum:
        raise ValueError(f"{label} is invalid")
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ValueError(f"{label} is not canonical URL-safe base64")
    return decoded


def _validate_mls_application_envelope(value: dict[str, Any]) -> None:
    required = {
        "version",
        "protocol",
        "suite",
        "group_id",
        "policy_generation",
        "epoch",
        "sender_device_id",
        "operation",
        "ciphertext",
    }
    optional = {"target_message", "attachment_manifest_digest"}
    if not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("MLS application envelope fields are invalid")
    if value["protocol"] != E2EE_PROTOCOL_MLS_10 or value["suite"] != E2EE_SUITE_MLS_128:
        raise ValueError("MLS application envelope suite is unsupported")
    _canonical_base64url(value["group_id"], "MLS group ID", maximum=128)
    _canonical_base64url(value["ciphertext"], "MLS ciphertext", maximum=60 * 1024)
    generation = _nonnegative_wire_integer(value["policy_generation"], "policy generation")
    epoch = _nonnegative_wire_integer(value["epoch"], "MLS epoch")
    if generation == 0:
        raise ValueError("MLS policy generation must be positive")
    if epoch < 0:
        raise ValueError("MLS epoch must be non-negative")
    if (
        not isinstance(value["sender_device_id"], str)
        or MLS_DEVICE_ID_RE.fullmatch(value["sender_device_id"]) is None
    ):
        raise ValueError("MLS sender device ID is invalid")
    operation = value["operation"]
    target = value.get("target_message")
    if operation in {"create", "welcome", "commit"}:
        if target is not None:
            raise ValueError("MLS envelope operation cannot have an edit target")
    elif operation == "edit":
        if not isinstance(target, str) or not target:
            raise ValueError("MLS edit envelope requires a target message")
    else:
        raise ValueError("MLS application operation is invalid")
    manifest = value.get("attachment_manifest_digest")
    if manifest is not None:
        _canonical_base64url(manifest, "attachment manifest digest", maximum=32)


def validate_message_encryption_policy(
    encryption_mode: str,
    *,
    content: object,
    e2ee: object,
    attachment_count: int = 0,
    deleted: bool = False,
    policy_generation: int = 0,
    policy_epoch: int | None = None,
    policy_group_id: str | None = None,
) -> None:
    """Reject mixed-mode writes before they reach storage or federation.

    Historical rows may retain the policy under which they were created, but
    every new body or edit must match the channel's current authoritative
    policy. Attachment rows are checked separately because their ciphertext
    policy is stored alongside each object rather than inside this message body.
    """

    if encryption_mode not in {"plaintext", "e2ee"}:
        raise MessageEncryptionPolicyError("MESSAGE_ENCRYPTION_POLICY_INVALID")
    if deleted:
        if content is not None or e2ee is not None:
            raise MessageEncryptionPolicyError("DELETED_MESSAGE_BODY_FORBIDDEN")
        return
    if encryption_mode == "plaintext":
        if e2ee is not None:
            raise MessageEncryptionPolicyError("E2EE_NOT_ENABLED")
        return
    if content is not None or e2ee is None:
        raise MessageEncryptionPolicyError("E2EE_ENVELOPE_REQUIRED")
    if policy_generation > 0:
        if not isinstance(e2ee, dict) or e2ee.get("version") != 2:
            raise MessageEncryptionPolicyError("E2EE_MLS_ENVELOPE_REQUIRED")
        if (
            e2ee.get("policy_generation") != str(policy_generation)
            or e2ee.get("epoch") != str(policy_epoch)
            or e2ee.get("group_id") != policy_group_id
        ):
            raise MessageEncryptionPolicyError("E2EE_POLICY_CONTEXT_MISMATCH")
