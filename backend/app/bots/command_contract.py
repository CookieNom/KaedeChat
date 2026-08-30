from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from math import isfinite
from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.chat.rich_content import SUPPORTED_CHANNEL_TYPES
from app.core.file_types import normalize_file_types
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import Permission

DISCORD_LOCALES = frozenset(
    {
        "id",
        "da",
        "de",
        "en-GB",
        "en-US",
        "es-ES",
        "es-419",
        "fr",
        "hr",
        "it",
        "lt",
        "hu",
        "nl",
        "no",
        "pl",
        "pt-BR",
        "ro",
        "fi",
        "sv-SE",
        "vi",
        "tr",
        "cs",
        "el",
        "bg",
        "ru",
        "uk",
        "hi",
        "th",
        "zh-CN",
        "ja",
        "ko",
        "zh-TW",
    }
)


def valid_chat_input_name(value: str) -> bool:
    """Match Discord's lowercase Unicode chat-input/option name contract."""

    if not 1 <= len(value) <= 32 or value != value.lower():
        return False
    for character in value:
        if character in "_-":
            continue
        category = unicodedata.category(character)
        if category[0] in {"L", "N"}:
            continue
        codepoint = ord(character)
        if category[0] == "M" and (
            0x0900 <= codepoint <= 0x097F
            or 0xA8E0 <= codepoint <= 0xA8FF
            or 0x11B00 <= codepoint <= 0x11B5F
            or 0x0E00 <= codepoint <= 0x0E7F
        ):
            continue
        return False
    return True


def valid_context_command_name(value: str) -> bool:
    """Context-menu names may use mixed case and spaces but not controls."""

    return (
        1 <= len(value) <= 32
        and value == value.strip()
        and all(not unicodedata.category(character).startswith("C") for character in value)
    )


def validate_localizations(
    value: Mapping[str, str],
    *,
    minimum: int,
    maximum: int,
    chat_input_names: bool = False,
    context_names: bool = False,
) -> dict[str, str]:
    """Validate Discord locale keys and localized command text."""

    normalized: dict[str, str] = {}
    for locale, text in value.items():
        if locale not in DISCORD_LOCALES:
            raise ValueError(f"unsupported localization key: {locale}")
        if not minimum <= len(text) <= maximum:
            raise ValueError("localized command text has an invalid length")
        if chat_input_names and not valid_chat_input_name(text):
            raise ValueError("localized chat input names must use lowercase command characters")
        if context_names and not valid_context_command_name(text):
            raise ValueError("localized context command names contain invalid characters")
        normalized[locale] = text
    return normalized


def normalize_permission_names(values: list[str]) -> list[str]:
    """Return unique canonical permission names suitable for a command definition."""

    normalized: list[str] = []
    seen_values: set[int] = set()
    for value in values:
        try:
            permission = Permission.__members__[value]
        except KeyError:
            raise ValueError(f"unknown default member permission: {value}") from None
        if permission.value in seen_values:
            raise ValueError("default member permissions must be unique")
        seen_values.add(permission.value)
        canonical_name = permission.name
        if canonical_name is None:  # pragma: no cover - every published flag is named
            raise ValueError("default member permission has no canonical name")
        normalized.append(canonical_name)
    return normalized


def command_permission_mask(definition: Mapping[str, object]) -> Permission:
    raw = definition.get("default_member_permissions", [])
    if not isinstance(raw, list):
        return Permission(0)
    mask = Permission(0)
    for name in raw:
        if not isinstance(name, str):
            continue
        permission = Permission.__members__.get(name)
        if permission is not None:
            mask |= permission
    return mask


def valid_numeric_command_value(
    value: object,
    *,
    integer: bool,
) -> bool:
    """Validate Discord's JSON-safe INTEGER/NUMBER command range."""

    if type(value) not in {int, float}:
        return False
    numeric_value = cast(int | float, value)
    if isinstance(numeric_value, float) and not isfinite(numeric_value):
        return False
    if integer:
        # Discord's INTEGER option is a JSON integer, not merely a numeric
        # value whose fractional part happens to be zero.  Keeping the types
        # distinct also prevents NUMBER and INTEGER choices from comparing as
        # the same Python value (for example, ``1`` and ``1.0``).
        if type(value) is not int:
            return False
        # Keep integer inputs as integers: converting 2**53 - 1 to binary64
        # rounds it up and would reject Discord's valid upper boundary.
        return -(2**53) + 1 <= numeric_value <= 2**53 - 1
    return -(2**53) <= numeric_value <= 2**53


class CommandChoice(UnambiguousInputModel):
    """A Discord-compatible localized application-command choice."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    name_localizations: dict[str, str] = Field(default_factory=dict, max_length=32)
    value: str | int | float

    @field_validator("value", mode="before")
    @classmethod
    def valid_wire_value_type(cls, value: object) -> object:
        if type(value) not in {str, int, float}:
            raise ValueError("choice values must be strings or JSON numbers")
        return value

    @field_validator("name_localizations")
    @classmethod
    def valid_name_localizations(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_localizations(value, minimum=1, maximum=100)

    @model_validator(mode="after")
    def valid_value(self) -> CommandChoice:
        if isinstance(self.value, str) and not 1 <= len(self.value) <= 100:
            raise ValueError("string choice values must contain between 1 and 100 characters")
        if isinstance(self.value, bool):
            raise ValueError("choice values cannot be booleans")
        return self


class CommandOptionDefinition(UnambiguousInputModel):
    """One recursively validated application-command option."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "subcommand",
        "subcommand_group",
        "string",
        "integer",
        "boolean",
        "user",
        "channel",
        "role",
        "mentionable",
        "number",
        "attachment",
    ]
    name: str = Field(min_length=1, max_length=32)
    name_localizations: dict[str, str] = Field(default_factory=dict, max_length=32)
    description: str = Field(min_length=1, max_length=100)
    description_localizations: dict[str, str] = Field(default_factory=dict, max_length=32)
    required: bool = False
    autocomplete: bool = False
    choices: list[CommandChoice] = Field(default_factory=list, max_length=25)
    options: list[CommandOptionDefinition] = Field(default_factory=list, max_length=25)
    channel_types: list[Annotated[int, Field(strict=True)]] = Field(
        default_factory=list,
        max_length=len(SUPPORTED_CHANNEL_TYPES),
    )
    min_value: int | float | None = None
    max_value: int | float | None = None
    min_length: int | None = Field(default=None, ge=0, le=6000)
    max_length: int | None = Field(default=None, ge=1, le=6000)
    file_types: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("min_value", "max_value", mode="before")
    @classmethod
    def valid_wire_bound_type(cls, value: object) -> object:
        if value is not None and type(value) not in {int, float}:
            raise ValueError("numeric bounds must be JSON numbers")
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not valid_chat_input_name(value):
            raise ValueError("command option names must use lowercase command characters")
        return value

    @field_validator("name_localizations")
    @classmethod
    def valid_name_localizations(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_localizations(
            value,
            minimum=1,
            maximum=32,
            chat_input_names=True,
        )

    @field_validator("description_localizations")
    @classmethod
    def valid_description_localizations(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_localizations(value, minimum=1, maximum=100)

    @field_validator("file_types")
    @classmethod
    def valid_file_types(cls, value: list[str]) -> list[str]:
        return normalize_file_types(value)

    @model_validator(mode="after")
    def valid_shape(self) -> CommandOptionDefinition:
        validate_option_nesting(self)
        validate_option_choices(self)
        validate_option_type_fields(self)
        validate_option_bounds(self)
        validate_command_option_order(self.options)
        return self


def validate_option_nesting(option: CommandOptionDefinition) -> None:
    container = option.type in {"subcommand", "subcommand_group"}
    if option.options and not container:
        raise ValueError("only subcommands and groups contain nested options")
    if option.type == "subcommand_group" and not option.options:
        raise ValueError("subcommand groups require at least one subcommand")
    if option.type == "subcommand_group" and any(
        child.type != "subcommand" for child in option.options
    ):
        raise ValueError("subcommand groups may contain only subcommands")
    if option.type == "subcommand" and any(
        child.type in {"subcommand", "subcommand_group"} for child in option.options
    ):
        raise ValueError("subcommands may contain only scalar options")
    if container and option.required:
        raise ValueError("subcommand containers cannot be required")


def validate_option_choices(option: CommandOptionDefinition) -> None:
    numeric_or_string = option.type in {"string", "integer", "number"}
    if option.choices and not numeric_or_string:
        raise ValueError("choices require a string or numeric option")
    if len({choice.name for choice in option.choices}) != len(option.choices):
        raise ValueError("command option choice names must be unique")
    choice_values = [(type(choice.value), choice.value) for choice in option.choices]
    if len(set(choice_values)) != len(choice_values):
        raise ValueError("command option choice values must be unique")
    for choice in option.choices:
        if option.type == "string" and not isinstance(choice.value, str):
            raise ValueError("string options require string choice values")
        if option.type == "integer" and not valid_numeric_command_value(
            choice.value,
            integer=True,
        ):
            raise ValueError("integer options require integer choice values")
        if option.type == "number" and not valid_numeric_command_value(
            choice.value,
            integer=False,
        ):
            raise ValueError("number options require numeric choice values")
    if option.autocomplete and (option.choices or not numeric_or_string):
        raise ValueError("autocomplete requires a string or numeric option without choices")


def validate_option_type_fields(option: CommandOptionDefinition) -> None:
    if option.channel_types and option.type != "channel":
        raise ValueError("channel_types require a channel option")
    if option.file_types and option.type != "attachment":
        raise ValueError("file_types require an attachment option")
    if len(option.channel_types) != len(set(option.channel_types)):
        raise ValueError("command option channel_types must be unique")
    if any(value not in SUPPORTED_CHANNEL_TYPES for value in option.channel_types):
        raise ValueError("command option contains an unsupported channel type")
    if (option.min_length is not None or option.max_length is not None) and option.type != "string":
        raise ValueError("length bounds require a string option")
    if (option.min_value is not None or option.max_value is not None) and option.type not in {
        "integer",
        "number",
    }:
        raise ValueError("numeric bounds require a numeric option")


def validate_option_bounds(option: CommandOptionDefinition) -> None:
    if (
        option.min_length is not None
        and option.max_length is not None
        and option.min_length > option.max_length
    ):
        raise ValueError("minimum length exceeds maximum length")
    if (
        option.min_value is not None
        and option.max_value is not None
        and option.min_value > option.max_value
    ):
        raise ValueError("minimum value exceeds maximum value")
    if option.type not in {"integer", "number"}:
        return
    integer = option.type == "integer"
    if any(
        value is not None and not valid_numeric_command_value(value, integer=integer)
        for value in (option.min_value, option.max_value)
    ):
        raise ValueError("numeric bounds exceed the option type's safe range")


def validate_command_option_order(options: list[CommandOptionDefinition]) -> None:
    names = [option.name for option in options]
    if len(names) != len(set(names)):
        raise ValueError("command option names must be unique among siblings")
    default_names = set(names)
    locales = {locale for option in options for locale in option.name_localizations}
    for option in options:
        other_defaults = default_names - {option.name}
        if any(name in other_defaults for name in option.name_localizations.values()):
            raise ValueError("localized option names must differ from sibling default names")
    for locale in locales:
        localized_names = [option.name_localizations.get(locale, option.name) for option in options]
        if len(localized_names) != len(set(localized_names)):
            raise ValueError(f"command option names must be unique in locale {locale}")
    optional_seen = False
    for option in options:
        if option.required and optional_seen:
            raise ValueError("required options must precede optional options")
        if not option.required:
            optional_seen = True


def command_character_count(command: CommandDefinition) -> int:
    """Count Discord's longest localization for every command text field."""

    def localized_length(default: str, values: dict[str, str]) -> int:
        return max((len(default), *(len(value) for value in values.values())))

    def option_length(option: CommandOptionDefinition) -> int:
        return (
            localized_length(option.name, option.name_localizations)
            + localized_length(option.description, option.description_localizations)
            + sum(
                localized_length(choice.name, choice.name_localizations) + len(str(choice.value))
                for choice in option.choices
            )
            + sum(option_length(child) for child in option.options)
        )

    return (
        localized_length(command.name, command.name_localizations)
        + localized_length(command.description, command.description_localizations)
        + sum(option_length(option) for option in command.options)
    )


def default_command_contexts() -> list[Literal["guild", "bot_dm", "private_channel"]]:
    # Discord defaults global commands to every interaction surface.  A
    # PRIVATE_CHANNEL context is simply ignored unless USER_INSTALL is also
    # supported; retaining it preserves the default if that install context is
    # enabled by a later bulk registration.
    return ["guild", "bot_dm", "private_channel"]


def default_command_integration_types() -> list[Literal["guild_install", "user_install"]]:
    return ["guild_install"]


class CommandDefinition(UnambiguousInputModel):
    """Canonical command schema shared by local registration and federation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=32)
    type: Literal["chat_input", "user", "message"] = "chat_input"
    description: str = Field(default="", max_length=100)
    name_localizations: dict[str, str] = Field(default_factory=dict, max_length=32)
    description_localizations: dict[str, str] = Field(default_factory=dict, max_length=32)
    default_member_permissions: list[str] | Literal["0"] = Field(
        default_factory=list,
        max_length=64,
    )
    nsfw: bool = False
    contexts: list[Literal["guild", "bot_dm", "private_channel"]] = Field(
        default_factory=default_command_contexts,
        min_length=1,
        max_length=3,
    )
    integration_types: list[Literal["guild_install", "user_install"]] = Field(
        default_factory=default_command_integration_types,
        min_length=1,
        max_length=2,
    )
    options: list[CommandOptionDefinition] = Field(default_factory=list, max_length=25)

    @field_validator("contexts", "integration_types")
    @classmethod
    def unique_command_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("command contexts and integration types must be unique")
        return value

    @field_validator("default_member_permissions")
    @classmethod
    def valid_default_member_permissions(
        cls,
        value: list[str] | Literal["0"],
    ) -> list[str] | Literal["0"]:
        return value if value == "0" else normalize_permission_names(value)

    @field_validator("description_localizations")
    @classmethod
    def valid_description_localizations(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_localizations(value, minimum=1, maximum=100)

    @model_validator(mode="after")
    def valid_command_shape(self) -> CommandDefinition:
        if self.type == "chat_input":
            if not valid_chat_input_name(self.name):
                raise ValueError("chat input command names must use lowercase command characters")
            self.name_localizations = validate_localizations(
                self.name_localizations,
                minimum=1,
                maximum=32,
                chat_input_names=True,
            )
        else:
            if not valid_context_command_name(self.name):
                raise ValueError("context command names contain invalid characters")
            self.name_localizations = validate_localizations(
                self.name_localizations,
                minimum=1,
                maximum=32,
                context_names=True,
            )
        if self.type == "chat_input" and not self.description:
            raise ValueError("chat input commands require a description")
        if self.type != "chat_input" and (
            self.description or self.description_localizations or self.options
        ):
            raise ValueError("context commands do not have descriptions or options")
        if any(
            option.type in {"subcommand", "subcommand_group"} for option in self.options
        ) and any(option.type not in {"subcommand", "subcommand_group"} for option in self.options):
            raise ValueError("subcommand containers cannot be mixed with scalar options")
        validate_command_option_order(self.options)
        if command_character_count(self) > 8000:
            raise ValueError("command definition exceeds the maximum character count")
        return self


class CommandsPut(UnambiguousInputModel):
    """Discord's independently capped global command set."""

    commands: list[CommandDefinition] = Field(max_length=130)

    @model_validator(mode="after")
    def bounded_tree(self) -> CommandsPut:
        def walk(options: list[CommandOptionDefinition], depth: int) -> None:
            if depth > 3:
                raise ValueError("command options exceed the maximum nesting depth")
            for option in options:
                if option.options:
                    walk(option.options, depth + 1)

        for command in self.commands:
            walk(command.options, 1)
        limits = {"chat_input": 100, "user": 15, "message": 15}
        for command_type, limit in limits.items():
            if sum(command.type == command_type for command in self.commands) > limit:
                raise ValueError(f"too many {command_type} commands")
        return self
