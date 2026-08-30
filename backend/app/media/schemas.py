from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.core.model_validation import UnambiguousInputModel
from app.core.types import EntityRef, WireSnowflake
from app.media.processing import normalize_declared_type, sanitize_filename

DISCORD_EMOJI_MAX_BYTES = 256 * 1024
DISCORD_STICKER_MAX_BYTES = 512 * 1024


def clean_sticker_name(value: str) -> str:
    cleaned = value.strip()
    if not 2 <= len(cleaned) <= 30:
        raise ValueError("sticker names must contain 2 to 30 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("sticker names cannot contain control characters")
    return cleaned


def clean_sticker_description(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not 2 <= len(cleaned) <= 100:
        raise ValueError("sticker descriptions must contain 2 to 100 characters")
    return cleaned


def clean_sticker_tags(value: list[str]) -> list[str]:
    cleaned = [item.strip() for item in value]
    if any(not item or len(item) > 100 or "," in item for item in cleaned):
        raise ValueError("sticker tags must contain 1 to 100 characters and cannot contain commas")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("sticker tags must be unique")
    if len(",".join(cleaned)) > 200:
        raise ValueError("sticker tags cannot exceed 200 serialized characters")
    return cleaned


def validate_voice_attachment_metadata(
    *,
    content_type: str,
    encryption_mode: str,
    duration_secs: float | None,
    waveform: str | None,
) -> None:
    if (duration_secs is None) != (waveform is None):
        raise ValueError("voice uploads require both duration_secs and waveform")
    if waveform is None:
        return
    if (
        duration_secs is None
        or not 0 < duration_secs <= 1_200
        or encryption_mode != "plaintext"
        or not content_type.startswith("audio/")
    ):
        raise ValueError("voice metadata requires a bounded plaintext audio attachment")
    try:
        samples = base64.b64decode(waveform, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("waveform must be canonical base64") from None
    if not 1 <= len(samples) <= 256 or base64.b64encode(samples).decode() != waveform:
        raise ValueError("waveform must contain 1 to 256 byte samples")


class UploadTicketRequest(UnambiguousInputModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)
    encryption_mode: Literal["plaintext", "e2ee"] = "plaintext"
    encryption_protocol: Literal["kaede-file-v1"] | None = None
    duration_secs: float | None = Field(default=None, gt=0, le=1_200, allow_inf_nan=False)
    waveform: str | None = Field(default=None, min_length=4, max_length=344)

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
        validate_voice_attachment_metadata(
            content_type=self.content_type,
            encryption_mode=self.encryption_mode,
            duration_secs=self.duration_secs,
            waveform=self.waveform,
        )
        return self


class AssetCommitRequest(UnambiguousInputModel):
    attachment_id: WireSnowflake


class EmojiCommitRequest(AssetCommitRequest):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")
    role_ids: list[EntityRef] = Field(default_factory=list, max_length=100)

    @field_validator("role_ids")
    @classmethod
    def unique_roles(cls, value: list[EntityRef]) -> list[EntityRef]:
        if len(value) != len(set(value)):
            raise ValueError("emoji role restrictions must be unique")
        return value


class StickerCrop(UnambiguousInputModel):
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
    name: str = Field(min_length=2, max_length=30)
    description: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return clean_sticker_name(value)

    @field_validator("description")
    @classmethod
    def valid_description(cls, value: str | None) -> str | None:
        return clean_sticker_description(value)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value: list[str]) -> list[str]:
        return clean_sticker_tags(value)


AssetKind = Literal["avatar", "banner"]
GuildAssetKind = Literal["icon", "banner"]
