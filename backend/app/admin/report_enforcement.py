from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import and_, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channels import (
    queue_attachment_tombstones,
    refresh_thread_last_message_after_delete,
)
from app.chat.channel_access import ChannelAccess, publish_channel_dispatch
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import channel_payload
from app.core.settings import Settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import Attachment, Channel, DMParticipant, Guild, Message, User
from app.federation.terminal_rooms import lock_terminal_room
from app.media.service import attachments_for_messages
from app.media.tombstones import lock_media_tombstone_ref
from app.tasks import federation_deliver, media_local_purge


@dataclass(slots=True)
class MessagePurgeResult:
    messages: list[Message]
    access_by_channel: dict[tuple[int, str], ChannelAccess]
    local_attachments: list[Attachment]
    delivery_destinations: set[str]
    guilds: dict[tuple[int, str], Guild]
    skipped_messages: int

    @property
    def deleted_count(self) -> int:
        return len(self.messages)


async def purge_author_messages(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    target: User,
    *,
    deleted_at: datetime,
    created_after: datetime | None = None,
    message_ref: tuple[int, str] | None = None,
) -> MessagePurgeResult:
    """Tombstone a local user's messages where this instance has room authority.

    Remote guild and federated-DM replicas are intentionally excluded: their
    home instance must authorize and distribute a deletion.
    """

    base_conditions = [
        Message.author_id == target.id,
        Message.author_domain == target.origin_domain,
        Message.deleted_at.is_(None),
    ]
    if created_after is not None:
        base_conditions.append(Message.created_at >= created_after)
    if message_ref is not None:
        base_conditions.extend(
            [Message.id == message_ref[0], Message.origin_domain == message_ref[1]]
        )

    total_count = int(
        await session.scalar(select(func.count()).select_from(Message).where(*base_conditions)) or 0
    )
    remote_dm_participant = exists().where(
        DMParticipant.conversation_id == Channel.id,
        DMParticipant.conversation_domain == Channel.origin_domain,
        DMParticipant.user_domain != settings.domain,
    )
    authority = or_(
        and_(
            Channel.guild_id.is_not(None),
            Channel.guild_domain == settings.domain,
        ),
        and_(
            Channel.guild_id.is_(None),
            Channel.type == 1,
            ~remote_dm_participant,
        ),
    )
    candidate_rows = list(
        (
            await session.execute(
                select(Message, Channel)
                .join(
                    Channel,
                    (Channel.id == Message.channel_id)
                    & (Channel.origin_domain == Message.channel_domain),
                )
                .where(
                    *base_conditions,
                    Channel.origin_domain == settings.domain,
                    Channel.unavailable.is_(False),
                    authority,
                )
                .order_by(Message.channel_domain, Message.channel_id, Message.id)
            )
        ).tuples()
    )
    candidate_channel_refs = {
        (channel.id, channel.origin_domain) for _message, channel in candidate_rows
    }
    candidate_guild_refs = {
        (channel.guild_id, channel.guild_domain)
        for _message, channel in candidate_rows
        if channel.guild_id is not None and channel.guild_domain is not None
    }
    for guild_id, guild_domain in sorted(candidate_guild_refs, key=lambda ref: (ref[1], ref[0])):
        await lock_terminal_room(session, "guild", guild_id, guild_domain)
    if candidate_channel_refs:
        await session.execute(
            select(Channel)
            .where(tuple_(Channel.id, Channel.origin_domain).in_(candidate_channel_refs))
            .order_by(Channel.origin_domain, Channel.id)
            .with_for_update()
        )
    message_refs = {(message.id, message.origin_domain) for message, _channel in candidate_rows}
    attachments_by_message = await attachments_for_messages(session, message_refs)
    for attachment in sorted(
        (item for items in attachments_by_message.values() for item in items),
        key=lambda item: (item.origin_domain, item.id),
    ):
        await lock_media_tombstone_ref(session, attachment.id, attachment.origin_domain)

    rows = list(
        (
            await session.execute(
                select(Message, Channel)
                .join(
                    Channel,
                    (Channel.id == Message.channel_id)
                    & (Channel.origin_domain == Message.channel_domain),
                )
                .where(
                    *base_conditions,
                    Channel.origin_domain == settings.domain,
                    Channel.unavailable.is_(False),
                    authority,
                )
                .order_by(Message.channel_domain, Message.channel_id, Message.id)
                .with_for_update(of=Message)
            )
        ).tuples()
    )
    messages = [message for message, _channel in rows]
    channel_by_ref = {(channel.id, channel.origin_domain): channel for _message, channel in rows}

    guild_refs = {
        (channel.guild_id, channel.guild_domain)
        for channel in channel_by_ref.values()
        if channel.guild_id is not None and channel.guild_domain is not None
    }
    guilds = (
        {
            (guild.id, guild.origin_domain): guild
            for guild in await session.scalars(
                select(Guild).where(tuple_(Guild.id, Guild.origin_domain).in_(guild_refs))
            )
        }
        if guild_refs
        else {}
    )
    access_by_channel: dict[tuple[int, str], ChannelAccess] = {}
    for channel_ref, channel in channel_by_ref.items():
        guild = (
            guilds.get((channel.guild_id, channel.guild_domain))
            if channel.guild_id is not None and channel.guild_domain is not None
            else None
        )
        participants = (
            []
            if guild is not None
            else list(
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
                )
            )
        )
        access_by_channel[channel_ref] = ChannelAccess(channel, guild, participants)

    messages_by_channel: dict[tuple[int, str], list[Message]] = {}
    for message in messages:
        message.content = None
        message.e2ee = None
        message.deleted_at = deleted_at
        messages_by_channel.setdefault((message.channel_id, message.channel_domain), []).append(
            message
        )
    await session.flush()

    local_attachments: list[Attachment] = []
    delivery_destinations: set[str] = set()
    for channel_ref, channel_messages in messages_by_channel.items():
        access = access_by_channel[channel_ref]
        channel = access.channel
        if channel.type in {10, 11, 12}:
            deleted_replies = sum(
                (message.id, message.origin_domain)
                != (channel.starter_message_id, channel.starter_message_domain)
                for message in channel_messages
            )
            channel.message_count = max(0, int(channel.message_count or 0) - deleted_replies)
            if (channel.last_message_id, channel.last_message_domain) in {
                (message.id, message.origin_domain) for message in channel_messages
            }:
                await refresh_thread_last_message_after_delete(session, channel)
        purged_attachments, destinations = await queue_attachment_tombstones(
            session, settings, access, actor, channel_messages
        )
        local_attachments.extend(purged_attachments)
        delivery_destinations.update(destinations)
        if access.guild is not None:
            for message in channel_messages:
                await queue_guild_mutation(
                    session,
                    settings,
                    access.guild,
                    actor,
                    "guild.message.delete",
                    {
                        "message": {
                            "id": str(message.id),
                            "origin_domain": message.origin_domain,
                        },
                        "deleted_at": deleted_at.isoformat(),
                    },
                    channel=channel,
                )
            if channel.type in {10, 11, 12}:
                await queue_guild_mutation(
                    session,
                    settings,
                    access.guild,
                    actor,
                    "guild.channel.update",
                    {"channel": federation_channel_state(channel)},
                    channel=channel,
                )

    return MessagePurgeResult(
        messages=messages,
        access_by_channel=access_by_channel,
        local_attachments=local_attachments,
        delivery_destinations=delivery_destinations,
        guilds=guilds,
        skipped_messages=max(0, total_count - len(messages)),
    )


async def publish_message_purge(
    redis: Redis,
    result: MessagePurgeResult,
) -> None:
    for guild in result.guilds.values():
        await wake_queued_guild_federation(guild)
    for attachment in result.local_attachments:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    for destination in result.delivery_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    for message in result.messages:
        access = result.access_by_channel[(message.channel_id, message.channel_domain)]
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_DELETE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(message.channel_id),
                "channel_domain": message.channel_domain,
            },
        )
    for access in result.access_by_channel.values():
        if access.channel.type in {10, 11, 12}:
            await publish_channel_dispatch(
                redis,
                access,
                "THREAD_UPDATE",
                channel_payload(access.channel),
            )
