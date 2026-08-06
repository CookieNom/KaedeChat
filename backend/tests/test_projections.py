from typing import Any, cast

import pytest

from app.chat.events import publish_dispatch
from app.core.task_wake import enqueue_best_effort


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
