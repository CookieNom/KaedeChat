from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.api.guilds as guilds
import app.api.management as management
import app.chat.permissions as chat_permissions
from app.chat.permissions import BotGuildPermissionGrant
from app.chat.schemas import ChannelCreate, ChannelPositionBatch, ChannelUpdate
from app.core.permissions import Permission
from app.core.types import EntityRef


def channel(
    channel_id: int,
    position: int,
    *,
    parent_id: int | None = None,
    channel_type: int = 0,
    flags: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=channel_id,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        unavailable=False,
        type=channel_type,
        position=position,
        parent_id=parent_id,
        parent_domain="guild.example" if parent_id is not None else None,
        permissions_synced=False,
        flags=flags,
        encryption_mode="plaintext",
        encryption_state="disabled",
    )


def reorder_context(
    monkeypatch: pytest.MonkeyPatch,
    channels: list[SimpleNamespace],
) -> tuple[SimpleNamespace, SimpleNamespace]:
    guild = SimpleNamespace(
        id=1,
        origin_domain="guild.example",
        permission_generation=0,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[channels, channels]),
        execute=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        management,
        "proxy_remote_guild_management",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(management, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        management,
        "federation_channel_state",
        lambda item: {
            "id": str(item.id),
            "flags": str(item.flags),
            "encryption_state": item.encryption_state,
        },
    )
    monkeypatch.setattr(
        management,
        "channel_payload",
        lambda item: {"id": str(item.id), "position": item.position},
    )
    monkeypatch.setattr(management, "queue_guild_mutation", AsyncMock())
    monkeypatch.setattr(management, "add_audit_entry", AsyncMock())
    monkeypatch.setattr(management, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(management, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(management, "publish_e2ee_policy_updates", AsyncMock())
    return session, guild


@pytest.mark.asyncio
async def test_restricted_bot_partial_reorder_returns_no_channel_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [channel(10, 0), channel(11, 1), channel(12, 2)]
    session, _guild = reorder_context(monkeypatch, channels)
    session.scalars.side_effect = [channels, [channels[2]]]
    permission_check = AsyncMock()
    monkeypatch.setattr(management, "require_permissions", permission_check)
    monkeypatch.setattr(
        chat_permissions,
        "bot_guild_permission_grant",
        AsyncMock(return_value=BotGuildPermissionGrant(99, 1, 0, ("12",))),
    )

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate({"channels": [{"id": "12", "position": 0}]}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="bot"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    assert result.body == b""
    assert [(item.id, item.position) for item in channels] == [(10, 1), (11, 2), (12, 0)]
    # Discord position-only moves rely on guild/current-parent authority, not
    # per-target VIEW_CHANNEL. The install restriction remains a separate cap.
    assert permission_check.await_count == 1


@pytest.mark.asyncio
async def test_restricted_bot_cannot_submit_a_blocked_channel_or_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [channel(10, 0), channel(11, 1), channel(20, 2, channel_type=4)]
    session, _guild = reorder_context(monkeypatch, channels)
    monkeypatch.setattr(
        chat_permissions,
        "bot_guild_permission_grant",
        AsyncMock(return_value=BotGuildPermissionGrant(99, 1, 0, ("10",))),
    )

    async def check_permission(*_args: object, **kwargs: object) -> None:
        target = cast(SimpleNamespace | None, kwargs.get("channel"))
        if target is not None and target.id in {11, 20}:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})

    monkeypatch.setattr(management, "require_permissions", AsyncMock(side_effect=check_permission))

    for body, locked in (
        ({"channels": [{"id": "11", "position": 0}]}, None),
        (
            {"channels": [{"id": "10", "position": 0, "parent_id": "20"}]},
            [channels[0], channels[2]],
        ),
    ):
        session.scalars.side_effect = [channels] if locked is None else [channels, locked]
        with pytest.raises(HTTPException) as denied:
            await management.reorder_channels(
                EntityRef("1@guild.example"),
                ChannelPositionBatch.model_validate(body),
                cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="bot"))),
                cast(Any, session),
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace(domain="guild.example")),
            )
        assert denied.value.status_code == 403

    assert [(item.id, item.position, item.parent_id) for item in channels] == [
        (10, 0, None),
        (11, 1, None),
        (20, 2, None),
    ]


@pytest.mark.asyncio
async def test_restricted_bot_cannot_lock_permissions_from_blocked_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [channel(10, 0, parent_id=20), channel(20, 1, channel_type=4)]
    session, _guild = reorder_context(monkeypatch, channels)
    session.scalars.side_effect = [channels]
    monkeypatch.setattr(management, "require_permissions", AsyncMock())
    monkeypatch.setattr(
        chat_permissions,
        "bot_guild_permission_grant",
        AsyncMock(return_value=BotGuildPermissionGrant(99, 1, 0, ("10",))),
    )

    with pytest.raises(HTTPException) as denied:
        await management.reorder_channels(
            EntityRef("1@guild.example"),
            ChannelPositionBatch.model_validate(
                {"channels": [{"id": "10", "lock_permissions": True}]}
            ),
            cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="bot"))),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    assert channels[0].permissions_synced is False
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_partial_reorder_preserves_an_omitted_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [
        channel(10, 0, parent_id=20),
        channel(11, 1),
        channel(20, 2, channel_type=4),
    ]
    session, _guild = reorder_context(monkeypatch, channels)
    session.scalars.side_effect = [channels, [channels[0], channels[2]]]
    monkeypatch.setattr(management, "require_permissions", AsyncMock())

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate({"channels": [{"id": "10", "position": 1}]}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    assert result.body == b""
    assert [(item.id, item.position) for item in channels] == [(10, 1), (11, 0), (20, 2)]
    assert channels[0].parent_id == 20
    permission_checks = cast(AsyncMock, management.require_permissions).await_args_list
    assert permission_checks[1].kwargs["channel"].id == 20
    assert permission_checks[1].args[4] == Permission.MANAGE_CHANNELS


@pytest.mark.asyncio
async def test_multiple_supplied_noop_parents_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [
        channel(20, 0, channel_type=4),
        channel(11, 1, parent_id=20),
        channel(10, 2),
    ]
    session, _guild = reorder_context(monkeypatch, channels)
    monkeypatch.setattr(management, "require_permissions", AsyncMock())

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate(
            {
                "channels": [
                    {"id": "20", "position": 0, "parent_id": None},
                    {"id": "11", "position": 1, "parent_id": "20"},
                    {"id": "10", "position": 2, "parent_id": None},
                ]
            }
        ),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    assert [(item.id, item.position, item.parent_id) for item in channels] == [
        (20, 0, None),
        (11, 1, 20),
        (10, 2, None),
    ]
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_actual_parent_changes_fail_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [
        channel(10, 0, parent_id=20),
        channel(11, 1, parent_id=20),
        channel(20, 2, channel_type=4),
        channel(21, 3, channel_type=4),
    ]
    session, _guild = reorder_context(monkeypatch, channels)
    monkeypatch.setattr(management, "require_permissions", AsyncMock())

    with pytest.raises(HTTPException) as rejected:
        await management.reorder_channels(
            EntityRef("1@guild.example"),
            ChannelPositionBatch.model_validate(
                {
                    "channels": [
                        {"id": "10", "parent_id": "21"},
                        {"id": "11", "parent_id": "21"},
                    ]
                }
            ),
            cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert rejected.value.status_code == 400
    assert rejected.value.detail == {
        "code": 40009,
        "message": "Only one channel can have a parent_id modified at a time",
    }
    assert [(item.id, item.parent_id) for item in channels] == [
        (10, 20),
        (11, 20),
        (20, None),
        (21, None),
    ]
    assert session.scalars.await_count == 1
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_requested_positions_are_grouped_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [channel(30, 0), channel(20, 1), channel(10, 2), channel(40, 3)]
    session, _guild = reorder_context(monkeypatch, channels)
    session.scalars.side_effect = [channels, [channels[1], channels[2]]]
    monkeypatch.setattr(management, "require_permissions", AsyncMock())

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate(
            {
                "channels": [
                    {"id": "20", "position": 1},
                    {"id": "10", "position": 1},
                ]
            }
        ),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    assert [
        (item.id, item.position) for item in sorted(channels, key=lambda item: item.position)
    ] == [
        (30, 0),
        (10, 1),
        (20, 2),
        (40, 3),
    ]


@pytest.mark.asyncio
async def test_reorder_flags_use_channel_update_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [channel(10, 0, channel_type=15)]
    session, guild = reorder_context(monkeypatch, channels)
    permission_check = AsyncMock()
    monkeypatch.setattr(management, "require_permissions", permission_check)

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate({"channels": [{"id": "10", "flags": 16}]}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    assert channels[0].flags == 16
    assert guild.permission_generation == 0
    assert permission_check.await_args_list[1].args[4] == Permission.MANAGE_CHANNELS
    mutation = cast(AsyncMock, management.queue_guild_mutation)
    assert mutation.await_args.args[5] == {
        "channel": {
            "id": "10",
            "flags": "16",
            "encryption_state": "disabled",
        }
    }
    cast(AsyncMock, management.add_audit_entry).assert_awaited_once()
    cast(AsyncMock, management.publish_dispatch).assert_awaited_once()


@pytest.mark.asyncio
async def test_nullable_position_lock_and_flags_are_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [channel(10, 0), channel(11, 1, channel_type=15)]
    session, _guild = reorder_context(monkeypatch, channels)
    monkeypatch.setattr(management, "require_permissions", AsyncMock())

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate(
            {
                "channels": [
                    {
                        "id": "10",
                        "position": None,
                        "lock_permissions": None,
                        "flags": 0,
                    },
                    {"id": "11", "flags": None},
                ]
            }
        ),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_change_checks_target_and_destination_and_can_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [
        channel(10, 0, parent_id=20),
        channel(20, 1, channel_type=4),
        channel(21, 2, channel_type=4),
    ]
    session, _guild = reorder_context(monkeypatch, channels)
    session.scalars.side_effect = [channels, [channels[0], channels[2]]]
    permission_check = AsyncMock()
    monkeypatch.setattr(management, "require_permissions", permission_check)

    moved = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate({"channels": [{"id": "10", "parent_id": "21"}]}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert moved.status_code == 204
    assert channels[0].parent_id == 21
    assert [call.kwargs["channel"].id for call in permission_check.await_args_list[1:]] == [
        10,
        21,
    ]
    expected = Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS
    assert all(call.args[4] == expected for call in permission_check.await_args_list[1:])

    # Explicit null is distinct from omission and detaches the channel.
    channels[0].parent_id = 20
    channels[0].parent_domain = "guild.example"
    session.scalars.side_effect = [channels, [channels[0]]]
    permission_check.reset_mock()
    detached = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate({"channels": [{"id": "10", "parent_id": None}]}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )
    assert detached.status_code == 204
    assert channels[0].parent_id is None
    assert channels[0].parent_domain is None
    assert permission_check.await_args_list[1].kwargs["channel"].id == 10


@pytest.mark.asyncio
async def test_parent_acl_change_fences_snapshot_and_publishes_e2ee_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [
        channel(10, 0, parent_id=20),
        channel(20, 1, channel_type=4),
        channel(21, 2, channel_type=4),
    ]
    channels[0].encryption_mode = "e2ee"
    channels[0].encryption_state = "active"
    session, guild = reorder_context(monkeypatch, channels)
    session.scalars.side_effect = [channels, [channels[0], channels[2]]]
    monkeypatch.setattr(management, "require_permissions", AsyncMock())

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate(
            {"channels": [{"id": "10", "position": 2, "parent_id": "21"}]}
        ),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    assert guild.permission_generation == 1
    assert channels[0].encryption_state == "rekeying"
    mutation = cast(AsyncMock, management.queue_guild_mutation)
    assert {
        call.kwargs["channel"].id: call.kwargs["snapshot_required"]
        for call in mutation.await_args_list
    } == {20: False, 21: False, 10: True}
    assert mutation.await_args.args[5]["channel"]["encryption_state"] == "rekeying"
    policy_update = cast(AsyncMock, management.publish_e2ee_policy_updates)
    assert policy_update.await_args.args[3] == [channels[0]]
    assert [
        call.args[3]["id"] for call in cast(AsyncMock, management.publish_dispatch).await_args_list
    ] == ["20", "21"]


@pytest.mark.asyncio
async def test_single_channel_parent_move_uses_the_same_acl_snapshot_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    child = channel(10, 0, parent_id=20)
    child.updated_at = updated_at
    child.encryption_mode = "e2ee"
    child.encryption_state = "active"
    destination = channel(21, 1, channel_type=4)
    session, guild = reorder_context(monkeypatch, [child, destination])
    monkeypatch.setattr(
        management,
        "guild_channel",
        AsyncMock(side_effect=[child, destination]),
    )
    permission_check = AsyncMock()
    monkeypatch.setattr(management, "require_permissions", permission_check)

    result = await management.update_channel(
        EntityRef("1@guild.example"),
        EntityRef("10@guild.example"),
        ChannelUpdate.model_validate({"parent_id": "21"}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example", e2ee_activation_enabled=True)),
        f'"{updated_at.isoformat()}"',
    )

    assert result["id"] == "10"
    assert child.parent_id == 21
    assert guild.permission_generation == 1
    assert child.encryption_state == "rekeying"
    mutation = cast(AsyncMock, management.queue_guild_mutation)
    assert mutation.await_args.kwargs["snapshot_required"] is True
    assert mutation.await_args.args[5]["channel"]["encryption_state"] == "rekeying"
    policy_update = cast(AsyncMock, management.publish_e2ee_policy_updates)
    assert policy_update.await_args.args[3] == [child]
    cast(AsyncMock, management.publish_dispatch).assert_not_awaited()
    assert [call.kwargs["channel"].id for call in permission_check.await_args_list] == [10, 21]
    assert permission_check.await_args_list[1].args[4] == (
        Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS
    )


@pytest.mark.asyncio
async def test_lock_permissions_requires_manage_roles_on_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = [channel(10, 0, parent_id=20), channel(20, 1, channel_type=4)]
    session, guild = reorder_context(monkeypatch, channels)
    session.scalars.side_effect = [channels, [channels[0]]]
    permission_check = AsyncMock()
    monkeypatch.setattr(management, "require_permissions", permission_check)

    result = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate({"channels": [{"id": "10", "lock_permissions": True}]}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result.status_code == 204
    assert channels[0].permissions_synced is True
    assert guild.permission_generation == 1
    assert permission_check.await_args_list[1].kwargs["channel"].id == 10
    assert permission_check.await_args_list[1].args[4] == Permission.MANAGE_ROLES
    session.execute.assert_awaited_once()
    mutation = cast(AsyncMock, management.queue_guild_mutation)
    assert mutation.await_args.kwargs["snapshot_required"] is True


def restricted_parent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    guild = SimpleNamespace(
        id=1,
        origin_domain="guild.example",
        permission_generation=0,
    )
    actor = SimpleNamespace(
        id=7,
        origin_domain="bot.example",
        account_type="bot",
    )
    child = channel(10, 0)
    parent = channel(20, 1, channel_type=4)
    monkeypatch.setattr(
        chat_permissions,
        "bot_guild_permission_grant",
        AsyncMock(
            return_value=BotGuildPermissionGrant(
                99,
                1,
                int(Permission.ADMINISTRATOR),
                ("10@guild.example",),
            )
        ),
    )
    return guild, actor, child, parent


@pytest.mark.asyncio
async def test_restricted_bot_cannot_create_channel_under_blocked_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor, _child, parent = restricted_parent_context(monkeypatch)
    snowflake = SimpleNamespace(mint=AsyncMock())
    session = SimpleNamespace()
    monkeypatch.setattr(guilds, "proxy_remote_guild_management", AsyncMock(return_value=None))
    monkeypatch.setattr(guilds, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(guilds, "require_permissions", AsyncMock())
    monkeypatch.setattr(guilds, "guild_channel", AsyncMock(return_value=parent))

    with pytest.raises(HTTPException) as denied:
        await guilds.create_channel(
            EntityRef("1@guild.example"),
            ChannelCreate.model_validate({"name": "blocked-child", "type": 0, "parent_id": "20"}),
            cast(Any, SimpleNamespace(user=actor)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, snowflake),
            cast(
                Any,
                SimpleNamespace(
                    domain="guild.example",
                    e2ee_activation_enabled=True,
                ),
            ),
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    snowflake.mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_restricted_bot_cannot_move_channel_to_blocked_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor, child, parent = restricted_parent_context(monkeypatch)
    updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    child.updated_at = updated_at
    child.e2ee_required = False
    child.available_tags = []
    child.default_reaction_emoji = None
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(
        management,
        "proxy_remote_guild_management",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(management, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        management,
        "guild_channel",
        AsyncMock(side_effect=[child, parent]),
    )
    monkeypatch.setattr(management, "require_permissions", AsyncMock())

    with pytest.raises(HTTPException) as denied:
        await management.update_channel(
            EntityRef("1@guild.example"),
            EntityRef("10@guild.example"),
            ChannelUpdate.model_validate({"parent_id": "20"}),
            cast(Any, SimpleNamespace(user=actor)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(
                Any,
                SimpleNamespace(
                    domain="guild.example",
                    e2ee_activation_enabled=True,
                ),
            ),
            f'"{updated_at.isoformat()}"',
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    assert child.parent_id is None
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_restricted_bot_cannot_sync_permissions_from_blocked_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor, child, parent = restricted_parent_context(monkeypatch)
    child.parent_id = parent.id
    child.parent_domain = parent.origin_domain
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(guilds, "proxy_remote_guild_management", AsyncMock(return_value=None))
    monkeypatch.setattr(guilds, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        guilds,
        "guild_channel",
        AsyncMock(side_effect=[child, parent]),
    )
    monkeypatch.setattr(guilds, "require_permissions", AsyncMock())

    with pytest.raises(HTTPException) as denied:
        await guilds.sync_channel_permissions(
            EntityRef("1@guild.example"),
            EntityRef("10@guild.example"),
            cast(Any, SimpleNamespace(user=actor)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    assert child.permissions_synced is False
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_partial_reorder_preserves_omitted_parent_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(management, "proxy_remote_guild_management", proxy)

    response = await management.reorder_channels(
        EntityRef("1@guild.example"),
        ChannelPositionBatch.model_validate({"channels": [{"id": "10", "position": 3}]}),
        cast(Any, SimpleNamespace(user=SimpleNamespace(account_type="human"))),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
    )

    assert response.status_code == 204
    assert proxy.await_args.args[5]["data"] == {"channels": [{"id": "10", "position": 3}]}
