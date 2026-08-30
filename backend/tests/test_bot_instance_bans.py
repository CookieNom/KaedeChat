from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response

from app.api.bots import (
    bot_ban_instance,
    bot_list_instance_bans,
    bot_unban_instance,
    router,
)
from app.chat.schemas import InstanceBanCreate
from app.core.types import EntityRef


def test_bot_instance_ban_routes_cover_read_create_and_delete() -> None:
    operations = {
        (route.path, method) for route in router.routes for method in (route.methods or set())
    }

    assert ("/api/v1/bots/guilds/{guild_ref}/instance-bans", "GET") in operations
    assert (
        "/api/v1/bots/guilds/{guild_ref}/instance-bans/{instance_domain}",
        "PUT",
    ) in operations
    assert (
        "/api/v1/bots/guilds/{guild_ref}/instance-bans/{instance_domain}",
        "DELETE",
    ) in operations


@pytest.mark.asyncio
async def test_bot_instance_bans_use_dedicated_ban_scope_and_human_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()))
    list_service = AsyncMock(return_value=[{"instance_domain": "remote.example"}])
    ban_service = AsyncMock(return_value=Response(status_code=204))
    unban_service = AsyncMock(return_value=Response(status_code=204))
    monkeypatch.setattr("app.api.bots.installation_for_guild", installer)
    monkeypatch.setattr("app.api.bots.list_instance_bans", list_service)
    monkeypatch.setattr("app.api.bots.ban_instance", ban_service)
    monkeypatch.setattr("app.api.bots.remove_instance_ban", unban_service)
    guild_ref = EntityRef("10@chat.example")
    principal = SimpleNamespace(user=SimpleNamespace())
    session = SimpleNamespace()
    redis = SimpleNamespace()
    snowflake = SimpleNamespace()
    settings = SimpleNamespace(domain="chat.example")

    listed = await bot_list_instance_bans(
        guild_ref,
        principal,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        limit=25,
        after="older.example",
    )
    banned = await bot_ban_instance(
        guild_ref,
        "remote.example",
        InstanceBanCreate(reason="raid"),
        principal,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        snowflake,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        reason="raid",
    )
    unbanned = await bot_unban_instance(
        guild_ref,
        "remote.example",
        principal,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        snowflake,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        reason="appeal",
    )

    assert listed == [{"instance_domain": "remote.example"}]
    assert banned.status_code == 204
    assert unbanned.status_code == 204
    assert installer.await_count == 3
    assert all(call.args[4] == "moderation.bans" for call in installer.await_args_list)
    list_service.assert_awaited_once()
    ban_service.assert_awaited_once()
    unban_service.assert_awaited_once()
