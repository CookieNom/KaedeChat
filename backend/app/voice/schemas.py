from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.core.model_validation import UnambiguousInputModel
from app.core.settings import VoiceRegionConfiguration
from app.core.types import EntityRef, WireSnowflake
from app.federation.schemas import FederationDomain, SnowflakeString


class StrictVoiceModel(UnambiguousInputModel):
    pass


class VoiceTokenRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    sender_device_id: str | None = Field(
        default=None,
        min_length=47,
        max_length=47,
        pattern=r"^ked_[A-Za-z0-9_-]{43}$",
    )
    connection_id: str | None = Field(
        default=None,
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    takeover: bool = False
    client_kind: Literal["web", "desktop", "mobile"] = "web"


class BotVoiceTokenRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    sender_device_id: str | None = Field(
        default=None,
        min_length=47,
        max_length=47,
        pattern=r"^kbe_[A-Za-z0-9_-]{43}$",
    )
    connection_id: str | None = Field(
        default=None,
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    takeover: bool = False
    listen: bool = False
    speak: bool = False
    stream: bool = False


class BotVoiceDisconnectRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    generation: int = Field(ge=0)


class BotVoiceSelfStateRequest(BotVoiceDisconnectRequest):
    self_mute: bool
    self_deaf: bool


class VoiceRegion(VoiceRegionConfiguration):
    """Public Discord-compatible voice-region object."""


class SoundboardSoundCreate(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: WireSnowflake
    name: str = Field(min_length=2, max_length=32)
    volume: float = Field(default=1.0, ge=0, le=1)
    emoji_id: WireSnowflake | None = None
    emoji_name: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("sound name must contain at least two non-whitespace characters")
        return cleaned

    @model_validator(mode="after")
    def one_emoji(self) -> SoundboardSoundCreate:
        if self.emoji_id is not None and self.emoji_name is not None:
            raise ValueError("emoji_id and emoji_name are mutually exclusive")
        return self


class SoundboardSoundUpdate(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=32)
    volume: float | None = Field(default=None, ge=0, le=1)
    emoji_id: WireSnowflake | None = None
    emoji_name: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("sound name must contain at least two non-whitespace characters")
        return cleaned

    @model_validator(mode="after")
    def valid_update(self) -> SoundboardSoundUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one soundboard field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("sound name cannot be null")
        if "volume" in self.model_fields_set and self.volume is None:
            raise ValueError("sound volume cannot be null")
        if self.emoji_id is not None and self.emoji_name is not None:
            raise ValueError("emoji_id and emoji_name are mutually exclusive")
        return self


class SoundboardPlayRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    sound_id: EntityRef
    source_guild_id: EntityRef | None = None
    sound_version: SnowflakeString = "1"
    volume: float | None = Field(default=None, ge=0, le=1)
    actor_intent: dict[str, object] | None = None


class VoiceTokenResponse(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=4096)
    url: str = Field(min_length=6, max_length=2048)
    room: str = Field(pattern=r"^[gd]\.[0-9]+\.[0-9]+$", max_length=80)
    generation: int = Field(ge=0)
    connection_id: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    expires_at: str = Field(min_length=20, max_length=64)
    can_speak: bool
    can_stream: bool
    # Missing means denied while peers roll through this additive grant.
    can_priority_speak: bool = False
    can_listen: bool = True
    # This default preserves compatibility with peers that predate the
    # explicit VAD grant. New home instances always send the authoritative value.
    can_use_vad: bool = True
    # Effective channel media/admission policy. Defaults keep rolling
    # federation upgrades compatible with peers that predate these fields.
    bitrate: int = Field(default=64_000, ge=8_000, le=384_000)
    user_limit: int = Field(default=0, ge=0, le=10_000)
    rtc_region: str | None = Field(default=None, min_length=1, max_length=64)
    video_quality_mode: Literal[1, 2] = 1
    e2ee: bool
    channel_id: SnowflakeString | None = None
    channel_domain: FederationDomain | None = None
    guild_id: SnowflakeString | None = None
    guild_domain: FederationDomain | None = None
    bot_installation_revision: SnowflakeString | None = None
    encryption_policy_generation: SnowflakeString | None = None
    encryption_epoch: SnowflakeString | None = None
    media_protocol: Literal["livekit-e2ee-v1"] | None = None
    media_suite: Literal["AES-256-GCM"] | None = None
    media_session_id: str | None = Field(
        default=None,
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    media_epoch: SnowflakeString | None = None

    @model_validator(mode="after")
    def consistent_e2ee_context(self) -> VoiceTokenResponse:
        context = (
            self.channel_id,
            self.channel_domain,
            self.encryption_policy_generation,
            self.encryption_epoch,
            self.media_protocol,
            self.media_suite,
            self.media_session_id,
            self.media_epoch,
        )
        if self.channel_id is None or self.channel_domain is None:
            raise ValueError("voice grant requires a channel reference")
        if (self.guild_id is None) != (self.guild_domain is None):
            raise ValueError("voice grant guild reference is incomplete")
        if self.can_priority_speak and (not self.can_speak or self.guild_id is None):
            raise ValueError("priority speaking requires guild speaking access")
        if self.e2ee and any(item is None for item in context):
            raise ValueError("encrypted voice grant requires complete room context")
        if self.e2ee and self.media_epoch != self.encryption_epoch:
            raise ValueError("encrypted voice grant media epoch does not match MLS epoch")
        if not self.e2ee and any(item is not None for item in context[2:]):
            raise ValueError("plaintext voice grant cannot carry an encryption epoch")
        return self

    # Present for a federated guild session. Clients retain this opaque value
    # and only accept pushed move grants carrying the same correlation.
    move_session_id: str | None = Field(
        default=None,
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @field_validator("video_quality_mode", mode="before")
    @classmethod
    def strict_video_quality_mode(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("video_quality_mode must be an integer")
        return value


class VoiceFlagsUpdate(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    self_mute: bool
    self_deaf: bool


class VoiceChannelStatusUpdate(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None

    @field_validator("status")
    @classmethod
    def normalized_status(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        normalized = normalized or None
        if normalized is not None and len(normalized) > 500:
            raise ValueError("voice channel status cannot exceed 500 characters")
        return normalized


class VoiceModerationUpdate(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    server_mute: bool | None = None
    server_deaf: bool | None = None

    @field_validator("server_deaf")
    @classmethod
    def deafening_is_explicit(cls, value: bool | None) -> bool | None:
        return value


class VoiceMoveRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef


def normalized_voice_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 20 <= len(value) <= 64:
        raise ValueError("request-to-speak timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("request-to-speak timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("request-to-speak timestamp requires a timezone")
    return parsed.astimezone(UTC).isoformat()


class CurrentUserVoiceStateUpdate(StrictVoiceModel):
    """Discord's writable current-user Stage voice-state subset."""

    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef | None = None
    suppress: bool | None = None
    request_to_speak_timestamp: str | None = None

    @field_validator("request_to_speak_timestamp")
    @classmethod
    def valid_timestamp(cls, value: str | None) -> str | None:
        return normalized_voice_timestamp(value)

    @model_validator(mode="after")
    def has_stage_change(self) -> CurrentUserVoiceStateUpdate:
        changed = self.model_fields_set - {"channel_id"}
        if not changed:
            raise ValueError("at least one voice-state field is required")
        if "suppress" in changed and self.suppress is None:
            raise ValueError("suppress cannot be null")
        return self


class UserVoiceStateUpdate(StrictVoiceModel):
    """Discord's writable other-user Stage voice-state subset."""

    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef | None = None
    suppress: bool


class VoiceMoveFederationRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    channel_id: SnowflakeString
    target_id: SnowflakeString
    target_domain: FederationDomain
    move_session_id: str = Field(
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    source_room: str = Field(pattern=r"^g\.[0-9]+\.[0-9]+$", max_length=80)
    source_generation: int = Field(ge=0)
    grant: VoiceTokenResponse

    @model_validator(mode="after")
    def grant_uses_move_session(self) -> VoiceMoveFederationRequest:
        if self.grant.move_session_id != self.move_session_id:
            raise ValueError("voice move grant correlation does not match the request")
        return self


class VoiceBrokerRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    channel_id: SnowflakeString
    actor_id: SnowflakeString
    actor_domain: FederationDomain
    move_session_id: str = Field(
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    sender_device_id: str | None = Field(
        default=None,
        min_length=47,
        max_length=47,
        pattern=r"^ked_[A-Za-z0-9_-]{43}$",
    )
    connection_id: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    takeover: bool = False
    client_kind: Literal["web", "desktop", "mobile"] = "web"
    allow_listen: bool = True
    allow_speak: bool = True
    allow_stream: bool = True

    @model_validator(mode="after")
    def human_capabilities_are_full(self) -> VoiceBrokerRequest:
        if not (self.allow_listen and self.allow_speak and self.allow_stream):
            raise ValueError("federated human voice grants require full media capabilities")
        return self


class DMVoiceBrokerRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    call_id: SnowflakeString
    actor_id: SnowflakeString
    actor_domain: FederationDomain
    move_session_id: str = Field(
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    sender_device_id: str | None = Field(
        default=None,
        min_length=47,
        max_length=47,
        pattern=r"^ked_[A-Za-z0-9_-]{43}$",
    )
    connection_id: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    takeover: bool = False
    client_kind: Literal["web", "desktop", "mobile"] = "web"


class VoiceOccupantState(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    identity: str = Field(min_length=3, max_length=286)
    user_id: str = Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=19)
    user_domain: FederationDomain
    room: str = Field(pattern=r"^[gd]\.[0-9]+\.[0-9]+$", max_length=80)
    guild_id: str | None = Field(default=None, pattern=r"^(0|[1-9][0-9]*)$", max_length=19)
    channel_id: str = Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=19)
    joined_at: int = Field(ge=0)
    self_mute: bool = False
    self_deaf: bool = False
    server_mute: bool = False
    server_deaf: bool = False
    suppressed: bool = False
    request_to_speak_timestamp: str | None = None
    can_speak: bool = False
    can_stream: bool = False
    can_priority_speak: bool = False

    @field_validator("request_to_speak_timestamp")
    @classmethod
    def valid_request_timestamp(cls, value: str | None) -> str | None:
        return normalized_voice_timestamp(value)

    @model_validator(mode="after")
    def consistent_priority_speaking(self) -> VoiceOccupantState:
        if self.can_priority_speak and (not self.can_speak or self.guild_id is None):
            raise ValueError("priority speaking requires guild speaking access")
        return self


class VoiceFederationOccupantState(VoiceOccupantState):
    """Peer-only room state with the capability fence omitted from clients."""

    generation: int = Field(ge=0)
    connection_id: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    move_session_id: str | None = Field(
        default=None,
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class VoiceStateFederationRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    room: str = Field(min_length=5, max_length=80)
    generated_at: int = Field(ge=0)
    snapshot_version: int = Field(ge=1)
    participants: list[VoiceFederationOccupantState] = Field(max_length=10_000)


class DMVoiceStateFederationRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    call_id: SnowflakeString
    channel_id: SnowflakeString
    room: str = Field(pattern=r"^d\.[0-9]+\.[0-9]+$", max_length=80)
    generated_at: int = Field(ge=0)
    snapshot_version: int = Field(ge=1)
    participants: list[VoiceFederationOccupantState] = Field(max_length=10)


class VoiceSelfStateFederationRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    actor_id: SnowflakeString
    room: str = Field(pattern=r"^g\.[0-9]+\.[0-9]+$", max_length=80)
    move_session_id: str = Field(
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    generation: int = Field(ge=0)
    connection_id: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    self_mute: bool
    self_deaf: bool


class DMVoiceSelfStateFederationRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    call_id: SnowflakeString
    channel_id: SnowflakeString
    actor_id: SnowflakeString
    room: str = Field(pattern=r"^d\.[0-9]+\.[0-9]+$", max_length=80)
    move_session_id: str = Field(
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    generation: int = Field(ge=0)
    connection_id: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    self_mute: bool
    self_deaf: bool


class VoiceSelfStateFederationResponse(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    state: VoiceOccupantState
    generation: int = Field(ge=0)


class CallCreate(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    ring: bool = True


class CallAction(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "decline", "end"]


class CallResponse(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    channel_id: SnowflakeString
    channel_domain: FederationDomain
    authority_domain: FederationDomain
    room: str = Field(pattern=r"^d\.[0-9]+\.[0-9]+$", max_length=80)
    state: Literal["ringing", "active", "ended"]
    created_at: int = Field(ge=0)
    ended_at: int | None = Field(default=None, ge=0)
    caller: str = Field(min_length=3, max_length=286)
    participants: list[str] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def coherent_call_state(self) -> CallResponse:
        if self.room != f"d.{self.channel_id}.{self.id}":
            raise ValueError("call room must match its channel and call identifiers")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("call participants must be unique")
        if self.caller not in self.participants:
            raise ValueError("call caller must be a participant")
        if self.state == "ended":
            if self.ended_at is None or self.ended_at < self.created_at:
                raise ValueError("ended call must have a terminal timestamp after creation")
        elif self.ended_at is not None:
            raise ValueError("non-terminal call cannot have an ended_at timestamp")
        return self


class BotCallResponse(CallResponse):
    """A bot-visible call projection bound to one exact DM capability."""

    bot_dm_capability_id: str = Field(pattern=r"^kbdg_[A-Za-z0-9_-]{43}$")
    bot_dm_capability_revision: SnowflakeString
    bot_installation_ref: str = Field(min_length=3, max_length=286)
    bot_installation_type: Literal["guild", "user"]

    @field_validator("bot_installation_ref")
    @classmethod
    def qualified_installation_ref(cls, value: str) -> str:
        parsed = EntityRef(value)
        if parsed.domain is None or str(parsed) != value:
            raise ValueError("bot installation reference must be qualified")
        return value

    @model_validator(mode="after")
    def positive_capability_revision(self) -> BotCallResponse:
        if int(self.bot_dm_capability_revision) < 1:
            raise ValueError("bot DM capability revision must be positive")
        return self


class CallFederationRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    call_id: SnowflakeString
    channel_id: SnowflakeString
    channel_domain: FederationDomain
    authority_domain: FederationDomain
    actor_id: SnowflakeString
    actor_domain: FederationDomain
    action: Literal["create", "ring", "accept", "decline", "end"]
    created_at: int = Field(ge=0)
    state_version: SnowflakeString | None = None
    ring: bool = True


class CallStateFederationRequest(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    call: CallResponse

    @model_validator(mode="after")
    def terminal_state_only(self) -> CallStateFederationRequest:
        if self.call.state != "ended":
            raise ValueError("replica propagation accepts only terminal call state")
        return self


class ActiveCallResponse(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    call: CallResponse | None
    joined: bool = False


class BotActiveCallResponse(StrictVoiceModel):
    model_config = ConfigDict(extra="forbid")

    call: BotCallResponse | None
    joined: bool = False
