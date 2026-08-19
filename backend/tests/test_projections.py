import json
from typing import Any, cast

import pytest

from app.chat.events import publish_dispatch
from app.core.task_wake import enqueue_best_effort


@pytest.mark.asyncio
async def test_targeted_dispatch_persists_its_audience_in_the_durable_event() -> None:
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
