from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.chat.schemas import RequestModel, cleaned_nonempty
from app.core.types import EntityRef

TrackerLaneKind = Literal["backlog", "planned", "in_progress", "completed", "custom"]
TrackerPriority = Literal["none", "low", "medium", "high", "urgent"]


def normalize_key_prefix(value: str) -> str:
    normalized = value.strip().upper()
    if (
        len(normalized) < 2
        or len(normalized) > 10
        or not normalized[0].isascii()
        or not normalized[0].isalpha()
        or not normalized.isascii()
        or not normalized.isalnum()
    ):
        raise ValueError("must be 2-10 uppercase ASCII letters or digits and start with a letter")
    return normalized


def require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("must include a timezone offset")
    return value


class TrackerBoardUpdate(RequestModel):
    key_prefix: str = Field(min_length=2, max_length=10)

    @field_validator("key_prefix")
    @classmethod
    def valid_key_prefix(cls, value: str) -> str:
        return normalize_key_prefix(value)


class TrackerLaneCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    color: int = Field(default=0, ge=0, le=0xFFFFFF)
    kind: TrackerLaneKind = "custom"
    completed: bool = False
    position: int | None = Field(default=None, ge=0, le=49)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)


class TrackerLaneUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: int | None = Field(default=None, ge=0, le=0xFFFFFF)
    kind: TrackerLaneKind | None = None
    completed: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @model_validator(mode="after")
    def at_least_one_change(self) -> TrackerLaneUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one lane field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("lane fields cannot be null")
        return self


class TrackerLaneMove(RequestModel):
    position: int = Field(ge=0, le=49)


class TrackerTaskCreate(RequestModel):
    lane_id: EntityRef
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    priority: TrackerPriority = "none"
    position: int | None = Field(default=None, ge=0, le=4_999)
    due_at: datetime | None = None
    assignee_id: EntityRef | None = None
    client_nonce: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        return cleaned_nonempty(value)

    @field_validator("due_at")
    @classmethod
    def aware_due_at(cls, value: datetime | None) -> datetime | None:
        return require_aware(value)


class TrackerTaskUpdate(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    priority: TrackerPriority | None = None
    due_at: datetime | None = None
    assignee_id: EntityRef | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @field_validator("due_at")
    @classmethod
    def aware_due_at(cls, value: datetime | None) -> datetime | None:
        return require_aware(value)

    @model_validator(mode="after")
    def at_least_one_change(self) -> TrackerTaskUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one task field is required")
        for field in ("title", "priority"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"task {field} cannot be null")
        return self


class TrackerTaskMove(RequestModel):
    lane_id: EntityRef
    position: int = Field(ge=0, le=4_999)
