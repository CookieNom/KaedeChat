from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.automod.safe_regex import UnsafeRegexError, validate_safe_regex
from app.chat.schemas import RequestModel
from app.core.types import EntityRef

MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60
SAFE_REGEX_FORBIDDEN = re.compile(r"\\[1-9]|\(\?<?[=!]|\(\?>|\(\?P|\{\d+,\d*}\s*[+*?]")


class TriggerMetadata(RequestModel):
    keyword_filter: list[str] = Field(default_factory=list, max_length=1_000)
    regex_patterns: list[str] = Field(default_factory=list, max_length=10)
    presets: list[Literal["profanity", "sexual_content", "slurs"]] = Field(
        default_factory=list, max_length=3
    )
    allow_list: list[str] = Field(default_factory=list, max_length=1_000)
    mention_total_limit: int | None = Field(default=None, ge=1, le=50)
    mention_raid_protection_enabled: bool = False

    @field_validator("keyword_filter", "allow_list")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("entries must be unique")
        for item in value:
            if not item.strip():
                raise ValueError("entries must not be blank")
            if not item.strip().strip("*"):
                raise ValueError("entries must contain a character other than a wildcard")
            if len(item) > 60:
                raise ValueError("entries cannot exceed 60 characters")
        return value

    @field_validator("regex_patterns")
    @classmethod
    def validate_regexes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("regex patterns must be unique")
        for pattern in value:
            if not pattern or len(pattern) > 260:
                raise ValueError("regex patterns must contain 1 to 260 characters")
            if SAFE_REGEX_FORBIDDEN.search(pattern):
                raise ValueError(
                    "regex patterns cannot use backreferences, lookarounds, atomic groups, "
                    "named groups, or nested counted quantifiers"
                )
            try:
                validate_safe_regex(pattern)
            except UnsafeRegexError as exc:
                raise ValueError(
                    "regex pattern must use RE2-safe syntax without backreferences or lookarounds"
                ) from exc
        return value

    @field_validator("presets")
    @classmethod
    def unique_presets(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("keyword presets must be unique")
        return value


class AutoModActionInput(RequestModel):
    type: Literal[
        "block_message",
        "send_alert_message",
        "timeout",
        "block_member_interaction",
    ]
    custom_message: str | None = Field(default=None, min_length=1, max_length=150)
    channel_id: EntityRef | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=MAX_TIMEOUT_SECONDS)

    @field_validator("custom_message")
    @classmethod
    def clean_message(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("custom message must not be blank")
        return value

    @model_validator(mode="after")
    def valid_metadata(self) -> AutoModActionInput:
        if self.type == "send_alert_message":
            if (
                self.channel_id is None
                or self.duration_seconds is not None
                or self.custom_message is not None
            ):
                raise ValueError("send_alert_message requires channel_id only")
        elif self.type == "timeout":
            if self.duration_seconds is None or self.channel_id is not None:
                raise ValueError("timeout requires duration_seconds only")
            if self.custom_message is not None:
                raise ValueError("timeout does not accept custom_message")
        elif self.type == "block_message":
            if self.channel_id is not None or self.duration_seconds is not None:
                raise ValueError("block_message accepts only custom_message")
        elif (
            self.channel_id is not None
            or self.duration_seconds is not None
            or self.custom_message is not None
        ):
            raise ValueError(f"{self.type} does not accept channel_id or duration_seconds")
        return self

    def metadata(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.custom_message is not None:
            result["custom_message"] = self.custom_message
        if self.channel_id is not None:
            result["channel_id"] = str(self.channel_id)
        if self.duration_seconds is not None:
            result["duration_seconds"] = self.duration_seconds
        return result


class AutoModRuleCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    event_type: Literal["message_send", "member_update"] = "message_send"
    trigger_type: Literal["keyword", "spam", "keyword_preset", "mention_spam", "member_profile"]
    trigger_metadata: TriggerMetadata = Field(default_factory=TriggerMetadata)
    actions: list[AutoModActionInput] = Field(min_length=1, max_length=3)
    enabled: bool = False
    exempt_roles: list[EntityRef] = Field(default_factory=list, max_length=20)
    exempt_channels: list[EntityRef] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule name must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_trigger(self) -> AutoModRuleCreate:
        metadata = self.trigger_metadata
        if self.trigger_type in {"keyword", "member_profile"} and not (
            metadata.keyword_filter or metadata.regex_patterns
        ):
            raise ValueError(
                f"{'keyword' if self.trigger_type == 'keyword' else 'member-profile'} "
                "rules require keyword_filter or regex_patterns"
            )
        if self.trigger_type == "keyword_preset" and not metadata.presets:
            raise ValueError("keyword_preset rules require at least one preset")
        if self.trigger_type == "mention_spam" and metadata.mention_total_limit is None:
            raise ValueError("mention_spam rules require mention_total_limit")
        if self.event_type == "member_update" and self.trigger_type != "member_profile":
            raise ValueError("member_update supports only the member_profile trigger")
        if self.event_type == "message_send" and self.trigger_type == "member_profile":
            raise ValueError("member_profile requires the member_update event")
        if self.trigger_type in {"keyword", "member_profile"}:
            if len(metadata.allow_list) > 100:
                raise ValueError(
                    "keyword and member-profile allow lists are limited to 100 entries"
                )
            if metadata.presets or metadata.mention_total_limit is not None:
                raise ValueError("keyword rules contain unsupported trigger metadata")
            if metadata.mention_raid_protection_enabled:
                raise ValueError("mention raid protection requires a mention_spam rule")
        elif self.trigger_type == "spam":
            if metadata != TriggerMetadata():
                raise ValueError("spam rules do not accept trigger metadata")
        elif self.trigger_type == "keyword_preset":
            if (
                metadata.keyword_filter
                or metadata.regex_patterns
                or metadata.mention_total_limit is not None
                or metadata.mention_raid_protection_enabled
            ):
                raise ValueError("keyword preset rules contain unsupported trigger metadata")
        elif self.trigger_type == "mention_spam" and (
            metadata.keyword_filter
            or metadata.regex_patterns
            or metadata.presets
            or metadata.allow_list
        ):
            raise ValueError("mention spam rules contain unsupported trigger metadata")
        if len(self.exempt_roles) != len(set(self.exempt_roles)):
            raise ValueError("exempt roles must be unique")
        if len(self.exempt_channels) != len(set(self.exempt_channels)):
            raise ValueError("exempt channels must be unique")
        action_types = [action.type for action in self.actions]
        if len(action_types) != len(set(action_types)):
            raise ValueError("a rule cannot contain duplicate action types")
        if "timeout" in action_types and self.trigger_type not in {"keyword", "mention_spam"}:
            raise ValueError("timeout actions support only keyword and mention-spam rules")
        if "block_member_interaction" in action_types and self.trigger_type != "member_profile":
            raise ValueError("block_member_interaction requires a member-profile rule")
        if self.trigger_type == "member_profile" and "block_message" in action_types:
            raise ValueError("member-profile rules cannot use block_message")
        return self


class AutoModRuleUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    event_type: Literal["message_send", "member_update"] | None = None
    trigger_metadata: TriggerMetadata | None = None
    actions: list[AutoModActionInput] | None = Field(default=None, min_length=1, max_length=3)
    enabled: bool | None = None
    exempt_roles: list[EntityRef] | None = Field(default=None, max_length=20)
    exempt_channels: list[EntityRef] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def not_empty(self) -> AutoModRuleUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one rule field is required")
        null_fields = sorted(
            field for field in self.model_fields_set if getattr(self, field) is None
        )
        if null_fields:
            raise ValueError(
                "auto moderation rule fields cannot be null: " + ", ".join(null_fields)
            )
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("rule name must not be blank")
        if self.exempt_roles is not None and len(self.exempt_roles) != len(set(self.exempt_roles)):
            raise ValueError("exempt roles must be unique")
        if self.exempt_channels is not None and len(self.exempt_channels) != len(
            set(self.exempt_channels)
        ):
            raise ValueError("exempt channels must be unique")
        return self
