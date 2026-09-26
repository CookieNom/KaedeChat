import json
from typing import Any, cast

import pytest

from app.chat.events import publish_dispatch
from app.core.task_wake import enqueue_best_effort


@pytest.mark.asyncio
async def test_durable_dispatch_audience_serialization() -> None:
    class RecordingRedis:
        encoded: str | None = None

        async def eval(self, _script: str, numkeys: int, *args: str) -> list[object]:
            assert numkeys == 3
            self.encoded = args[-1]
            event = json.loads(self.encoded)
            event["topic_seq"] = 1
            return [1, json.dumps(event)]

    redis = RecordingRedis()
    rendered = await publish_dispatch(
        cast(Any, redis),
        "guild:guild.test:7",
        "INTERACTION_CREATE",
        {"options": {"secret": "value"}},
        audience_user_refs=("10@apps.test",),
    )

    assert rendered is not None
    assert rendered["audience_user_refs"] == ["10@apps.test"]
    assert json.loads(cast(str, redis.encoded))["audience_user_refs"] == ["10@apps.test"]


@pytest.mark.asyncio
async def test_dispatch_projection_failure_is_best_effort() -> None:
    class UnavailableRedis:
        async def eval(self, *_args: object) -> object:
            raise ConnectionError("Dragonfly unavailable")

    assert (
        await publish_dispatch(
            cast(Any, UnavailableRedis()),
            "user:example.test:1",
            "MESSAGE_CREATE",
            {"id": "2"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_durable_task_wake_failure_is_best_effort() -> None:
    class UnavailableTask:
        task_name = "test.task"

        async def kiq(self, *_args: object, **_kwargs: object) -> None:
            raise ConnectionError("broker unavailable")

    assert not await enqueue_best_effort(UnavailableTask(), "destination.example")


@pytest.mark.asyncio
async def test_delayed_projection_does_not_restore_a_deleted_message(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app import tasks
    from app.api import channels
    from app.db.models import Channel, Message

    message = SimpleNamespace(
        id=30,
        origin_domain="home.test",
        channel_id=10,
        channel_domain="home.test",
        deleted_at=datetime.now(UTC),
    )
    channel = SimpleNamespace(id=10, origin_domain="home.test", guild_id=None)
    projection = SimpleNamespace(
        message_id=30,
        message_domain="home.test",
        channel_id=10,
        channel_domain="home.test",
        processed_at=None,
    )
    session = AsyncMock()
    session.get.side_effect = lambda model, _key, **_kwargs: (
        message if model is Message else channel if model is Channel else None
    )
    session.scalar.return_value = projection
    monkeypatch.setattr(channels, "lock_message_delete_access", AsyncMock())
    redis = AsyncMock()
    assert (
        await tasks.project_message_record(
            session, redis, SimpleNamespace(domain="home.test"), 30, "home.test"
        )
        == 0
    )
    assert projection.processed_at is not None
    session.execute.assert_not_awaited()
    redis.eval.assert_not_awaited()
    assert session.get.await_args.kwargs == {"with_for_update": True, "populate_existing": True}
