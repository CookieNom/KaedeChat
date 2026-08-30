from collections.abc import Iterable

from redis.asyncio import Redis
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import channel_encryption_policy_payload
from app.chat.e2ee_controls import room_policy_change_context
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.payloads import channel_payload
from app.core.settings import Settings
from app.db.bot_models import BotApplication, BotE2EEDevice, BotE2EEParticipation
from app.db.materialization import materialize_updated_at
from app.db.models import Channel, DMConversation, DMParticipant, Guild, GuildMember, User
from app.federation.events import (
    build_envelope,
    discard_superseded_latest_state_event,
    queue_event,
)

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


async def queue_e2ee_policy_federation(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel: Channel,
    *,
    destinations: Iterable[str] | None = None,
    authority_attested_actor: bool = False,
) -> set[str]:
    """Queue one compacted, independently ACKed room-policy event per destination."""

    destination_set = set(
        destinations
        if destinations is not None
        else await e2ee_policy_destinations(session, settings, channel)
    )
    destination_set.discard(settings.domain)
    for destination in sorted(destination_set):
        await discard_superseded_latest_state_event(
            session,
            destination=destination,
            event_type="e2ee.room-policy.changed",
            channel_ref=(channel.id, channel.origin_domain),
        )
        envelope = await build_envelope(
            session,
            settings,
            "e2ee.room-policy.changed",
            actor,
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "encryption_policy": channel_encryption_policy_payload(channel),
            },
            context=room_policy_change_context(channel, actor),
            authority_attested_actor=authority_attested_actor,
        )
        await queue_event(session, settings, destination, envelope)
    return destination_set


async def publish_e2ee_policy_updates(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    channels: Iterable[Channel],
) -> None:
    materialized_channels = list(channels)
    # Access changes move these channels out of ``active``. PostgreSQL owns
    # ``updated_at`` and SQLAlchemy expires it on UPDATE, so materialize all
    # versions at this shared fanout boundary before synchronous rendering.
    await materialize_updated_at(session, *materialized_channels)
    for channel in materialized_channels:
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
    bot_participation_refs = (
        select(
            BotE2EEParticipation.channel_id,
            BotE2EEParticipation.channel_domain,
        )
        .join(BotE2EEDevice, BotE2EEDevice.id == BotE2EEParticipation.device_id)
        .join(
            BotApplication,
            (BotApplication.id == BotE2EEDevice.application_id)
            & (BotApplication.origin_domain == BotE2EEDevice.application_domain),
        )
        .where(
            BotApplication.bot_user_id == user.id,
            BotApplication.bot_user_domain == user.origin_domain,
            BotE2EEParticipation.status.in_(("pending", "active")),
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
                    | tuple_(Channel.id, Channel.origin_domain).in_(bot_participation_refs)
                ),
            )
            .with_for_update()
        )
    )
    for channel in channels:
        channel.encryption_state = "rekeying"
    return channels
