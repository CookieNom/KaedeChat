from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.types import WireSnowflake
from app.media.processing import normalize_declared_type, sanitize_filename


class UploadTicketRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)
    encryption_mode: Literal["plaintext", "e2ee"] = "plaintext"
    encryption_protocol: Literal["kaede-file-v1"] | None = None

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        return sanitize_filename(value)

    @field_validator("content_type")
    @classmethod
    def safe_type(cls, value: str) -> str:
        return normalize_declared_type(value)

    @model_validator(mode="after")
    def encryption_metadata_is_consistent(self) -> UploadTicketRequest:
        if self.encryption_mode == "e2ee":
            if self.encryption_protocol != "kaede-file-v1":
                raise ValueError("encrypted uploads require kaede-file-v1")
            if self.filename != "encrypted-file" or self.content_type != "application/octet-stream":
                raise ValueError("encrypted uploads require opaque metadata")
        elif self.encryption_protocol is not None:
            raise ValueError("plaintext uploads cannot specify an encryption protocol")
        return self


class AssetCommitRequest(BaseModel):
    attachment_id: WireSnowflake


class EmojiCommitRequest(AssetCommitRequest):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")


class StickerCrop(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def contained(self) -> StickerCrop:
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("crop must be contained inside the image")
        return self


class StickerTicketRequest(UploadTicketRequest):
    crop: StickerCrop | None = None
    remove_background: bool = False


class StickerCommitRequest(AssetCommitRequest):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")
    description: str | None = Field(default=None, max_length=100)


AssetKind = Literal["avatar", "banner"]
GuildAssetKind = Literal["icon", "banner"]
