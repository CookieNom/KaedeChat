from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.core.model_validation import UnambiguousInputModel
from app.core.types import EntityRef


class MessageSearchFilters(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")
    channel_ids: list[EntityRef] = Field(default_factory=list, max_length=500)
    authors: list[EntityRef] = Field(default_factory=list, max_length=100)
    mentions: list[EntityRef] = Field(default_factory=list, max_length=100)
    mentions_role_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    mention_everyone: bool | None = None
    replied_to_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    replied_to_message_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    has: list[
        Literal[
            "image",
            "sound",
            "audio",
            "video",
            "file",
            "sticker",
            "embed",
            "link",
            "poll",
            "snapshot",
            "-image",
            "-sound",
            "-audio",
            "-video",
            "-file",
            "-sticker",
            "-embed",
            "-link",
            "-poll",
            "-snapshot",
        ]
    ] = Field(default_factory=list, max_length=20)
    embed_types: list[Literal["image", "video", "gif", "sound", "article"]] = Field(
        default_factory=list,
        max_length=5,
    )
    embed_providers: list[str] = Field(default_factory=list, max_length=100)
    link_hostnames: list[str] = Field(default_factory=list, max_length=100)
    attachment_filenames: list[str] = Field(default_factory=list, max_length=100)
    attachment_extensions: list[str] = Field(default_factory=list, max_length=100)
    max_id: EntityRef | None = None
    min_id: EntityRef | None = None
    before: datetime | None = None
    after: datetime | None = None
    pinned: bool | None = None
    author_type: Literal["user", "bot", "webhook"] | None = None
    author_types: list[Literal["user", "bot", "webhook", "-user", "-bot", "-webhook"]] = Field(
        default_factory=list, max_length=6
    )

    @field_validator("embed_providers", "attachment_filenames")
    @classmethod
    def bounded_filter_strings(cls, values: list[str], info: ValidationInfo) -> list[str]:
        limit = 256 if info.field_name == "embed_providers" else 1_024
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > limit for value in normalized):
            raise ValueError("search filter strings are empty or too large")
        return (
            [value.casefold() for value in normalized]
            if info.field_name == "attachment_filenames"
            else normalized
        )

    @field_validator("link_hostnames")
    @classmethod
    def canonical_hostnames(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            candidate = value.strip().rstrip(".").lower()
            try:
                parsed = urlsplit(f"//{candidate}")
                port = parsed.port
            except ValueError as exc:
                raise ValueError("search link hostnames must be canonical hostnames") from exc
            if (
                not candidate
                or len(candidate) > 256
                or parsed.hostname != candidate
                or port is not None
                or parsed.username is not None
            ):
                raise ValueError("search link hostnames must be canonical hostnames")
            normalized.append(candidate)
        return normalized

    @field_validator("attachment_extensions")
    @classmethod
    def canonical_extensions(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().removeprefix(".").lower() for value in values]
        if any(
            not value
            or len(value) > 256
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)
            for value in normalized
        ):
            raise ValueError("search attachment extensions are invalid")
        return normalized

    @model_validator(mode="after")
    def valid_date_range(self) -> MessageSearchFilters:
        if self.before is not None and self.after is not None and self.after >= self.before:
            raise ValueError("after must be earlier than before")
        reference_lists = (
            self.channel_ids,
            self.authors,
            self.mentions,
            self.mentions_role_ids,
            self.replied_to_user_ids,
            self.replied_to_message_ids,
        )
        string_lists = (
            self.has,
            self.embed_types,
            self.embed_providers,
            self.link_hostnames,
            self.attachment_filenames,
            self.attachment_extensions,
            self.author_types,
        )
        if any(len(values) != len(set(values)) for values in (*reference_lists, *string_lists)):
            raise ValueError("search filters must be unique")
        if self.author_type is not None and self.author_types:
            raise ValueError("author_type and author_types cannot both be supplied")
        for values in (self.has, self.author_types):
            base = {value.removeprefix("-") for value in values}
            if any(value in values and f"-{value}" in values for value in base):
                raise ValueError("search filters cannot include and exclude the same value")
        return self


class MessageSearchRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=1_024)
    scope: Literal["channel", "guild", "dms"]
    scope_ref: EntityRef | None = None
    filters: MessageSearchFilters = Field(default_factory=MessageSearchFilters)
    sort: Literal["relevance", "newest", "oldest"] = "newest"
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=25, ge=1, le=25)
    slop: int = Field(default=2, ge=0, le=100)
    include_nsfw: bool = False

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
                self.filters.channel_ids,
                self.filters.mentions,
                self.filters.mentions_role_ids,
                self.filters.mention_everyone is not None,
                self.filters.replied_to_user_ids,
                self.filters.replied_to_message_ids,
                self.filters.has,
                self.filters.embed_types,
                self.filters.embed_providers,
                self.filters.link_hostnames,
                self.filters.attachment_filenames,
                self.filters.attachment_extensions,
                self.filters.max_id,
                self.filters.min_id,
                self.filters.before,
                self.filters.after,
                self.filters.pinned is not None,
                self.filters.author_type,
                self.filters.author_types,
            )
        ):
            raise ValueError("a query or filter is required")
        return self


class FederatedMessageSearchRequest(MessageSearchRequest):
    actor_ref: EntityRef


class FederatedMessageSearchResult(UnambiguousInputModel):
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
    ranking_score: float = Field(ge=0, le=1)
    cursor_after: str = Field(min_length=1, max_length=512)

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


class FederatedMessageSearchResponse(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")
    results: list[FederatedMessageSearchResult] = Field(default_factory=list, max_length=25)
    next_cursor: str | None = Field(default=None, max_length=512)
    encrypted_channel_refs: list[EntityRef] = Field(default_factory=list, max_length=10_000)
    indexing: bool = False
