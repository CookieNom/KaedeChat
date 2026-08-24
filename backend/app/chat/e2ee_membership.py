from collections.abc import Iterable

from redis.asyncio import Redis
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.payloads import channel_payload
from app.core.settings import Settings
from app.db.models import Channel, DMConversation, DMParticipant, Guild, GuildMember, User

GUILD_E2EE_ACCESS_MUTATION_EVENTS = frozenset(
    {
        "guild.member.add",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
    }
)


async def pause_guild_e2ee_for_membership_change(
    session: AsyncSession,
    guild: Guild,
) -> list[Channel]:
    """Fail closed until an MLS-capable manager rotates affected room keys."""

    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.encryption_mode == "e2ee",
                Channel.encryption_state == "active",
            )
            .with_for_update()
        )
    )
    for channel in channels:
        channel.encryption_state = "rekeying"
    return channels


async def remote_e2ee_authorities_for_user(
    session: AsyncSession,
    settings: Settings,
    user: User,
) -> set[str]:
    guild_authorities = set(
        await session.scalars(
            select(GuildMember.guild_domain)
            .where(
                GuildMember.user_id == user.id,
                GuildMember.user_domain == user.origin_domain,
                GuildMember.guild_domain != settings.domain,
            )
            .distinct()
        )
    )
    dm_authorities = set(
        await session.scalars(
            select(DMConversation.authority_domain)
            .join(
                DMParticipant,
                (DMParticipant.conversation_id == DMConversation.id)
                & (DMParticipant.conversation_domain == DMConversation.origin_domain),
            )
            .where(
                DMParticipant.user_id == user.id,
                DMParticipant.user_domain == user.origin_domain,
                DMConversation.authority_domain != settings.domain,
            )
            .distinct()
        )
    )
    return guild_authorities | dm_authorities


async def e2ee_policy_destinations(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
) -> set[str]:
    if channel.guild_id is not None:
        return set(
            await session.scalars(
                select(GuildMember.user_domain)
                .where(
                    GuildMember.guild_id == channel.guild_id,
                    GuildMember.guild_domain == channel.guild_domain,
                    GuildMember.user_domain != settings.domain,
                )
                .distinct()
            )
        )
    return set(
        await session.scalars(
            select(DMParticipant.user_domain)
            .where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
                DMParticipant.user_domain != settings.domain,
            )
            .distinct()
        )
    )


async def publish_e2ee_policy_updates(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    channels: Iterable[Channel],
) -> None:
    for channel in channels:
        rendered = channel_payload(channel)
        event_type = "THREAD_UPDATE" if channel.type in {10, 11, 12} else "CHANNEL_UPDATE"
        if channel.guild_id is not None and channel.guild_domain is not None:
            await publish_dispatch(
                redis,
                guild_topic(channel.guild_domain, channel.guild_id),
                event_type,
                rendered,
            )
            continue
        local_participants = set(
            await session.scalars(
                select(DMParticipant.user_id).where(
                    DMParticipant.conversation_id == channel.id,
                    DMParticipant.conversation_domain == channel.origin_domain,
                    DMParticipant.user_domain == settings.domain,
                )
            )
        )
        for user_id in local_participants:
            await publish_dispatch(
                redis,
                user_topic(settings.domain, user_id),
                event_type,
                rendered,
            )


async def pause_local_e2ee_for_device_change(
    session: AsyncSession,
    settings: Settings,
    user: User,
) -> list[Channel]:
    guild_refs = select(GuildMember.guild_id, GuildMember.guild_domain).where(
        GuildMember.user_id == user.id,
        GuildMember.user_domain == user.origin_domain,
        GuildMember.guild_domain == settings.domain,
    )
    dm_refs = (
        select(DMParticipant.conversation_id, DMParticipant.conversation_domain)
        .join(
            DMConversation,
            (DMConversation.id == DMParticipant.conversation_id)
            & (DMConversation.origin_domain == DMParticipant.conversation_domain),
        )
        .where(
            DMParticipant.user_id == user.id,
            DMParticipant.user_domain == user.origin_domain,
            DMConversation.authority_domain == settings.domain,
        )
    )
    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.encryption_mode == "e2ee",
                Channel.encryption_state == "active",
                (
                    tuple_(Channel.guild_id, Channel.guild_domain).in_(guild_refs)
                    | tuple_(Channel.id, Channel.origin_domain).in_(dm_refs)
                ),
            )
            .with_for_update()
        )
    )
    for channel in channels:
        channel.encryption_state = "rekeying"
    return channels
