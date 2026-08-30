from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum
import re
from typing import Any, ClassVar, cast

from .embeds import PartialEmoji, _boolean, _http_url, _media_url, _text
from .refs import EntityRef

MAX_ACTION_ROWS = 5
MAX_COMPONENTS = 40
MAX_COMPONENT_ID = 0xFFFFFFFF
_CHANNEL_TYPES = {0, 1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15, 16, 17}


class ButtonStyle(IntEnum):
    primary = 1
    secondary = 2
    success = 3
    danger = 4
    link = 5
    premium = 6


class TextInputStyle(IntEnum):
    short = 1
    paragraph = 2


def _integer(value: int, *, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


@dataclass(frozen=True, slots=True)
class Button:
    type: ClassVar[int] = 2

    style: int | ButtonStyle = ButtonStyle.primary
    id: int | None = None
    label: str | None = None
    emoji: PartialEmoji | None = None
    custom_id: str | None = None
    url: str | None = None
    sku_id: EntityRef | None = None
    disabled: bool = False

    def __post_init__(self) -> None:
        _integer(self.style, name="button style", minimum=1, maximum=6)
        if self.id is not None:
            _integer(self.id, name="component id", minimum=0, maximum=MAX_COMPONENT_ID)
        _text(
            self.label,
            name="button label",
            minimum=1,
            maximum=80,
            meaningful=True,
        )
        _text(
            self.custom_id,
            name="button custom_id",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        _http_url(self.url, name="button URL")
        _text(self.url, name="button URL", maximum=512)
        if self.emoji is not None and not isinstance(self.emoji, PartialEmoji):
            raise TypeError("button emoji must be PartialEmoji")
        if self.sku_id is not None and not isinstance(self.sku_id, EntityRef):
            raise TypeError("button sku_id must be an EntityRef")
        _boolean(self.disabled, name="button disabled")
        if self.style == ButtonStyle.premium:
            if (
                self.sku_id is None
                or self.label is not None
                or self.emoji is not None
                or self.custom_id is not None
                or self.url is not None
            ):
                raise ValueError(
                    "a premium button requires only sku_id and cannot have label, emoji, custom_id, or url"
                )
        elif self.label is None and self.emoji is None:
            raise ValueError("a button requires a label or emoji")
        elif self.style == ButtonStyle.link:
            if (
                self.url is None
                or self.custom_id is not None
                or self.sku_id is not None
            ):
                raise ValueError(
                    "a link button requires url and cannot have custom_id or sku_id"
                )
        elif self.custom_id is None or self.url is not None:
            raise ValueError("a non-link button requires custom_id and cannot have url")
        elif self.sku_id is not None:
            raise ValueError("only premium buttons can have sku_id")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "style": int(self.style),
            "disabled": self.disabled,
        }
        if self.label is not None:
            payload["label"] = self.label
        if self.id is not None:
            payload["id"] = self.id
        if self.emoji is not None:
            payload["emoji"] = self.emoji.to_dict()
        if self.custom_id is not None:
            payload["custom_id"] = self.custom_id
        if self.url is not None:
            payload["url"] = self.url
        if self.sku_id is not None:
            payload["sku_id"] = str(self.sku_id)
        return payload


@dataclass(frozen=True, slots=True)
class SelectOption:
    label: str
    value: str
    description: str | None = None
    emoji: PartialEmoji | None = None
    default: bool = False

    def __post_init__(self) -> None:
        _text(
            self.label,
            name="select option label",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        if self.emoji is not None and not isinstance(self.emoji, PartialEmoji):
            raise TypeError("select option emoji must be PartialEmoji")
        _boolean(self.default, name="select option default")
        _text(
            self.value,
            name="select option value",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        _text(
            self.description,
            name="select option description",
            minimum=1,
            maximum=100,
            meaningful=True,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "label": self.label,
            "value": self.value,
            "default": self.default,
        }
        if self.description is not None:
            payload["description"] = self.description
        if self.emoji is not None:
            payload["emoji"] = self.emoji.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class SelectDefaultValue:
    id: EntityRef
    type: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, EntityRef):
            raise TypeError("select default id must be an EntityRef")
        if self.type not in {"user", "role", "channel"}:
            raise ValueError("select default type must be user, role, or channel")

    def to_dict(self) -> dict[str, object]:
        return {"id": str(self.id), "type": self.type}


def _validate_select(
    *,
    custom_id: str,
    placeholder: str | None,
    min_values: int,
    max_values: int,
    disabled: bool,
    required: bool | None,
    default_values: Sequence[SelectDefaultValue] = (),
) -> None:
    _text(
        custom_id,
        name="select custom_id",
        minimum=1,
        maximum=100,
        meaningful=True,
    )
    _text(
        placeholder,
        name="select placeholder",
        minimum=1,
        maximum=150,
        meaningful=True,
    )
    _integer(min_values, name="select min_values", minimum=0, maximum=25)
    _integer(max_values, name="select max_values", minimum=1, maximum=25)
    if min_values > max_values:
        raise ValueError("select min_values cannot exceed max_values")
    _boolean(disabled, name="select disabled")
    if required is not None:
        _boolean(required, name="select required")
    if len(default_values) > 25:
        raise ValueError("a select can contain at most 25 default values")
    if len({(item.id, item.type) for item in default_values}) != len(default_values):
        raise ValueError("select default values must be unique")
    if default_values and not min_values <= len(default_values) <= max_values:
        raise ValueError("default value count must be within the select value range")


def _select_payload(
    *,
    type: int,
    custom_id: str,
    placeholder: str | None,
    min_values: int,
    max_values: int,
    disabled: bool,
    required: bool | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": type,
        "custom_id": custom_id,
        "min_values": min_values,
        "max_values": max_values,
        "disabled": disabled,
    }
    if placeholder is not None:
        payload["placeholder"] = placeholder
    if required is not None:
        payload["required"] = required
    return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class StringSelect:
    type: ClassVar[int] = 3

    custom_id: str
    options: Sequence[SelectOption]
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    disabled: bool = False
    required: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        _validate_select(
            custom_id=self.custom_id,
            placeholder=self.placeholder,
            min_values=self.min_values,
            max_values=self.max_values,
            disabled=self.disabled,
            required=self.required,
        )
        if not 1 <= len(self.options) <= 25:
            raise ValueError("a string select requires between 1 and 25 options")
        if not all(isinstance(item, SelectOption) for item in self.options):
            raise TypeError("string select options must be SelectOption instances")
        if self.max_values > len(self.options):
            raise ValueError("select max_values cannot exceed its option count")
        values = [item.value for item in self.options]
        if len(values) != len(set(values)):
            raise ValueError("select option values must be unique")
        defaults = sum(item.default for item in self.options)
        if defaults and not self.min_values <= defaults <= self.max_values:
            raise ValueError(
                "default option count must be within the select value range"
            )

    def to_dict(self) -> dict[str, object]:
        payload = _select_payload(
            type=self.type,
            custom_id=self.custom_id,
            placeholder=self.placeholder,
            min_values=self.min_values,
            max_values=self.max_values,
            disabled=self.disabled,
            required=self.required,
        )
        payload["options"] = [item.to_dict() for item in self.options]
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class UserSelect:
    type: ClassVar[int] = 5

    custom_id: str
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    disabled: bool = False
    required: bool | None = None
    default_values: Sequence[SelectDefaultValue] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_values", tuple(self.default_values))
        _validate_select_instance(self, {"user"})

    def to_dict(self) -> dict[str, object]:
        return _select_with_defaults_payload(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class RoleSelect:
    type: ClassVar[int] = 6

    custom_id: str
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    disabled: bool = False
    required: bool | None = None
    default_values: Sequence[SelectDefaultValue] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_values", tuple(self.default_values))
        _validate_select_instance(self, {"role"})

    def to_dict(self) -> dict[str, object]:
        return _select_with_defaults_payload(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class MentionableSelect:
    type: ClassVar[int] = 7

    custom_id: str
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    disabled: bool = False
    required: bool | None = None
    default_values: Sequence[SelectDefaultValue] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_values", tuple(self.default_values))
        _validate_select_instance(self, {"user", "role"})

    def to_dict(self) -> dict[str, object]:
        return _select_with_defaults_payload(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelSelect:
    type: ClassVar[int] = 8

    custom_id: str
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    disabled: bool = False
    required: bool | None = None
    default_values: Sequence[SelectDefaultValue] = field(default_factory=tuple)
    channel_types: Sequence[int] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_values", tuple(self.default_values))
        object.__setattr__(self, "channel_types", tuple(self.channel_types))
        _validate_select_instance(self, {"channel"})
        if len(self.channel_types) != len(set(self.channel_types)):
            raise ValueError("channel select channel_types must be unique")
        if any(item not in _CHANNEL_TYPES for item in self.channel_types):
            raise ValueError("channel select contains an unsupported channel type")

    def to_dict(self) -> dict[str, object]:
        payload = _select_with_defaults_payload(self)
        if self.channel_types:
            payload["channel_types"] = list(self.channel_types)
        return payload


AutoSelect = UserSelect | RoleSelect | MentionableSelect | ChannelSelect


def _validate_select_instance(select: AutoSelect, allowed_types: set[str]) -> None:
    if not all(isinstance(item, SelectDefaultValue) for item in select.default_values):
        raise TypeError("select defaults must be SelectDefaultValue instances")
    if any(item.type not in allowed_types for item in select.default_values):
        raise ValueError("select contains an incompatible default value type")
    _validate_select(
        custom_id=select.custom_id,
        placeholder=select.placeholder,
        min_values=select.min_values,
        max_values=select.max_values,
        disabled=select.disabled,
        required=select.required,
        default_values=select.default_values,
    )


def _select_with_defaults_payload(select: AutoSelect) -> dict[str, object]:
    payload = _select_payload(
        type=select.type,
        custom_id=select.custom_id,
        placeholder=select.placeholder,
        min_values=select.min_values,
        max_values=select.max_values,
        disabled=select.disabled,
        required=select.required,
    )
    if select.default_values:
        payload["default_values"] = [item.to_dict() for item in select.default_values]
    return payload


@dataclass(frozen=True, slots=True)
class TextInput:
    type: ClassVar[int] = 4

    custom_id: str
    label: str | None = None
    style: int | TextInputStyle = TextInputStyle.short
    min_length: int | None = None
    max_length: int | None = None
    required: bool = True
    value: str | None = None
    placeholder: str | None = None

    def __post_init__(self) -> None:
        _text(
            self.custom_id,
            name="text input custom_id",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        _text(
            self.label, name="text input label", minimum=1, maximum=45, meaningful=True
        )
        _integer(self.style, name="text input style", minimum=1, maximum=2)
        if self.min_length is not None:
            _integer(
                self.min_length, name="text input min_length", minimum=0, maximum=4_000
            )
        if self.max_length is not None:
            _integer(
                self.max_length, name="text input max_length", minimum=1, maximum=4_000
            )
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("text input min_length cannot exceed max_length")
        _text(self.value, name="text input value", maximum=4_000)
        _text(self.placeholder, name="text input placeholder", maximum=100)
        _boolean(self.required, name="text input required")
        if self.value is not None:
            if self.min_length is not None and len(self.value) < self.min_length:
                raise ValueError("text input value is shorter than min_length")
            if self.max_length is not None and len(self.value) > self.max_length:
                raise ValueError("text input value is longer than max_length")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "custom_id": self.custom_id,
            "style": int(self.style),
            "required": self.required,
        }
        if self.label is not None:
            payload["label"] = self.label
        if self.min_length is not None:
            payload["min_length"] = self.min_length
        if self.max_length is not None:
            payload["max_length"] = self.max_length
        if self.value is not None:
            payload["value"] = self.value
        if self.placeholder is not None:
            payload["placeholder"] = self.placeholder
        return payload


def _component_id(value: int | None) -> None:
    if value is not None:
        _integer(value, name="component id", minimum=0, maximum=MAX_COMPONENT_ID)


def _with_id(payload: dict[str, object], value: int | None) -> dict[str, object]:
    if value is not None:
        payload["id"] = value
    return payload


@dataclass(frozen=True, slots=True)
class MediaItem:
    url: str

    def __post_init__(self) -> None:
        _media_url(self.url, name="component media URL")
        _text(self.url, name="component media URL", minimum=1, maximum=2_048)

    def to_dict(self) -> dict[str, object]:
        return {"url": self.url}


@dataclass(frozen=True, slots=True)
class TextDisplay:
    type: ClassVar[int] = 10
    content: str
    id: int | None = None

    def __post_init__(self) -> None:
        _text(
            self.content,
            name="text display content",
            minimum=1,
            maximum=4_000,
            meaningful=True,
        )
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id({"type": self.type, "content": self.content}, self.id)


@dataclass(frozen=True, slots=True)
class Thumbnail:
    type: ClassVar[int] = 11
    media: MediaItem
    description: str | None = None
    spoiler: bool = False
    id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.media, MediaItem):
            raise TypeError("thumbnail media must be a MediaItem")
        _text(self.description, name="thumbnail description", maximum=1_024)
        _boolean(self.spoiler, name="thumbnail spoiler")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "media": self.media.to_dict(),
            "spoiler": self.spoiler,
        }
        if self.description is not None:
            payload["description"] = self.description
        return _with_id(payload, self.id)


@dataclass(frozen=True, slots=True)
class Section:
    type: ClassVar[int] = 9
    components: Sequence[TextDisplay]
    accessory: Button | Thumbnail
    id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        if not 1 <= len(self.components) <= 3 or not all(
            isinstance(item, TextDisplay) for item in self.components
        ):
            raise ValueError("a section requires between one and three text displays")
        if not isinstance(self.accessory, (Button, Thumbnail)):
            raise TypeError("a section accessory must be a Button or Thumbnail")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {
                "type": self.type,
                "components": [item.to_dict() for item in self.components],
                "accessory": self.accessory.to_dict(),
            },
            self.id,
        )


@dataclass(frozen=True, slots=True)
class MediaGalleryItem:
    media: MediaItem
    description: str | None = None
    spoiler: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.media, MediaItem):
            raise TypeError("gallery media must be a MediaItem")
        _text(self.description, name="gallery description", maximum=1_024)
        _boolean(self.spoiler, name="gallery spoiler")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "media": self.media.to_dict(),
            "spoiler": self.spoiler,
        }
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True, slots=True)
class MediaGallery:
    type: ClassVar[int] = 12
    items: Sequence[MediaGalleryItem]
    id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if not 1 <= len(self.items) <= 10 or not all(
            isinstance(item, MediaGalleryItem) for item in self.items
        ):
            raise ValueError("a media gallery requires between one and ten items")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {"type": self.type, "items": [item.to_dict() for item in self.items]},
            self.id,
        )


@dataclass(frozen=True, slots=True)
class FileComponent:
    type: ClassVar[int] = 13
    file: MediaItem
    spoiler: bool = False
    id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.file, MediaItem) or not self.file.url.startswith(
            "attachment://"
        ):
            raise ValueError("a file component must use an attachment:// MediaItem")
        _boolean(self.spoiler, name="file spoiler")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {"type": self.type, "file": self.file.to_dict(), "spoiler": self.spoiler},
            self.id,
        )


@dataclass(frozen=True, slots=True)
class Separator:
    type: ClassVar[int] = 14
    divider: bool = True
    spacing: int = 1
    id: int | None = None

    def __post_init__(self) -> None:
        _boolean(self.divider, name="separator divider")
        _integer(self.spacing, name="separator spacing", minimum=1, maximum=2)
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {"type": self.type, "divider": self.divider, "spacing": self.spacing},
            self.id,
        )


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    label: str
    value: str
    description: str | None = None
    default: bool = False

    def __post_init__(self) -> None:
        _text(self.label, name="choice label", minimum=1, maximum=100, meaningful=True)
        _text(self.value, name="choice value", minimum=1, maximum=100, meaningful=True)
        _text(self.description, name="choice description", maximum=100)
        _boolean(self.default, name="choice default")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "label": self.label,
            "value": self.value,
            "default": self.default,
        }
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True, slots=True)
class FileUpload:
    type: ClassVar[int] = 19
    custom_id: str
    min_values: int = 1
    max_values: int = 1
    required: bool = True
    file_types: Sequence[str] = field(default_factory=tuple)
    id: int | None = None

    def __post_init__(self) -> None:
        _text(
            self.custom_id,
            name="file upload custom_id",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        _integer(self.min_values, name="file upload min_values", minimum=0, maximum=10)
        _integer(self.max_values, name="file upload max_values", minimum=1, maximum=10)
        if self.min_values > self.max_values:
            raise ValueError("file upload min_values cannot exceed max_values")
        object.__setattr__(
            self, "file_types", tuple(item.lower() for item in self.file_types)
        )
        if len(self.file_types) > 10 or len(set(self.file_types)) != len(
            self.file_types
        ):
            raise ValueError("file upload accepts at most ten unique file types")
        for item in self.file_types:
            if item not in {"image", "video", "audio"} and not (
                len(item) <= 65
                and re.fullmatch(r"\.[a-z0-9][a-z0-9._+-]*", item) is not None
            ):
                raise ValueError(
                    "file types must be media categories or dot extensions"
                )
        if self.required and self.min_values == 0:
            raise ValueError(
                "a required file upload must have min_values of at least one"
            )
        _boolean(self.required, name="file upload required")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {
                "type": self.type,
                "custom_id": self.custom_id,
                "min_values": self.min_values,
                "max_values": self.max_values,
                "required": self.required,
                "file_types": list(self.file_types),
            },
            self.id,
        )


@dataclass(frozen=True, slots=True)
class RadioGroup:
    type: ClassVar[int] = 21
    custom_id: str
    options: Sequence[ChoiceOption]
    required: bool = True
    id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        _text(
            self.custom_id,
            name="radio custom_id",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        if not 2 <= len(self.options) <= 10 or not all(
            isinstance(item, ChoiceOption) for item in self.options
        ):
            raise ValueError("a radio group requires between two and ten options")
        if len({item.value for item in self.options}) != len(self.options):
            raise ValueError("radio option values must be unique")
        if sum(item.default for item in self.options) > 1:
            raise ValueError("a radio group can have at most one default")
        _boolean(self.required, name="radio required")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {
                "type": self.type,
                "custom_id": self.custom_id,
                "options": [item.to_dict() for item in self.options],
                "required": self.required,
            },
            self.id,
        )


@dataclass(frozen=True, slots=True)
class CheckboxGroup:
    type: ClassVar[int] = 22
    custom_id: str
    options: Sequence[ChoiceOption]
    min_values: int = 1
    max_values: int | None = None
    required: bool = True
    id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        _text(
            self.custom_id,
            name="checkbox group custom_id",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        if not 1 <= len(self.options) <= 10 or not all(
            isinstance(item, ChoiceOption) for item in self.options
        ):
            raise ValueError("a checkbox group requires between one and ten options")
        _integer(
            self.min_values, name="checkbox group min_values", minimum=0, maximum=10
        )
        maximum = len(self.options) if self.max_values is None else self.max_values
        object.__setattr__(self, "max_values", maximum)
        _integer(maximum, name="checkbox group max_values", minimum=1, maximum=10)
        if self.min_values > maximum or maximum > len(self.options):
            raise ValueError("checkbox group limits must fit its option count")
        if len({item.value for item in self.options}) != len(self.options):
            raise ValueError("checkbox option values must be unique")
        _boolean(self.required, name="checkbox group required")
        if self.required and self.min_values == 0:
            raise ValueError(
                "a required checkbox group must have min_values of at least one"
            )
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {
                "type": self.type,
                "custom_id": self.custom_id,
                "options": [item.to_dict() for item in self.options],
                "min_values": self.min_values,
                "max_values": self.max_values,
                "required": self.required,
            },
            self.id,
        )


@dataclass(frozen=True, slots=True)
class CheckboxV2:
    type: ClassVar[int] = 23
    custom_id: str
    default: bool = False
    id: int | None = None

    def __post_init__(self) -> None:
        _text(
            self.custom_id,
            name="checkbox custom_id",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        _boolean(self.default, name="checkbox default")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {"type": self.type, "custom_id": self.custom_id, "default": self.default},
            self.id,
        )


ModalV2Input = (
    TextInput
    | StringSelect
    | UserSelect
    | RoleSelect
    | MentionableSelect
    | ChannelSelect
    | FileUpload
    | RadioGroup
    | CheckboxGroup
    | CheckboxV2
)
_MODAL_V2_INPUT_TYPES = (
    TextInput,
    StringSelect,
    UserSelect,
    RoleSelect,
    MentionableSelect,
    ChannelSelect,
    FileUpload,
    RadioGroup,
    CheckboxGroup,
    CheckboxV2,
)


@dataclass(frozen=True, slots=True)
class Label:
    type: ClassVar[int] = 18
    label: str
    component: ModalV2Input
    description: str | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        _text(self.label, name="label", minimum=1, maximum=45, meaningful=True)
        _text(self.description, name="label description", maximum=100)
        if not isinstance(self.component, _MODAL_V2_INPUT_TYPES):
            raise TypeError("a label contains one supported modal input")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "label": self.label,
            "component": self.component.to_dict(),
        }
        if self.description is not None:
            payload["description"] = self.description
        return _with_id(payload, self.id)


Component = (
    Button
    | StringSelect
    | UserSelect
    | RoleSelect
    | MentionableSelect
    | ChannelSelect
    | TextInput
)
_COMPONENT_TYPES = (
    Button,
    StringSelect,
    UserSelect,
    RoleSelect,
    MentionableSelect,
    ChannelSelect,
    TextInput,
)


@dataclass(frozen=True, slots=True)
class ActionRow:
    type: ClassVar[int] = 1

    components: Sequence[Component]
    id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        _component_id(self.id)
        if not 1 <= len(self.components) <= 5:
            raise ValueError("an action row requires between 1 and 5 components")
        if not all(isinstance(item, _COMPONENT_TYPES) for item in self.components):
            raise TypeError("action row contains an unsupported component")
        first = self.components[0]
        if isinstance(first, Button):
            if not all(isinstance(item, Button) for item in self.components):
                raise ValueError(
                    "buttons cannot share an action row with other components"
                )
        elif len(self.components) != 1:
            raise ValueError(
                "selects and text inputs must be the only item in their action row"
            )

    def to_dict(self) -> dict[str, object]:
        return _with_id(
            {
                "type": self.type,
                "components": [item.to_dict() for item in self.components],
            },
            self.id,
        )


ContainerChild = (
    ActionRow | TextDisplay | Section | MediaGallery | Separator | FileComponent
)
_CONTAINER_CHILD_TYPES = (
    ActionRow,
    TextDisplay,
    Section,
    MediaGallery,
    Separator,
    FileComponent,
)


@dataclass(frozen=True, slots=True)
class Container:
    type: ClassVar[int] = 17
    components: Sequence[ContainerChild]
    accent_color: int | None = None
    spoiler: bool = False
    id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        if not 1 <= len(self.components) <= MAX_COMPONENTS or not all(
            isinstance(item, _CONTAINER_CHILD_TYPES) for item in self.components
        ):
            raise ValueError("a container requires supported child components")
        if self.accent_color is not None:
            _integer(
                self.accent_color,
                name="container accent_color",
                minimum=0,
                maximum=0xFFFFFF,
            )
        _boolean(self.spoiler, name="container spoiler")
        _component_id(self.id)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "components": [item.to_dict() for item in self.components],
            "spoiler": self.spoiler,
        }
        if self.accent_color is not None:
            payload["accent_color"] = self.accent_color
        return _with_id(payload, self.id)


LayoutComponent = (
    ActionRow
    | Section
    | TextDisplay
    | MediaGallery
    | FileComponent
    | Separator
    | Container
)
_LAYOUT_COMPONENT_TYPES = (
    ActionRow,
    Section,
    TextDisplay,
    MediaGallery,
    FileComponent,
    Separator,
    Container,
)


def _walk_layout(component: object) -> list[object]:
    result = [component]
    if isinstance(component, (ActionRow, Section, Container)):
        for child in component.components:
            result.extend(_walk_layout(child))
    if isinstance(component, Section):
        result.extend(_walk_layout(component.accessory))
    return result


def _interactive_items(layouts: Sequence[LayoutComponent]) -> list[Component]:
    return [
        item
        for layout in layouts
        for item in _walk_layout(layout)
        if isinstance(item, _COMPONENT_TYPES)
    ]


def _validate_layouts(layouts: Sequence[LayoutComponent]) -> None:
    if not layouts or not all(
        isinstance(item, _LAYOUT_COMPONENT_TYPES) for item in layouts
    ):
        raise TypeError("a view requires supported message components")
    flattened = [item for layout in layouts for item in _walk_layout(layout)]
    if len(flattened) > MAX_COMPONENTS:
        raise ValueError(
            f"a message can contain at most {MAX_COMPONENTS} nested components"
        )
    if (
        all(isinstance(item, ActionRow) for item in layouts)
        and len(layouts) > MAX_ACTION_ROWS
    ):
        raise ValueError(f"a legacy view can contain at most {MAX_ACTION_ROWS} rows")
    if any(isinstance(item, TextInput) for item in flattened):
        raise ValueError("text inputs are only valid in modals")
    custom_ids = [
        getattr(item, "custom_id")
        for item in flattened
        if getattr(item, "custom_id", None) is not None
    ]
    if len(custom_ids) != len(set(custom_ids)):
        raise ValueError("component custom_ids must be unique")


def _set_layout_disabled(layout: LayoutComponent, disabled: bool) -> LayoutComponent:
    if isinstance(layout, ActionRow):
        return replace(
            layout,
            components=tuple(
                replace(cast(Any, item), disabled=disabled)
                if hasattr(item, "disabled")
                else item
                for item in layout.components
            ),
        )
    if isinstance(layout, Section) and isinstance(layout.accessory, Button):
        return replace(layout, accessory=replace(layout.accessory, disabled=disabled))
    if isinstance(layout, Container):
        return replace(
            layout,
            components=tuple(
                cast(ContainerChild, _set_layout_disabled(item, disabled))
                for item in layout.components
            ),
        )
    return layout


def _validate_rows(rows: Sequence[ActionRow], *, modal: bool) -> None:
    if not 1 <= len(rows) <= MAX_ACTION_ROWS:
        raise ValueError(f"between 1 and {MAX_ACTION_ROWS} action rows are required")
    if not all(isinstance(row, ActionRow) for row in rows):
        raise TypeError("rows must be ActionRow instances")
    custom_ids: list[str] = []
    for row in rows:
        for item in row.components:
            custom_id = getattr(item, "custom_id", None)
            if custom_id is not None:
                custom_ids.append(custom_id)
        if modal:
            if len(row.components) != 1 or not isinstance(
                row.components[0],
                (
                    TextInput,
                    StringSelect,
                    UserSelect,
                    RoleSelect,
                    MentionableSelect,
                    ChannelSelect,
                ),
            ):
                raise ValueError(
                    "modal rows must contain one text input, select, or Kaede checkbox"
                )
            if getattr(row.components[0], "disabled", False):
                raise ValueError("modal inputs cannot be disabled")
        elif any(isinstance(item, TextInput) for item in row.components):
            raise ValueError("text inputs are only valid in modals")
    if len(custom_ids) != len(set(custom_ids)):
        raise ValueError("component custom_ids must be unique")


@dataclass(frozen=True, slots=True)
class Modal:
    title: str
    custom_id: str
    components: Sequence[ActionRow | Label | TextDisplay]

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        _text(
            self.title,
            name="modal title",
            minimum=1,
            maximum=45,
            meaningful=True,
        )
        _text(
            self.custom_id,
            name="modal custom_id",
            minimum=1,
            maximum=100,
            meaningful=True,
        )
        if not 1 <= len(self.components) <= MAX_ACTION_ROWS:
            raise ValueError(
                f"between 1 and {MAX_ACTION_ROWS} modal components are required"
            )
        custom_ids: list[str] = []
        for top_level in self.components:
            if isinstance(top_level, TextDisplay):
                continue
            if isinstance(top_level, Label):
                modal_input = top_level.component
                if (
                    isinstance(
                        modal_input,
                        (
                            StringSelect,
                            UserSelect,
                            RoleSelect,
                            MentionableSelect,
                            ChannelSelect,
                        ),
                    )
                    and modal_input.required is not False
                    and modal_input.min_values == 0
                ):
                    raise ValueError(
                        "a required modal select must have min_values of at least one"
                    )
                custom_ids.append(modal_input.custom_id)
                continue
            if (
                not isinstance(top_level, ActionRow)
                or len(top_level.components) != 1
                or not isinstance(top_level.components[0], TextInput)
            ):
                raise ValueError(
                    "modal selects and Components V2 inputs must be inside a Label"
                )
            modal_input = top_level.components[0]
            if isinstance(modal_input, TextInput) and modal_input.label is None:
                raise ValueError("legacy modal text inputs require their own label")
            if getattr(modal_input, "disabled", False):
                raise ValueError("modal inputs cannot be disabled")
            custom_ids.append(modal_input.custom_id)
        if len(custom_ids) != len(set(custom_ids)):
            raise ValueError("modal component custom_ids must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "custom_id": self.custom_id,
            "components": [row.to_dict() for row in self.components],
        }


@dataclass(slots=True)
class View:
    rows: list[LayoutComponent] = field(default_factory=list)
    timeout: float | None = 180.0
    callbacks: dict[str, Callable[[Any], Awaitable[None]]] = field(default_factory=dict)
    disable_on_timeout: bool = True
    _timeout_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _cleanup: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _timeout_error: Callable[[Exception], Awaitable[None]] | None = field(
        default=None, init=False, repr=False
    )
    _timeout_action: Callable[[], Awaitable[None]] | None = field(
        default=None, init=False, repr=False
    )
    _waiters: list[asyncio.Future[bool]] = field(
        default_factory=list, init=False, repr=False
    )
    _finished: bool = field(default=False, init=False, repr=False)
    _timed_out: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.rows = list(self.rows)
        if self.timeout is not None:
            if isinstance(self.timeout, bool) or not isinstance(
                self.timeout, (int, float)
            ):
                raise TypeError("view timeout must be a number or None")
            if self.timeout <= 0:
                raise ValueError("view timeout must be positive")
            if self.timeout > 86_400:
                raise ValueError("view timeout cannot exceed 86400 seconds")
        if self.rows:
            _validate_layouts(self.rows)
        _boolean(self.disable_on_timeout, name="view disable_on_timeout")
        known_ids = {
            getattr(item, "custom_id", None) for item in _interactive_items(self.rows)
        }
        if any(custom_id not in known_ids for custom_id in self.callbacks):
            raise ValueError("view callback custom_id is not present in the view")

    def add_row(self, row: LayoutComponent) -> View:
        candidate = [*self.rows, row]
        _validate_layouts(candidate)
        self.rows.append(row)
        return self

    def set_callback(
        self,
        custom_id: str,
        callback: Callable[[Any], Awaitable[None]],
    ) -> View:
        known_ids = {
            getattr(item, "custom_id", None) for item in _interactive_items(self.rows)
        }
        if custom_id not in known_ids:
            raise ValueError("callback custom_id is not present in the view")
        self.callbacks[custom_id] = callback
        return self

    async def dispatch(self, interaction: Any) -> bool:
        if self._finished:
            return False
        custom_id = getattr(interaction, "custom_id", None)
        if not isinstance(custom_id, str):
            return False
        callback = self.callbacks.get(custom_id)
        if callback is None:
            return False
        component = next(
            (
                item
                for item in _interactive_items(self.rows)
                if getattr(item, "custom_id", None) == custom_id
            ),
            None,
        )
        try:
            if not await self.interaction_check(interaction):
                return False
            await callback(interaction)
        except Exception as error:
            await self.on_error(interaction, error, component)
        return True

    async def interaction_check(self, interaction: Any) -> bool:
        """Return whether an interaction may invoke this view's callback."""

        del interaction
        return True

    async def on_error(
        self,
        interaction: Any,
        error: Exception,
        item: Component | None,
    ) -> None:
        """Handle a check or callback exception; override to consume it."""

        del interaction, item
        raise error

    async def on_timeout(self) -> None:
        """Run after local timeout state and optional disabling are applied."""

    def disable_all_items(self) -> None:
        self.rows = [_set_layout_disabled(row, True) for row in self.rows]

    def enable_all_items(self) -> None:
        self.rows = [_set_layout_disabled(row, False) for row in self.rows]

    def _start_listening(
        self,
        cleanup: Callable[[], None],
        timeout_error: Callable[[Exception], Awaitable[None]] | None = None,
        timeout_action: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if self._finished:
            raise RuntimeError("a finished view cannot be registered again")
        self._cleanup = cleanup
        self._timeout_error = timeout_error
        self._timeout_action = timeout_action
        if self.timeout is None or self._timeout_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Persistent views are commonly registered before the event loop.
            # A finite view registered that early starts when first dispatched.
            self._timeout_task = None
        else:
            self._timeout_task = loop.create_task(self._run_timeout())

    async def _run_timeout(self) -> None:
        try:
            await asyncio.sleep(float(self.timeout or 0))
            self._timed_out = True
            if self.disable_on_timeout:
                self.disable_all_items()
                if self._timeout_action is not None:
                    try:
                        await self._timeout_action()
                    except Exception as error:
                        if self._timeout_error is not None:
                            await self._timeout_error(error)
            await self.on_timeout()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._timeout_error is not None:
                await self._timeout_error(error)
        finally:
            if not self._finished:
                self._finish(timed_out=True)

    def _finish(self, *, timed_out: bool) -> None:
        self._finished = True
        self._timed_out = timed_out
        cleanup, self._cleanup = self._cleanup, None
        self._timeout_action = None
        self._timeout_error = None
        if cleanup is not None:
            cleanup()
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(timed_out)
        self._waiters.clear()

    def stop(self) -> None:
        """Stop dispatch, cancel timeout work, and unregister this view."""

        if self._finished:
            return
        task, self._timeout_task = self._timeout_task, None
        if task is not None:
            task.cancel()
        self._finish(timed_out=False)

    async def wait(self) -> bool:
        """Wait for stop/timeout; return True only when the view timed out."""

        if self._finished:
            return self._timed_out
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        return await future

    def is_finished(self) -> bool:
        return self._finished

    @property
    def custom_ids(self) -> tuple[str, ...]:
        return tuple(
            cast(str, item.custom_id)
            for item in _interactive_items(self.rows)
            if getattr(item, "custom_id", None) is not None
        )

    @property
    def is_persistent(self) -> bool:
        if self.timeout is not None or not self.rows:
            return False
        return all(
            getattr(item, "custom_id", None) is not None
            for item in _interactive_items(self.rows)
        )

    @property
    def is_components_v2(self) -> bool:
        """Whether this view requires the immutable Components V2 flag."""

        return any(not isinstance(item, ActionRow) for item in self.rows)

    def to_components(self) -> list[dict[str, Any]]:
        if not self.rows:
            return []
        _validate_layouts(self.rows)
        return [row.to_dict() for row in self.rows]


__all__ = [
    "ActionRow",
    "Button",
    "ButtonStyle",
    "ChannelSelect",
    "CheckboxGroup",
    "CheckboxV2",
    "ChoiceOption",
    "Component",
    "Container",
    "FileComponent",
    "FileUpload",
    "Label",
    "LayoutComponent",
    "MAX_ACTION_ROWS",
    "MAX_COMPONENTS",
    "MediaGallery",
    "MediaGalleryItem",
    "MediaItem",
    "MentionableSelect",
    "Modal",
    "RadioGroup",
    "RoleSelect",
    "SelectDefaultValue",
    "SelectOption",
    "Section",
    "Separator",
    "StringSelect",
    "TextDisplay",
    "TextInput",
    "TextInputStyle",
    "Thumbnail",
    "UserSelect",
    "View",
]
