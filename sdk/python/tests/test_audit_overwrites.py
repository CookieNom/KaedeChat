from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.models import AuditLogEntry, Channel, ChannelOverwrite
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def audit_entry(identifier: int) -> AuditLogEntry:
    return AuditLogEntry.from_payload(
        {
            "id": str(identifier),
            "guild_id": "10",
            "guild_domain": "chat.example",
            "actor_id": "2",
            "actor_domain": "apps.example",
            "action_type": 15,
            "target_type": "channel",
            "target_ref": {"id": "20", "origin_domain": "chat.example"},
            "reason": "tidy up",
            "changes": [{"key": "permissions", "old_value": "0", "new_value": "4"}],
            "created_at": "2026-08-26T00:00:00+00:00",
        }
    )


@pytest.mark.asyncio
async def test_fetch_audit_logs_serializes_cursor_filters_and_models_entries() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "id": "9",
                "guild_id": "10",
                "guild_domain": "chat.example",
                "actor_id": "2",
                "actor_domain": "apps.example",
                "action_type": 15,
                "target_type": "channel",
                "target_ref": {"id": "20"},
                "reason": None,
                "changes": [],
                "created_at": "2026-08-26T00:00:00+00:00",
            }
        ]
    )

    entries = await bot.fetch_audit_logs(
        EntityRef(10, "chat.example"),
        target="https://chat.example",
        before=99,
        user=EntityRef(2, "apps.example"),
        action_type=15,
        target_type="channel",
        limit=25,
    )

    assert [entry.id for entry in entries] == [9]
    assert bot.request.await_args.kwargs["params"] == {
        "limit": 25,
        "before": "99",
        "user_id": "2@apps.example",
        "action_type": 15,
        "target_type": "channel",
    }

    await bot.fetch_audit_logs(
        EntityRef(10, "chat.example"),
        after=7,
        limit=10,
    )
    assert bot.request.await_args.kwargs["params"] == {"limit": 10, "after": "7"}

    with pytest.raises(ValueError, match="either before or after"):
        await bot.fetch_audit_logs(EntityRef(10, "chat.example"), before=9, after=7)


@pytest.mark.asyncio
async def test_audit_log_cursor_iterator_stops_at_requested_limit() -> None:
    bot = client()
    bot.fetch_audit_logs = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[audit_entry(9), audit_entry(8)], [audit_entry(7)]]
    )

    entries = [
        entry
        async for entry in bot.audit_logs(
            EntityRef(10, "chat.example"), page_size=2, limit=3
        )
    ]

    assert [entry.id for entry in entries] == [9, 8, 7]
    assert bot.fetch_audit_logs.await_args_list[1].kwargs["before"] == 8
    assert bot.fetch_audit_logs.await_args_list[1].kwargs["limit"] == 1


@pytest.mark.asyncio
async def test_audit_log_cursor_iterator_pages_forward_with_after() -> None:
    bot = client()
    bot.fetch_audit_logs = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[audit_entry(7), audit_entry(8)], []]
    )

    entries = [
        entry
        async for entry in bot.audit_logs(
            EntityRef(10, "chat.example"), after=6, page_size=2
        )
    ]

    assert [entry.id for entry in entries] == [7, 8]
    assert bot.fetch_audit_logs.await_args_list[0].kwargs["after"] == 6
    assert bot.fetch_audit_logs.await_args_list[1].kwargs["after"] == 8
    assert bot.fetch_audit_logs.await_args_list[1].kwargs["before"] is None


@pytest.mark.asyncio
async def test_channel_overwrite_methods_use_bot_routes_and_wire_masks() -> None:
    bot = client()
    bot.request = AsyncMock(return_value={"status": "updated"})  # type: ignore[method-assign]
    guild = EntityRef(10, "chat.example")
    channel = EntityRef(20, "chat.example")
    role = EntityRef(30, "chat.example")

    overwrite = await bot.set_channel_overwrite(
        guild,
        channel,
        role,
        "role",
        target="https://chat.example",
        allow=4,
        deny=8,
        reason="private staff room",
    )

    assert overwrite == ChannelOverwrite(role, "role", 4, 8)
    assert bot.request.await_args.args[:2] == (
        "PUT",
        "/api/v1/bots/guilds/10@chat.example/channels/20@chat.example/overwrites",
    )
    assert bot.request.await_args.kwargs["json"] == {
        "target_id": "30@chat.example",
        "target_type": "role",
        "allow": "4",
        "deny": "8",
    }

    managed = Channel(
        client=bot,
        target="https://chat.example",
        ref=channel,
        guild_ref=guild,
        type=0,
    )
    bot.fetch_channel_overwrites = AsyncMock(return_value=[overwrite])  # type: ignore[method-assign]
    assert await managed.overwrites() == [overwrite]
