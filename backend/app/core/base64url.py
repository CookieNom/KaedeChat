from __future__ import annotations

import base64


def encode_base64url(value: bytes) -> str:
    """Encode bytes using the one canonical unpadded base64url form."""

    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_base64url(
    value: str,
    *,
    size: int | None = None,
    maximum: int | None = None,
) -> bytes:
    """Decode canonical unpadded base64url with optional byte bounds."""

    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("value is not canonical URL-safe base64") from exc
    if encode_base64url(decoded) != value:
        raise ValueError("value is not canonical URL-safe base64")
    if size is not None and len(decoded) != size:
        raise ValueError(f"value must decode to exactly {size} bytes")
    if maximum is not None and len(decoded) > maximum:
        raise ValueError(f"value must decode to at most {maximum} bytes")
    return decoded
