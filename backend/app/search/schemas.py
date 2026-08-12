from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.types import EntityRef


class MessageSearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authors: list[EntityRef] = Field(default_factory=list, max_length=25)
    mentions: list[EntityRef] = Field(default_factory=list, max_length=25)
    has: list[Literal["image", "video", "audio", "file", "link", "embed"]] = Field(
        default_factory=list, max_length=6
    )
    before: datetime | None = None
    after: datetime | None = None
    pinned: bool | None = None
    author_type: Literal["user", "webhook"] | None = None

    @model_validator(mode="after")
    def valid_date_range(self) -> MessageSearchFilters:
        if self.before is not None and self.after is not None and self.after >= self.before:
            raise ValueError("after must be earlier than before")
        return self


class MessageSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=512)
    scope: Literal["channel", "guild", "dms"]
    scope_ref: EntityRef | None = None
    filters: MessageSearchFilters = Field(default_factory=MessageSearchFilters)
    sort: Literal["relevance", "newest", "oldest"] = "relevance"
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=25, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @model_validator(mode="after")
    def valid_scope(self) -> MessageSearchRequest:
        if self.scope in {"channel", "guild"} and self.scope_ref is None:
            raise ValueError("scope_ref is required for channel and guild search")
        if self.scope == "dms" and self.scope_ref is not None:
            raise ValueError("scope_ref is not allowed for account-wide DM search")
        if not self.query and not any(
            (
                self.filters.authors,
                self.filters.mentions,
                self.filters.has,
                self.filters.before,
                self.filters.after,
                self.filters.pinned is not None,
                self.filters.author_type,
            )
        ):
            raise ValueError("a query or filter is required")
        return self


class FederatedMessageSearchRequest(MessageSearchRequest):
    actor_ref: EntityRef


class FederatedMessageSearchResult(BaseModel):
    """Minimal, bounded result accepted from an untrusted remote authority.

    Full message/channel payloads are deliberately not federated through the
    search endpoint.  The receiving instance rebuilds those from its own
    authorized state, which prevents a peer from injecting attachment URLs or
    arbitrary nested client payloads into search results.
    """

    model_config = ConfigDict(extra="forbid")

    message_ref: EntityRef
    channel_ref: EntityRef
    guild_ref: EntityRef | None = None
    author_ref: EntityRef
    snippet: str = Field(default="", max_length=280)
    created_at: datetime

    @field_validator("snippet")
    @classmethod
    def single_line_text(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if any(ord(character) < 0x20 for character in cleaned):
            raise ValueError("search result text contains control characters")
        return cleaned

    @field_validator("created_at")
    @classmethod
    def bounded_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("search result timestamp must include a timezone")
        normalized = value.astimezone(UTC)
        if normalized > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("search result timestamp is too far in the future")
        return normalized


class FederatedMessageSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[FederatedMessageSearchResult] = Field(default_factory=list, max_length=50)
    next_cursor: str | None = Field(default=None, max_length=512)
    encrypted_channel_refs: list[EntityRef] = Field(default_factory=list, max_length=10_000)
