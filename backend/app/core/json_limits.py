from __future__ import annotations

import json
from dataclasses import dataclass

MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


@dataclass(frozen=True, slots=True)
class JsonTreeLimits:
    """Resource limits for an already decoded JSON-compatible value."""

    max_depth: int
    max_nodes: int
    max_object_members: int
    max_array_members: int
    max_key_bytes: int
    max_string_bytes: int


FEDERATION_JSON_LIMITS = JsonTreeLimits(
    max_depth=24,
    max_nodes=16_384,
    max_object_members=1024,
    max_array_members=4096,
    max_key_bytes=256,
    max_string_bytes=1024 * 1024,
)


def validate_json_tree(
    value: object,
    *,
    limits: JsonTreeLimits,
    label: str = "JSON value",
) -> None:
    """Validate JSON shape iteratively before canonicalization or persistence.

    JSON received from the wire cannot contain aliases or cycles, so rejecting
    those shapes also makes this helper safe for values constructed internally.
    The iterative walk avoids consuming the Python call stack on hostile input.
    """

    stack: list[tuple[object, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    nodes = 0

    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            raise ValueError(f"{label} contains too many values")
        if depth > limits.max_depth:
            raise ValueError(f"{label} exceeds the nesting depth limit")

        if isinstance(item, dict):
            identity = id(item)
            if identity in seen_containers:
                raise ValueError(f"{label} contains a cyclic or shared container")
            seen_containers.add(identity)
            if len(item) > limits.max_object_members:
                raise ValueError(f"{label} object contains too many members")
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} object keys must be strings")
                _validate_string(
                    key,
                    max_bytes=limits.max_key_bytes,
                    label=f"{label} object key",
                )
                stack.append((child, depth + 1))
            continue

        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in seen_containers:
                raise ValueError(f"{label} contains a cyclic or shared container")
            seen_containers.add(identity)
            if len(item) > limits.max_array_members:
                raise ValueError(f"{label} array contains too many items")
            stack.extend((child, depth + 1) for child in item)
            continue

        if isinstance(item, str):
            _validate_string(item, max_bytes=limits.max_string_bytes, label=f"{label} string")
            continue
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            if not -MAX_SAFE_JSON_INTEGER <= item <= MAX_SAFE_JSON_INTEGER:
                raise ValueError(f"{label} integer is outside the supported range")
            continue
        if isinstance(item, float):
            # Python and JavaScript do not serialize integral/exponential
            # floating-point values identically. Federation signatures and
            # future E2EE AAD therefore use integers or strings exclusively.
            raise ValueError(f"{label} floating-point numbers are not supported")
        raise ValueError(f"{label} contains a value that JSON cannot encode")


def strict_json_loads(
    value: bytes | str,
    *,
    limits: JsonTreeLimits = FEDERATION_JSON_LIMITS,
    label: str = "federation JSON",
) -> object:
    """Decode interoperable JSON while rejecting ambiguous duplicate names."""

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"{label} contains a duplicate object key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError(f"{label} contains a non-finite number")

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    validate_json_tree(decoded, limits=limits, label=label)
    return decoded


def _validate_string(value: str, *, max_bytes: int, label: str) -> None:
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain valid Unicode") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} is too large")
