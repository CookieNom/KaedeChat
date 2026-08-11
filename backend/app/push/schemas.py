from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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
