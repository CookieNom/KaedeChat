"""Inbox permission boundaries and bounded mention queries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from app.api import users
from app.core.types import EntityReference


@pytest.mark.asyncio
@pytest.mark.parametrize("bookmarks", [False, True])
async def test_inbox_never_queries_history_for_inaccessible_channels(monkeypatch, bookmarks):
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
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test"))
    result = (
        await users.inbox_bookmarks(None, 50, auth, session, Mock())
        if bookmarks
        else await users.inbox_mentions(
            None,
            None,
            True,
            True,
            50,
            auth,
            session,
            Mock(),
            SimpleNamespace(domain="home.test"),
        )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("unread", [False, True])
async def test_read_snapshot_returns_first_unread_time_from_message(unread):
    first_time = datetime(2026, 9, 14, 15, 32, tzinfo=UTC)
    channel = SimpleNamespace(
        id=10,
        origin_domain="remote.test",
        guild_id=None,
        guild_domain=None,
        name="DM",
        last_message_id=30,
        last_message_domain="remote.test",
    )
    state = SimpleNamespace(
        channel_id=10,
        channel_domain="remote.test",
        last_message_id=20 if unread else 30,
        last_message_domain="remote.test",
        read_version=2,
        mention_count=0,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[channel], [], [state]]),
        execute=AsyncMock(
            side_effect=[
                Mock(tuples=Mock(return_value=[(10, "remote.test", 10)] if unread else [])),
                [(10, "remote.test", 21, "remote.test", first_time)] if unread else [],
            ]
        ),
    )
    result = await users.list_read_states(
        SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")),
        session,
        SimpleNamespace(mget=AsyncMock(return_value=[None])),
    )
    assert result[0]["first_unread_at"] == (first_time.isoformat() if unread else None)
    assert result[0]["unread_count"] == (10 if unread else 0)
    assert result[0]["read_message_id"] == ("20" if unread else "30")
    assert result[0]["read_version"] == 2


@pytest.mark.asyncio
async def test_bookmarks_are_private_paginated_and_filter_deleted_messages(monkeypatch):
    state = {"channel_id": "10", "channel_domain": "remote.test", "can_read_history": True}
    monkeypatch.setattr(users, "list_read_states", AsyncMock(return_value=[state]))
    now = datetime.now(UTC)
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(saved_at=now)),
        scalars=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=20,
                    origin_domain="remote.test",
                    channel_id=10,
                    channel_domain="remote.test",
                    created_at=now,
                )
            ]
        ),
    )
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test"))
    result = await users.inbox_bookmarks(
        EntityReference(30, "remote.test"), 10, auth, session, Mock()
    )
    assert result[0]["message_id"] == "20"
    session.get.assert_awaited_once_with(users.MessageBookmark, (1, "home.test", 30, "remote.test"))
    compiled = session.scalars.call_args.args[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "message_bookmarks.user_id =" in sql
    assert "message_bookmarks.user_domain =" in sql
    assert "messages.deleted_at IS NULL" in sql
    assert "ORDER BY message_bookmarks.saved_at DESC" in sql
    assert "message_bookmarks.saved_at, messages.id, messages.origin_domain) <" in sql
    assert [(10, "remote.test")] in compiled.params.values()
    assert 10 in compiled.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("floor", [0, 30])
async def test_bookmark_save_requires_history_and_is_idempotent(monkeypatch, allowed, floor):
    from fastapi import HTTPException

    from app.chat import channel_access
    from app.core.permissions import Permission

    message = SimpleNamespace(
        id=20,
        origin_domain="remote.test",
        deleted_at=None,
        channel_id=10,
        channel_domain="remote.test",
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=message), execute=AsyncMock(), commit=AsyncMock()
    )
    monkeypatch.setattr(
        channel_access,
        "load_channel_access",
        AsyncMock(
            return_value=SimpleNamespace(
                guild=Mock(), channel=SimpleNamespace(created_floor_id=floor)
            )
        ),
    )
    monkeypatch.setattr(
        users,
        "get_permissions",
        AsyncMock(
            return_value=Permission.VIEW_CHANNEL
            | (Permission.READ_MESSAGE_HISTORY if allowed else Permission(0))
        ),
    )
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test"))
    if not allowed or floor > message.id:
        with pytest.raises(HTTPException) as error:
            await users.save_bookmark(
                EntityReference(20, "remote.test"), auth, session, Mock(), Mock()
            )
        assert error.value.status_code == 404
        session.execute.assert_not_awaited()
    else:
        response = await users.save_bookmark(
            EntityReference(20, "remote.test"), auth, session, Mock(), Mock()
        )
        assert response.status_code == 204
        compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
        assert "ON CONFLICT DO NOTHING" in str(compiled)
        assert compiled.params["user_id"] == 1
        assert compiled.params["user_domain"] == "home.test"
        assert compiled.params["message_domain"] == "remote.test"


@pytest.mark.asyncio
async def test_remove_bookmark_only_deletes_current_users_reference():
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test"))
    await users.remove_bookmark(EntityReference(20, "remote.test"), auth, session)
    compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert compiled.params == {
        "user_id_1": 1,
        "user_domain_1": "home.test",
        "message_id_1": 20,
        "message_domain_1": "remote.test",
    }
    session.commit.assert_awaited_once()
