"""Small, coercion-free helpers for values received from JSON APIs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import math
import re
from typing import cast


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _present_boolean(
    payload: Mapping[str, object],
    key: str,
    aliases: Sequence[str],
) -> bool | None:
    present = [
        payload[candidate] for candidate in (key, *aliases) if candidate in payload
    ]
    if not present:
        return None
    if any(type(value) is not bool for value in present) or any(
        value is not present[0] for value in present[1:]
    ):
        raise ValueError(f"{key} must be one consistent boolean")
    return cast(bool, present[0])


def strict_payload_bool(
    payload: Mapping[str, object],
    key: str,
    *,
    default: bool,
    aliases: Sequence[str] = (),
) -> bool:
    """Read a JSON boolean without accepting Python truthiness coercions."""

    parsed = _present_boolean(payload, key, aliases)
    return default if parsed is None else parsed


def optional_payload_bool(
    payload: Mapping[str, object],
    key: str,
    *,
    aliases: Sequence[str] = (),
) -> bool | None:
    """Read an optional JSON boolean and reject contradictory aliases."""

    return _present_boolean(payload, key, aliases)


def strict_payload_string(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL characters")
    if minimum is not None and len(value) < minimum:
        raise ValueError(f"{label} must contain at least {minimum} characters")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters")
    return value


def strict_payload_sha256(value: object, label: str) -> str:
    """Read a canonical lowercase SHA-256 digest."""

    parsed = strict_payload_string(value, label)
    if _SHA256.fullmatch(parsed) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return parsed


def _bounded_integer(
    value: int,
    label: str,
    *,
    minimum: int | None,
    maximum: int | None,
) -> int:
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} cannot exceed {maximum}")
    return value


def strict_payload_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an exact JSON integer without accepting booleans or coercions."""

    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return _bounded_integer(
        value,
        label,
        minimum=minimum,
        maximum=maximum,
    )


def strict_payload_decimal_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read a canonical ASCII decimal-string integer from a JSON payload."""

    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise ValueError(f"{label} must be a canonical decimal string")
    return _bounded_integer(
        int(value),
        label,
        minimum=minimum,
        maximum=maximum,
    )


def strict_payload_number(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Read a finite JSON number without accepting strings or booleans."""

    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be a JSON number")
    parsed = float(cast(int | float, value))
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{label} must be at least {minimum:g}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{label} cannot exceed {maximum:g}")
    return parsed


def strict_payload_datetime(value: object, label: str) -> datetime:
    """Read a timezone-aware ISO 8601 datetime without string coercion."""

    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed
