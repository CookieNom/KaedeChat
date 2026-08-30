from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.core.settings import Settings
from app.core.types import EntityReferenceLike
from app.db.models import Channel, DMParticipant, Guild, GuildMember, User


@dataclass(frozen=True)
class ChannelAccess:
    channel: Channel
    guild: Guild | None
    participants: list[User]


async def effective_channel_nsfw(
    session: AsyncSession,
    channel: Channel,
) -> bool | None:
    """Return a channel's effective NSFW state, resolving thread inheritance.

    ``None`` means the thread parent cannot be authoritatively resolved. Callers
    that guard content disclosure must reject that state; discovery callers can
    safely treat it as unavailable.
    """

    if channel.guild_id is None:
        return False
    if channel.type not in {10, 11, 12}:
        return bool(getattr(channel, "nsfw", False))
    if channel.parent_id is None or channel.parent_domain is None:
        return None
    parent = await session.get(Channel, (channel.parent_id, channel.parent_domain))
    if (
        parent is None
        or parent.unavailable
        or (parent.guild_id, parent.guild_domain) != (channel.guild_id, channel.guild_domain)
    ):
        return None
    return bool(getattr(parent, "nsfw", False))


async def load_channel_access(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel_ref: EntityReferenceLike,
) -> ChannelAccess:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    guild_access = exists().where(
        GuildMember.guild_id == Channel.guild_id,
        GuildMember.guild_domain == Channel.guild_domain,
        GuildMember.user_id == actor.id,
        GuildMember.user_domain == actor.origin_domain,
    )
    dm_access = exists().where(
        DMParticipant.conversation_id == Channel.id,
        DMParticipant.conversation_domain == Channel.origin_domain,
        DMParticipant.user_id == actor.id,
        DMParticipant.user_domain == actor.origin_domain,
    )
    channel = await session.scalar(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.origin_domain == channel_domain,
            Channel.unavailable.is_(False),
            or_(guild_access, dm_access),
        )
    )
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.guild_id is not None:
        guild = await session.scalar(
            select(Guild).where(
                Guild.id == channel.guild_id,
                Guild.origin_domain == channel.guild_domain,
            )
        )
        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        return ChannelAccess(channel=channel, guild=guild, participants=[])
    if channel.type != 1:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    is_participant = await session.scalar(
        select(DMParticipant.user_id).where(
            DMParticipant.conversation_id == channel.id,
            DMParticipant.conversation_domain == channel.origin_domain,
            DMParticipant.user_id == actor.id,
            DMParticipant.user_domain == actor.origin_domain,
        )
    )
    if is_participant is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    participants = list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
            )
            .order_by(User.origin_domain, User.username)
        )
    )
    return ChannelAccess(channel=channel, guild=None, participants=participants)


async def lock_local_channel_mutation(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
) -> ChannelAccess:
    """Fence a local guild mutation against membership and permission changes.

    Every local guild structural/moderation mutation takes the Guild row lock
    first. Channel writes join that same ordering before authorization, then
    refresh the channel after the lock is acquired. A kick, ban, role change,
    overwrite change, or channel deletion therefore commits wholly before or
    after this request instead of racing between its permission check and write.
    """

    guild = access.guild
    if guild is None or guild.origin_domain != settings.domain:
        return access
    locked_guild = await session.scalar(
        select(Guild)
        .where(
            Guild.id == guild.id,
            Guild.origin_domain == guild.origin_domain,
            Guild.unavailable.is_(False),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_guild is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    refreshed_channel = await session.scalar(
        select(Channel)
        .where(
            Channel.id == access.channel.id,
            Channel.origin_domain == access.channel.origin_domain,
            Channel.guild_id == locked_guild.id,
            Channel.guild_domain == locked_guild.origin_domain,
            Channel.unavailable.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if refreshed_channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    return ChannelAccess(channel=refreshed_channel, guild=locked_guild, participants=[])


async def publish_channel_dispatch(
    redis: Redis,
    access: ChannelAccess,
    event_type: str,
    data: dict[str, object],
) -> None:
    if access.guild is not None:
        await publish_dispatch(
            redis,
            guild_topic(access.guild.origin_domain, access.guild.id),
            event_type,
            data,
        )
        return
    for participant in access.participants:
        await publish_dispatch(
            redis,
            user_topic(participant.origin_domain, participant.id),
            event_type,
            data,
        )
