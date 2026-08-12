from __future__ import annotations

import json
from typing import Any, cast

from app.core.json_limits import JsonTreeLimits, validate_json_tree

MAX_E2EE_ENVELOPE_BYTES = 64 * 1024
MAX_E2EE_ENVELOPE_VERSION = (1 << 31) - 1
MAX_E2EE_ENVELOPE_DEPTH = 16
MAX_E2EE_ENVELOPE_NODES = 4096
MAX_E2EE_OBJECT_MEMBERS = 256
MAX_E2EE_ARRAY_MEMBERS = 1024
MAX_E2EE_KEY_BYTES = 256

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

    The server deliberately does not prescribe a cryptographic protocol here.
    It only provides stable, bounded transport so a future MLS-style guild or
    double-ratchet DM protocol can evolve without a message-schema migration.
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
    return cast(dict[str, Any], normalized)
