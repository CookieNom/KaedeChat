from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator

from app.chat.custom_emojis import (
    canonical_reaction_emoji,
    canonical_unicode_reaction_emoji,
)
from app.chat.e2ee import validate_e2ee_envelope
from app.chat.expression_authorization import canonical_expression_authority_map
from app.chat.forwarding import validate_forward_snapshot
from app.chat.mention_policy import AllowedMentions
from app.chat.message_flags import (
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
    PUBLIC_MESSAGE_CREATE_FLAGS,
    PUBLIC_MESSAGE_EDIT_FLAGS,
)
from app.chat.rich_content import (
    Embed,
    MessageLayoutComponent,
    PollCreate,
    uses_components_v2,
    validate_embed_collection,
    validate_message_components,
)
from app.core.channel_types import GUILD_VOICE_CHANNEL_TYPES, validate_voice_channel_limits
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import ALL_PERMISSIONS
from app.core.settings import DOMAIN_RE
from app.core.types import EntityRef, WireSnowflake


def cleaned_nonempty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must contain a non-whitespace character")
    return cleaned


def cleaned_optional(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def cleaned_optional_nonempty(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} cannot be blank")
    return cleaned


def meaningful_optional_content(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("content must not be blank")
    return value


class RequestModel(UnambiguousInputModel):
    pass


def canonical_actor_intent_authority_map(
    value: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Canonicalize a small receiver-keyed map without merging authorities."""

    canonical: dict[str, dict[str, object]] = {}
    for raw_domain, intent in value.items():
        domain = raw_domain.rstrip(".").lower()
        if not DOMAIN_RE.fullmatch(domain) or domain in canonical:
            raise ValueError("actor intent authority is invalid or duplicated")
        canonical[domain] = intent
    return canonical


def parse_actor_intent_headers(
    actor_intent_header: str | None,
    actor_intents_header: str | None,
    *,
    max_authorities: int = 2,
) -> tuple[dict[str, object] | None, dict[str, dict[str, object]]]:
    """Decode the compatible single- and receiver-keyed wire encodings."""

    if actor_intent_header is not None and actor_intents_header is not None:
        raise ValueError("actor intent encodings are mutually exclusive")
    raw_intent = json.loads(actor_intent_header or "null")
    raw_intents = json.loads(actor_intents_header or "{}")
    if raw_intent is not None and not isinstance(raw_intent, dict):
        raise ValueError("actor intent is invalid")
    if not isinstance(raw_intents, dict) or any(
        not isinstance(domain, str) or not isinstance(intent, dict)
        for domain, intent in raw_intents.items()
    ):
        raise ValueError("actor intent authority map is invalid")
    actor_intents = canonical_actor_intent_authority_map(raw_intents)
    if len(actor_intents) > max_authorities:
        raise ValueError("actor intent authority map is too large")
    return cast(dict[str, object] | None, raw_intent), actor_intents


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

    @field_validator("emoji_name")
    @classmethod
    def canonical_unicode_emoji_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return canonical_unicode_reaction_emoji(value)

    @model_validator(mode="after")
    def exactly_one_emoji(self) -> DefaultReactionEmoji:
        if (self.emoji_id is None) == (self.emoji_name is None):
            raise ValueError("exactly one default reaction emoji is required")
        return self


class ChannelCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    type: int = Field(default=0)
    topic: str | None = Field(default=None, max_length=4096)
    nsfw: bool = False
    parent_id: WireSnowflake | None = None
    rate_limit_per_user: int = Field(default=0, ge=0, le=21_600)
    bitrate: int | None = Field(default=None, ge=8_000, le=384_000)
    user_limit: int | None = Field(default=None, ge=0, le=10_000)
    rtc_region: str | None = Field(default=None, min_length=1, max_length=64)
    video_quality_mode: Literal[1, 2] | None = None
    available_tags: list[ForumTag] = Field(default_factory=list, max_length=20)
    default_reaction_emoji: DefaultReactionEmoji | None = None
    default_auto_archive_duration: Literal[60, 1440, 4320, 10080] = 1440
    default_thread_rate_limit_per_user: int = Field(default=0, ge=0, le=21_600)
    default_sort_order: Literal[0, 1] | None = None
    default_forum_layout: Literal[0, 1, 2] = 0
    e2ee_required: bool = False
    flags: Literal[0, 16] = 0
    tracker_key_prefix: str | None = Field(default=None, min_length=2, max_length=10)

    @field_validator("video_quality_mode", mode="before")
    @classmethod
    def strict_video_quality_mode(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("video_quality_mode must be an integer")
        return value

    @field_validator("type")
    @classmethod
    def supported_guild_type(cls, value: int) -> int:
        if value not in {0, 2, 4, 5, 13, 15, 17}:
            raise ValueError(
                "must be a guild text, voice, category, announcement, Stage, forum, "
                "or tracker channel"
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

    @field_validator("rtc_region")
    @classmethod
    def clean_rtc_region(cls, value: str | None) -> str | None:
        return cleaned_optional_nonempty(value, field="rtc_region")

    @model_validator(mode="after")
    def valid_forum_fields(self) -> ChannelCreate:
        voice_fields = {
            "bitrate",
            "user_limit",
            "rtc_region",
            "video_quality_mode",
        }
        if self.type not in GUILD_VOICE_CHANNEL_TYPES and self.model_fields_set & voice_fields:
            raise ValueError("voice settings are only valid for voice and Stage channels")
        if self.type in GUILD_VOICE_CHANNEL_TYPES:
            validate_voice_channel_limits(
                self.type,
                bitrate=self.bitrate,
                user_limit=self.user_limit,
            )
            if "bitrate" in self.model_fields_set and self.bitrate is None:
                raise ValueError("voice channel bitrate cannot be null")
            if "user_limit" in self.model_fields_set and self.user_limit is None:
                raise ValueError("voice channel user_limit cannot be null")
            if "video_quality_mode" in self.model_fields_set and self.video_quality_mode is None:
                raise ValueError("voice channel video_quality_mode cannot be null")
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
    nsfw: bool | None = None
    position: int | None = Field(default=None, ge=0)
    parent_id: WireSnowflake | None = None
    rate_limit_per_user: int | None = Field(default=None, ge=0, le=21_600)
    bitrate: int | None = Field(default=None, ge=8_000, le=384_000)
    user_limit: int | None = Field(default=None, ge=0, le=10_000)
    rtc_region: str | None = Field(default=None, min_length=1, max_length=64)
    video_quality_mode: Literal[1, 2] | None = None
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

    @field_validator("video_quality_mode", mode="before")
    @classmethod
    def strict_video_quality_mode(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("video_quality_mode must be an integer")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @field_validator("rtc_region")
    @classmethod
    def clean_rtc_region(cls, value: str | None) -> str | None:
        return cleaned_optional_nonempty(value, field="rtc_region")

    @model_validator(mode="after")
    def at_least_one_change(self) -> ChannelUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one channel field is required")
        for field in (
            "name",
            "nsfw",
            "position",
            "rate_limit_per_user",
            "bitrate",
            "user_limit",
            "video_quality_mode",
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
    position: int | None = Field(default=None, ge=0)
    parent_id: WireSnowflake | None = None
    lock_permissions: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("lock_permissions", "sync_permissions"),
    )
    flags: Literal[0, 16] | None = None

    @field_validator("lock_permissions", mode="before")
    @classmethod
    def strict_lock_permissions(cls, value: object) -> object:
        if value is not None and type(value) is not bool:
            raise ValueError("lock_permissions must be a boolean or null")
        return value

    @model_validator(mode="after")
    def at_least_one_change(self) -> ChannelPosition:
        if not self.model_fields_set & {
            "position",
            "parent_id",
            "lock_permissions",
            "flags",
        }:
            raise ValueError("at least one channel position field is required")
        return self


class ChannelPositionBatch(RequestModel):
    channels: list[ChannelPosition] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_partial_order(self) -> ChannelPositionBatch:
        ids = [channel.id for channel in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError("channel IDs must be unique")
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
    tts: bool = False
    voice_message: bool = False
    flags: int = Field(default=0, ge=0, le=2_147_483_647)
    embeds: list[Embed] = Field(default_factory=list, max_length=10)
    components: list[MessageLayoutComponent] = Field(default_factory=list, max_length=40)
    poll: PollCreate | None = None
    sticker_ids: list[EntityRef] = Field(default_factory=list, max_length=3)
    # Bot workers sign one source-specific intent for each expression
    # authority. Human callers leave this empty; their home instance creates
    # the signed source request.
    expression_actor_intents: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        max_length=16,
    )
    # The server resolves this authorized source into an immutable, author-free
    # snapshot. ``content`` remains available as Discord-style optional note.
    forwarded_message_id: EntityRef | None = None
    # A source-authority signed, requester/destination/nonce-bound proof is
    # required whenever the source cannot be re-authorized in this process.
    # It contains an authoritative snapshot only for plaintext sources.
    forward_source_proof: dict[str, object] | None = None
    # An E2EE source can be disclosed into a plaintext destination only through
    # its source-committed, author-free snapshot. Encrypted destinations carry
    # the same object inside MLS instead of exposing it here.
    forward_snapshot: dict[str, object] | None = None
    # View lifetime is interpreted only by an authenticated application
    # adapter.  Ordinary user and webhook callers cannot claim ownership.
    view_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    view_persistent: bool = False
    client_nonce: str | None = Field(default=None, min_length=1, max_length=64)
    referenced_message_id: EntityRef | None = None
    allowed_mentions: AllowedMentions | None = None
    mention_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    attachment_ids: list[WireSnowflake] = Field(default_factory=list, max_length=10)

    @field_validator("expression_actor_intents")
    @classmethod
    def canonical_expression_intents(
        cls, value: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return canonical_expression_authority_map(value)

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

    @field_validator("forward_snapshot")
    @classmethod
    def valid_forward_snapshot(cls, value: object) -> dict[str, object] | None:
        return validate_forward_snapshot(value) if value is not None else None

    @model_validator(mode="after")
    def content_or_attachment(self) -> MessageCreate:
        if self.content is not None and self.e2ee is not None:
            raise ValueError("a message cannot contain plaintext and encrypted content")
        if self.e2ee is not None and self.allowed_mentions is not None:
            raise ValueError("encrypted messages carry allowed mentions inside ciphertext")
        validate_embed_collection(self.embeds)
        validate_message_components(self.components)
        components_v2 = uses_components_v2(self.components)
        if self.flags & ~PUBLIC_MESSAGE_CREATE_FLAGS:
            raise ValueError("message flags contain server-owned or unsupported bits")
        if self.flags & MESSAGE_FLAG_IS_VOICE_MESSAGE and not self.voice_message:
            raise ValueError("the voice-message flag requires a voice-message body")
        encrypted_rich = isinstance(self.e2ee, dict) and "rich_payload_digest" in self.e2ee
        if self.flags & MESSAGE_FLAG_IS_COMPONENTS_V2 and not components_v2 and not encrypted_rich:
            raise ValueError("the Components V2 flag requires a Components V2 body")
        if components_v2 and (
            self.content is not None or self.embeds or self.poll is not None or self.sticker_ids
        ):
            raise ValueError(
                "Components V2 messages cannot include content, embeds, polls, or stickers"
            )
        if self.e2ee is not None and (
            self.embeds or self.components or self.poll is not None or self.sticker_ids
        ):
            raise ValueError("a message cannot contain rich plaintext and encrypted content")
        if self.forward_snapshot is not None and (
            self.forwarded_message_id is None or self.e2ee is not None
        ):
            raise ValueError("a client forward snapshot requires a plaintext forwarded message")
        if self.forward_source_proof is not None and (
            self.forwarded_message_id is None or self.client_nonce is None
        ):
            raise ValueError("a forward source proof requires a forwarded message and nonce")
        if self.forwarded_message_id is not None and (
            (self.e2ee is not None and not encrypted_rich)
            or self.embeds
            or self.components
            or self.poll is not None
            or self.sticker_ids
            or (
                self.attachment_ids
                and not encrypted_rich
                and self.forward_snapshot is None
                and self.forward_source_proof is None
            )
            or self.referenced_message_id is not None
            or self.mention_user_ids
        ):
            raise ValueError("a forwarded message can contain only an optional text note")
        if self.voice_message and (
            self.tts
            or self.content is not None
            or self.embeds
            or self.components
            or self.poll is not None
            or self.sticker_ids
            or self.forwarded_message_id is not None
            or self.mention_user_ids
            or len(self.attachment_ids) != 1
        ):
            raise ValueError(
                "a voice message requires exactly one audio attachment and cannot include "
                "content, rich content, forwarding, or mentions"
            )
        encrypted_contract = (
            self.e2ee.get("interaction_contract") if isinstance(self.e2ee, dict) else None
        )
        encrypted_controls = (
            encrypted_contract.get("components") if isinstance(encrypted_contract, dict) else None
        )
        if (
            (self.view_timeout_seconds is not None or self.view_persistent)
            and not self.components
            and not encrypted_controls
        ):
            raise ValueError("view lifetime options require message components")
        if self.view_persistent and self.view_timeout_seconds is not None:
            raise ValueError("a persistent view cannot have a timeout")
        if (
            self.content is None
            and self.e2ee is None
            and not self.attachment_ids
            and not self.embeds
            and not self.components
            and self.poll is None
            and not self.sticker_ids
            and self.forwarded_message_id is None
        ):
            raise ValueError(
                "a message requires content, encrypted content, an attachment, or rich content"
            )
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("attachment IDs must be unique")
        if len(set(self.sticker_ids)) != len(self.sticker_ids):
            raise ValueError("sticker IDs must be unique")
        return self


class MessageEdit(RequestModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    e2ee: dict[str, object] | None = None
    embeds: list[Embed] | None = Field(default=None, max_length=10)
    components: list[MessageLayoutComponent] | None = Field(default=None, max_length=40)
    allowed_mentions: AllowedMentions | None = None
    flags: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # When present this is the complete retained + newly uploaded attachment
    # set, matching Discord's edit-message/webhook semantics.
    attachment_ids: list[WireSnowflake] | None = Field(default=None, max_length=10)
    # Optimistic ownership fence for application-authored component views.
    view_version: int | None = Field(default=None, ge=1)
    view_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    view_persistent: bool | None = None
    expression_actor_intents: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        max_length=16,
    )

    @field_validator("expression_actor_intents")
    @classmethod
    def canonical_expression_intents(
        cls, value: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return canonical_expression_authority_map(value)

    @model_validator(mode="before")
    @classmethod
    def poll_is_create_only(cls, value: object) -> object:
        if isinstance(value, dict) and "poll" in value:
            raise ValueError("polls cannot be added or replaced after a message has been created")
        return value

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
        if not self.model_fields_set - {"expression_actor_intents"}:
            raise ValueError("an edit requires at least one field")
        if self.e2ee is not None and self.model_fields_set - {
            "e2ee",
            "attachment_ids",
            "flags",
            "expression_actor_intents",
        }:
            raise ValueError("encrypted edits cannot contain rich plaintext fields")
        if self.flags is not None and self.flags & ~PUBLIC_MESSAGE_EDIT_FLAGS:
            raise ValueError("message edit flags contain unsupported bits")
        if self.embeds is not None:
            validate_embed_collection(self.embeds)
        if self.components is not None:
            validate_message_components(self.components)
            if uses_components_v2(self.components) and (
                ("content" in self.model_fields_set and self.content is not None)
                or bool(self.embeds)
            ):
                raise ValueError("Components V2 messages cannot include content or embeds")
        if "embeds" in self.model_fields_set and self.embeds is None:
            raise ValueError("embeds cannot be null; use an empty list to clear them")
        if "components" in self.model_fields_set and self.components is None:
            raise ValueError("components cannot be null; use an empty list to clear them")
        if self.attachment_ids is not None and len(self.attachment_ids) != len(
            set(self.attachment_ids)
        ):
            raise ValueError("attachment IDs must be unique")
        view_fields = {"view_version", "view_timeout_seconds", "view_persistent"}
        if self.model_fields_set & view_fields and "components" not in self.model_fields_set:
            raise ValueError("view options require a components edit")
        if self.view_persistent is True and self.view_timeout_seconds is not None:
            raise ValueError("a persistent view cannot have a timeout")
        return self


class MessageForwardPrepareDestination(RequestModel):
    channel_id: EntityRef
    client_nonce: str = Field(min_length=1, max_length=64)


class MessageForwardPrepare(RequestModel):
    destinations: list[MessageForwardPrepareDestination] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_destinations(self) -> MessageForwardPrepare:
        refs = [item.channel_id for item in self.destinations]
        nonces = [item.client_nonce for item in self.destinations]
        if len(refs) != len(set(refs)) or len(nonces) != len(set(nonces)):
            raise ValueError("forward destinations and nonces must be unique")
        return self


class BotForwardSourceAuthorizationCreate(RequestModel):
    destination_channel_id: EntityRef
    destination_encryption_mode: Literal["plaintext", "e2ee"]
    client_nonce: str = Field(min_length=1, max_length=64)


class PreparedMessageForwardDestination(RequestModel):
    destination_channel_id: EntityRef
    message: MessageCreate


class MessageForwardCreate(RequestModel):
    destination_channel_ids: list[EntityRef] = Field(default_factory=list, max_length=5)
    content: str | None = Field(default=None, min_length=1, max_length=4_000)
    destinations: list[PreparedMessageForwardDestination] = Field(
        default_factory=list,
        max_length=5,
    )

    @field_validator("content")
    @classmethod
    def meaningful_note(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("forward note must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def unique_destinations(self) -> MessageForwardCreate:
        legacy = self.destination_channel_ids
        prepared = [item.destination_channel_id for item in self.destinations]
        if bool(legacy) == bool(prepared):
            raise ValueError("choose legacy or prepared forward destinations")
        if len(legacy) != len(set(legacy)) or len(prepared) != len(set(prepared)):
            raise ValueError("forward destinations must be unique")
        if prepared and self.content is not None:
            raise ValueError("prepared forward notes belong to each destination message")
        return self


class MessageBulkDelete(RequestModel):
    message_ids: list[EntityRef] = Field(min_length=2, max_length=100)

    @field_validator("message_ids")
    @classmethod
    def unique_message_ids(cls, value: list[EntityRef]) -> list[EntityRef]:
        if len(set(value)) != len(value):
            raise ValueError("message IDs must be unique")
        return value


class ChannelFollowCreate(RequestModel):
    target_channel_id: EntityRef
    # Compatibility input for one receiving authority. Multi-authority relays
    # must use ``actor_intents`` so a receiver-bound proof is never reused at a
    # different authority.
    actor_intent: dict[str, Any] | None = None
    actor_intents: dict[str, dict[str, object]] = Field(default_factory=dict, max_length=2)

    @field_validator("actor_intents")
    @classmethod
    def canonical_actor_intents(
        cls,
        value: dict[str, dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        return canonical_actor_intent_authority_map(value)

    @model_validator(mode="after")
    def one_actor_intent_encoding(self) -> ChannelFollowCreate:
        if self.actor_intent is not None and self.actor_intents:
            raise ValueError("actor_intent and actor_intents are mutually exclusive")
        return self


class ReadStateUpdate(RequestModel):
    message_id: EntityRef


class ReactionCreate(RequestModel):
    # A canonical custom-emoji token includes a fully qualified federation
    # domain and can be longer than an ordinary Unicode grapheme.
    emoji: str = Field(min_length=1, max_length=320)
    expression_actor_intents: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        max_length=1,
    )

    @field_validator("expression_actor_intents")
    @classmethod
    def canonical_expression_intents(
        cls, value: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return canonical_expression_authority_map(value)

    @field_validator("emoji")
    @classmethod
    def canonical_emoji(cls, value: str) -> str:
        return canonical_reaction_emoji(value)


class InviteCreate(RequestModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef | None = None
    # Discord's Create Channel Invite contract caps non-zero use limits at 100.
    # Kaede represents Discord's wire value ``0`` (unlimited) as ``None``.
    max_uses: int | None = Field(default=None, ge=1, le=100)
    max_age_seconds: int | None = Field(default=86_400, ge=60, le=604_800)
    temporary: bool = False
    # Discord calls this option ``unique``.  When false the server is allowed
    # to reuse a compatible invite rather than allocating another code.
    unique: bool = False
    # Kaede implements a real, authority-verified Go Live target. Embedded
    # Activities remain omitted until an Activity runtime exists. A scheduled
    # event is an independent invite association, not a target type.
    target_type: Literal["stream"] | None = None
    target_user_id: EntityRef | None = None
    scheduled_event_id: EntityRef | None = None
    role_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    target_user_ids: list[EntityRef] = Field(default_factory=list, max_length=1000)

    @field_validator("role_ids")
    @classmethod
    def unique_invite_roles(cls, value: list[EntityRef]) -> list[EntityRef]:
        if len(value) != len(set(value)):
            raise ValueError("invite role references must be unique")
        return value

    @field_validator("target_user_ids")
    @classmethod
    def deduplicate_invite_targets(cls, value: list[EntityRef]) -> list[EntityRef]:
        # Discord ignores duplicate IDs in targeted-invite CSV files.
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def target_matches_type(self) -> InviteCreate:
        if self.target_type == "stream":
            valid = self.target_user_id is not None
        else:
            valid = self.target_user_id is None
        if not valid:
            raise ValueError("invite target fields must match target_type")
        return self


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
        return cleaned_optional(value)

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
        return cleaned_optional(value)


class DMGroupUpdate(RequestModel):
    name: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_group_name(cls, value: str | None) -> str | None:
        return cleaned_optional(value)


class DMGroupMemberAdd(RequestModel):
    handle: str = Field(min_length=4, max_length=286)
