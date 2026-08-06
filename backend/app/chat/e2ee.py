from __future__ import annotations

import json
from typing import Any

MAX_E2EE_ENVELOPE_BYTES = 64 * 1024


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
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("encrypted message envelope requires a positive version")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_E2EE_ENVELOPE_BYTES:
        raise ValueError("encrypted message envelope is too large")
    return dict(value)
