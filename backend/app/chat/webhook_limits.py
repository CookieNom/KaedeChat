from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Channel,
    ChannelFollow,
    FederatedChannelFollow,
    Guild,
    Webhook,
)

MAX_WEBHOOKS_PER_CHANNEL = 15
MAX_WEBHOOKS_PER_GUILD = 1_000


async def lock_webhook_capacity_guild(
    session: AsyncSession,
    guild: Guild,
) -> Guild:
    """Serialize every webhook/follower admission for one guild authority."""

    locked = await session.scalar(
        select(Guild)
        .where(
            Guild.id == guild.id,
            Guild.origin_domain == guild.origin_domain,
        )
        .with_for_update()
    )
    if locked is None or locked.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return locked


async def webhook_capacity_counts(
    session: AsyncSession,
    guild: Guild,
    channel: Channel,
) -> tuple[int, int]:
    """Count active incoming and follower webhooks at their target authority."""

    webhook_guild = int(
        await session.scalar(
            select(func.count())
            .select_from(Webhook)
            .where(
                Webhook.guild_id == guild.id,
                Webhook.guild_domain == guild.origin_domain,
                Webhook.revoked_at.is_(None),
            )
        )
        or 0
    )
    webhook_channel = int(
        await session.scalar(
            select(func.count())
            .select_from(Webhook)
            .where(
                Webhook.channel_id == channel.id,
                Webhook.channel_domain == channel.origin_domain,
                Webhook.revoked_at.is_(None),
            )
        )
        or 0
    )
    local_follow_guild = int(
        await session.scalar(
            select(func.count())
            .select_from(ChannelFollow)
            .join(
                Channel,
                and_(
                    Channel.id == ChannelFollow.target_channel_id,
                    Channel.origin_domain == ChannelFollow.target_channel_domain,
                ),
            )
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                ChannelFollow.active.is_(True),
            )
        )
        or 0
    )
    local_follow_channel = int(
        await session.scalar(
            select(func.count())
            .select_from(ChannelFollow)
            .where(
                ChannelFollow.target_channel_id == channel.id,
                ChannelFollow.target_channel_domain == channel.origin_domain,
                ChannelFollow.active.is_(True),
            )
        )
        or 0
    )
    federated_follow_guild = int(
        await session.scalar(
            select(func.count())
            .select_from(FederatedChannelFollow)
            .join(
                Channel,
                and_(
                    Channel.id == FederatedChannelFollow.target_channel_id,
                    Channel.origin_domain == FederatedChannelFollow.target_channel_domain,
                ),
            )
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                FederatedChannelFollow.local_role == "target",
                FederatedChannelFollow.active.is_(True),
            )
        )
        or 0
    )
    federated_follow_channel = int(
        await session.scalar(
            select(func.count())
            .select_from(FederatedChannelFollow)
            .where(
                FederatedChannelFollow.target_channel_id == channel.id,
                FederatedChannelFollow.target_channel_domain == channel.origin_domain,
                FederatedChannelFollow.local_role == "target",
                FederatedChannelFollow.active.is_(True),
            )
        )
        or 0
    )
    return (
        webhook_guild + local_follow_guild + federated_follow_guild,
        webhook_channel + local_follow_channel + federated_follow_channel,
    )


async def require_webhook_capacity(
    session: AsyncSession,
    guild: Guild,
    channel: Channel,
    *,
    adding_to_guild: bool,
    lock_guild: bool = True,
) -> None:
    """Enforce Discord's 15/channel and 1,000/guild webhook limits atomically."""

    if lock_guild:
        await lock_webhook_capacity_guild(session, guild)
    guild_count, channel_count = await webhook_capacity_counts(session, guild, channel)
    if channel_count >= MAX_WEBHOOKS_PER_CHANNEL:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WEBHOOK_CHANNEL_LIMIT_REACHED",
                "message": (f"A channel can have at most {MAX_WEBHOOKS_PER_CHANNEL} webhooks."),
                "limit": MAX_WEBHOOKS_PER_CHANNEL,
            },
        )
    if adding_to_guild and guild_count >= MAX_WEBHOOKS_PER_GUILD:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WEBHOOK_GUILD_LIMIT_REACHED",
                "message": f"A guild can have at most {MAX_WEBHOOKS_PER_GUILD} webhooks.",
                "limit": MAX_WEBHOOKS_PER_GUILD,
            },
        )
