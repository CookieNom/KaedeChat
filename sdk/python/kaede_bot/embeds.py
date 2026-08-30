from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .refs import EntityRef

MAX_EMBEDS = 10
MAX_EMBED_CHARACTERS = 6_000


def _text(
    value: str | None,
    *,
    name: str,
    minimum: int = 0,
    maximum: int,
    meaningful: bool = False,
) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL characters")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum} characters")
    if meaningful and not value.strip():
        raise ValueError(f"{name} must contain a non-whitespace character")


def _boolean(value: bool, *, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")


def _http_url(value: str | None, *, name: str) -> None:
    if value is None:
        return
    _text(value, name=name, minimum=1, maximum=2_048)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"{name} must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} contains an invalid port") from exc


def _media_url(value: str | None, *, name: str) -> None:
    if value is None:
        return
    _text(value, name=name, minimum=1, maximum=2_048)
    if value.startswith("attachment://"):
        filename = value.removeprefix("attachment://")
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
        ):
            raise ValueError(f"{name} attachment URL must contain a single filename")
        return
    _http_url(value, name=name)


@dataclass(frozen=True, slots=True)
class PartialEmoji:
    id: EntityRef | None = None
    name: str | None = None
    animated: bool = False

    def __post_init__(self) -> None:
        if self.id is not None and not isinstance(self.id, EntityRef):
            raise TypeError("emoji id must be an EntityRef")
        _text(self.name, name="emoji name", minimum=1, maximum=64, meaningful=True)
        _boolean(self.animated, name="emoji animated")
        if self.id is None and self.name is None:
            raise ValueError("an emoji requires an ID or name")
        if self.animated and self.id is None:
            raise ValueError("only custom emoji can be animated")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"animated": self.animated}
        if self.id is not None:
            payload["id"] = str(self.id)
        if self.name is not None:
            payload["name"] = self.name
        return payload


@dataclass(frozen=True, slots=True)
class EmbedFooter:
    text: str
    icon_url: str | None = None

    def __post_init__(self) -> None:
        _text(
            self.text,
            name="embed footer text",
            minimum=1,
            maximum=2_048,
            meaningful=True,
        )
        _media_url(self.icon_url, name="embed footer icon URL")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"text": self.text}
        if self.icon_url is not None:
            payload["icon_url"] = self.icon_url
        return payload


@dataclass(frozen=True, slots=True)
class EmbedMedia:
    url: str

    def __post_init__(self) -> None:
        _media_url(self.url, name="embed media URL")

    def to_dict(self) -> dict[str, object]:
        return {"url": self.url}


@dataclass(frozen=True, slots=True)
class EmbedAuthor:
    name: str
    url: str | None = None
    icon_url: str | None = None

    def __post_init__(self) -> None:
        _text(
            self.name,
            name="embed author name",
            minimum=1,
            maximum=256,
            meaningful=True,
        )
        _http_url(self.url, name="embed author URL")
        _media_url(self.icon_url, name="embed author icon URL")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"name": self.name}
        if self.url is not None:
            payload["url"] = self.url
        if self.icon_url is not None:
            payload["icon_url"] = self.icon_url
        return payload


@dataclass(frozen=True, slots=True)
class EmbedField:
    name: str
    value: str
    inline: bool = False

    def __post_init__(self) -> None:
        _text(
            self.name,
            name="embed field name",
            minimum=1,
            maximum=256,
            meaningful=True,
        )
        _text(
            self.value,
            name="embed field value",
            minimum=1,
            maximum=1_024,
            meaningful=True,
        )
        _boolean(self.inline, name="embed field inline")

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "value": self.value, "inline": self.inline}


@dataclass(frozen=True, slots=True)
class Embed:
    title: str | None = None
    description: str | None = None
    url: str | None = None
    timestamp: datetime | None = None
    color: int | None = None
    footer: EmbedFooter | None = None
    image: EmbedMedia | None = None
    thumbnail: EmbedMedia | None = None
    author: EmbedAuthor | None = None
    fields: Sequence[EmbedField] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        _text(
            self.title,
            name="embed title",
            minimum=1,
            maximum=256,
            meaningful=True,
        )
        _text(
            self.description,
            name="embed description",
            minimum=1,
            maximum=4_096,
            meaningful=True,
        )
        _http_url(self.url, name="embed URL")
        if self.timestamp is not None:
            if not isinstance(self.timestamp, datetime):
                raise TypeError("embed timestamp must be a datetime")
            if self.timestamp.utcoffset() is None:
                raise ValueError("embed timestamp must include a timezone offset")
        if self.color is not None:
            if isinstance(self.color, bool) or not isinstance(self.color, int):
                raise TypeError("embed color must be an integer")
            if not 0 <= self.color <= 0xFFFFFF:
                raise ValueError("embed color must be a 24-bit integer")
        nested = (
            (self.footer, EmbedFooter, "embed footer"),
            (self.image, EmbedMedia, "embed image"),
            (self.thumbnail, EmbedMedia, "embed thumbnail"),
            (self.author, EmbedAuthor, "embed author"),
        )
        for value, expected, name in nested:
            if value is not None and not isinstance(value, expected):
                raise TypeError(f"{name} has an invalid type")
        if len(self.fields) > 25:
            raise ValueError("an embed can contain at most 25 fields")
        if not all(isinstance(item, EmbedField) for item in self.fields):
            raise TypeError("embed fields must be EmbedField instances")
        if not any(
            (
                self.title,
                self.description,
                self.url,
                self.timestamp,
                self.color is not None,
                self.footer,
                self.image,
                self.thumbnail,
                self.author,
                self.fields,
            )
        ):
            raise ValueError("an embed must contain at least one field")

    @property
    def character_count(self) -> int:
        total = len(self.title or "") + len(self.description or "")
        if self.footer is not None:
            total += len(self.footer.text)
        if self.author is not None:
            total += len(self.author.name)
        total += sum(len(item.name) + len(item.value) for item in self.fields)
        return total

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.title is not None:
            payload["title"] = self.title
        if self.description is not None:
            payload["description"] = self.description
        if self.url is not None:
            payload["url"] = self.url
        if self.timestamp is not None:
            payload["timestamp"] = self.timestamp.isoformat()
        if self.color is not None:
            payload["color"] = self.color
        if self.footer is not None:
            payload["footer"] = self.footer.to_dict()
        if self.image is not None:
            payload["image"] = self.image.to_dict()
        if self.thumbnail is not None:
            payload["thumbnail"] = self.thumbnail.to_dict()
        if self.author is not None:
            payload["author"] = self.author.to_dict()
        if self.fields:
            payload["fields"] = [item.to_dict() for item in self.fields]
        return payload


def validate_embeds(embeds: Sequence[Embed]) -> None:
    if len(embeds) > MAX_EMBEDS:
        raise ValueError(f"a message can contain at most {MAX_EMBEDS} embeds")
    if not all(isinstance(embed, Embed) for embed in embeds):
        raise TypeError("embeds must be Embed instances")
    if sum(embed.character_count for embed in embeds) > MAX_EMBED_CHARACTERS:
        raise ValueError(
            f"embed text cannot exceed {MAX_EMBED_CHARACTERS} characters per message"
        )


def serialize_embeds(embeds: Sequence[Embed]) -> list[dict[str, Any]]:
    validate_embeds(embeds)
    return [embed.to_dict() for embed in embeds]


__all__ = [
    "Embed",
    "EmbedAuthor",
    "EmbedField",
    "EmbedFooter",
    "EmbedMedia",
    "MAX_EMBED_CHARACTERS",
    "MAX_EMBEDS",
    "PartialEmoji",
    "serialize_embeds",
    "validate_embeds",
]
