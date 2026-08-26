from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic, publish_dispatch
from app.core.task_wake import enqueue_best_effort
from app.db.models import TrackerDispatchOutbox

log = structlog.get_logger()

TRACKER_EVENT_TYPES = frozenset(
    {
        "TRACKER_BOARD_UPDATE",
        "TRACKER_LANE_CREATE",
        "TRACKER_LANE_UPDATE",
        "TRACKER_LANE_DELETE",
        "TRACKER_TASK_CREATE",
        "TRACKER_TASK_UPDATE",
        "TRACKER_TASK_DELETE",
    }
)


def queue_tracker_dispatch(
    session: AsyncSession,
    *,
    channel_id: int,
    channel_domain: str,
    guild_id: int,
    guild_domain: str,
    event_type: str,
    payload: dict[str, object],
) -> TrackerDispatchOutbox:
    """Add a gateway projection to the caller's current SQL transaction."""

    if event_type not in TRACKER_EVENT_TYPES:
        raise ValueError("unsupported tracker dispatch event")
    row = TrackerDispatchOutbox(
        channel_id=channel_id,
        channel_domain=channel_domain,
        guild_id=guild_id,
        guild_domain=guild_domain,
        event_type=event_type,
        payload=payload,
    )
    session.add(row)
    return row


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(300, 2 ** min(attempts, 8)))


async def drain_tracker_dispatch_outbox(
    session: AsyncSession,
    redis: Redis,
    *,
    limit: int = 100,
) -> int:
    """Project due committed rows, retaining failures for a later retry.

    Rows are locked while publishing. A worker crash after publishing but before
    deletion deliberately causes a replay: tracker delivery is at least once.
    """

    if not 1 <= limit <= 500:
        raise ValueError("tracker outbox batch limit must be between 1 and 500")
    now = datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(TrackerDispatchOutbox)
            .where(TrackerDispatchOutbox.next_attempt_at <= now)
            .order_by(TrackerDispatchOutbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    delivered = 0
    for row in rows:
        event = await publish_dispatch(
            redis,
            guild_topic(row.guild_domain, row.guild_id),
            row.event_type,
            row.payload,
        )
        if event is not None:
            await session.delete(row)
            delivered += 1
            continue
        row.attempts += 1
        row.next_attempt_at = now + _retry_delay(row.attempts)
        log.warning(
            "tracker_dispatch_deferred",
            outbox_id=row.id,
            event_type=row.event_type,
            attempts=row.attempts,
        )
    await session.commit()
    return delivered


async def wake_tracker_dispatch_outbox() -> bool:
    """Wake the durable drain without coupling tracker services to Taskiq."""

    try:
        from app.tasks import tracker_dispatch_outbox_drain
    except Exception:
        # The committed SQL row is the source of truth. Import/configuration
        # trouble in the optional fast-path must not turn that commit into a
        # false mutation failure; the periodic sweep remains the fallback.
        log.exception("tracker_dispatch_wake_unavailable")
        return False

    return await enqueue_best_effort(tracker_dispatch_outbox_drain)
