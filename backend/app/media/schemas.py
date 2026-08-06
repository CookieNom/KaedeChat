from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.types import WireSnowflake
from app.media.processing import normalize_declared_type, sanitize_filename


class UploadTicketRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        return sanitize_filename(value)

    @field_validator("content_type")
    @classmethod
    def safe_type(cls, value: str) -> str:
        return normalize_declared_type(value)


class AssetCommitRequest(BaseModel):
    attachment_id: WireSnowflake


class EmojiCommitRequest(AssetCommitRequest):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")


AssetKind = Literal["avatar", "banner"]
GuildAssetKind = Literal["icon", "banner"]
