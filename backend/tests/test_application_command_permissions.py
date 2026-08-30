from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import Index

from app.api import interactions
from app.api.applications import (
    FederatedGuildCommandsPut,
    federated_guild_command_payload,
)
from app.api.interactions import guild_install_command, proxy_command_permissions
from app.bots.command_permissions import (
    CommandPermissionsPut,
    CommandPermissionSubject,
    command_permission_allowed,
    permission_subject,
    select_effective_rows,
)
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.base import Base
from app.db.bot_models import ApplicationCommandPermission
from app.db.models import Channel, Guild, User


def guild() -> Guild:
    return Guild(
        id=100,
        origin_domain="guild.example",
        name="Guild",
        owner_id=1,
        owner_domain="guild.example",
    )


def actor() -> User:
    return User(
        id=200,
        origin_domain="users.example",
        username="member",
        is_local=False,
        account_type="human",
    )


def federated_permission_scope(
    *,
    application_ref: str = "300@apps.example",
    guild_ref: str = "100@remote.example",
    command_ref: str | None = "700@apps.example",
    permissions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    application_id, _, application_domain = application_ref.partition("@")
    guild_id, _, guild_domain = guild_ref.partition("@")
    command_id, _, command_domain = (command_ref or "").partition("@")
    return {
        "id": command_ref or application_ref,
        "application_id": application_id,
        "application_domain": application_domain,
        "application_ref": application_ref,
        "application_name": "Weather Bot",
        "guild_id": guild_id,
        "guild_domain": guild_domain,
        "guild_ref": guild_ref,
        "command": (
            {
                "id": command_id,
                "origin_domain": command_domain,
                "ref": command_ref,
                "name": "weather",
                "type": "chat_input",
                "guild_ref": None,
            }
            if command_ref is not None
            else None
        ),
        "command_ref": command_ref,
        "synced": False,
        "permissions": permissions or [],
    }


def overwrite(
    *,
    command_id: int | None,
    target_type: str,
    target_id: int,
    target_domain: str,
    permission: bool,
) -> ApplicationCommandPermission:
    return ApplicationCommandPermission(
        id=900 + target_id,
        application_id=300,
        application_domain="apps.example",
        guild_id=100,
        guild_domain="guild.example",
        command_id=command_id,
        target_type=target_type,
        target_id=target_id,
        target_domain=target_domain,
        permission=permission,
    )


def test_permission_schema_is_bounded_and_targets_are_unique() -> None:
    CommandPermissionsPut(
        permissions=[
            {"id": "100@guild.example", "type": "role", "permission": False},
            {"id": "200@users.example", "type": "user", "permission": True},
        ]
    )
    with pytest.raises(ValidationError, match="unique"):
        CommandPermissionsPut(
            permissions=[
                {"id": "100@guild.example", "type": "role", "permission": False},
                {"id": "100@guild.example", "type": "role", "permission": True},
            ]
        )
    with pytest.raises(ValidationError):
        CommandPermissionsPut(
            permissions=[
                {"id": f"{index + 1}@users.example", "type": "user", "permission": True}
                for index in range(101)
            ]
        )
    with pytest.raises(ValidationError):
        CommandPermissionsPut.model_validate(
            {"permissions": [{"id": "200@users.example", "type": "user", "permission": 1}]}
        )


def test_explicit_grants_override_disabled_default_and_admin_bypasses() -> None:
    current_guild = guild()
    current_actor = actor()
    subject = CommandPermissionSubject(
        user_ref=(current_actor.id, current_actor.origin_domain),
        role_refs=frozenset(
            {
                (current_guild.id, current_guild.origin_domain),
                (500, current_guild.origin_domain),
            }
        ),
        channel_ref=(600, current_guild.origin_domain),
    )
    role_grant = overwrite(
        command_id=700,
        target_type="role",
        target_id=500,
        target_domain=current_guild.origin_domain,
        permission=True,
    )
    assert command_permission_allowed(
        {"default_member_permissions": "0"},
        0,
        [role_grant],
        subject,
        current_guild,
    )
    user_deny = overwrite(
        command_id=700,
        target_type="user",
        target_id=current_actor.id,
        target_domain=current_actor.origin_domain,
        permission=False,
    )
    assert not command_permission_allowed(
        {"default_member_permissions": []},
        0,
        [role_grant, user_deny],
        subject,
        current_guild,
    )
    assert command_permission_allowed(
        {"default_member_permissions": "0"},
        int(Permission.ADMINISTRATOR),
        [user_deny],
        subject,
        current_guild,
    )


def test_channel_denial_applies_to_threads_via_parent() -> None:
    current_guild = guild()
    current_actor = actor()
    thread = Channel(
        id=601,
        origin_domain=current_guild.origin_domain,
        guild_id=current_guild.id,
        guild_domain=current_guild.origin_domain,
        parent_id=600,
        parent_domain=current_guild.origin_domain,
        type=11,
        name="thread",
    )
    subject = permission_subject(current_guild, current_actor, [], thread)
    assert subject.channel_ref == (600, current_guild.origin_domain)
    denial = overwrite(
        command_id=700,
        target_type="channel",
        target_id=600,
        target_domain=current_guild.origin_domain,
        permission=False,
    )
    assert not command_permission_allowed(
        {"default_member_permissions": []},
        0,
        [denial],
        subject,
        current_guild,
    )


def test_everyone_and_all_channels_constants_are_composed() -> None:
    current_guild = guild()
    current_actor = actor()
    subject = CommandPermissionSubject(
        user_ref=(current_actor.id, current_actor.origin_domain),
        role_refs=frozenset({(current_guild.id, current_guild.origin_domain)}),
        channel_ref=(600, current_guild.origin_domain),
    )
    everyone_allow = overwrite(
        command_id=700,
        target_type="role",
        target_id=current_guild.id,
        target_domain=current_guild.origin_domain,
        permission=True,
    )
    all_channels_deny = overwrite(
        command_id=700,
        target_type="channel",
        target_id=current_guild.id - 1,
        target_domain=current_guild.origin_domain,
        permission=False,
    )
    assert not command_permission_allowed(
        {"default_member_permissions": "0"},
        0,
        [everyone_allow, all_channels_deny],
        subject,
        current_guild,
    )
    all_channels_deny.permission = True
    assert command_permission_allowed(
        {"default_member_permissions": "0"},
        0,
        [all_channels_deny],
        subject,
        current_guild,
    )


@pytest.mark.asyncio
async def test_permission_editor_needs_both_management_bits_and_command_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_guild = guild()
    current_actor = actor()
    command = SimpleNamespace(
        id=700,
        application_id=300,
        application_domain="apps.example",
        definition={"default_member_permissions": ["BAN_MEMBERS"]},
    )
    get_permissions = AsyncMock(return_value=int(Permission.MANAGE_GUILD))
    monkeypatch.setattr(interactions, "get_permissions", get_permissions)
    with pytest.raises(interactions.HTTPException) as missing_manage_roles:
        await interactions.require_command_permission_manager(
            SimpleNamespace(),
            SimpleNamespace(),
            current_guild,
            current_actor,
            command,
        )
    assert missing_manage_roles.value.detail == {"code": "MISSING_PERMISSIONS"}

    get_permissions.return_value = int(Permission.MANAGE_GUILD | Permission.MANAGE_ROLES)
    monkeypatch.setattr(interactions, "guild_permission_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(interactions, "guild_member_role_refs", AsyncMock(return_value=[]))
    with pytest.raises(interactions.HTTPException) as cannot_run:
        await interactions.require_command_permission_manager(
            SimpleNamespace(),
            SimpleNamespace(),
            current_guild,
            current_actor,
            command,
        )
    assert cannot_run.value.detail == {"code": "APPLICATION_COMMAND_PERMISSION_DENIED"}

    get_permissions.return_value = int(
        Permission.MANAGE_GUILD | Permission.MANAGE_ROLES | Permission.BAN_MEMBERS
    )
    await interactions.require_command_permission_manager(
        SimpleNamespace(),
        SimpleNamespace(),
        current_guild,
        current_actor,
        command,
    )


@pytest.mark.asyncio
async def test_target_normalization_checks_role_user_and_channel_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_guild = guild()
    current_actor = actor()
    role_check = AsyncMock(return_value=(500, current_guild.origin_domain))
    user_check = AsyncMock()
    channel_check = AsyncMock(return_value=(600, current_guild.origin_domain))
    monkeypatch.setattr(interactions, "normalized_permission_role", role_check)
    monkeypatch.setattr(interactions, "require_manageable_permission_user", user_check)
    monkeypatch.setattr(interactions, "normalized_permission_channel", channel_check)
    payload = CommandPermissionsPut(
        permissions=[
            {"id": "500", "type": "role", "permission": True},
            {"id": "200@users.example", "type": "user", "permission": False},
            {"id": "600", "type": "channel", "permission": True},
        ]
    )
    normalized = await interactions.normalized_command_permission_entries(
        SimpleNamespace(),
        SimpleNamespace(),
        current_guild,
        current_actor,
        payload.permissions,
    )
    assert normalized == [
        ("role", 500, current_guild.origin_domain, True),
        ("user", 200, "users.example", False),
        ("channel", 600, current_guild.origin_domain, True),
    ]
    role_check.assert_awaited_once_with(
        ANY,
        current_guild,
        current_actor,
        (500, current_guild.origin_domain),
    )
    user_check.assert_awaited_once_with(
        ANY,
        current_guild,
        current_actor,
        (200, "users.example"),
    )
    channel_check.assert_awaited_once_with(
        ANY,
        ANY,
        current_guild,
        current_actor,
        (600, current_guild.origin_domain),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_ref", "error_code"),
    [
        ((200, "users.example"), "CANNOT_MANAGE_SELF"),
        ((1, "guild.example"), "OWNER_IMMUNE"),
    ],
)
async def test_permission_editor_cannot_manage_self_or_guild_owner(
    target_ref: tuple[int, str],
    error_code: str,
) -> None:
    current_guild = guild()
    current_actor = actor()
    member = SimpleNamespace()
    session = SimpleNamespace(get=AsyncMock(return_value=member))
    with pytest.raises(interactions.HTTPException) as denied:
        await interactions.require_manageable_permission_user(
            session,
            current_guild,
            current_actor,
            target_ref,
        )
    assert denied.value.detail == {"code": error_code}


@pytest.mark.asyncio
async def test_all_channels_constant_requires_channel_management(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_guild = guild()
    current_actor = actor()
    get_permissions = AsyncMock(return_value=int(Permission.MANAGE_GUILD))
    monkeypatch.setattr(interactions, "get_permissions", get_permissions)
    all_channels_ref = current_guild.id - 1, current_guild.origin_domain
    with pytest.raises(interactions.HTTPException) as denied:
        await interactions.normalized_permission_channel(
            SimpleNamespace(),
            SimpleNamespace(),
            current_guild,
            current_actor,
            all_channels_ref,
        )
    assert denied.value.detail == {"code": "MISSING_PERMISSIONS"}
    get_permissions.return_value = int(Permission.MANAGE_CHANNELS)
    assert (
        await interactions.normalized_permission_channel(
            SimpleNamespace(),
            SimpleNamespace(),
            current_guild,
            current_actor,
            all_channels_ref,
        )
        == all_channels_ref
    )


def test_command_overwrites_replace_application_synchronized_rows() -> None:
    app_deny = overwrite(
        command_id=None,
        target_type="role",
        target_id=100,
        target_domain="guild.example",
        permission=False,
    )
    command_allow = overwrite(
        command_id=700,
        target_type="role",
        target_id=100,
        target_domain="guild.example",
        permission=True,
    )
    assert select_effective_rows(
        [app_deny, command_allow],
        command_id=700,
        application_ref=(300, "apps.example"),
    ) == [command_allow]
    assert select_effective_rows(
        [app_deny],
        command_id=700,
        application_ref=(300, "apps.example"),
    ) == [app_deny]


def test_permission_table_and_migration_preserve_scope_uniqueness() -> None:
    table = Base.metadata.tables["application_command_permissions"]
    indexes = {index.name: index for index in table.indexes if isinstance(index, Index)}
    assert indexes["uq_application_command_permission_application_target"].unique
    assert indexes["uq_application_command_permission_command_target"].unique
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()
    assert 'op.create_table(\n        "application_command_permissions"' in migration
    assert migration.index('op.drop_table("application_command_permissions")') < migration.index(
        'op.drop_column("bot_applications", "supported_install_types")'
    )


def test_guild_command_projection_has_fixed_context_and_strips_local_metadata() -> None:
    projection = FederatedGuildCommandsPut(
        generation="2",
        commands=[
            {
                "id": "700",
                "name": "weather",
                "description": "Forecast",
                "contexts": ["guild"],
                "integration_types": ["guild_install"],
            }
        ],
    )
    assert projection.commands[0].contexts == ["guild"]
    with pytest.raises(ValidationError, match="fixed contexts"):
        FederatedGuildCommandsPut(
            generation="2",
            commands=[
                {
                    "id": "700",
                    "name": "weather",
                    "description": "Forecast",
                    "contexts": ["bot_dm"],
                    "integration_types": ["guild_install"],
                }
            ],
        )
    wire = federated_guild_command_payload(
        {
            "generation": "2",
            "items": [
                {
                    "id": "700",
                    "origin_domain": "apps.example",
                    "ref": "700@apps.example",
                    "guild_ref": "100@guild.example",
                    "name": "weather",
                    "description": "Forecast",
                }
            ],
        }
    )
    assert wire == {
        "generation": "2",
        "commands": [{"id": "700", "name": "weather", "description": "Forecast"}],
    }


@pytest.mark.asyncio
async def test_guild_command_resolution_prefers_scoped_override() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    await guild_install_command(
        session,
        guild(),
        application_ref=(300, "apps.example"),
        name="weather",
        command_type="chat_input",
    )
    query = str(session.scalar.await_args.args[0])
    assert "application_commands.guild_id DESC NULLS LAST" in query
    assert "application_commands.guild_id IS NULL" in query


@pytest.mark.asyncio
async def test_remote_permission_proxy_qualifies_bare_guild_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock(status_code=200)
    signed = AsyncMock(return_value=response)
    monkeypatch.setattr(interactions, "signed_request", signed)
    monkeypatch.setattr(
        interactions,
        "decode_federation_response_json",
        lambda _response: federated_permission_scope(
            permissions=[{"id": "100@remote.example", "type": "role", "permission": False}]
        ),
    )
    remote_guild = guild()
    remote_guild.origin_domain = "remote.example"
    payload = CommandPermissionsPut(
        permissions=[{"id": "100", "type": "role", "permission": False}]
    )
    rendered = await proxy_command_permissions(
        SimpleNamespace(),
        SimpleNamespace(domain="home.example"),
        remote_guild,
        actor(),
        (300, "apps.example"),
        command_ref=(700, "apps.example"),
        payload=payload,
    )
    assert signed.await_args.kwargs["payload"]["permissions"][0]["id"] == ("100@remote.example")
    assert rendered["permissions"] == [
        {"id": "100@remote.example", "type": "role", "permission": False}
    ]


@pytest.mark.asyncio
async def test_remote_permission_proxy_normalizes_thread_target_before_echo_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = AsyncMock(return_value=Mock(status_code=200))
    monkeypatch.setattr(interactions, "signed_request", signed)
    monkeypatch.setattr(
        interactions,
        "decode_federation_response_json",
        lambda _response: federated_permission_scope(
            permissions=[{"id": "600@remote.example", "type": "channel", "permission": False}]
        ),
    )
    remote_guild = guild()
    remote_guild.origin_domain = "remote.example"
    thread = Channel(
        id=601,
        origin_domain="remote.example",
        guild_id=remote_guild.id,
        guild_domain=remote_guild.origin_domain,
        parent_id=600,
        parent_domain=remote_guild.origin_domain,
        type=11,
        name="thread",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=thread))
    payload = CommandPermissionsPut(
        permissions=[{"id": "601", "type": "channel", "permission": False}]
    )

    rendered = await proxy_command_permissions(
        session,
        SimpleNamespace(domain="home.example"),
        remote_guild,
        actor(),
        (300, "apps.example"),
        command_ref=(700, "apps.example"),
        payload=payload,
    )

    session.get.assert_awaited_once_with(Channel, (601, "remote.example"))
    assert signed.await_args.kwargs["payload"]["permissions"] == [
        {"id": "600@remote.example", "type": "channel", "permission": False}
    ]
    assert rendered["permissions"] == signed.await_args.kwargs["payload"]["permissions"]


def test_remote_permission_response_is_bound_to_requested_scope() -> None:
    responses = [
        federated_permission_scope(application_ref="301@apps.example"),
        federated_permission_scope(guild_ref="101@remote.example"),
        federated_permission_scope(command_ref="701@apps.example"),
    ]
    for response in responses:
        with pytest.raises(interactions.HTTPException) as invalid:
            interactions.validate_remote_command_permissions(
                response,
                application_ref=(300, "apps.example"),
                guild_ref=(100, "remote.example"),
                command_ref=(700, "apps.example"),
            )
        assert invalid.value.status_code == 502
        assert invalid.value.detail == {"code": "REMOTE_COMMAND_PERMISSIONS_INVALID"}


def test_remote_permission_response_enforces_scope_cardinality() -> None:
    application_scope = federated_permission_scope(command_ref=None)
    command_scope = federated_permission_scope()
    invalid_collections = [
        [],
        [command_scope],
        [application_scope, command_scope, command_scope],
        [
            application_scope,
            *[
                federated_permission_scope(command_ref=f"{700 + index}@apps.example")
                for index in range(131)
            ],
        ],
    ]
    for response in invalid_collections:
        with pytest.raises(interactions.HTTPException) as invalid:
            interactions.validate_remote_command_permissions(
                response,
                application_ref=(300, "apps.example"),
                guild_ref=(100, "remote.example"),
                command_ref=None,
            )
        assert invalid.value.detail == {"code": "REMOTE_COMMAND_PERMISSIONS_INVALID"}


@pytest.mark.parametrize(
    "permissions",
    [
        [
            {"id": "500@remote.example", "type": "role", "permission": True},
            {"id": "500@remote.example", "type": "role", "permission": False},
        ],
        [
            {
                "id": f"{index + 1}@users.example",
                "type": "user",
                "permission": True,
            }
            for index in range(101)
        ],
        [{"id": "500", "type": "role", "permission": True}],
        [{"id": "500@other.example", "type": "channel", "permission": True}],
    ],
)
def test_remote_permission_response_enforces_target_contract(
    permissions: list[dict[str, object]],
) -> None:
    with pytest.raises(interactions.HTTPException) as invalid:
        interactions.validate_remote_command_permissions(
            federated_permission_scope(permissions=permissions),
            application_ref=(300, "apps.example"),
            guild_ref=(100, "remote.example"),
            command_ref=(700, "apps.example"),
        )
    assert invalid.value.detail == {"code": "REMOTE_COMMAND_PERMISSIONS_INVALID"}


@pytest.mark.asyncio
async def test_remote_permission_put_rejects_non_echoing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interactions,
        "signed_request",
        AsyncMock(return_value=Mock(status_code=200)),
    )
    monkeypatch.setattr(
        interactions,
        "decode_federation_response_json",
        lambda _response: federated_permission_scope(
            permissions=[{"id": "100@remote.example", "type": "role", "permission": True}]
        ),
    )
    remote_guild = guild()
    remote_guild.origin_domain = "remote.example"
    payload = CommandPermissionsPut(
        permissions=[{"id": "100", "type": "role", "permission": False}]
    )
    with pytest.raises(interactions.HTTPException) as invalid:
        await proxy_command_permissions(
            SimpleNamespace(),
            SimpleNamespace(domain="home.example"),
            remote_guild,
            actor(),
            (300, "apps.example"),
            command_ref=(700, "apps.example"),
            payload=payload,
        )
    assert invalid.value.status_code == 502
    assert invalid.value.detail == {"code": "REMOTE_COMMAND_PERMISSIONS_INVALID"}


@pytest.mark.asyncio
async def test_bot_can_read_own_command_permissions_at_exact_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_guild = guild()
    principal = SimpleNamespace(
        application=SimpleNamespace(id=300, origin_domain="apps.example"),
        user=SimpleNamespace(id=201, origin_domain="apps.example"),
    )
    authorize = AsyncMock(return_value=(current_guild, SimpleNamespace()))
    listed = AsyncMock(return_value=[{"id": "300@apps.example"}])
    fetched = AsyncMock(return_value={"id": "700@apps.example"})
    monkeypatch.setattr(interactions, "installation_for_guild", authorize)
    monkeypatch.setattr(interactions, "list_local_command_permissions", listed)
    monkeypatch.setattr(interactions, "get_local_command_permissions", fetched)
    session = SimpleNamespace()
    redis = SimpleNamespace()
    settings = SimpleNamespace(domain="guild.example")
    guild_ref = EntityRef("100@guild.example")

    assert await interactions.bot_list_application_command_permissions(
        guild_ref,
        principal,
        session,
        redis,
        settings,
    ) == [{"id": "300@apps.example"}]
    assert await interactions.bot_get_application_command_permissions(
        guild_ref,
        EntityRef("700@apps.example"),
        principal,
        session,
        redis,
        settings,
    ) == {"id": "700@apps.example"}

    assert authorize.await_count == 2
    for call in authorize.await_args_list:
        assert call.args == (
            session,
            settings,
            principal,
            guild_ref,
            "applications.commands",
        )
    listed.assert_awaited_once_with(
        session,
        redis,
        current_guild,
        principal.user,
        (300, "apps.example"),
        require_manage_guild=False,
    )
    fetched.assert_awaited_once_with(
        session,
        redis,
        current_guild,
        principal.user,
        (300, "apps.example"),
        (700, "apps.example"),
        require_manage_guild=False,
    )

    authorize.side_effect = interactions.HTTPException(
        status_code=403,
        detail={"code": "BOT_NOT_INSTALLED"},
    )
    with pytest.raises(interactions.HTTPException) as denied:
        await interactions.bot_list_application_command_permissions(
            guild_ref,
            principal,
            session,
            redis,
            settings,
        )
    assert denied.value.detail == {"code": "BOT_NOT_INSTALLED"}
    assert listed.await_count == 1
