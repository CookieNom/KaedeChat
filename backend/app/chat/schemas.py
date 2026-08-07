from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.chat.e2ee import validate_e2ee_envelope
from app.core.permissions import ALL_PERMISSIONS
from app.core.types import EntityRef, WireSnowflake


def cleaned_nonempty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must contain a non-whitespace character")
    return cleaned


class RequestModel(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def reject_database_nul(cls, value: object) -> object:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("must not contain NUL characters")
        return value


class GuildCreate(RequestModel):
    name: str = Field(min_length=2, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)


class GuildUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    federated_history_policy: Literal["disabled", "full_retained"] | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @model_validator(mode="after")
    def at_least_one_change(self) -> GuildUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one guild field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("guild name cannot be null")
        if (
            "federated_history_policy" in self.model_fields_set
            and self.federated_history_policy is None
        ):
            raise ValueError("guild federated history policy cannot be null")
        return self


class GuildNotificationSettingsUpdate(RequestModel):
    level: Literal["all", "mentions", "none"]


class GuildOwnershipTransfer(RequestModel):
    owner_id: EntityRef


class ChannelCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    type: int = Field(default=0)
    topic: str | None = Field(default=None, max_length=1024)
    parent_id: WireSnowflake | None = None
    rate_limit_per_user: int = Field(default=0, ge=0, le=21_600)

    @field_validator("type")
    @classmethod
    def supported_guild_type(cls, value: int) -> int:
        if value not in {0, 2, 4, 5}:
            raise ValueError("must be a guild text, voice, category, or announcement channel")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)


class ChannelUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    topic: str | None = Field(default=None, max_length=1024)
    position: int | None = Field(default=None, ge=0)
    parent_id: WireSnowflake | None = None
    rate_limit_per_user: int | None = Field(default=None, ge=0, le=21_600)
    federated_history_policy: Literal["inherit", "disabled", "full_retained"] | None = None
    sync_permissions: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @model_validator(mode="after")
    def at_least_one_change(self) -> ChannelUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one channel field is required")
        for field in (
            "name",
            "position",
            "rate_limit_per_user",
            "federated_history_policy",
            "sync_permissions",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"channel {field} cannot be null")
        return self


class ChannelPosition(RequestModel):
    id: WireSnowflake
    position: int = Field(ge=0)
    parent_id: WireSnowflake | None = None
    sync_permissions: bool = False


class ChannelPositionBatch(RequestModel):
    channels: list[ChannelPosition] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def complete_unique_order(self) -> ChannelPositionBatch:
        ids = [channel.id for channel in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError("channel IDs must be unique")
        positions = sorted(channel.position for channel in self.channels)
        if positions != list(range(len(self.channels))):
            raise ValueError("channel positions must be contiguous and start at zero")
        return self


class RoleCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    permissions: WireSnowflake = 0
    color: int = Field(default=0, ge=0, le=0xFFFFFF)
    hoist: bool = False
    mentionable: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)

    @field_validator("permissions")
    @classmethod
    def known_permissions(cls, value: int) -> int:
        if value & ~ALL_PERMISSIONS:
            raise ValueError("permissions contain unknown bits")
        return value


class RoleUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    permissions: WireSnowflake | None = None
    color: int | None = Field(default=None, ge=0, le=0xFFFFFF)
    position: int | None = Field(default=None, ge=1)
    hoist: bool | None = None
    mentionable: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @field_validator("permissions")
    @classmethod
    def known_permissions(cls, value: int | None) -> int | None:
        if value is not None and value & ~ALL_PERMISSIONS:
            raise ValueError("permissions contain unknown bits")
        return value

    @model_validator(mode="after")
    def at_least_one_change(self) -> RoleUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one role field is required")
        for field in (
            "name",
            "permissions",
            "color",
            "position",
            "hoist",
            "mentionable",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"role {field} cannot be null")
        return self


class RolePosition(RequestModel):
    id: WireSnowflake
    position: int = Field(ge=1)
    version: str = Field(min_length=1, max_length=64)


class RolePositionBatch(RequestModel):
    roles: list[RolePosition] = Field(min_length=1, max_length=250)

    @model_validator(mode="after")
    def unique_roles_and_positions(self) -> RolePositionBatch:
        ids = [role.id for role in self.roles]
        positions = [role.position for role in self.roles]
        if len(ids) != len(set(ids)):
            raise ValueError("role IDs must be unique")
        if len(positions) != len(set(positions)):
            raise ValueError("role positions must be unique")
        return self


class OverwritePut(RequestModel):
    target_id: EntityRef
    target_type: str
    allow: WireSnowflake = 0
    deny: WireSnowflake = 0

    @field_validator("target_type")
    @classmethod
    def supported_target(cls, value: str) -> str:
        if value not in {"role", "member"}:
            raise ValueError("must be role or member")
        return value

    @model_validator(mode="after")
    def disjoint_masks(self) -> OverwritePut:
        if self.allow & self.deny:
            raise ValueError("allow and deny permissions must not overlap")
        if (self.allow | self.deny) & ~ALL_PERMISSIONS:
            raise ValueError("permission masks contain unknown bits")
        return self


class MessageCreate(RequestModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    e2ee: dict[str, object] | None = None
    client_nonce: str | None = Field(default=None, min_length=1, max_length=64)
    referenced_message_id: EntityRef | None = None
    mention_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    attachment_ids: list[WireSnowflake] = Field(default_factory=list, max_length=10)

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must contain a non-whitespace character")
        return value

    @field_validator("e2ee")
    @classmethod
    def valid_e2ee(cls, value: object) -> dict[str, object] | None:
        return validate_e2ee_envelope(value)

    @model_validator(mode="after")
    def content_or_attachment(self) -> MessageCreate:
        if self.content is not None and self.e2ee is not None:
            raise ValueError("a message cannot contain plaintext and encrypted content")
        if self.content is None and self.e2ee is None and not self.attachment_ids:
            raise ValueError("a message requires content, encrypted content, or an attachment")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("attachment IDs must be unique")
        return self


class MessageEdit(RequestModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    e2ee: dict[str, object] | None = None

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must contain a non-whitespace character")
        return value

    @field_validator("e2ee")
    @classmethod
    def valid_e2ee(cls, value: object) -> dict[str, object] | None:
        return validate_e2ee_envelope(value)

    @model_validator(mode="after")
    def exactly_one_body(self) -> MessageEdit:
        if (self.content is None) == (self.e2ee is None):
            raise ValueError("an edit requires exactly one plaintext or encrypted body")
        return self


class MessageBulkDelete(RequestModel):
    message_ids: list[EntityRef] = Field(min_length=2, max_length=100)

    @field_validator("message_ids")
    @classmethod
    def unique_message_ids(cls, value: list[EntityRef]) -> list[EntityRef]:
        if len(set(value)) != len(value):
            raise ValueError("message IDs must be unique")
        return value


class ReadStateUpdate(RequestModel):
    message_id: EntityRef


class ReactionCreate(RequestModel):
    # A canonical custom-emoji token includes a fully qualified federation
    # domain and can be longer than an ordinary Unicode grapheme.
    emoji: str = Field(min_length=1, max_length=320)


class InviteCreate(RequestModel):
    channel_id: WireSnowflake | None = None
    max_uses: int | None = Field(default=None, ge=1, le=1000)
    max_age_seconds: int | None = Field(default=86_400, ge=60, le=604_800)


class MemberUpdate(RequestModel):
    nickname: str | None = Field(default=None, max_length=100)
    timeout_until: datetime | None = None
    timeout_indefinite: bool = False

    @model_validator(mode="after")
    def at_least_one_change(self) -> MemberUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one member field is required")
        return self


class BanCreate(RequestModel):
    reason: str | None = Field(default=None, max_length=512)
    delete_message_seconds: int = Field(default=0, ge=0, le=604_800)
    expires_at: datetime | None = None


class InstanceBanCreate(RequestModel):
    reason: str | None = Field(default=None, max_length=512)
    expires_at: datetime | None = None


class RelationshipRequest(RequestModel):
    handle: str = Field(min_length=4, max_length=286)


class ProfilePatch(RequestModel):
    display_name: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=500)
    custom_status: str | None = Field(default=None, max_length=128)

    @field_validator("display_name", "bio", "custom_status")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def at_least_one_change(self) -> ProfilePatch:
        if not self.model_fields_set:
            raise ValueError("at least one profile field is required")
        return self


class DMOpenRequest(RequestModel):
    handle: str = Field(min_length=4, max_length=286)
