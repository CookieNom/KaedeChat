from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.guild_revision import queue_guild_mutation
from app.core.settings import Settings
from app.db.models import Channel, Guild, User

TRACKER_FEDERATION_INVALIDATION_REASONS = frozenset(
    {
        "settings_updated",
        "lane_created",
        "lane_updated",
        "lane_completion_updated",
        "lane_order_updated",
        "lane_deleted",
        "task_created",
        "task_updated",
        "task_order_updated",
        "task_deleted",
        "assignee_membership_removed",
    }
)


class FederatedTrackerBoard(Protocol):
    channel_id: int
    channel_domain: str
    guild_id: int
    guild_domain: str
    key_prefix: str
    next_task_number: int
    updated_at: datetime


async def queue_tracker_federation_invalidation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    actor: User,
    board: FederatedTrackerBoard,
    *,
    reason: str,
) -> None:
    """Durably invalidate one replicated tracker board with its SQL mutation."""

    if reason not in TRACKER_FEDERATION_INVALIDATION_REASONS:
        raise ValueError("unknown tracker federation invalidation reason")
    if (
        board.channel_id,
        board.channel_domain,
        board.guild_id,
        board.guild_domain,
        channel.guild_id,
        channel.guild_domain,
    ) != (
        channel.id,
        channel.origin_domain,
        guild.id,
        guild.origin_domain,
        guild.id,
        guild.origin_domain,
    ):
        raise RuntimeError("tracker federation invalidation identity is inconsistent")
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.tracker.board.invalidate",
        {
            "tracker": {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "key_prefix": board.key_prefix,
                "next_task_number": str(board.next_task_number),
                "version": board.updated_at.isoformat(),
            },
            "reason": reason,
        },
        channel=channel,
    )
