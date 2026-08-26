from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.guild_revision import wake_queued_guild_federation
from app.core.settings import Settings
from app.db.models import Channel, Guild, TrackerBoard, TrackerTask, User
from app.tracker.federation import queue_tracker_federation_invalidation
from app.tracker.outbox import queue_tracker_dispatch, wake_tracker_dispatch_outbox


@dataclass(frozen=True, slots=True)
class TrackerBoardRefresh:
    channel_id: int
    channel_domain: str
    guild_id: int
    guild_domain: str
    key_prefix: str
    next_task_number: int
    version: datetime

    @classmethod
    def from_board(cls, board: TrackerBoard) -> TrackerBoardRefresh:
        return cls(
            channel_id=board.channel_id,
            channel_domain=board.channel_domain,
            guild_id=board.guild_id,
            guild_domain=board.guild_domain,
            key_prefix=board.key_prefix,
            next_task_number=board.next_task_number,
            version=board.updated_at,
        )

    def payload(self, *, reason: str) -> dict[str, object]:
        return {
            "channel_id": str(self.channel_id),
            "channel_domain": self.channel_domain,
            "key_prefix": self.key_prefix,
            "next_task_number": str(self.next_task_number),
            "version": self.version.isoformat(),
            "full_refresh": True,
            "reason": reason,
        }


async def clear_tracker_assignees(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    user_refs: list[tuple[int, str]],
) -> list[TrackerBoardRefresh]:
    """Clear assignments before guild memberships are removed.

    Callers hold the guild mutation lock. Tracker mutations take that same lock
    before their board lock, so locking affected boards here preserves the
    global lock order and makes the cleanup atomic with the membership change.
    One board invalidation is enough even when many tasks lose an assignee.
    """

    exact_refs = list(dict.fromkeys(user_refs))
    if not exact_refs:
        return []
    affected_refs = list(
        (
            await session.execute(
                select(TrackerTask.channel_id, TrackerTask.channel_domain)
                .where(
                    TrackerTask.guild_id == guild.id,
                    TrackerTask.guild_domain == guild.origin_domain,
                    tuple_(TrackerTask.assignee_id, TrackerTask.assignee_domain).in_(exact_refs),
                )
                .distinct()
            )
        ).tuples()
    )
    if not affected_refs:
        return []
    affected_refs.sort(key=lambda ref: (str(ref[1]), int(ref[0])))
    board_rows = list(
        (
            await session.execute(
                select(TrackerBoard, Channel)
                .join(
                    Channel,
                    (Channel.id == TrackerBoard.channel_id)
                    & (Channel.origin_domain == TrackerBoard.channel_domain),
                )
                .where(
                    TrackerBoard.guild_id == guild.id,
                    TrackerBoard.guild_domain == guild.origin_domain,
                    tuple_(TrackerBoard.channel_id, TrackerBoard.channel_domain).in_(affected_refs),
                )
                .order_by(TrackerBoard.channel_domain, TrackerBoard.channel_id)
                .with_for_update(of=TrackerBoard)
            )
        ).tuples()
    )
    if len(board_rows) != len(affected_refs):
        raise RuntimeError("tracker assignment references a missing board")

    # Keep optimistic-concurrency versions monotonic even if a database value
    # is marginally ahead of the API host's wall clock.
    now = max(
        datetime.now(UTC),
        max(board.updated_at for board, _channel in board_rows) + timedelta(microseconds=1),
    )
    await session.execute(
        update(TrackerTask)
        .where(
            TrackerTask.guild_id == guild.id,
            TrackerTask.guild_domain == guild.origin_domain,
            tuple_(TrackerTask.channel_id, TrackerTask.channel_domain).in_(affected_refs),
            tuple_(TrackerTask.assignee_id, TrackerTask.assignee_domain).in_(exact_refs),
        )
        .values(assignee_id=None, assignee_domain=None, updated_at=now)
    )
    refreshes: list[TrackerBoardRefresh] = []
    for board, channel in board_rows:
        board.updated_at = now
        refresh = TrackerBoardRefresh.from_board(board)
        queue_tracker_dispatch(
            session,
            channel_id=board.channel_id,
            channel_domain=board.channel_domain,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            event_type="TRACKER_BOARD_UPDATE",
            payload=refresh.payload(reason="assignee_membership_removed"),
        )
        await queue_tracker_federation_invalidation(
            session,
            settings,
            guild,
            channel,
            actor,
            board,
            reason="assignee_membership_removed",
        )
        refreshes.append(refresh)
    return refreshes


async def wake_tracker_membership_cleanup(
    guild: Guild,
) -> None:
    await wake_tracker_dispatch_outbox()
    await wake_queued_guild_federation(guild)
