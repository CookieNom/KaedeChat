from __future__ import annotations

import base64


def encode_base64url(value: bytes) -> str:
    """Return the canonical unpadded URL-safe base64 representation."""

    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
