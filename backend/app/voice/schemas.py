from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.types import EntityRef
from app.federation.schemas import FederationDomain, SnowflakeString


class VoiceTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=4096)
    url: str = Field(min_length=6, max_length=2048)
    room: str = Field(pattern=r"^[gd]\.[0-9]+\.[0-9]+$", max_length=80)
    generation: int = Field(ge=0)
    expires_at: str = Field(min_length=20, max_length=64)
    can_speak: bool
    can_stream: bool
    # Defaults preserve compatibility with peers that predate the explicit
    # grant. New home instances always send the authoritative value.
    can_use_vad: bool = True
    # Present for a federated guild session. Clients retain this opaque value
    # and only accept pushed move grants carrying the same correlation.
    move_session_id: str | None = Field(
        default=None,
        min_length=32,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class VoiceFlagsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self_mute: bool
    self_deaf: bool


class VoiceModerationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_mute: bool | None = None
    server_deaf: bool | None = None

    @field_validator("server_deaf")
    @classmethod
    def deafening_is_explicit(cls, value: bool | None) -> bool | None:
        return value


class VoiceMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef


class VoiceMoveFederationRequest(BaseModel):
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


class VoiceBrokerRequest(BaseModel):
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


class DMVoiceBrokerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: SnowflakeString
    actor_id: SnowflakeString
    actor_domain: FederationDomain


class VoiceOccupantState(BaseModel):
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
    can_speak: bool = False
    can_stream: bool = False


class VoiceStateFederationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    room: str = Field(min_length=5, max_length=80)
    generated_at: int = Field(ge=0)
    participants: list[VoiceOccupantState] = Field(max_length=10_000)


class CallCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ring: bool = True


class CallAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "decline", "end"]


class CallResponse(BaseModel):
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


class CallFederationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: SnowflakeString
    channel_id: SnowflakeString
    channel_domain: FederationDomain
    authority_domain: FederationDomain
    actor_id: SnowflakeString
    actor_domain: FederationDomain
    action: Literal["create", "ring", "accept", "decline", "end"]
    created_at: int = Field(ge=0)


class CallStateFederationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call: CallResponse

    @model_validator(mode="after")
    def terminal_state_only(self) -> CallStateFederationRequest:
        if self.call.state != "ended":
            raise ValueError("replica propagation accepts only terminal call state")
        return self


class ActiveCallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call: CallResponse | None
    joined: bool = False
