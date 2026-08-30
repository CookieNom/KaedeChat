from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, cast

from app.core.base64url import decode_base64url, encode_base64url
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
# Human, bot-worker, and webhook automation MLS devices share the same
# authenticated envelope shape but retain distinct credential namespaces.
MLS_DEVICE_ID_RE = re.compile(r"(?:ked|kbe|kwe)_[A-Za-z0-9_-]{43}")
INTERACTION_RESPONSE_ENVELOPE_FIELDS = frozenset(
    {
        "interaction_ref",
        "response_ref",
        "sequence",
        "revision",
        "callback_type",
        "attachment_refs",
    }
)
INTERACTION_CONTRACT_ENVELOPE_FIELDS = frozenset(
    {"interaction_contract", "interaction_contract_digest"}
)
RICH_MESSAGE_ENVELOPE_FIELDS = frozenset(
    {
        "author_ref",
        "message_revision",
        "message_attachment_refs",
        "message_mention_everyone",
        "message_mention_refs",
        "message_mention_role_refs",
        "message_mention_user_refs",
        "message_replied_user_ref",
        "message_sticker_refs",
        "message_custom_emoji_refs",
        "referenced_message_ref",
        "rich_payload_digest",
        "forward_projection_version",
        "forward_projection_digest",
        "application_ref",
        "interaction_integration_type",
        "interaction_installation_ref",
        "interaction_installation_revision",
        "view_version",
        "view_persistent",
        "tts",
        "voice_message",
        "message_flags",
        "forwarded_message_ref",
        "forwarded_channel_ref",
        "forward_snapshot_digest",
        "forward_source_projection_digest",
        "forwarded_created_at",
        "forwarded_edited_at",
        "forwarded_flags",
        "forwarded_message_type",
    }
)
CUSTOM_EMOJI_ROUTING_RE = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)
ACCOUNT_VAULT_LEASE_TTL_SECONDS = 120
RELEASE_ACCOUNT_VAULT_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def account_vault_lease_key(user_id: int, user_domain: str) -> str:
    """Return the single distributed-lease key for an account vault."""

    return f"e2ee:account-vault-lease:{user_domain}:{user_id}"


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


def _routing_option_digests(value: object, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError("interaction routing options are invalid")
    for raw in value:
        if (
            not isinstance(raw, str)
            or len(
                _canonical_base64url(
                    raw,
                    "interaction routing option digest",
                    maximum=32,
                )
            )
            != 32
        ):
            raise ValueError("interaction routing option digest is invalid")
    if value != sorted(value) or len(set(value)) != len(value):
        raise ValueError("interaction routing option digests must be sorted and unique")
    return cast(list[str], value)


def _routing_control(value: object, *, modal: bool) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("interaction routing control is invalid")
    control_type = value.get("type")
    custom_id = value.get("custom_id")
    if (
        isinstance(control_type, bool)
        or control_type not in ({4, 19, 21, 22, 23} if modal else {2}) | {3, 5, 6, 7, 8}
        or not isinstance(custom_id, str)
        or not 1 <= len(custom_id) <= 100
    ):
        raise ValueError("interaction routing control identity is invalid")
    if control_type == 2:
        expected = {"type", "custom_id", "disabled"}
        if set(value) != expected or not isinstance(value["disabled"], bool):
            raise ValueError("interaction routing button is invalid")
    elif control_type in {3, 5, 6, 7, 8}:
        expected = {
            "type",
            "custom_id",
            "disabled",
            "min_values",
            "max_values",
            *({"required"} if modal else set()),
            *({"option_value_digests"} if control_type == 3 else set()),
            *({"channel_types"} if control_type == 8 else set()),
        }
        minimum = value.get("min_values")
        maximum = value.get("max_values")
        if (
            set(value) != expected
            or not isinstance(value.get("disabled"), bool)
            or (modal and not isinstance(value.get("required"), bool))
            or (modal and value.get("disabled") is True)
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 0 <= minimum <= maximum <= 25
            or (modal and value.get("required") is True and minimum == 0)
        ):
            raise ValueError("interaction routing select is invalid")
        if control_type == 3 and maximum > len(
            _routing_option_digests(value.get("option_value_digests"), maximum=25)
        ):
            raise ValueError("interaction routing select range is invalid")
        if control_type == 8:
            channel_types = value.get("channel_types")
            if (
                not isinstance(channel_types, list)
                or len(channel_types) > 19
                or any(
                    isinstance(item, bool) or not isinstance(item, int) for item in channel_types
                )
                or len(channel_types) != len(set(channel_types))
            ):
                raise ValueError("interaction routing channel filter is invalid")
    elif control_type == 4:
        expected = {"type", "custom_id", "required", "min_length", "max_length"}
        minimum = value.get("min_length")
        maximum = value.get("max_length")
        if (
            set(value) != expected
            or not isinstance(value.get("required"), bool)
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 0 <= minimum <= maximum <= 4000
        ):
            raise ValueError("interaction routing text input is invalid")
    elif control_type == 19:
        expected = {
            "type",
            "custom_id",
            "required",
            "min_values",
            "max_values",
            "file_types",
        }
        file_types = value.get("file_types")
        minimum = value.get("min_values")
        maximum = value.get("max_values")
        if (
            set(value) != expected
            or not isinstance(value.get("required"), bool)
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 0 <= minimum <= maximum <= 10
            or value.get("required") is True
            and minimum == 0
            or not isinstance(file_types, list)
            or len(file_types) > 10
            or any(not isinstance(item, str) or not item for item in file_types)
            or len(file_types) != len(set(file_types))
        ):
            raise ValueError("interaction routing file input is invalid")
    elif control_type in {21, 22}:
        expected = {
            "type",
            "custom_id",
            "required",
            "option_value_digests",
            *({"min_values", "max_values"} if control_type == 22 else set()),
        }
        options = _routing_option_digests(
            value.get("option_value_digests"),
            maximum=10,
        )
        if set(value) != expected or not isinstance(value.get("required"), bool):
            raise ValueError("interaction routing choice input is invalid")
        if control_type == 22:
            minimum = value.get("min_values")
            maximum = value.get("max_values")
            if (
                isinstance(minimum, bool)
                or not isinstance(minimum, int)
                or isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 0 <= minimum <= maximum <= len(options)
            ):
                raise ValueError("interaction routing choice range is invalid")
    elif set(value) != {"type", "custom_id"}:
        raise ValueError("interaction routing checkbox is invalid")
    return {str(key): item for key, item in value.items()}


def validate_interaction_routing_contract(
    value: object,
    *,
    callback_type: int | None,
) -> dict[str, object]:
    """Validate the only public metadata permitted beside encrypted rich content."""

    validate_json_tree(
        value,
        limits=JsonTreeLimits(
            max_depth=6,
            max_nodes=256,
            max_object_members=12,
            max_array_members=40,
            max_key_bytes=64,
            max_string_bytes=400,
        ),
        label="interaction routing contract",
        allow_floats=False,
    )
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("interaction routing contract is invalid")
    kind = value.get("kind")
    if kind == "message":
        expected = {
            "version",
            "kind",
            "view_timeout_seconds",
            "components",
        }
        has_poll = "poll" in value
        if has_poll:
            expected.add("poll")
        if callback_type not in {None, 4, 7} or set(value) != expected:
            raise ValueError("interaction message routing contract is invalid")
        timeout = value.get("view_timeout_seconds")
        raw_components = value.get("components")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 1 <= timeout <= 86_400
            or not isinstance(raw_components, list)
            or len(raw_components) > 40
            or not raw_components
            and not has_poll
        ):
            raise ValueError("interaction message routing contract is invalid")
        components = [_routing_control(item, modal=False) for item in raw_components]
        if len({str(item["custom_id"]) for item in components}) != len(components):
            raise ValueError("interaction routing custom IDs must be unique")
        if has_poll:
            _validate_poll_routing_contract(value["poll"])
    elif kind == "modal":
        if callback_type != 9 or set(value) != {
            "version",
            "kind",
            "custom_id",
            "components",
        }:
            raise ValueError("interaction modal routing contract is invalid")
        custom_id = value.get("custom_id")
        rows = value.get("components")
        if (
            not isinstance(custom_id, str)
            or not 1 <= len(custom_id) <= 100
            or not isinstance(rows, list)
            or not 1 <= len(rows) <= 5
        ):
            raise ValueError("interaction modal routing contract is invalid")
        components = []
        for row in rows:
            if not isinstance(row, dict) or row.get("type") not in {1, 18}:
                raise ValueError("interaction modal routing row is invalid")
            field_key = "components" if row["type"] == 1 else "component"
            if set(row) != {"type", field_key}:
                raise ValueError("interaction modal routing row is invalid")
            raw_field = row[field_key]
            if field_key == "components":
                if not isinstance(raw_field, list) or len(raw_field) != 1:
                    raise ValueError("interaction modal routing row is invalid")
                raw_field = raw_field[0]
            components.append(_routing_control(raw_field, modal=True))
        if len({str(item["custom_id"]) for item in components}) != len(components):
            raise ValueError("interaction routing custom IDs must be unique")
    else:
        raise ValueError("interaction routing contract kind is invalid")
    return {str(key): item for key, item in value.items()}


def _validate_poll_routing_contract(value: object) -> dict[str, object]:
    """Validate the label-free state required to route an encrypted poll."""

    if not isinstance(value, dict) or set(value) != {
        "version",
        "answer_ids",
        "allow_multiselect",
        "duration_seconds",
        "layout_type",
    }:
        raise ValueError("encrypted poll routing contract is invalid")
    answer_ids = value.get("answer_ids")
    duration = value.get("duration_seconds")
    if (
        value.get("version") != 1
        or not isinstance(answer_ids, list)
        or answer_ids != list(range(1, len(answer_ids) + 1))
        or not 2 <= len(answer_ids) <= 10
        or any(isinstance(item, bool) or not isinstance(item, int) for item in answer_ids)
        or not isinstance(value.get("allow_multiselect"), bool)
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 3_600 <= duration <= 2_764_800
        or duration % 3_600 != 0
        or value.get("layout_type") != 1
    ):
        raise ValueError("encrypted poll routing contract is invalid")
    return {str(key): item for key, item in value.items()}


def interaction_routing_poll(contract: object) -> dict[str, object] | None:
    """Return the authenticated opaque poll contract, if one is present."""

    normalized = validate_interaction_routing_contract(contract, callback_type=None)
    poll = normalized.get("poll")
    return _validate_poll_routing_contract(poll) if poll is not None else None


def interaction_routing_contract_digest(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return encode_base64url(hashlib.sha256(encoded).digest())


def interaction_routing_component(
    contract: object,
    custom_id: str,
) -> dict[str, object]:
    normalized = validate_interaction_routing_contract(contract, callback_type=4)
    matches = [
        item
        for item in cast(list[object], normalized["components"])
        if isinstance(item, dict) and item.get("custom_id") == custom_id
    ]
    if len(matches) != 1 or matches[0].get("disabled") is True:
        raise ValueError("interaction routing component is unavailable")
    return {str(key): item for key, item in matches[0].items()}


def interaction_routing_modal(
    contract: object,
    custom_id: str,
) -> dict[str, object]:
    normalized = validate_interaction_routing_contract(contract, callback_type=9)
    if normalized.get("custom_id") != custom_id:
        raise ValueError("interaction routing modal is unavailable")
    return normalized


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


def classify_channel_encryption_policy_update(
    channel: Any,
    incoming: dict[str, Any],
    *,
    label: str,
) -> Literal["stale", "current", "apply"]:
    """Classify a durable authority projection without retrying old state forever."""

    current = validate_channel_encryption_policy(channel_encryption_policy_payload(channel))
    incoming_generation = int(incoming["generation"])
    current_generation = int(current["generation"])
    if incoming_generation < current_generation:
        return "stale"
    if incoming_generation == current_generation and incoming == current:
        return "current"
    validate_channel_encryption_policy_transition(channel, incoming, label=label)
    return "apply"


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


def validate_e2ee_message_projection(
    envelope: dict[str, Any] | None,
    *,
    message_id: int,
    message_domain: str,
    edited: bool,
) -> None:
    """Bind an MLS application operation to its immutable message row.

    MLS controls are rejected here. Welcome and Commit records are consumed
    only from the separately authenticated durable control log, where the
    room-operation metadata also binds their apply mode.
    """

    if envelope is None:
        return
    if INTERACTION_RESPONSE_ENVELOPE_FIELDS & envelope.keys():
        raise ValueError("interaction response envelope cannot be projected as a message")
    expected_ref = f"{message_id}@{message_domain}"
    operation = envelope.get("operation")
    if edited:
        if operation != "edit" or envelope.get("target_message") != expected_ref:
            raise ValueError("MLS edit does not target its projected message")
        return
    if operation != "create" or "target_message" in envelope:
        raise ValueError("MLS create does not match its projected message")


def validate_e2ee_message_revision(
    envelope: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> None:
    """Require an exact monotonic revision for the authenticated rich protocol."""

    if envelope is None or "rich_payload_digest" not in envelope:
        return
    current = _nonnegative_wire_integer(envelope.get("message_revision"), "message revision")
    prior = (
        _nonnegative_wire_integer(previous.get("message_revision"), "prior message revision")
        if isinstance(previous, dict) and "rich_payload_digest" in previous
        else 1
    )
    if current != prior + 1:
        raise ValueError("encrypted rich message revision is not the next revision")


def _canonical_base64url(value: object, label: str, *, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum * 2:
        raise ValueError(f"{label} is invalid")
    try:
        decoded = decode_base64url(value, maximum=maximum)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not decoded:
        raise ValueError(f"{label} is invalid")
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
    response_fields = INTERACTION_RESPONSE_ENVELOPE_FIELDS
    contract_fields = INTERACTION_CONTRACT_ENVELOPE_FIELDS
    optional = {
        "target_message",
        "attachment_manifest_digest",
        *response_fields,
        *contract_fields,
        *RICH_MESSAGE_ENVELOPE_FIELDS,
    }
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
    present_response_fields = response_fields & value.keys()
    if present_response_fields and present_response_fields != response_fields:
        raise ValueError("interaction response envelope identity is incomplete")
    present_contract_fields = contract_fields & value.keys()
    if present_contract_fields and present_contract_fields != contract_fields:
        raise ValueError("interaction routing contract identity is incomplete")
    if present_response_fields:
        if manifest is not None:
            raise ValueError(
                "interaction response envelopes use attachment refs, not a manifest digest"
            )
        for field in ("interaction_ref", "response_ref"):
            if not isinstance(value[field], str) or not value[field]:
                raise ValueError("interaction response envelope identity is invalid")
        _nonnegative_wire_integer(value["sequence"], "interaction response sequence")
        revision = _nonnegative_wire_integer(value["revision"], "interaction response revision")
        if revision < 1:
            raise ValueError("interaction response revision must be positive")
        if value["callback_type"] not in {4, 7, 8, 9}:
            raise ValueError("interaction response callback type is invalid")
        attachment_refs = value["attachment_refs"]
        if (
            not isinstance(attachment_refs, list)
            or len(attachment_refs) > 10
            or any(not isinstance(item, str) or not item for item in attachment_refs)
            or attachment_refs != sorted(attachment_refs)
            or len(attachment_refs) != len(set(attachment_refs))
        ):
            raise ValueError("interaction response attachments are invalid")
    present_rich_fields = RICH_MESSAGE_ENVELOPE_FIELDS & value.keys()
    if present_rich_fields and present_rich_fields != RICH_MESSAGE_ENVELOPE_FIELDS:
        raise ValueError("encrypted rich message identity is incomplete")
    if present_rich_fields:
        if present_response_fields:
            raise ValueError("interaction response cannot use an ordinary message identity")
        _validate_rich_message_envelope(value)
    callback_type = value.get("callback_type") if present_response_fields else None
    if present_contract_fields:
        contract = validate_interaction_routing_contract(
            value["interaction_contract"],
            callback_type=cast(int | None, callback_type),
        )
        digest = value["interaction_contract_digest"]
        _canonical_base64url(digest, "interaction routing contract digest", maximum=32)
        if digest != interaction_routing_contract_digest(contract):
            raise ValueError("interaction routing contract digest is invalid")
    if present_response_fields:
        if callback_type == 9 and not present_contract_fields:
            raise ValueError("encrypted modals require an interaction routing contract")
        if callback_type == 8 and present_contract_fields:
            raise ValueError("autocomplete responses cannot carry a routing contract")


def _qualified_routing_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not 3 <= len(value) <= 320 or "@" not in value:
        raise ValueError(f"{label} is invalid")
    identifier, domain = value.rsplit("@", 1)
    _nonnegative_wire_integer(identifier, f"{label} identifier")
    if not domain or len(domain) > 253 or domain != domain.lower() or "\x00" in domain:
        raise ValueError(f"{label} is invalid")
    return value


def _validate_custom_emoji_routing_token(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("encrypted message custom emoji reference is invalid")
    match = CUSTOM_EMOJI_ROUTING_RE.fullmatch(value)
    if match is None:
        raise ValueError("encrypted message custom emoji reference is invalid")
    domain = match.group("domain")
    if domain != domain.rstrip(".").lower() or len(domain) > 253:
        raise ValueError("encrypted message custom emoji reference is invalid")
    _nonnegative_wire_integer(match.group("id"), "encrypted message custom emoji identifier")
    return value


def _validate_sorted_routing_values(
    value: object,
    *,
    label: str,
    maximum: int,
) -> list[object]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError(f"{label} are invalid")
    if value != sorted(value) or len(value) != len(set(value)):
        raise ValueError(f"{label} must be sorted and unique")
    return value


def _validate_rich_message_envelope(value: dict[str, Any]) -> None:
    """Validate the public identity bound to an encrypted rich message body."""

    _qualified_routing_ref(value["author_ref"], "encrypted message author reference")
    revision = _nonnegative_wire_integer(value["message_revision"], "message revision")
    if revision < 1:
        raise ValueError("message revision must be positive")
    operation = value["operation"]
    if (operation == "create" and revision != 1) or (operation == "edit" and revision <= 1):
        raise ValueError("encrypted message revision does not match its operation")
    attachment_refs = value["message_attachment_refs"]
    if (
        not isinstance(attachment_refs, list)
        or len(attachment_refs) > 10
        or any(not isinstance(item, str) for item in attachment_refs)
        or attachment_refs != sorted(attachment_refs)
        or len(attachment_refs) != len(set(attachment_refs))
    ):
        raise ValueError("encrypted message attachment references are invalid")
    for attachment_ref in attachment_refs:
        _qualified_routing_ref(attachment_ref, "encrypted message attachment reference")
    mention_refs = value["message_mention_refs"]
    if (
        not isinstance(mention_refs, list)
        or len(mention_refs) > 5_000
        or any(not isinstance(item, str) for item in mention_refs)
        or mention_refs != sorted(mention_refs)
        or len(mention_refs) != len(set(mention_refs))
    ):
        raise ValueError("encrypted message mention references are invalid")
    for mention_ref in mention_refs:
        _qualified_routing_ref(mention_ref, "encrypted message mention reference")
    for field_name, label in (
        ("message_mention_user_refs", "encrypted message user mention references"),
        ("message_mention_role_refs", "encrypted message role mention references"),
    ):
        selected_refs = _validate_sorted_routing_values(
            value[field_name],
            label=label,
            maximum=100,
        )
        for selected_ref in selected_refs:
            _qualified_routing_ref(selected_ref, label[:-1])
    if not isinstance(value["message_mention_everyone"], bool):
        raise ValueError("encrypted message broad mention intent is invalid")
    replied_user_ref = value["message_replied_user_ref"]
    if replied_user_ref is not None:
        _qualified_routing_ref(
            replied_user_ref,
            "encrypted message replied-user reference",
        )
    sticker_refs = _validate_sorted_routing_values(
        value["message_sticker_refs"],
        label="encrypted message sticker references",
        maximum=9,
    )
    for sticker_ref in sticker_refs:
        _qualified_routing_ref(sticker_ref, "encrypted message sticker reference")
    custom_emoji_refs = _validate_sorted_routing_values(
        value["message_custom_emoji_refs"],
        label="encrypted message custom emoji references",
        maximum=256,
    )
    for custom_emoji_ref in custom_emoji_refs:
        _validate_custom_emoji_routing_token(custom_emoji_ref)
    referenced_message_ref = value["referenced_message_ref"]
    if referenced_message_ref is not None:
        _qualified_routing_ref(
            referenced_message_ref,
            "encrypted referenced message reference",
        )
    _canonical_base64url(
        value["rich_payload_digest"],
        "encrypted rich payload digest",
        maximum=32,
    )
    manifest = value.get("attachment_manifest_digest")
    if bool(attachment_refs) != (manifest is not None):
        raise ValueError("encrypted message attachment manifest identity is incomplete")

    application_ref = value["application_ref"]
    integration_type = value["interaction_integration_type"]
    installation_ref = value["interaction_installation_ref"]
    installation_revision = value["interaction_installation_revision"]
    lineage = (application_ref, integration_type, installation_ref, installation_revision)
    if any(item is not None for item in lineage):
        if any(item is None for item in lineage):
            raise ValueError("encrypted rich message application lineage is incomplete")
        _qualified_routing_ref(application_ref, "encrypted message application reference")
        _qualified_routing_ref(installation_ref, "encrypted message installation reference")
        if integration_type not in {"guild_install", "user_install", "dm_capability"}:
            raise ValueError("encrypted message integration type is invalid")
        if (
            _nonnegative_wire_integer(
                installation_revision,
                "encrypted message installation revision",
            )
            < 1
        ):
            raise ValueError("encrypted message installation revision must be positive")

    view_version = _nonnegative_wire_integer(value["view_version"], "message view version")
    if not isinstance(value["view_persistent"], bool):
        raise ValueError("encrypted message view persistence is invalid")
    if not isinstance(value["tts"], bool) or not isinstance(value["voice_message"], bool):
        raise ValueError("encrypted message delivery markers are invalid")
    flags = value["message_flags"]
    if isinstance(flags, bool) or not isinstance(flags, int) or not 0 <= flags <= 2_147_483_647:
        raise ValueError("encrypted message flags are invalid")
    if value["tts"] and value["voice_message"]:
        raise ValueError("encrypted voice messages cannot request text-to-speech")
    if value["voice_message"] and len(attachment_refs) != 1:
        raise ValueError("encrypted voice messages require one attachment")

    forwarded_message_ref = value["forwarded_message_ref"]
    forwarded_channel_ref = value["forwarded_channel_ref"]
    forward_snapshot_digest = value["forward_snapshot_digest"]
    forward_source_projection_digest = value["forward_source_projection_digest"]
    forwarded_created_at = value["forwarded_created_at"]
    forwarded_edited_at = value["forwarded_edited_at"]
    forwarded_flags = value["forwarded_flags"]
    forwarded_message_type = value["forwarded_message_type"]
    forward_lineage = (
        forwarded_message_ref,
        forwarded_channel_ref,
        forward_snapshot_digest,
        forward_source_projection_digest,
        forwarded_created_at,
        forwarded_flags,
        forwarded_message_type,
    )
    if any(item is not None for item in forward_lineage):
        if any(item is None for item in forward_lineage):
            raise ValueError("encrypted forward lineage is incomplete")
        _qualified_routing_ref(
            forwarded_message_ref,
            "encrypted forwarded message reference",
        )
        _qualified_routing_ref(
            forwarded_channel_ref,
            "encrypted forwarded channel reference",
        )
        _canonical_base64url(
            forward_snapshot_digest,
            "encrypted forward snapshot digest",
            maximum=32,
        )
        _canonical_base64url(
            forward_source_projection_digest,
            "encrypted forward source projection digest",
            maximum=32,
        )
        try:
            created_at = datetime.fromisoformat(forwarded_created_at)
            edited_at = (
                datetime.fromisoformat(forwarded_edited_at)
                if forwarded_edited_at is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("encrypted forward timestamps are invalid") from exc
        if (
            created_at.tzinfo is None
            or edited_at is not None
            and (edited_at.tzinfo is None or edited_at < created_at)
            or isinstance(forwarded_message_type, bool)
            or forwarded_message_type not in {0, 19, 20, 23}
            or isinstance(forwarded_flags, bool)
            or not isinstance(forwarded_flags, int)
            or forwarded_flags & ~((1 << 2) | (1 << 13) | (1 << 15))
        ):
            raise ValueError("encrypted forward metadata is invalid")
    elif forwarded_edited_at is not None:
        raise ValueError("encrypted forward lineage is incomplete")

    contract = value.get("interaction_contract")
    has_controls = False
    if contract is not None:
        normalized = validate_interaction_routing_contract(contract, callback_type=None)
        has_controls = bool(normalized.get("components"))
        has_poll = normalized.get("poll") is not None
    else:
        has_poll = False
    forward_projection_version = value["forward_projection_version"]
    forward_projection_digest = value["forward_projection_digest"]
    if has_poll:
        if forward_projection_version is not None or forward_projection_digest is not None:
            raise ValueError("encrypted polls cannot be forwarded")
    else:
        if forward_projection_version != 2:
            raise ValueError("encrypted forward projection version is invalid")
        _canonical_base64url(
            forward_projection_digest,
            "encrypted forward projection digest",
            maximum=32,
        )
    if has_controls:
        if application_ref is None or view_version < 1:
            raise ValueError("encrypted interactive message lineage is invalid")
    elif (operation == "create" and view_version != 0) or value["view_persistent"]:
        raise ValueError("encrypted message has view metadata without interactive controls")


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
