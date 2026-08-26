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


class ForumTag(RequestModel):
    id: WireSnowflake | None = None
    # Discord's Available Tag structure specifies only a 20-character maximum.
    name: str = Field(max_length=20)
    moderated: bool = False
    emoji_id: WireSnowflake | None = None
    emoji_name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def one_emoji(self) -> ForumTag:
        if self.emoji_id is not None and self.emoji_name is not None:
            raise ValueError("forum tag emoji_id and emoji_name are mutually exclusive")
        return self


class DefaultReactionEmoji(RequestModel):
    emoji_id: WireSnowflake | None = None
    emoji_name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def exactly_one_emoji(self) -> DefaultReactionEmoji:
        if (self.emoji_id is None) == (self.emoji_name is None):
            raise ValueError("exactly one default reaction emoji is required")
        return self


class ChannelCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    type: int = Field(default=0)
    topic: str | None = Field(default=None, max_length=4096)
    parent_id: WireSnowflake | None = None
    rate_limit_per_user: int = Field(default=0, ge=0, le=21_600)
    available_tags: list[ForumTag] = Field(default_factory=list, max_length=20)
    default_reaction_emoji: DefaultReactionEmoji | None = None
    default_auto_archive_duration: Literal[60, 1440, 4320, 10080] = 1440
    default_thread_rate_limit_per_user: int = Field(default=0, ge=0, le=21_600)
    default_sort_order: Literal[0, 1] | None = None
    default_forum_layout: Literal[0, 1, 2] = 0
    e2ee_required: bool = False
    flags: Literal[0, 16] = 0
    tracker_key_prefix: str | None = Field(default=None, min_length=2, max_length=10)

    @field_validator("type")
    @classmethod
    def supported_guild_type(cls, value: int) -> int:
        if value not in {0, 2, 4, 5, 15, 17}:
            raise ValueError(
                "must be a guild text, voice, category, announcement, forum, or tracker channel"
            )
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)

    @field_validator("tracker_key_prefix")
    @classmethod
    def valid_tracker_key_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized.isascii() or not normalized.isalnum() or not normalized[0].isalpha():
            raise ValueError(
                "must contain uppercase ASCII letters or digits and start with a letter"
            )
        return normalized

    @model_validator(mode="after")
    def valid_forum_fields(self) -> ChannelCreate:
        if self.type != 17 and self.tracker_key_prefix is not None:
            raise ValueError("tracker_key_prefix is only valid for tracker channels")
        if self.type != 15 and (
            self.available_tags
            or self.default_reaction_emoji is not None
            or self.default_sort_order is not None
            or self.e2ee_required
            or self.flags
            or self.default_forum_layout != 0
        ):
            raise ValueError("forum defaults are only valid for forum channels")
        if self.type not in {0, 15} and self.default_thread_rate_limit_per_user != 0:
            raise ValueError("default thread slowmode is only valid for text and forum channels")
        if self.type != 15 and self.topic is not None and len(self.topic) > 1024:
            raise ValueError("non-forum channel topics are limited to 1024 characters")
        names = [tag.name.casefold() for tag in self.available_tags]
        ids = [tag.id for tag in self.available_tags if tag.id is not None]
        if len(names) != len(set(names)) or len(ids) != len(set(ids)):
            raise ValueError("forum tags must have unique names and IDs")
        return self


class ChannelUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    topic: str | None = Field(default=None, max_length=4096)
    position: int | None = Field(default=None, ge=0)
    parent_id: WireSnowflake | None = None
    rate_limit_per_user: int | None = Field(default=None, ge=0, le=21_600)
    federated_history_policy: Literal["inherit", "disabled", "full_retained"] | None = None
    sync_permissions: bool | None = None
    available_tags: list[ForumTag] | None = Field(default=None, max_length=20)
    default_reaction_emoji: DefaultReactionEmoji | None = None
    default_auto_archive_duration: Literal[60, 1440, 4320, 10080] | None = None
    default_thread_rate_limit_per_user: int | None = Field(default=None, ge=0, le=21_600)
    default_sort_order: Literal[0, 1] | None = None
    default_forum_layout: Literal[0, 1, 2] | None = None
    e2ee_required: bool | None = None
    flags: Literal[0, 16] | None = None

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
            "available_tags",
            "default_auto_archive_duration",
            "default_thread_rate_limit_per_user",
            "default_forum_layout",
            "e2ee_required",
            "flags",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"channel {field} cannot be null")
        if self.available_tags is not None:
            names = [tag.name.casefold() for tag in self.available_tags]
            ids = [tag.id for tag in self.available_tags if tag.id is not None]
            if len(names) != len(set(names)) or len(ids) != len(set(ids)):
                raise ValueError("forum tags must have unique names and IDs")
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


class MemberRoleSet(RequestModel):
    role_ids: list[EntityRef] = Field(default_factory=list, max_length=250)

    @model_validator(mode="after")
    def unique_roles(self) -> MemberRoleSet:
        if len(self.role_ids) != len(set(self.role_ids)):
            raise ValueError("role IDs must be unique")
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


class DMGroupCreate(RequestModel):
    handles: list[str] = Field(min_length=2, max_length=9)
    name: str | None = Field(default=None, max_length=100)

    @field_validator("handles")
    @classmethod
    def unique_handles(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        if any(not item or len(item) > 286 for item in normalized):
            raise ValueError("group members require valid handles")
        if len(set(normalized)) != len(normalized):
            raise ValueError("group members must be unique")
        return [item.strip() for item in value]

    @field_validator("name")
    @classmethod
    def clean_group_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class DMGroupUpdate(RequestModel):
    name: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_group_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class DMGroupMemberAdd(RequestModel):
    handle: str = Field(min_length=4, max_length=286)
