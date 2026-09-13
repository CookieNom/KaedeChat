"""Inbox permission boundaries and bounded mention queries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from app.api import users
from app.core.types import EntityReference


@pytest.mark.asyncio
async def test_inbox_never_queries_history_for_inaccessible_channels(monkeypatch):
    monkeypatch.setattr(
        users,
        "list_read_states",
        AsyncMock(
            return_value=[
                {
                    "channel_id": "10",
                    "channel_domain": "home.test",
                    "guild_id": "5",
                    "guild_domain": "home.test",
                    "can_read_history": False,
                }
            ]
        ),
    )
    session = SimpleNamespace(scalars=AsyncMock())
    result = await users.inbox_mentions(
        None,
        None,
        True,
        True,
        50,
        SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")),
        session,
        Mock(),
        SimpleNamespace(domain="home.test"),
    )
    assert result == []
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_mentions_are_permission_filtered_and_cursor_paginated(monkeypatch):
    monkeypatch.setattr(
        users,
        "list_read_states",
        AsyncMock(
            return_value=[
                {
                    "channel_id": "10",
                    "channel_domain": "home.test",
                    "guild_id": "5",
                    "guild_domain": "home.test",
                    "can_read_history": True,
                },
                {
                    "channel_id": "99",
                    "channel_domain": "other.test",
                    "guild_id": "6",
                    "guild_domain": "other.test",
                    "can_read_history": True,
                },
            ]
        ),
    )
    session = SimpleNamespace(
        scalars=AsyncMock(
            return_value=[
                SimpleNamespace(
                    channel_id=10,
                    channel_domain="home.test",
                    id=20,
                    origin_domain="home.test",
                    created_at=datetime.now(UTC),
                )
            ]
        )
    )
    result = await users.inbox_mentions(
        EntityReference(30, "home.test"),
        EntityReference(5, "home.test"),
        False,
        False,
        10,
        SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")),
        session,
        Mock(),
        SimpleNamespace(domain="home.test"),
    )
    assert result[0]["message_id"] == "20"
    statement = session.scalars.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "inbox_dismissals" in sql
    assert "messages.deleted_at IS NULL" in sql
    assert "messages.created_at >=" in sql
    assert "LIMIT" in sql
    assert any(value == [(10, "home.test")] for value in compiled.params.values())
    assert 10 in compiled.params.values()
    assert "home.test" in compiled.params.values()
