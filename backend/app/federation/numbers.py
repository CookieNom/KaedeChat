"""Represent schema-defined numeric fields in integer-only signed federation JSON."""

from __future__ import annotations

import math
import re
from typing import Annotated

from pydantic import BeforeValidator, Field, PlainSerializer, SerializationInfo

from app.core.json_limits import MAX_SAFE_JSON_INTEGER

_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def read_wire_number(value: object) -> int | float:
    if isinstance(value, str):
        if len(value) > 32 or not _DECIMAL.fullmatch(value):
            raise ValueError("invalid federation decimal")
        value = float(value) if any(c in value for c in ".eE") else int(value)
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise ValueError("expected a finite federation number")
    return value


def write_wire_number(value: int | float) -> int | str:
    if value == int(value) and abs(value) <= MAX_SAFE_JSON_INTEGER:
        return int(value)
    return str(value)


def _write_unit_fraction(value: float, info: SerializationInfo) -> float | int | str:
    if info.context and info.context.get("federation"):
        return write_wire_number(value)
    return value


WireUnitFraction = Annotated[
    float,
    BeforeValidator(read_wire_number),
    Field(ge=0, le=1),
    PlainSerializer(_write_unit_fraction, when_used="json"),
]
