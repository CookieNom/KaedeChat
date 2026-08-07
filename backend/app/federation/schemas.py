from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from app.chat.e2ee import validate_e2ee_envelope
from app.core.settings import DOMAIN_RE
from app.core.types import EntityRef

MAX_DATABASE_SNOWFLAKE = (1 << 63) - 1
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


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


class InstanceBlockPut(BaseModel):
    domain: FederationDomain
    level: Literal["silence", "suspend"]
    include_subdomains: bool = True
    reason: str | None = Field(default=None, max_length=500)
