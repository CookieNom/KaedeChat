from __future__ import annotations

import json
import math
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, field_validator, model_validator

from app.chat.schemas import RequestModel, cleaned_nonempty
from app.core.types import EntityRef

FieldKind = Literal[
    "text",
    "textarea",
    "select",
    "multiselect",
    "number",
    "date",
    "checkbox",
    "url",
    "users",
    "channels",
    "attachments",
]


class TrackerField(RequestModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    type: FieldKind
    options: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        if value in {"__proto__", "constructor", "prototype"}:
            raise ValueError("reserved field ID")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)

    @model_validator(mode="after")
    def valid_options(self) -> TrackerField:
        self.options = [cleaned_nonempty(option) for option in self.options]
        if any(len(option) > 100 for option in self.options):
            raise ValueError("choices must be at most 100 characters")
        if len(set(self.options)) != len(self.options):
            raise ValueError("choices must be unique")
        if self.type in {"select", "multiselect"}:
            if not self.options:
                raise ValueError("choice fields need at least one choice")
        elif self.options:
            raise ValueError("only choice fields may have choices")
        return self


def field_definitions(value: object) -> list[dict[str, Any]]:
    fields = TypeAdapter(list[TrackerField]).validate_python(value)
    if len(fields) > 50 or len({field.id for field in fields}) != len(fields):
        raise ValueError("a board supports up to 50 fields with unique IDs")
    if len({field.name.casefold() for field in fields}) != len(fields):
        raise ValueError("field names must be unique")
    result = [field.model_dump() for field in fields]
    if len(json.dumps(result).encode()) > 64_000:
        raise ValueError("field definitions exceed 64 KB")
    return result


def safe_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def validate_values(fields: list[dict[str, Any]], values: object) -> dict[str, Any]:
    if not isinstance(values, dict) or len(values) > 50:
        raise ValueError("custom fields must be an object with at most 50 entries")
    definitions = {field["id"]: field for field in fields}
    result = {}
    for key, value in values.items():
        if key not in definitions:
            raise ValueError("a custom field no longer exists; refresh the board")
        if value is None or value == "" or value == []:
            continue
        field = definitions[key]
        kind = field["type"]
        valid = False
        if kind in {"text", "textarea"}:
            valid = isinstance(value, str) and len(value) <= (10000 if kind == "textarea" else 500)
        elif kind == "number":
            valid = type(value) in {int, float} and abs(value) <= 1e15 and math.isfinite(value)
        elif kind == "checkbox":
            valid = type(value) is bool
        elif kind == "date":
            valid = isinstance(value, str) and len(value) == 10
            if valid:
                date.fromisoformat(value)
        elif kind == "url":
            valid = safe_url(value)
        elif kind == "select":
            valid = isinstance(value, str) and value in field["options"]
        elif kind in {"multiselect", "users", "channels"}:
            valid = (
                isinstance(value, list)
                and len(value) <= 100
                and all(isinstance(v, str) for v in value)
            )
            if valid:
                valid = len(set(value)) == len(value)
                if kind == "multiselect":
                    valid = valid and all(v in field["options"] for v in value)
                else:
                    for ref in value:
                        if "@" not in ref:
                            raise ValueError(
                                "user and channel references must include their domain"
                            )
                        if str(TypeAdapter(EntityRef).validate_python(ref)) != ref:
                            raise ValueError("use canonical user and channel references")
        elif kind == "attachments":
            valid = isinstance(value, list) and len(value) <= 10
            if valid:
                valid = all(
                    isinstance(item, dict)
                    and set(item) in ({"name", "url", "type"}, {"id", "name", "type"})
                    and isinstance(item["name"], str)
                    and 0 < len(item["name"]) <= 255
                    and isinstance(item["type"], str)
                    and item["type"] in {"file", "image", "video"}
                    and (
                        safe_url(item["url"])
                        if "url" in item
                        else isinstance(item["id"], str)
                        and "@" in item["id"]
                        and str(TypeAdapter(EntityRef).validate_python(item["id"])) == item["id"]
                    )
                    for item in value
                )
        if not valid:
            raise ValueError(f"Invalid value for {field['name']}")
        result[key] = value
    if len(json.dumps(result).encode()) > 32_000:
        raise ValueError("custom field values exceed 32 KB")
    return result
