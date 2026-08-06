from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger()


async def enqueue_best_effort(task: Any, *args: object, **kwargs: object) -> bool:
    """Wake a durable background workflow without invalidating committed state."""

    try:
        await task.kiq(*args, **kwargs)
    except Exception:
        log.exception(
            "task_enqueue_failed",
            task_name=str(getattr(task, "task_name", type(task).__name__)),
        )
        return False
    return True
