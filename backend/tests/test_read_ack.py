"""Read acknowledgements retain mentions beyond a partial-history cursor."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api import channels
from app.chat.schemas import ReadStateUpdate
from app.core.types import EntityReference


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_cursor,mentions,unread", [(20, 1, True), (30, 0, False)])
async def test_ack_broadcasts_durable_state_after_partial_or_stale_ack(
    monkeypatch, durable_cursor, mentions, unread
):
    channel = SimpleNamespace(
        id=10, origin_domain="home.test", last_message_id=30, last_message_domain="home.test"
    )
    access = SimpleNamespace(channel=channel)
    for name in ("load_channel_access", "lock_local_channel_mutation"):
        monkeypatch.setattr(channels, name, AsyncMock(return_value=access))
    monkeypatch.setattr(channels, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(
        channels,
        "channel_message",
        AsyncMock(
            return_value=SimpleNamespace(
                id=20, origin_domain="home.test", created_at=datetime(2026, 9, 14, tzinfo=UTC)
            )
        ),
    )
    publish = AsyncMock()
    monkeypatch.setattr(channels, "publish_dispatch", publish)
    state = SimpleNamespace(
        read_version=0,
        last_message_id=durable_cursor,
        last_message_domain="home.test",
        mention_count=mentions,
    )
    session = SimpleNamespace(
        execute=AsyncMock(),
        scalar=AsyncMock(return_value=SimpleNamespace(read_version=0)),
        scalars=AsyncMock(return_value=Mock(one=Mock(return_value=state))),
        commit=AsyncMock(),
    )
    response = await channels.acknowledge_channel(
        EntityReference(10, "home.test"),
        ReadStateUpdate(message_id="20@home.test"),
        SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")),
        session,
        Mock(),
        SimpleNamespace(domain="home.test"),
    )
    assert response.status_code == 204
    session.commit.assert_awaited_once()
    assert publish.call_args.args[-1]["mention_count"] == mentions
    assert publish.call_args.args[-1]["last_message_id"] == str(durable_cursor)
    assert publish.call_args.args[-1]["unread"] is unread


@pytest.mark.asyncio
@pytest.mark.parametrize("first_message", [False, True])
async def test_manual_unread_rewinds_and_versions_cursor(monkeypatch, first_message):
    channel = SimpleNamespace(
        id=10, origin_domain="home.test", last_message_id=30, last_message_domain="home.test"
    )
    access = SimpleNamespace(channel=channel)
    for name in ("load_channel_access", "lock_local_channel_mutation"):
        monkeypatch.setattr(channels, name, AsyncMock(return_value=access))
    monkeypatch.setattr(channels, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(
        channels,
        "channel_message",
        AsyncMock(
            return_value=SimpleNamespace(
                id=20, origin_domain="home.test", created_at=datetime(2026, 9, 14, tzinfo=UTC)
            )
        ),
    )
    publish = AsyncMock()
    monkeypatch.setattr(channels, "publish_dispatch", publish)
    predecessor = None if first_message else SimpleNamespace(id=19, origin_domain="home.test")
    durable = SimpleNamespace(
        read_version=1,
        last_message_id=None if first_message else 19,
        last_message_domain=None if first_message else "home.test",
        mention_count=1,
    )
    session = SimpleNamespace(
        execute=AsyncMock(),
        scalar=AsyncMock(side_effect=[SimpleNamespace(read_version=0), predecessor]),
        scalars=AsyncMock(return_value=Mock(one=Mock(return_value=durable))),
        commit=AsyncMock(),
    )
    response = await channels.acknowledge_channel(
        EntityReference(10, "home.test"),
        ReadStateUpdate(message_id="20@home.test", mark_unread=True),
        SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")),
        session,
        Mock(),
        SimpleNamespace(domain="home.test"),
    )
    assert response.status_code == 204
    event = publish.call_args.args[-1]
    assert event["first_unread_at"] == "2026-09-14T00:00:00+00:00"
    assert event["manual_unread"] is True
    assert event["read_version"] == 1
    assert event["last_message_id"] == (None if first_message else "19")
    assert event["unread_message_id"] == "20"
    assert event["unread"] is True
    statement = session.scalars.call_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [None, SimpleNamespace(read_version=1)])
async def test_stale_or_missing_ack_cannot_undo_manual_unread(monkeypatch, locked):
    from fastapi import HTTPException

    access = SimpleNamespace(channel=SimpleNamespace(id=10, origin_domain="home.test"))
    for name in ("load_channel_access", "lock_local_channel_mutation"):
        monkeypatch.setattr(channels, name, AsyncMock(return_value=access))
    monkeypatch.setattr(channels, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(channels, "channel_message", AsyncMock())
    session = SimpleNamespace(
        execute=AsyncMock(),
        scalar=AsyncMock(return_value=locked),
        commit=AsyncMock(),
        scalars=AsyncMock(),
    )
    with pytest.raises(HTTPException) as error:
        await channels.acknowledge_channel(
            EntityReference(10, "home.test"),
            ReadStateUpdate(message_id="30@home.test", read_version=0),
            SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")),
            session,
            Mock(),
            SimpleNamespace(domain="home.test"),
        )
    assert error.value.status_code == 409
    session.commit.assert_not_awaited()
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_ack_counts_old_mentions_after_projection_retention(postgres_schema, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    for ddl in (
        """CREATE TABLE messages (id bigint, origin_domain text, channel_id bigint,
        channel_domain text, author_id bigint, author_domain text, deleted_at timestamptz,
        mention_user_refs jsonb)""",
        """CREATE TABLE message_projections (message_id bigint, message_domain text,
        processed_at timestamptz)""",
        """CREATE TABLE read_states (user_id bigint, user_domain text, user_is_local boolean,
        channel_id bigint, channel_domain text, last_message_id bigint, last_message_domain text,
        mention_count integer DEFAULT 0, read_version integer DEFAULT 0,
        updated_at timestamptz DEFAULT now(),
        PRIMARY KEY(user_id,user_domain,channel_id,channel_domain))""",
    ):
        await postgres_schema.execute(text(ddl))
    await postgres_schema.execute(
        text("""
        INSERT INTO messages
        SELECT id, 'home.test', 10, 'home.test', 2, 'home.test',
            CASE WHEN id=60 THEN now() ELSE NULL END,
            '[{"id":"1","origin_domain":"home.test"}]'::jsonb
        FROM unnest(ARRAY[20,30,40,50,60]) AS id
    """)
    )
    await postgres_schema.execute(
        text("""
        INSERT INTO message_projections VALUES (40,'home.test',NULL),(50,'home.test',now())
    """)
    )
    channel = SimpleNamespace(
        id=10, origin_domain="home.test", last_message_id=60, last_message_domain="home.test"
    )
    access = SimpleNamespace(channel=channel)
    for name in ("load_channel_access", "lock_local_channel_mutation"):
        monkeypatch.setattr(channels, name, AsyncMock(return_value=access))
    monkeypatch.setattr(channels, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(
        channels,
        "channel_message",
        AsyncMock(return_value=SimpleNamespace(id=20, origin_domain="home.test")),
    )
    publish = AsyncMock()
    monkeypatch.setattr(channels, "publish_dispatch", publish)
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        for prune in (False, True):
            if prune:
                await session.execute(
                    text("DELETE FROM message_projections WHERE processed_at IS NOT NULL")
                )
            await channels.acknowledge_channel(
                EntityReference(10, "home.test"),
                ReadStateUpdate(message_id="20@home.test"),
                SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.test")),
                session,
                Mock(),
                SimpleNamespace(domain="home.test"),
            )
            assert publish.call_args.args[-1]["mention_count"] == 2
