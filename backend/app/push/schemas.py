from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.push.relay import OPAQUE_TOKEN_PATTERN, RELAY_SUBSCRIPTION_PATTERN


class PushDeviceCreate(BaseModel):
    installation_id: UUID
    platform: Literal["android", "ios"]
    token: str = Field(min_length=20, max_length=4096)
    device_name: str | None = Field(default=None, max_length=100)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("push token must not contain whitespace")
        return value


class PushDeviceResponse(BaseModel):
    id: str
    platform: Literal["android", "ios"]
    device_name: str | None
    enabled: bool
    last_seen_at: str
    transport: Literal["relay", "direct_fcm"] = "direct_fcm"
    relay_origin: str | None = None


class PushRelayEnrollmentCreate(BaseModel):
    installation_id: UUID
    platform: Literal["android", "ios"]
    route_id: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)
    app_id: str = Field(min_length=3, max_length=160, pattern=r"^[A-Za-z][A-Za-z0-9_.-]+$")


class PushRelayEnrollmentComplete(BaseModel):
    installation_id: UUID
    platform: Literal["android", "ios"]
    route_id: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)
    wake_secret: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)
    receipt: dict[str, object]
    device_name: str | None = Field(default=None, max_length=100)


class PushRelaySubscriptionCreate(BaseModel):
    grant: dict[str, object]
    provider_token: str = Field(min_length=20, max_length=4096)
    management_secret: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)

    @field_validator("provider_token")
    @classmethod
    def validate_provider_token(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("provider token must not contain whitespace")
        return value


class PushRelayWakeCreate(BaseModel):
    version: Literal[2]
    request_id: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)
    subscription_id: str = Field(min_length=36, max_length=64, pattern=RELAY_SUBSCRIPTION_PATTERN)
    route_id: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)
    event_token: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)
    delivery_id: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)
    expires_at: int = Field(gt=0)
    priority: Literal["normal", "urgent"] = "normal"
    wake_mac: str = Field(min_length=43, max_length=43, pattern=OPAQUE_TOKEN_PATTERN)


class PushNotificationRedeem(BaseModel):
    installation_id: UUID
    event_token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class PushNotificationResponse(BaseModel):
    kind: Literal["direct_message", "mention", "guild_message"]
    title: str
    body: str
    channel_ref: str
    message_ref: str
    sender_name: str | None = None
    sender_ref: str | None = None
    sender_avatar_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    sent_at: str
