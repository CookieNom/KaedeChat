from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, field_validator

from app.bots.application_contract import validate_application_https_url
from app.bots.command_contract import DISCORD_LOCALES
from app.core.model_validation import UnambiguousInputModel
from app.federation.schemas import SnowflakeString

DIRECTORY_MEDIA_LIMIT = 5
DIRECTORY_EXTERNAL_LINK_LIMIT = 5
DIRECTORY_DESCRIPTION_LIMIT = 1_000
DIRECTORY_LOCALE_LIMIT = len(DISCORD_LOCALES)


class _StrictDirectoryModel(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")


class DirectoryImageMedia(_StrictDirectoryModel):
    type: Literal["image"]
    asset_id: SnowflakeString

    @field_validator("asset_id")
    @classmethod
    def positive_asset_id(cls, value: str) -> str:
        if int(value) < 1:
            raise ValueError("directory media asset IDs must be positive")
        return value


class DirectoryYouTubeMedia(_StrictDirectoryModel):
    type: Literal["youtube"]
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")


DirectoryMedia = Annotated[
    DirectoryImageMedia | DirectoryYouTubeMedia,
    Field(discriminator="type"),
]


def _unique_directory_media(value: list[DirectoryMedia]) -> list[DirectoryMedia]:
    identities = [
        (item.type, item.asset_id if isinstance(item, DirectoryImageMedia) else item.video_id)
        for item in value
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("directory media entries must be unique")
    return value


DirectoryMediaList = Annotated[
    list[DirectoryMedia],
    Field(max_length=DIRECTORY_MEDIA_LIMIT),
    AfterValidator(_unique_directory_media),
]


class DirectoryExternalLink(_StrictDirectoryModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("directory external-link names must not be blank")
        return cleaned

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        validated = validate_application_https_url(value)
        if validated is None:  # pragma: no cover - the field is non-null
            raise ValueError("directory external-link URLs are required")
        return validated


def _unique_external_links(value: list[DirectoryExternalLink]) -> list[DirectoryExternalLink]:
    names = [item.name.casefold() for item in value]
    urls = [item.url for item in value]
    if len(names) != len(set(names)) or len(urls) != len(set(urls)):
        raise ValueError("directory external links must have unique names and URLs")
    return value


DirectoryExternalLinks = Annotated[
    list[DirectoryExternalLink],
    Field(max_length=DIRECTORY_EXTERNAL_LINK_LIMIT),
    AfterValidator(_unique_external_links),
]


def _directory_locale(value: str) -> str:
    if value not in DISCORD_LOCALES:
        raise ValueError(f"unsupported directory locale: {value}")
    return value


DirectoryLocale = Annotated[str, AfterValidator(_directory_locale)]


def _canonical_supported_locales(value: list[DirectoryLocale]) -> list[DirectoryLocale]:
    if len(value) != len(set(value)):
        raise ValueError("directory supported locales must be unique")
    return sorted(value)


DirectorySupportedLocales = Annotated[
    list[DirectoryLocale],
    Field(max_length=DIRECTORY_LOCALE_LIMIT),
    AfterValidator(_canonical_supported_locales),
]


def _clean_description_localizations(
    value: dict[DirectoryLocale, str],
) -> dict[DirectoryLocale, str]:
    cleaned: dict[DirectoryLocale, str] = {}
    for locale, description in value.items():
        text = description.strip()
        if not text or len(text) > DIRECTORY_DESCRIPTION_LIMIT:
            raise ValueError("localized directory descriptions must be 1-1000 characters")
        cleaned[locale] = text
    return {locale: cleaned[locale] for locale in sorted(cleaned)}


DirectoryDescriptionLocalizations = Annotated[
    dict[DirectoryLocale, str],
    Field(max_length=DIRECTORY_LOCALE_LIMIT),
    AfterValidator(_clean_description_localizations),
]


def validate_directory_localizations(
    supported_locales: list[str],
    descriptions: dict[str, str],
) -> None:
    if not set(descriptions) <= set(supported_locales):
        raise ValueError("localized directory descriptions require a supported locale")


def directory_image_asset_ids(media: Sequence[object]) -> list[int]:
    ids: list[int] = []
    for item in media:
        if isinstance(item, DirectoryImageMedia):
            ids.append(int(item.asset_id))
        elif isinstance(item, dict) and item.get("type") == "image":
            raw_id = item.get("asset_id")
            if isinstance(raw_id, str) and raw_id.isdigit() and int(raw_id) > 0:
                ids.append(int(raw_id))
    return ids


def append_directory_image(
    media: list[dict[str, object]], asset_id: int
) -> list[dict[str, object]]:
    if len(media) >= DIRECTORY_MEDIA_LIMIT:
        raise ValueError("directory media limit reached")
    return [*media, {"type": "image", "asset_id": str(asset_id)}]


def remove_directory_image(
    media: list[dict[str, object]], asset_id: int
) -> list[dict[str, object]]:
    expected = str(asset_id)
    return [
        item
        for item in media
        if not (item.get("type") == "image" and item.get("asset_id") == expected)
    ]
