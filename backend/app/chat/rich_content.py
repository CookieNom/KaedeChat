from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.core.file_types import normalize_file_types
from app.core.model_validation import UnambiguousInputModel
from app.core.types import EntityRef

MAX_EMBEDS = 10
MAX_EMBED_CHARACTERS = 6_000
MAX_ACTION_ROWS = 5
MAX_COMPONENTS = 40
MAX_COMPONENT_ID = 0xFFFFFFFF
SUPPORTED_CHANNEL_TYPES = {0, 1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15, 16, 17}
EMBED_ATTACHMENT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _meaningful(value: str, *, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must contain a non-whitespace character")
    return value


def _validate_http_url(value: str) -> str:
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("URL must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL port is invalid") from exc
    return value


def _validate_media_url(value: str) -> str:
    if value.startswith("attachment://"):
        filename = value.removeprefix("attachment://")
        if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
            raise ValueError("attachment URL must contain a single filename")
        return value
    return _validate_http_url(value)


def _validate_embed_media_url(value: str) -> str:
    validated = _validate_media_url(value)
    if validated.startswith("attachment://") and not validated.lower().endswith(
        EMBED_ATTACHMENT_IMAGE_EXTENSIONS
    ):
        raise ValueError("embed attachments must be JPG, JPEG, PNG, WEBP, or GIF images")
    return validated


def _attachment_url_filenames(value: object) -> set[str]:
    """Collect attachment-backed media names from validated rich content.

    Rich content is represented by Pydantic models on local writes and plain
    mappings on federation/history projections.  Keeping the traversal here
    gives every admission path the same Discord-compatible reference rule.
    """

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=True)
    if isinstance(value, Mapping):
        filenames: set[str] = set()
        for key, child in value.items():
            if (
                key in {"url", "icon_url"}
                and isinstance(child, str)
                and child.startswith("attachment://")
            ):
                filenames.add(child.removeprefix("attachment://"))
            filenames.update(_attachment_url_filenames(child))
        return filenames
    if isinstance(value, (list, tuple)):
        return {filename for child in value for filename in _attachment_url_filenames(child)}
    return set()


def validate_attachment_url_references(
    *,
    embeds: Sequence[object],
    components: Sequence[object],
    attachments: Sequence[object],
) -> None:
    """Require every ``attachment://`` rich-media URL to name an attachment.

    Discord resolves these URLs by the uploaded filename, rather than by the
    attachment snowflake.  A dangling name otherwise survives local validation
    and, worse, can become a signed but permanently unrenderable federation
    projection.
    """

    referenced = _attachment_url_filenames(embeds) | _attachment_url_filenames(components)
    if not referenced:
        return
    filenames: set[str] = set()
    for attachment in attachments:
        if isinstance(attachment, Mapping):
            filename = attachment.get("filename")
        else:
            filename = getattr(attachment, "filename", None)
        if isinstance(filename, str):
            filenames.add(filename)
    missing = sorted(referenced - filenames)
    if missing:
        raise ValueError("attachment:// rich-media URLs must reference an attached filename")


class RichContentModel(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")


class PartialEmoji(RichContentModel):
    id: EntityRef | None = None
    name: str | None = Field(default=None, min_length=1, max_length=64)
    animated: bool = False

    @model_validator(mode="after")
    def identifiable(self) -> PartialEmoji:
        if self.id is None and self.name is None:
            raise ValueError("an emoji requires an ID or name")
        if self.name is not None:
            _meaningful(self.name, field="emoji name")
        if self.animated and self.id is None:
            raise ValueError("only custom emoji can be animated")
        return self


class EmbedFooter(RichContentModel):
    text: str = Field(min_length=1, max_length=2_048)
    icon_url: str | None = Field(default=None, max_length=2_048)

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        return _meaningful(value, field="embed footer text")

    @field_validator("icon_url")
    @classmethod
    def valid_icon_url(cls, value: str | None) -> str | None:
        return _validate_embed_media_url(value) if value is not None else None


class EmbedMedia(RichContentModel):
    url: str = Field(min_length=1, max_length=2_048)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _validate_embed_media_url(value)


class EmbedAuthor(RichContentModel):
    name: str = Field(min_length=1, max_length=256)
    url: str | None = Field(default=None, max_length=2_048)
    icon_url: str | None = Field(default=None, max_length=2_048)

    @field_validator("name")
    @classmethod
    def meaningful_name(cls, value: str) -> str:
        return _meaningful(value, field="embed author name")

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value) if value is not None else None

    @field_validator("icon_url")
    @classmethod
    def valid_icon_url(cls, value: str | None) -> str | None:
        return _validate_embed_media_url(value) if value is not None else None


class EmbedField(RichContentModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=1_024)
    inline: bool = False

    @field_validator("name", "value")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        return _meaningful(value, field="embed field text")


class Embed(RichContentModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, min_length=1, max_length=4_096)
    url: str | None = Field(default=None, max_length=2_048)
    timestamp: datetime | None = None
    color: int | None = Field(default=None, ge=0, le=0xFFFFFF)
    footer: EmbedFooter | None = None
    image: EmbedMedia | None = None
    thumbnail: EmbedMedia | None = None
    author: EmbedAuthor | None = None
    fields: list[EmbedField] = Field(default_factory=list, max_length=25)

    @field_validator("title", "description")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        if value is not None:
            return _meaningful(value, field="embed text")
        return None

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value) if value is not None else None

    @field_validator("timestamp")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("embed timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def not_empty(self) -> Embed:
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
        return self

    def character_count(self) -> int:
        total = len(self.title or "") + len(self.description or "")
        if self.footer is not None:
            total += len(self.footer.text)
        if self.author is not None:
            total += len(self.author.name)
        total += sum(len(item.name) + len(item.value) for item in self.fields)
        return total


def validate_embed_collection(embeds: list[Embed]) -> list[Embed]:
    if len(embeds) > MAX_EMBEDS:
        raise ValueError(f"a message can contain at most {MAX_EMBEDS} embeds")
    if sum(embed.character_count() for embed in embeds) > MAX_EMBED_CHARACTERS:
        raise ValueError(f"embed text cannot exceed {MAX_EMBED_CHARACTERS} characters per message")
    return embeds


class Button(RichContentModel):
    type: Literal[2] = 2
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    style: Literal[1, 2, 3, 4, 5, 6] = 1
    label: str | None = Field(default=None, min_length=1, max_length=80)
    emoji: PartialEmoji | None = None
    custom_id: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, max_length=512)
    sku_id: EntityRef | None = None
    disabled: bool = False

    @field_validator("label", "custom_id")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        if value is not None:
            return _meaningful(value, field="button text")
        return None

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value) if value is not None else None

    @model_validator(mode="after")
    def valid_target(self) -> Button:
        if self.style == 6:
            if (
                self.sku_id is None
                or self.label is not None
                or self.emoji is not None
                or self.custom_id is not None
                or self.url is not None
            ):
                raise ValueError(
                    "a premium button requires only sku_id and cannot have "
                    "label, emoji, custom_id, or url"
                )
        elif self.label is None and self.emoji is None:
            raise ValueError("a button requires a label or emoji")
        elif self.style == 5:
            if self.url is None or self.custom_id is not None or self.sku_id is not None:
                raise ValueError("a link button requires url and cannot have custom_id or sku_id")
        elif self.custom_id is None or self.url is not None:
            raise ValueError("a non-link button requires custom_id and cannot have url")
        elif self.sku_id is not None:
            raise ValueError("only premium buttons can have sku_id")
        return self


class SelectOption(RichContentModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=100)
    emoji: PartialEmoji | None = None
    default: bool = False

    @field_validator("label", "value", "description")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        if value is not None:
            return _meaningful(value, field="select option text")
        return None


class SelectDefaultValue(RichContentModel):
    id: EntityRef
    type: Literal["user", "role", "channel"]


class SelectBase(RichContentModel):
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    custom_id: str = Field(min_length=1, max_length=100)
    placeholder: str | None = Field(default=None, min_length=1, max_length=150)
    min_values: int = Field(default=1, ge=0, le=25)
    max_values: int = Field(default=1, ge=1, le=25)
    disabled: bool = False
    required: bool | None = None

    @field_validator("custom_id", "placeholder")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        if value is not None:
            return _meaningful(value, field="select text")
        return None

    @model_validator(mode="after")
    def valid_range(self) -> SelectBase:
        if self.min_values > self.max_values:
            raise ValueError("select min_values cannot exceed max_values")
        return self


class StringSelect(SelectBase):
    type: Literal[3] = 3
    options: list[SelectOption] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def valid_options(self) -> StringSelect:
        if self.max_values > len(self.options):
            raise ValueError("select max_values cannot exceed its option count")
        values = [option.value for option in self.options]
        if len(values) != len(set(values)):
            raise ValueError("select option values must be unique")
        defaults = sum(option.default for option in self.options)
        if not self.min_values <= defaults <= self.max_values and defaults:
            raise ValueError("default option count must be within the select value range")
        return self


class UserSelect(SelectBase):
    type: Literal[5] = 5
    default_values: list[SelectDefaultValue] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def user_defaults(self) -> UserSelect:
        if any(item.type != "user" for item in self.default_values):
            raise ValueError("user select defaults must reference users")
        _validate_default_values(self)
        return self


class RoleSelect(SelectBase):
    type: Literal[6] = 6
    default_values: list[SelectDefaultValue] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def role_defaults(self) -> RoleSelect:
        if any(item.type != "role" for item in self.default_values):
            raise ValueError("role select defaults must reference roles")
        _validate_default_values(self)
        return self


class MentionableSelect(SelectBase):
    type: Literal[7] = 7
    default_values: list[SelectDefaultValue] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def mentionable_defaults(self) -> MentionableSelect:
        if any(item.type not in {"user", "role"} for item in self.default_values):
            raise ValueError("mentionable select defaults must reference users or roles")
        _validate_default_values(self)
        return self


class ChannelSelect(SelectBase):
    type: Literal[8] = 8
    channel_types: list[int] = Field(default_factory=list, max_length=19)
    default_values: list[SelectDefaultValue] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def channel_defaults(self) -> ChannelSelect:
        if any(item.type != "channel" for item in self.default_values):
            raise ValueError("channel select defaults must reference channels")
        if len(self.channel_types) != len(set(self.channel_types)):
            raise ValueError("channel select channel_types must be unique")
        if any(value not in SUPPORTED_CHANNEL_TYPES for value in self.channel_types):
            raise ValueError("channel select contains an unsupported channel type")
        _validate_default_values(self)
        return self


def _validate_default_values(select: SelectBase) -> None:
    values = getattr(select, "default_values", [])
    if len(values) != len({(item.id, item.type) for item in values}):
        raise ValueError("select default values must be unique")
    if values and not select.min_values <= len(values) <= select.max_values:
        raise ValueError("default value count must be within the select value range")


class TextInput(RichContentModel):
    type: Literal[4] = 4
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    custom_id: str = Field(min_length=1, max_length=100)
    style: Literal[1, 2] = 1
    label: str | None = Field(default=None, min_length=1, max_length=45)
    min_length: int | None = Field(default=None, ge=0, le=4_000)
    max_length: int | None = Field(default=None, ge=1, le=4_000)
    required: bool = True
    value: str | None = Field(default=None, max_length=4_000)
    placeholder: str | None = Field(default=None, max_length=100)

    @field_validator("custom_id", "label")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        return _meaningful(value, field="text input text") if value is not None else None

    @model_validator(mode="after")
    def valid_lengths(self) -> TextInput:
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("text input min_length cannot exceed max_length")
        if self.value is not None:
            if self.min_length is not None and len(self.value) < self.min_length:
                raise ValueError("text input value is shorter than min_length")
            if self.max_length is not None and len(self.value) > self.max_length:
                raise ValueError("text input value is longer than max_length")
        return self


MessageComponent = Annotated[
    Button | StringSelect | UserSelect | RoleSelect | MentionableSelect | ChannelSelect | TextInput,
    Field(discriminator="type"),
]


class ActionRow(RichContentModel):
    type: Literal[1] = 1
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    components: list[MessageComponent] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def compatible_children(self) -> ActionRow:
        first = self.components[0]
        if isinstance(first, Button):
            if not all(isinstance(item, Button) for item in self.components):
                raise ValueError("buttons cannot share an action row with other components")
        elif len(self.components) != 1:
            raise ValueError("selects and text inputs must be the only item in their action row")
        return self


class UnfurledMediaItem(RichContentModel):
    url: str = Field(min_length=1, max_length=2_048)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _validate_media_url(value)


class TextDisplay(RichContentModel):
    type: Literal[10] = 10
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    content: str = Field(min_length=1, max_length=4_000)

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str) -> str:
        return _meaningful(value, field="text display content")


class Thumbnail(RichContentModel):
    type: Literal[11] = 11
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    media: UnfurledMediaItem
    description: str | None = Field(default=None, max_length=1_024)
    spoiler: bool = False


class Section(RichContentModel):
    type: Literal[9] = 9
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    components: list[TextDisplay] = Field(min_length=1, max_length=3)
    accessory: Button | Thumbnail


class MediaGalleryItem(RichContentModel):
    media: UnfurledMediaItem
    description: str | None = Field(default=None, max_length=1_024)
    spoiler: bool = False


class MediaGallery(RichContentModel):
    type: Literal[12] = 12
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    items: list[MediaGalleryItem] = Field(min_length=1, max_length=10)


class FileComponent(RichContentModel):
    type: Literal[13] = 13
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    file: UnfurledMediaItem
    spoiler: bool = False

    @model_validator(mode="after")
    def attachment_only(self) -> FileComponent:
        if not self.file.url.startswith("attachment://"):
            raise ValueError("file components must reference an attachment:// filename")
        return self


class Separator(RichContentModel):
    type: Literal[14] = 14
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    divider: bool = True
    spacing: Literal[1, 2] = 1


ContainerChild = Annotated[
    ActionRow | TextDisplay | Section | MediaGallery | Separator | FileComponent,
    Field(discriminator="type"),
]


class Container(RichContentModel):
    type: Literal[17] = 17
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    components: list[ContainerChild] = Field(min_length=1, max_length=MAX_COMPONENTS)
    accent_color: int | None = Field(default=None, ge=0, le=0xFFFFFF)
    spoiler: bool = False


class FileUpload(RichContentModel):
    type: Literal[19] = 19
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    custom_id: str = Field(min_length=1, max_length=100)
    min_values: int = Field(default=1, ge=0, le=10)
    max_values: int = Field(default=1, ge=1, le=10)
    required: bool = True
    file_types: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("file_types")
    @classmethod
    def valid_file_types(cls, value: list[str]) -> list[str]:
        return normalize_file_types(value)

    @model_validator(mode="after")
    def valid_range(self) -> FileUpload:
        if self.min_values > self.max_values:
            raise ValueError("file upload min_values cannot exceed max_values")
        if self.required and self.min_values == 0:
            raise ValueError("a required file upload must have min_values of at least one")
        return self


class ChoiceOption(RichContentModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=100)
    default: bool = False

    @field_validator("label", "value")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        return _meaningful(value, field="choice option text")


class RadioGroup(RichContentModel):
    type: Literal[21] = 21
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    custom_id: str = Field(min_length=1, max_length=100)
    options: list[ChoiceOption] = Field(min_length=2, max_length=10)
    required: bool = True

    @model_validator(mode="after")
    def one_default(self) -> RadioGroup:
        if sum(option.default for option in self.options) > 1:
            raise ValueError("a radio group can have at most one default option")
        if len({option.value for option in self.options}) != len(self.options):
            raise ValueError("radio option values must be unique")
        return self


class CheckboxGroup(RichContentModel):
    type: Literal[22] = 22
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    custom_id: str = Field(min_length=1, max_length=100)
    options: list[ChoiceOption] = Field(min_length=1, max_length=10)
    min_values: int = Field(default=1, ge=0, le=10)
    max_values: int | None = Field(default=None, ge=1, le=10)
    required: bool = True

    @model_validator(mode="after")
    def valid_options(self) -> CheckboxGroup:
        max_values = self.max_values
        if max_values is None:
            max_values = len(self.options)
            self.max_values = max_values
        if self.min_values > max_values or max_values > len(self.options):
            raise ValueError("checkbox group value limits must fit its option count")
        defaults = sum(option.default for option in self.options)
        if defaults and not self.min_values <= defaults <= max_values:
            raise ValueError("default checkbox count must be within the value range")
        if len({option.value for option in self.options}) != len(self.options):
            raise ValueError("checkbox option values must be unique")
        if self.required and self.min_values == 0:
            raise ValueError("a required checkbox group must have min_values of at least one")
        return self


class CheckboxV2(RichContentModel):
    type: Literal[23] = 23
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    custom_id: str = Field(min_length=1, max_length=100)
    default: bool = False


ModalInput = Annotated[
    TextInput
    | StringSelect
    | UserSelect
    | RoleSelect
    | MentionableSelect
    | ChannelSelect
    | FileUpload
    | RadioGroup
    | CheckboxGroup
    | CheckboxV2,
    Field(discriminator="type"),
]


class Label(RichContentModel):
    type: Literal[18] = 18
    id: int | None = Field(default=None, ge=0, le=MAX_COMPONENT_ID)
    label: str = Field(min_length=1, max_length=45)
    description: str | None = Field(default=None, max_length=100)
    component: ModalInput


MessageLayoutComponent = Annotated[
    ActionRow | Section | TextDisplay | MediaGallery | FileComponent | Separator | Container,
    Field(discriminator="type"),
]
ModalLayoutComponent = Annotated[ActionRow | Label | TextDisplay, Field(discriminator="type")]


def uses_components_v2(components: Sequence[object]) -> bool:
    return any(
        (
            component.get("type") != 1
            if isinstance(component, dict)
            else not isinstance(component, ActionRow)
        )
        for component in components
    )


def walk_component_tree(component: object) -> list[object]:
    descendants = [component]
    if isinstance(component, (ActionRow, Section, Container)):
        descendants.extend(
            child for nested in component.components for child in walk_component_tree(nested)
        )
    if isinstance(component, Section):
        descendants.extend(walk_component_tree(component.accessory))
    if isinstance(component, Label):
        descendants.extend(walk_component_tree(component.component))
    return descendants


def assign_component_ids(components: list[object]) -> list[object]:
    flattened = [item for component in components for item in walk_component_tree(component)]
    used = {
        value
        for item in flattened
        if isinstance((value := getattr(item, "id", None)), int) and value > 0
    }
    next_id = 1
    for item in flattened:
        if not hasattr(item, "id") or cast(Any, item).id not in (None, 0):
            continue
        while next_id in used:
            next_id += 1
        cast(Any, item).id = next_id
        used.add(next_id)
        next_id += 1
    return components


def require_unique_assigned_component_ids(
    flattened: list[object],
    *,
    scope: str,
) -> None:
    assigned = [
        value
        for item in flattened
        if isinstance((value := getattr(item, "id", None)), int) and value > 0
    ]
    if len(assigned) != len(set(assigned)):
        raise ValueError(f"{scope} component ids must be unique")


def validate_message_components(
    components: list[MessageLayoutComponent],
) -> list[MessageLayoutComponent]:
    if not components:
        return components
    is_v2 = uses_components_v2(components)
    if not is_v2 and len(components) > MAX_ACTION_ROWS:
        raise ValueError(f"a legacy message can contain at most {MAX_ACTION_ROWS} action rows")
    flattened = [item for component in components for item in walk_component_tree(component)]
    if len(flattened) > MAX_COMPONENTS:
        raise ValueError(f"a message can contain at most {MAX_COMPONENTS} nested components")
    if any(isinstance(item, TextInput) for item in flattened):
        raise ValueError("text inputs are only valid in modals")
    custom_ids = [
        item.custom_id
        for item in flattened
        if isinstance(item, (Button, SelectBase)) and item.custom_id is not None
    ]
    if len(custom_ids) != len(set(custom_ids)):
        raise ValueError("message component custom_ids must be unique")
    require_unique_assigned_component_ids(flattened, scope="message")
    assign_component_ids(list(components))
    return components


def modal_input_components(components: list[ModalLayoutComponent]) -> list[ModalInput]:
    inputs: list[ModalInput] = []
    for top_level in components:
        if isinstance(top_level, TextDisplay):
            continue
        if isinstance(top_level, Label):
            inputs.append(top_level.component)
            continue
        if len(top_level.components) != 1 or not isinstance(top_level.components[0], TextInput):
            raise ValueError("modal selects and Components V2 inputs must be inside a label")
        modal_input = top_level.components[0]
        if modal_input.label is None:
            raise ValueError("legacy modal text inputs require their own label")
        inputs.append(modal_input)
    return inputs


def validate_modal_inputs(inputs: list[ModalInput]) -> None:
    for item in inputs:
        if isinstance(item, SelectBase) and item.required is not False and item.min_values == 0:
            raise ValueError("a required modal select must have min_values of at least one")
        if isinstance(item, SelectBase) and item.disabled:
            raise ValueError("modal inputs cannot be disabled")
    custom_ids = [item.custom_id for item in inputs]
    if len(custom_ids) != len(set(custom_ids)):
        raise ValueError("modal component custom_ids must be unique")


class Modal(RichContentModel):
    title: str = Field(min_length=1, max_length=45)
    custom_id: str = Field(min_length=1, max_length=100)
    components: list[ModalLayoutComponent] = Field(min_length=1, max_length=5)

    @field_validator("title", "custom_id")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        return _meaningful(value, field="modal text")

    @model_validator(mode="after")
    def input_rows_only(self) -> Modal:
        validate_modal_inputs(modal_input_components(self.components))
        flattened = [
            child for component in self.components for child in walk_component_tree(component)
        ]
        require_unique_assigned_component_ids(flattened, scope="modal")
        assign_component_ids(list(self.components))
        return self


MESSAGE_LAYOUT_COMPONENT_ADAPTER: TypeAdapter[MessageLayoutComponent] = TypeAdapter(
    MessageLayoutComponent
)


class PollMedia(RichContentModel):
    text: str | None = Field(default=None, min_length=1, max_length=300)
    emoji: PartialEmoji | None = None

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        if value is not None:
            return _meaningful(value, field="poll text")
        return None

    @model_validator(mode="after")
    def not_empty(self) -> PollMedia:
        if self.text is None and self.emoji is None:
            raise ValueError("poll media requires text or emoji")
        return self


class PollAnswer(RichContentModel):
    poll_media: PollMedia

    @model_validator(mode="after")
    def answer_limits(self) -> PollAnswer:
        if self.poll_media.text is not None and len(self.poll_media.text) > 55:
            raise ValueError("poll answer text cannot exceed 55 characters")
        return self


class PollCreate(RichContentModel):
    question: PollMedia
    answers: list[PollAnswer] = Field(min_length=2, max_length=10)
    duration: int = Field(ge=1, le=768)
    allow_multiselect: bool = False
    layout_type: Literal[1] = 1

    @model_validator(mode="after")
    def valid_question_and_answers(self) -> PollCreate:
        if self.question.text is None or self.question.emoji is not None:
            raise ValueError("a poll question requires text and cannot contain emoji")
        return self


def message_automod_text(
    content: str | None,
    *,
    poll: PollCreate | Mapping[str, object] | None = None,
    components: Sequence[object] | None = None,
) -> str | None:
    """Return the visible, user-authored plaintext AutoMod evaluates."""

    parts: list[str] = []
    if content:
        parts.append(content)
    if isinstance(poll, PollCreate):
        if poll.question.text:
            parts.append(poll.question.text)
        parts.extend(answer.poll_media.text for answer in poll.answers if answer.poll_media.text)
    elif isinstance(poll, Mapping):
        question = poll.get("question")
        if isinstance(question, Mapping) and isinstance(question.get("text"), str):
            parts.append(cast(str, question["text"]))
        answers = poll.get("answers")
        if isinstance(answers, Sequence) and not isinstance(answers, (str, bytes)):
            for answer in answers:
                media = answer.get("poll_media") if isinstance(answer, Mapping) else None
                if isinstance(media, Mapping) and isinstance(media.get("text"), str):
                    parts.append(cast(str, media["text"]))
    if components:

        def append_text_displays(component: object) -> None:
            if isinstance(component, TextDisplay):
                parts.append(component.content)
                return
            if isinstance(component, Mapping):
                if component.get("type") == 10 and isinstance(component.get("content"), str):
                    parts.append(cast(str, component["content"]))
                for nested in component.values():
                    append_text_displays(nested)
                return
            if isinstance(component, Sequence) and not isinstance(component, (str, bytes)):
                for nested in component:
                    append_text_displays(nested)
                return
            for node in walk_component_tree(component):
                if node is not component and isinstance(node, TextDisplay):
                    parts.append(node.content)

        for component in components:
            append_text_displays(component)
    return "\n".join(parts) or None
