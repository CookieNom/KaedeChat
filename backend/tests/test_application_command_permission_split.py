from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.api.interactions as interactions
from app.chat.channel_access import ChannelAccess
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation, BotUserInstallation
from app.db.models import Channel, Guild, User


def guild_installation() -> BotInstallation:
    return BotInstallation(
        id=10,
        application_id=20,
        application_domain="apps.example",
        guild_id=30,
        guild_domain="guild.example",
        bot_user_id=40,
        bot_user_domain="apps.example",
        installer_id=50,
        installer_domain="guild.example",
        grant_revision=1,
        status="active",
    )


def user_installation() -> BotUserInstallation:
    return BotUserInstallation(
        id=11,
        application_id=20,
        application_domain="apps.example",
        user_id=50,
        user_domain="users.example",
        granted_scopes=["applications.commands", "interactions.respond"],
        granted_intents=["interactions"],
        contexts=["guild"],
        grant_revision=1,
        status="active",
    )


@pytest.mark.parametrize(
    ("guild_installed", "interaction_type", "command_type", "permissions", "allowed"),
    [
        (True, "command", "chat_input", Permission.VIEW_CHANNEL, False),
        (False, "command", "chat_input", Permission.VIEW_CHANNEL, True),
        (True, "command", "message", Permission.VIEW_CHANNEL, False),
        (False, "command", "message", Permission.VIEW_CHANNEL, True),
        (True, "autocomplete", "chat_input", Permission.VIEW_CHANNEL, False),
        (False, "autocomplete", "chat_input", Permission.VIEW_CHANNEL, True),
        (True, "component", "chat_input", Permission.VIEW_CHANNEL, False),
        (False, "component", "chat_input", Permission.VIEW_CHANNEL, True),
        (True, "modal_submit", "chat_input", Permission.VIEW_CHANNEL, False),
        (False, "modal_submit", "chat_input", Permission.VIEW_CHANNEL, True),
        (False, "command", "user", Permission.VIEW_CHANNEL, False),
        (
            False,
            "command",
            "user",
            Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES,
            True,
        ),
        (True, "command", "chat_input", Permission.ADMINISTRATOR, True),
    ],
)
def test_application_interaction_permission_policy_is_installation_aware(
    guild_installed: bool,
    interaction_type: str,
    command_type: str,
    permissions: Permission,
    allowed: bool,
) -> None:
    assert (
        interactions.application_interaction_allowed(
            int(permissions),
            guild_installed=guild_installed,
            interaction_type=interaction_type,
            command_type=command_type,
            channel_type=0,
        )
        is allowed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_domain", ["local.example", "remote.example"])
async def test_channel_discovery_hides_only_guild_installs_without_use_commands(
    monkeypatch: pytest.MonkeyPatch,
    guild_domain: str,
) -> None:
    actor = User(
        id=50,
        origin_domain="users.example",
        username="member",
        is_local=True,
    )
    guild = Guild(
        id=30,
        origin_domain=guild_domain,
        name="Guild",
        owner_id=60,
        owner_domain="users.example",
    )
    channel = cast(Any, SimpleNamespace(id=31, origin_domain=guild_domain, type=0))
    access = ChannelAccess(channel=channel, guild=guild, participants=[])
    require_permissions = AsyncMock(return_value=int(Permission.VIEW_CHANNEL))
    local_guild_commands = AsyncMock(
        return_value=[
            {
                "name": "server",
                "type": "chat_input",
                "integration_type": "guild_install",
            }
        ]
    )
    remote_guild_commands = AsyncMock(
        return_value=[
            {
                "name": "server",
                "type": "chat_input",
                "integration_type": "guild_install",
            }
        ]
    )
    monkeypatch.setattr(interactions, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(interactions, "effective_channel_nsfw", AsyncMock(return_value=False))
    monkeypatch.setattr(interactions, "require_permissions", require_permissions)
    monkeypatch.setattr(interactions, "_local_application_commands", local_guild_commands)
    monkeypatch.setattr(interactions, "_remote_guild_application_commands", remote_guild_commands)
    monkeypatch.setattr(
        interactions,
        "_local_user_application_commands",
        AsyncMock(
            return_value=[
                {
                    "application_ref": "20@apps.example",
                    "name": "external",
                    "type": "chat_input",
                    "integration_type": "user_install",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        interactions,
        "filter_guild_commands_for_permissions",
        AsyncMock(return_value=[]),
    )

    result = await interactions.channel_application_commands(
        EntityRef(f"31@{guild_domain}"),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="local.example")),
    )

    assert [command["name"] for command in result] == ["external"]
    assert require_permissions.await_args.args[4] == Permission.VIEW_CHANNEL
    local_guild_commands.assert_not_awaited()
    remote_guild_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_federated_channel_discovery_returns_no_guild_commands_without_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=50,
        origin_domain="users.example",
        username="member",
        is_local=False,
    )
    guild = Guild(
        id=30,
        origin_domain="local.example",
        name="Guild",
        owner_id=60,
        owner_domain="users.example",
    )
    channel = Channel(
        id=31,
        origin_domain="local.example",
        guild_id=30,
        guild_domain="local.example",
        type=0,
        name="general",
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[actor, guild, channel]))
    require_permissions = AsyncMock(return_value=int(Permission.VIEW_CHANNEL))
    local_commands = AsyncMock(return_value=[{"name": "server"}])
    monkeypatch.setattr(interactions, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(interactions, "require_permissions", require_permissions)
    monkeypatch.setattr(interactions, "effective_channel_nsfw", AsyncMock(return_value=False))
    monkeypatch.setattr(interactions, "_local_application_commands", local_commands)

    result = await interactions.federation_guild_application_commands(
        30,
        "50",
        cast(Any, SimpleNamespace(origin="users.example", silenced=False)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="local.example")),
        channel_id=31,
    )

    assert result == []
    assert require_permissions.await_args.args[4] == Permission.VIEW_CHANNEL
    local_commands.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interaction_type", "command_type"),
    [
        ("command", "chat_input"),
        ("command", "user"),
        ("command", "message"),
        ("autocomplete", "chat_input"),
        ("component", "chat_input"),
        ("modal_submit", "chat_input"),
    ],
)
async def test_user_install_invocations_survive_missing_use_application_commands(
    monkeypatch: pytest.MonkeyPatch,
    interaction_type: str,
    command_type: str,
) -> None:
    installation = user_installation()
    command = (
        None
        if interaction_type in {"component", "modal_submit"}
        else SimpleNamespace(definition={})
    )
    granular = AsyncMock()
    monkeypatch.setattr(interactions, "require_guild_command_permission", granular)
    context = SimpleNamespace(
        session=SimpleNamespace(),
        redis=SimpleNamespace(),
        settings=SimpleNamespace(),
        access=SimpleNamespace(
            guild=SimpleNamespace(),
            channel=SimpleNamespace(type=0),
        ),
        actor=SimpleNamespace(),
        payload=SimpleNamespace(
            interaction_type=interaction_type,
            command_type=command_type,
        ),
        application=interactions.InteractionApplicationContext(
            command=cast(Any, command),
            installation=None,
            user_installation=installation,
            dm_capability=None,
            application=cast(Any, SimpleNamespace()),
            bot=cast(Any, SimpleNamespace()),
            interaction_context="guild",
        ),
        invoker_policy=interactions.InteractionInvokerPolicy("en-US", True, True),
        invocation_permissions=int(Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES),
    )

    await interactions.validate_interaction_command_access(cast(Any, context))

    granular.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interaction_type", "command_type"),
    [
        ("command", "chat_input"),
        ("command", "user"),
        ("command", "message"),
        ("autocomplete", "chat_input"),
        ("component", "chat_input"),
        ("modal_submit", "chat_input"),
    ],
)
async def test_guild_install_invocations_are_blocked_without_use_application_commands(
    monkeypatch: pytest.MonkeyPatch,
    interaction_type: str,
    command_type: str,
) -> None:
    installation = guild_installation()
    command = (
        None
        if interaction_type in {"component", "modal_submit"}
        else SimpleNamespace(definition={})
    )
    granular = AsyncMock()
    monkeypatch.setattr(interactions, "require_guild_command_permission", granular)
    context = SimpleNamespace(
        session=SimpleNamespace(),
        redis=SimpleNamespace(),
        settings=SimpleNamespace(),
        access=SimpleNamespace(
            guild=SimpleNamespace(),
            channel=SimpleNamespace(type=0),
        ),
        actor=SimpleNamespace(),
        payload=SimpleNamespace(
            interaction_type=interaction_type,
            command_type=command_type,
        ),
        application=interactions.InteractionApplicationContext(
            command=cast(Any, command),
            installation=installation,
            user_installation=None,
            dm_capability=None,
            application=cast(Any, SimpleNamespace()),
            bot=cast(Any, SimpleNamespace()),
            interaction_context="guild",
        ),
        invoker_policy=interactions.InteractionInvokerPolicy("en-US", True, True),
        invocation_permissions=int(Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES),
    )

    with pytest.raises(HTTPException) as denied:
        await interactions.validate_interaction_command_access(cast(Any, context))

    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "MISSING_PERMISSIONS"
    granular.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_install_invocation_still_enforces_command_defaults() -> None:
    context = SimpleNamespace(
        session=SimpleNamespace(),
        redis=SimpleNamespace(),
        settings=SimpleNamespace(),
        access=SimpleNamespace(
            guild=SimpleNamespace(),
            channel=SimpleNamespace(type=0),
        ),
        actor=SimpleNamespace(),
        payload=SimpleNamespace(interaction_type="command", command_type="chat_input"),
        application=interactions.InteractionApplicationContext(
            command=cast(Any, SimpleNamespace(definition={"default_member_permissions": "0"})),
            installation=None,
            user_installation=user_installation(),
            dm_capability=None,
            application=cast(Any, SimpleNamespace()),
            bot=cast(Any, SimpleNamespace()),
            interaction_context="guild",
        ),
        invoker_policy=interactions.InteractionInvokerPolicy("en-US", True, True),
        invocation_permissions=int(
            Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES | Permission.USE_EXTERNAL_APPS
        ),
    )

    with pytest.raises(HTTPException) as denied:
        await interactions.validate_interaction_command_access(cast(Any, context))

    assert denied.value.detail["code"] == "APPLICATION_COMMAND_PERMISSION_DENIED"


@pytest.mark.parametrize("interaction_type", ["component", "modal_submit"])
def test_user_install_continuation_response_is_forced_ephemeral_without_use_commands(
    interaction_type: str,
) -> None:
    interaction = cast(
        Any,
        SimpleNamespace(
            status="pending",
            interaction_type=interaction_type,
            guild_id=30,
            invocation_permissions=int(Permission.SEND_MESSAGES | Permission.USE_EXTERNAL_APPS),
            invocation_channel_type=0,
        ),
    )

    flags, ephemeral = interactions.validate_interaction_callback_type(
        interaction,
        interactions.InteractionCallback(type=4),
        user_installation(),
    )

    assert flags & interactions.INTERACTION_EPHEMERAL_FLAG
    assert ephemeral


@pytest.mark.parametrize("interaction_type", ["component", "modal_submit"])
@pytest.mark.parametrize("callback_type", [6, 7])
def test_user_install_public_update_is_blocked_when_private_response_is_required(
    interaction_type: str,
    callback_type: int,
) -> None:
    interaction = cast(
        Any,
        SimpleNamespace(
            status="pending",
            interaction_type=interaction_type,
            guild_id=30,
            message_id=40,
            invocation_permissions=int(Permission.SEND_MESSAGES | Permission.USE_EXTERNAL_APPS),
            invocation_channel_type=0,
            payload={},
        ),
    )

    with pytest.raises(HTTPException) as denied:
        interactions.validate_interaction_callback_type(
            interaction,
            interactions.InteractionCallback(type=cast(Any, callback_type)),
            user_installation(),
        )

    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "USER_INSTALL_EPHEMERAL_REQUIRED"


@pytest.mark.parametrize("interaction_type", ["component", "modal_submit"])
@pytest.mark.parametrize("callback_type", [6, 7])
def test_user_install_private_source_update_remains_available_when_private_response_is_required(
    interaction_type: str,
    callback_type: int,
) -> None:
    interaction = cast(
        Any,
        SimpleNamespace(
            status="pending",
            interaction_type=interaction_type,
            guild_id=30,
            message_id=None,
            invocation_permissions=int(Permission.SEND_MESSAGES | Permission.USE_EXTERNAL_APPS),
            invocation_channel_type=0,
            payload={"response_id": "40"},
        ),
    )

    flags, ephemeral = interactions.validate_interaction_callback_type(
        interaction,
        interactions.InteractionCallback(type=cast(Any, callback_type)),
        user_installation(),
    )

    assert flags == 0
    assert not ephemeral
