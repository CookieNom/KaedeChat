from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from app.chat.e2ee import validate_e2ee_envelope
from app.core.json_limits import JsonTreeLimits, validate_json_tree
from app.core.settings import DOMAIN_RE
from app.core.text import sanitize_single_line_text
from app.core.types import EntityRef

MAX_DATABASE_SNOWFLAKE = (1 << 63) - 1
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
FEDERATION_EVENT_JSON_LIMITS = JsonTreeLimits(
    max_depth=24,
    max_nodes=16_384,
    max_object_members=1024,
    max_array_members=4096,
    max_key_bytes=256,
    max_string_bytes=1024 * 1024,
)


def _snowflake_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("snowflake must be a decimal string")
    rendered = value
    if (
        not rendered
        or not rendered.isascii()
        or not rendered.isdecimal()
        or (len(rendered) > 1 and rendered.startswith("0"))
    ):
        raise ValueError("snowflake must be a decimal string")
    parsed = int(rendered)
    if parsed > MAX_DATABASE_SNOWFLAKE:
        raise ValueError("snowflake is outside the PostgreSQL BIGINT range")
    return rendered


def _federation_domain(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("domain must be a string")
    domain = value.rstrip(".").lower()
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("invalid federation domain")
    return domain


SnowflakeString = Annotated[str, BeforeValidator(_snowflake_string)]
FederationDomain = Annotated[str, BeforeValidator(_federation_domain)]


class ActorRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: SnowflakeString
    domain: FederationDomain


class EventEnvelope(BaseModel):
    # Unknown optional envelope members must remain in the canonical signing input.
    # Dropping them before verification would make those members malleable in transit.
    model_config = ConfigDict(extra="allow")

    event_id: str = Field(pattern=r"^kcfe_[A-Za-z0-9_-]{16,59}$")
    origin: FederationDomain
    type: str = Field(min_length=1, max_length=100)
    ts: int = Field(ge=0)
    actor: ActorRef
    context: dict[str, Any] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    signatures: dict[str, dict[str, str]]

    @model_validator(mode="before")
    @classmethod
    def bounded_json_structure(cls, value: object) -> object:
        validate_json_tree(
            value,
            limits=FEDERATION_EVENT_JSON_LIMITS,
            label="federation event envelope",
        )
        return value

    @field_validator("signatures")
    @classmethod
    def bounded_signatures(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        if not 1 <= len(value) <= 8:
            raise ValueError("an envelope must contain between one and eight signers")
        for domain, keys in value.items():
            _federation_domain(domain)
            if not 1 <= len(keys) <= 8:
                raise ValueError("an envelope signer must contain between one and eight keys")
            for key_id, signature in keys.items():
                if not KEY_ID_RE.fullmatch(key_id) or len(signature) > 128:
                    raise ValueError("invalid envelope signature entry")
        return value


class InboxRequest(BaseModel):
    events: list[EventEnvelope] = Field(min_length=1, max_length=100)


class InboxResult(BaseModel):
    event_id: str
    status: Literal["accepted", "duplicate", "rejected", "retry"]
    code: str | None = None


class RemoteUserProfile(BaseModel):
    id: SnowflakeString
    origin_domain: FederationDomain
    username: str = Field(pattern=r"^[a-z0-9_.]{2,32}$")
    display_name: str | None = Field(default=None, max_length=100)
    avatar_hash: str | None = Field(default=None, max_length=128)
    banner_hash: str | None = Field(default=None, max_length=128)
    bio: str | None = Field(default=None, max_length=500)
    custom_status: str | None = Field(default=None, max_length=128)
    profile_version: int = Field(default=1, ge=1, le=2_147_483_647)


class RelationshipEventContent(BaseModel):
    actor: RemoteUserProfile
    target: ActorRef
    request_id: str | None = Field(default=None, pattern=r"^kcr_[A-Za-z0-9_-]{16,59}$")


class DMOpenFederationRequest(BaseModel):
    participants: list[RemoteUserProfile] = Field(min_length=2, max_length=2)

    @field_validator("participants")
    @classmethod
    def distinct_participants(cls, value: list[RemoteUserProfile]) -> list[RemoteUserProfile]:
        identities = {(item.id, item.origin_domain) for item in value}
        if len(identities) != 2:
            raise ValueError("participants must be distinct")
        return value


class DMGroupAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inviter: RemoteUserProfile
    invitee: RemoteUserProfile


class DMGroupMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "rename", "leave", "remove"]
    conversation_id: SnowflakeString
    conversation_domain: FederationDomain
    actor: RemoteUserProfile
    target: RemoteUserProfile | None = None
    name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def coherent_mutation(self) -> DMGroupMutationRequest:
        if self.action in {"add", "remove"} and self.target is None:
            raise ValueError("member mutation requires a target")
        if self.action not in {"add", "remove"} and self.target is not None:
            raise ValueError("target is not valid for this mutation")
        if self.action == "rename":
            self.name = self.name.strip() if self.name is not None else None
            if self.name == "":
                self.name = None
        elif self.name is not None:
            raise ValueError("name is only valid for rename")
        return self


class InviteResolveRequest(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9]{8}$")


class PresenceFederationRequest(BaseModel):
    """Short-lived presence projected by a user's authoritative instance."""

    model_config = ConfigDict(extra="forbid")

    user_id: SnowflakeString
    user_domain: FederationDomain
    status: Literal["online", "idle", "dnd", "offline"]
    observed_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)


class GuildJoinRequest(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9]{8}$")
    user: RemoteUserProfile


class GuildLeaveRequest(BaseModel):
    user: ActorRef


class GuildSelfModerationStatus(BaseModel):
    """Private timeout state returned only to the affected user's home."""

    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    guild_domain: FederationDomain
    timed_out: bool
    timeout_until: datetime | None = None
    timeout_indefinite: bool = False
    reason: str | None = Field(default=None, max_length=512)
    details_available: bool = True

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return sanitize_single_line_text(value, max_characters=512)

    @model_validator(mode="after")
    def consistent_timeout(self) -> GuildSelfModerationStatus:
        if self.timeout_until is not None and self.timeout_until.tzinfo is None:
            raise ValueError("timeout expiry requires a timezone")
        if not self.timed_out:
            if self.timeout_until is not None or self.timeout_indefinite or self.reason is not None:
                raise ValueError("inactive moderation status contains timeout details")
            return self
        if self.timeout_indefinite == (self.timeout_until is not None):
            raise ValueError("active timeout requires exactly one duration mode")
        return self


class GuildHistoryExportRequest(BaseModel):
    user: ActorRef


class GuildProxyRequest(BaseModel):
    operation: Literal["message.create"]
    actor: RemoteUserProfile
    channel_id: SnowflakeString
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    e2ee: dict[str, object] | None = None
    client_nonce: str = Field(min_length=1, max_length=64)
    referenced_message_id: EntityRef | None = None
    mention_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    attachments: list[dict[str, object]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def content_or_attachment(self) -> GuildProxyRequest:
        self.e2ee = validate_e2ee_envelope(self.e2ee)
        if self.content is not None and self.e2ee is not None:
            raise ValueError("a proxied message cannot mix plaintext and encrypted content")
        if self.content is None and self.e2ee is None and not self.attachments:
            raise ValueError(
                "a proxied message requires content, encrypted content, or an attachment"
            )
        return self


class GuildPinProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef
    pinned: bool


class GuildReactionProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef
    emoji: str = Field(min_length=1, max_length=320)
    remove: bool = False


class InstanceBlockPut(BaseModel):
    domain: FederationDomain
    level: Literal["silence", "suspend"]
    include_subdomains: bool = True
    reason: str | None = Field(default=None, max_length=500)
