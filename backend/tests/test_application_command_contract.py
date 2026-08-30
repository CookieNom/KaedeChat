from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.applications import (
    CommandChoice,
    CommandDefinition,
    CommandOptionDefinition,
    CommandsPut,
    command_character_count,
)
from app.api.bot_federation import ManifestCommand
from app.api.interactions import (
    filter_commands_for_permissions,
    interaction_message_from_data,
    validate_command_options,
)
from app.core.file_types import attachment_matches_file_types
from app.core.permissions import Permission
from app.db.bot_models import ApplicationCommand


def test_command_names_match_discord_type_specific_contract() -> None:
    assert CommandDefinition(name="météo", description="Prévisions").name == "météo"
    assert CommandDefinition(name="lʼheure", description="Heure").name == "lʼheure"
    assert CommandDefinition(name="क꣠", description="Devanagari mark").name == "क꣠"
    assert CommandDefinition(name="Inspect User", type="user").name == "Inspect User"
    assert CommandDefinition(name="Quote Message", type="message").name == "Quote Message"
    with pytest.raises(ValidationError):
        CommandDefinition(name="Uppercase", description="No")
    with pytest.raises(ValidationError):
        CommandDefinition(name=" spaced ", type="user")
    with pytest.raises(ValidationError):
        CommandDefinition(name="don't", description="ASCII apostrophes are not valid")


def test_command_localizations_permissions_and_nsfw_round_trip() -> None:
    command = CommandDefinition(
        name="weather",
        description="Forecast",
        name_localizations={"fr": "météo"},
        description_localizations={"fr": "Prévisions"},
        default_member_permissions=["MANAGE_AUTO_MODERATION", "SEND_MESSAGES"],
        nsfw=True,
    )
    assert command.default_member_permissions == ["MANAGE_GUILD", "SEND_MESSAGES"]
    assert command.name_localizations == {"fr": "météo"}
    assert command.nsfw is True
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="weather",
            description="Forecast",
            default_member_permissions=["NOT_A_PERMISSION"],
        )
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="weather",
            description="Forecast",
            default_member_permissions=["MANAGE_GUILD", "MANAGE_AUTO_MODERATION"],
        )
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="weather",
            description="Forecast",
            name_localizations={"xx": "weather"},
        )


def test_command_option_tree_rejects_ambiguous_definitions() -> None:
    required = CommandOptionDefinition(
        type="string",
        name="query",
        description="Query",
        required=True,
    )
    optional = CommandOptionDefinition(
        type="string",
        name="units",
        description="Units",
    )
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="search",
            description="Search",
            options=[optional, required],
        )
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="search",
            description="Search",
            options=[required, required],
        )
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="integer",
            name="count",
            description="Count",
            choices=[CommandChoice(name="one", value="1")],
        )
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="mixed",
            description="Invalid mixed tree",
            options=[
                CommandOptionDefinition(
                    type="subcommand",
                    name="run",
                    description="Run",
                ),
                optional,
            ],
        )
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="localized",
            description="Localized",
            options=[
                CommandOptionDefinition(
                    type="string",
                    name="first",
                    name_localizations={"fr": "second"},
                    description="First",
                ),
                CommandOptionDefinition(
                    type="string",
                    name="second",
                    description="Second",
                ),
            ],
        )
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="subcommand",
            name="outer",
            description="Outer",
            options=[
                CommandOptionDefinition(
                    type="subcommand",
                    name="inner",
                    description="Inner",
                )
            ],
        )


def test_command_numeric_ranges_and_localization_budget_match_discord() -> None:
    assert (
        CommandOptionDefinition(
            type="integer",
            name="count",
            description="Count",
            min_value=-(2**53) + 1,
            max_value=2**53 - 1,
        ).max_value
        == 2**53 - 1
    )
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="integer",
            name="count",
            description="Count",
            min_value=-(2**53),
        )
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="integer",
            name="count",
            description="Count",
            min_value=1.0,
        )
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="integer",
            name="count",
            description="Count",
            choices=[CommandChoice(name="unsafe", value=2**53)],
        )
    command = CommandDefinition(
        name="weather",
        description="Forecast",
        name_localizations={"fr": "météorologie", "de": "wetter"},
        description_localizations={"fr": "Prévisions détaillées"},
    )
    assert command_character_count(command) == len("météorologie") + len("Prévisions détaillées")

    stored = ApplicationCommand(
        id=1,
        application_id=2,
        application_domain="apps.example",
        name="number",
        type="chat_input",
        generation=1,
        definition={
            "name": "number",
            "description": "Number",
            "options": [
                {"type": "integer", "name": "integer", "description": "Integer"},
                {"type": "number", "name": "number", "description": "Number"},
            ],
        },
    )
    with pytest.raises(HTTPException):
        validate_command_options(stored, {"integer": 2**53}, require_complete=True)
    with pytest.raises(HTTPException):
        validate_command_options(stored, {"integer": 1.0}, require_complete=True)
    with pytest.raises(HTTPException):
        validate_command_options(stored, {"number": 2**53 + 2}, require_complete=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "string", "name": "query", "description": "Query", "required": 1},
        {"type": "string", "name": "query", "description": "Query", "autocomplete": 0},
        {"type": "string", "name": "query", "description": "Query", "min_length": True},
        {"type": "number", "name": "amount", "description": "Amount", "min_value": "1"},
    ],
)
def test_command_options_reject_ambiguous_wire_types(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommandOptionDefinition.model_validate(payload)

    with pytest.raises(ValidationError):
        CommandDefinition.model_validate({"name": "command", "description": "Command", "nsfw": 0})
    with pytest.raises(ValidationError):
        CommandChoice.model_validate({"name": "boolean", "value": True})


def test_federated_manifest_uses_the_same_recursive_command_contract() -> None:
    command = ManifestCommand(
        id="1",
        name="upload",
        description="Upload",
        contexts=["guild"],
        integration_types=["guild_install"],
        options=[
            {
                "type": "attachment",
                "name": "document",
                "description": "Document",
                "file_types": [".PDF"],
            }
        ],
    )
    assert command.options[0].file_types == [".pdf"]
    with pytest.raises(ValidationError):
        ManifestCommand(
            id="2",
            name="broken",
            description="Broken",
            contexts=["guild"],
            integration_types=["guild_install"],
            options=[
                {
                    "type": "integer",
                    "name": "count",
                    "description": "Count",
                    "choices": [{"name": "unsafe", "value": 2**53}],
                }
            ],
        )


def test_command_set_applies_discord_per_type_limits() -> None:
    CommandsPut(
        commands=[
            *(CommandDefinition(name=f"c{index}", description="Command") for index in range(100)),
            *(CommandDefinition(name=f"User {index}", type="user") for index in range(15)),
            *(CommandDefinition(name=f"Message {index}", type="message") for index in range(15)),
        ]
    )
    with pytest.raises(ValidationError):
        CommandsPut(
            commands=[CommandDefinition(name=f"User {index}", type="user") for index in range(16)]
        )


def test_attachment_option_normalizes_and_scopes_file_filters() -> None:
    option = CommandOptionDefinition(
        type="attachment",
        name="document",
        description="Document",
        file_types=["IMAGE", ".PDF"],
    )
    assert option.file_types == ["image", ".pdf"]
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="string",
            name="document",
            description="Document",
            file_types=[".pdf"],
        )
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="attachment",
            name="document",
            description="Document",
            file_types=[".PDF", ".pdf"],
        )
    assert attachment_matches_file_types(
        filename="photo.png",
        content_type="application/octet-stream",
        file_types=["image"],
    )
    assert not attachment_matches_file_types(
        filename="payload.exe",
        content_type="image/png",
        file_types=["image"],
    )


def test_command_discovery_filters_default_permissions() -> None:
    commands: list[dict[str, object]] = [
        {"name": "open", "default_member_permissions": []},
        {"name": "moderate", "default_member_permissions": ["MANAGE_MESSAGES"]},
        {"name": "admin-only", "default_member_permissions": "0"},
    ]
    assert [item["name"] for item in filter_commands_for_permissions(commands, 0)] == ["open"]
    assert [
        item["name"]
        for item in filter_commands_for_permissions(commands, int(Permission.MANAGE_MESSAGES))
    ] == ["open", "moderate"]
    assert [
        item["name"]
        for item in filter_commands_for_permissions(
            commands,
            int(Permission.ADMINISTRATOR),
        )
    ] == ["open", "admin-only"]


def test_interaction_message_rejects_unknown_data_and_flags() -> None:
    message = interaction_message_from_data(
        {
            "content": "hello",
            "tts": True,
            "flags": (1 << 2) | (1 << 6),
        }
    )
    assert message.tts is True
    assert message.flags == 1 << 2
    with pytest.raises(HTTPException) as unknown:
        interaction_message_from_data({"content": "hello", "surprise": True})
    assert unknown.value.detail["code"] == "INTERACTION_CALLBACK_DATA_INVALID"
    with pytest.raises(HTTPException) as flags:
        interaction_message_from_data({"content": "hello", "flags": 1 << 30})
    assert flags.value.detail["code"] == "INTERACTION_CALLBACK_FLAGS_INVALID"
