from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot import (
    ActionRow,
    Button,
    Client,
    EntityRef,
    Interaction,
    InteractionSourceMessage,
    Message,
    View,
    WorkerState,
)

TARGET = "https://guild.example"


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def user_payload(*, user_id: int = 3) -> dict[str, Any]:
    return {
        "id": str(user_id),
        "origin_domain": "users.example",
        "username": "member",
    }


def message_payload() -> dict[str, Any]:
    return {
        "id": "20",
        "origin_domain": "guild.example",
        "channel_id": "7",
        "channel_domain": "guild.example",
        "content": "Press me",
        "created_at": "2026-08-29T00:00:00+00:00",
        "attachments": [],
    }


def guild_interaction_payload() -> dict[str, Any]:
    return {
        "id": "90",
        "interaction_ref": "90@guild.example",
        "version": 1,
        "type": "component",
        "context": "guild",
        "integration_type": "guild_install",
        "application_ref": "1@apps.example",
        "guild_ref": "10@guild.example",
        "channel_ref": "7@guild.example",
        "member": {
            "guild_id": "10",
            "guild_domain": "guild.example",
            "user": user_payload(),
            "nickname": "Member",
            "joined_at": "2026-08-01T00:00:00+00:00",
            "role_ids": ["4"],
            "permissions": "274877906944",
        },
        "user_ref": "3@users.example",
        "locale": "ko",
        "guild_locale": "en-US",
        "app_permissions": "274877906944",
        "authorizing_integration_owners": {
            "guild_install": "10@guild.example",
            "user_install": "8@users.example",
        },
        "attachment_size_limit": 10_000_000,
        "message_ref": "20@guild.example",
        "message": message_payload(),
        "command": None,
        "options": None,
        "encrypted_payload": None,
    }


def private_interaction_payload() -> dict[str, Any]:
    return {
        "id": "91",
        "interaction_ref": "91@dm.example",
        "version": 1,
        "type": "command",
        "context": "private_channel",
        "integration_type": "user_install",
        "application_ref": "1@apps.example",
        "guild_ref": None,
        "channel_ref": "8@dm.example",
        "user": user_payload(),
        "user_ref": "3@users.example",
        "locale": "en-GB",
        "app_permissions": "0",
        "authorizing_integration_owners": {"user_install": "8@users.example"},
        "attachment_size_limit": 25_000_000,
        "command": {"name": "ping"},
        "options": {},
        "encrypted_payload": None,
    }


def test_interaction_hydrates_discord_event_metadata_and_source_message() -> None:
    current = Interaction.from_payload(client(), TARGET, guild_interaction_payload())

    assert current.version == 1
    assert current.locale == "ko"
    assert current.guild_locale == "en-US"
    assert current.app_permissions == 274877906944
    assert current.attachment_size_limit == 10_000_000
    assert current.authorizing_integration_owners == {
        "guild_install": EntityRef(10, "guild.example"),
        "user_install": EntityRef(8, "users.example"),
    }
    assert current.member is not None
    assert current.member.permissions == 274877906944
    assert current.user is current.member.user
    assert isinstance(current.message, Message)
    assert current.message_ref == EntityRef(20, "guild.example")


def test_private_interaction_uses_top_level_user_without_member() -> None:
    payload = private_interaction_payload()
    payload["locale"] = "en-CA"
    current = Interaction.from_payload(client(), TARGET, payload)

    assert current.guild_ref is None
    assert current.locale == "en-CA"
    assert current.guild_locale is None
    assert current.member is None
    assert current.user.ref == EntityRef(3, "users.example")
    assert current.authorizing_integration_owners == {
        "user_install": EntityRef(8, "users.example")
    }


def bot_dm_ephemeral_interaction_payload() -> dict[str, Any]:
    payload = private_interaction_payload()
    payload.update(
        {
            "id": "92",
            "interaction_ref": "92@dm.example",
            "type": "component",
            "context": "bot_dm",
            "integration_type": "dm_capability",
            "command": None,
            "options": None,
            "response_id": "42",
            "custom_id": "next",
            "authorizing_integration_owners": {
                "guild_install": "0",
                "user_install": "8@users.example",
            },
            "message": {
                "id": "42",
                "origin_domain": "dm.example",
                "response_id": "42",
                "response_ref": "42@dm.example",
                "interaction_id": "80",
                "interaction_ref": "80@dm.example",
                "channel_id": "8",
                "channel_domain": "dm.example",
                "channel_ref": "8@dm.example",
                "author_id": "5",
                "author_domain": "apps.example",
                "author": {
                    "id": "5",
                    "origin_domain": "apps.example",
                    "username": "bot",
                    "bot": True,
                },
                "application_id": "1",
                "application_domain": "apps.example",
                "application_ref": "1@apps.example",
                "content": "Private controls",
                "e2ee": None,
                "embeds": [],
                "components": [
                    {
                        "type": 1,
                        "components": [{"type": 2, "custom_id": "next"}],
                    }
                ],
                "attachments": [],
                "poll": None,
                "flags": 64,
                "tts": False,
                "message_type": 20,
                "interaction_metadata": {},
                "view_version": 1,
                "view_expires_at": "2026-08-29T00:10:00+00:00",
                "created_at": "2026-08-29T00:00:00+00:00",
                "ephemeral": True,
                "durable": False,
                "sequence": 0,
                "revision": "1",
            },
        }
    )
    return payload


def test_bot_dm_owner_sentinel_and_ephemeral_source_have_distinct_types() -> None:
    current = Interaction.from_payload(
        client(),
        "https://dm.example",
        bot_dm_ephemeral_interaction_payload(),
    )

    assert current.authorizing_integration_owners == {
        "guild_install": "0",
        "user_install": EntityRef(8, "users.example"),
    }
    assert isinstance(current.message, InteractionSourceMessage)
    assert current.message.ref == EntityRef(42, "dm.example")
    assert current.message.content == "Private controls"
    assert current.message.ephemeral is True
    assert current.message.durable is False
    assert current.message_ref is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response_ref", "43@dm.example"),
        ("interaction_ref", "81@dm.example"),
        ("application_ref", "2@apps.example"),
        ("application_id", "2"),
    ],
)
def test_ephemeral_source_identity_is_bound_to_outer_interaction(
    field: str,
    value: str,
) -> None:
    payload = bot_dm_ephemeral_interaction_payload()
    message = payload["message"]
    assert isinstance(message, dict)
    message[field] = value

    with pytest.raises(ValueError, match="interaction source"):
        Interaction.from_payload(client(), "https://dm.example", payload)


def test_guild_owner_sentinel_is_rejected_outside_bot_dm_capabilities() -> None:
    payload = guild_interaction_payload()
    payload["authorizing_integration_owners"] = {"guild_install": "0"}

    with pytest.raises(ValueError, match="only valid for bot-DM"):
        Interaction.from_payload(client(), TARGET, payload)


def lifecycle_event(*, authority: str, token: str) -> dict[str, Any]:
    return {
        "id": "90",
        "interaction_ref": f"90@{authority}",
        "channel_ref": f"7@{authority}",
        "token": token,
        "expires_at": "2030-08-29T00:00:00+00:00",
        "integration_type": "guild_install",
        "installation_revision": "1",
        "installation_id": "4",
        "user_installation_id": None,
        "bot_dm_capability_id": None,
        "bot_dm_capability_revision": None,
        "installation_ref": None,
        "installation_type": None,
    }


def test_same_snowflake_lifecycle_and_response_views_are_qualified_by_authority() -> (
    None
):
    bot = client()
    first_target = "https://one.example"
    second_target = "https://two.example"
    bot._remember_interaction_lifecycle_grant(  # noqa: SLF001
        lifecycle_event(authority="one.example", token="a" * 43),
        target=first_target,
    )
    bot._remember_interaction_lifecycle_grant(  # noqa: SLF001
        lifecycle_event(authority="two.example", token="b" * 43),
        target=second_target,
    )
    first = View([ActionRow([Button(label="First", custom_id="same")])])
    second = View([ActionRow([Button(label="Second", custom_id="same")])])
    bot.add_view(first, response_id=42, target=first_target)
    bot.add_view(second, response_id=42, target=second_target)

    assert (
        bot._interaction_lifecycle_headers_for_path(  # noqa: SLF001
            "/api/v1/bots/interactions/90/callback",
            origin=first_target,
        )["X-Kaede-Interaction-Token"]
        == "a" * 43
    )
    assert (
        bot._interaction_lifecycle_headers_for_path(  # noqa: SLF001
            "/api/v1/bots/interactions/90/callback",
            origin=second_target,
        )["X-Kaede-Interaction-Token"]
        == "b" * 43
    )
    assert bot._response_views[EntityRef(42, "one.example")] is first  # noqa: SLF001
    assert bot._response_views[EntityRef(42, "two.example")] is second  # noqa: SLF001

    bot.remove_response_view(42, target=first_target)

    assert EntityRef(42, "one.example") not in bot._response_views  # noqa: SLF001
    assert bot._response_views[EntityRef(42, "two.example")] is second  # noqa: SLF001


def component_event(*, authority: str, token: str) -> dict[str, Any]:
    payload = private_interaction_payload()
    payload.update(
        {
            "id": "90",
            "interaction_ref": f"90@{authority}",
            "channel_ref": f"8@{authority}",
            "type": "component",
            "command": None,
            "options": None,
            "response_id": "42",
            "custom_id": "same",
            "token": token,
            "expires_at": "2030-08-29T00:00:00+00:00",
            "installation_revision": "1",
            "installation_id": None,
            "user_installation_id": "4",
            "bot_dm_capability_id": None,
            "bot_dm_capability_revision": None,
            "installation_ref": None,
            "installation_type": None,
        }
    )
    return payload


@pytest.mark.asyncio
async def test_same_snowflake_view_dispatch_uses_event_authority() -> None:
    bot = client()
    calls: list[str] = []
    first = View([ActionRow([Button(label="First", custom_id="same")])])
    second = View([ActionRow([Button(label="Second", custom_id="same")])])

    async def first_callback(interaction: Interaction) -> None:
        calls.append(f"first:{interaction.channel_ref.domain}")

    async def second_callback(interaction: Interaction) -> None:
        calls.append(f"second:{interaction.channel_ref.domain}")

    first.set_callback("same", first_callback)
    second.set_callback("same", second_callback)
    bot.add_view(first, response_id=42, target="https://one.example")
    bot.add_view(second, response_id=42, target="https://two.example")

    await bot.dispatch(
        "INTERACTION_CREATE",
        component_event(authority="one.example", token="a" * 43),
        target="https://one.example",
    )
    await bot.dispatch(
        "INTERACTION_CREATE",
        component_event(authority="two.example", token="b" * 43),
        target="https://two.example",
    )

    assert calls == ["first:one.example", "second:two.example"]


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"version": "1"}, "version"),
        ({"locale": "en_CA"}, "locale"),
        ({"app_permissions": "01"}, "app_permissions"),
        ({"attachment_size_limit": True}, "attachment_size_limit"),
        (
            {"authorizing_integration_owners": {"user_install": "8@users.example"}},
            "selected installation",
        ),
    ],
)
def test_versioned_interaction_rejects_malformed_metadata(
    mutation: dict[str, Any], error: str
) -> None:
    payload = guild_interaction_payload()
    payload.update(mutation)

    with pytest.raises(ValueError, match=error):
        Interaction.from_payload(client(), TARGET, payload)


def test_versioned_interaction_enforces_guild_member_user_shape() -> None:
    guild_payload = guild_interaction_payload()
    guild_payload["user"] = user_payload()
    with pytest.raises(ValueError, match="require member"):
        Interaction.from_payload(client(), TARGET, guild_payload)

    private_payload = private_interaction_payload()
    private_payload["member"] = deepcopy(guild_interaction_payload()["member"])
    with pytest.raises(ValueError, match="member"):
        Interaction.from_payload(client(), TARGET, private_payload)


@pytest.mark.asyncio
async def test_interaction_upload_preflights_event_attachment_limit() -> None:
    bot = client()
    current = Interaction.from_payload(bot, TARGET, guild_interaction_payload())
    current.attachment_size_limit = 3
    bot.upload_interaction_attachment = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="attachment_size_limit"):
        await current.upload_attachment(
            b"data",
            filename="too-large.bin",
            content_type="application/octet-stream",
        )

    bot.upload_interaction_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_opts_into_and_unwraps_discord_response_object() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "interaction": {
                    "id": "90",
                    "type": 4,
                    "response_message_id": "21",
                    "response_message_loading": False,
                    "response_message_ephemeral": False,
                },
                "resource": {"type": 4, "message": message_payload() | {"id": "21"}},
            },
            {
                "interaction": {
                    "id": "90",
                    "type": 5,
                    "response_message_loading": True,
                    "response_message_ephemeral": False,
                },
                "resource": {"type": 5},
            },
        ]
    )

    response = await bot.interaction_callback(90, 4, {"content": "done"}, target=TARGET)
    deferred = await bot.interaction_callback(90, 5, target=TARGET)

    assert isinstance(response, Message)
    assert response.ref == EntityRef(21, "guild.example")
    assert deferred is None
    first_call = bot.request.await_args_list[0]
    assert first_call.kwargs["params"] == {"with_response": "true"}
    assert first_call.kwargs["json"] == {"type": 4, "data": {"content": "done"}}
