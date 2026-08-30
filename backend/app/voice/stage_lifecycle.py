from __future__ import annotations

from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import message_payload
from app.chat.postcommit import publish_committed_dispatches, queue_postcommit_dispatch
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Channel,
    Guild,
    GuildMember,
    GuildScheduledEvent,
    Message,
    MessageProjection,
    StageInstance,
    User,
)
from app.federation.replication import profile_from_user
from app.scheduled_events.service import (
    event_creator,
    materialize_next_recurrence,
    scheduled_event_payload,
    subscriber_count,
)
from app.voice.rooms import guild_room_name
from app.voice.state import room_occupants

STAGE_START_MESSAGE = 27
STAGE_END_MESSAGE = 28
STAGE_SPEAKER_MESSAGE = 29
STAGE_TOPIC_MESSAGE = 31


async def persist_stage_system_message(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    *,
    guild: Guild,
    channel: Channel,
    author: User,
    message_type: int,
    topic: str,
    message_id: int | None = None,
) -> dict[str, object]:
    """Persist and federate one Discord-compatible Stage system message."""

    if message_type not in {
        STAGE_START_MESSAGE,
        STAGE_END_MESSAGE,
        STAGE_SPEAKER_MESSAGE,
        STAGE_TOPIC_MESSAGE,
    }:
        raise ValueError("unsupported Stage system message type")
    if channel.type != 13 or (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise ValueError("Stage system message channel does not belong to the guild")
    message = Message(
        id=message_id if message_id is not None else await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=author.id,
        author_domain=author.origin_domain,
        content=topic,
        encryption_policy_generation=channel.encryption_policy_generation,
        encryption_epoch=channel.encryption_epoch,
        message_type=message_type,
        flags=0,
        mention_user_refs=[],
    )
    session.add(message)
    await session.flush()
    channel.last_message_id = message.id
    channel.last_message_domain = message.origin_domain
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            mention_user_refs=[],
        )
    )
    rendered = message_payload(message, author, [])
    await queue_guild_mutation(
        session,
        settings,
        guild,
        author,
        "guild.message.create",
        {"message": rendered, "author": profile_from_user(author)},
        channel=channel,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "MESSAGE_CREATE",
        rendered,
    )
    return rendered


async def _automatic_stage_author(
    session: AsyncSession,
    stage: StageInstance,
    guild: Guild,
) -> User | None:
    creator = await session.get(User, (stage.creator_id, stage.creator_domain))
    creator_member = (
        await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, stage.creator_id, stage.creator_domain),
        )
        if creator is not None
        else None
    )
    if creator is not None and creator_member is not None:
        return creator
    return await session.get(User, (guild.owner_id, guild.owner_domain))


async def advance_stage_instance_lifecycle(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    """Close authority-owned Stages after the configured no-speaker grace.

    ``empty_since`` is durable, while Redis remains the authoritative live
    occupancy source. Each candidate is locked and rechecked immediately before
    commit so a speaker returning at the completion edge wins the race.
    """

    current = (now or datetime.now(UTC)).astimezone(UTC)
    grace = timedelta(seconds=settings.voice_stage_empty_grace_seconds)
    cursor = -1
    closed = 0
    for _ in range(batch_size):
        stage = await session.scalar(
            select(StageInstance)
            .where(
                StageInstance.origin_domain == settings.domain,
                StageInstance.id > cursor,
            )
            .order_by(StageInstance.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if stage is None:
            break
        cursor = stage.id
        channel = await session.get(Channel, (stage.channel_id, stage.channel_domain))
        guild = await session.get(Guild, (stage.guild_id, stage.guild_domain))
        if channel is None or guild is None or channel.type != 13:
            await session.rollback()
            continue
        room = guild_room_name(guild.id, channel.id)
        speakers = [
            item
            for item in await room_occupants(redis, settings.domain, room)
            if not item.suppressed
        ]
        if speakers:
            if stage.empty_since is not None:
                stage.empty_since = None
                await session.commit()
            else:
                await session.rollback()
            continue
        if stage.empty_since is None:
            stage.empty_since = current
            await session.commit()
            continue
        if current - stage.empty_since < grace:
            await session.rollback()
            continue
        author = await _automatic_stage_author(session, stage, guild)
        if author is None:
            # A guild without a resolvable owner/creator is already corrupt;
            # leave the durable Stage visible for operator repair.
            await session.rollback()
            continue
        end_message_id = await snowflake.mint()
        # The second read is the completion-edge fence. Minting may await a
        # lease renewal, so occupancy must be checked after that await.
        if any(not item.suppressed for item in await room_occupants(redis, settings.domain, room)):
            await session.rollback()
            continue
        rendered_stage = {
            "id": str(stage.id),
            "origin_domain": stage.origin_domain,
            "guild_id": str(stage.guild_id),
            "guild_domain": stage.guild_domain,
            "channel_id": str(stage.channel_id),
            "channel_domain": stage.channel_domain,
            "topic": stage.topic,
            "privacy_level": stage.privacy_level,
            "discoverable_disabled": stage.discoverable_disabled,
            "guild_scheduled_event_id": (
                str(stage.scheduled_event_id) if stage.scheduled_event_id is not None else None
            ),
            "guild_scheduled_event_domain": stage.scheduled_event_domain,
        }
        await persist_stage_system_message(
            session,
            settings,
            snowflake,
            guild=guild,
            channel=channel,
            author=author,
            message_type=STAGE_END_MESSAGE,
            topic=stage.topic,
            message_id=end_message_id,
        )
        if stage.scheduled_event_id is not None and stage.scheduled_event_domain is not None:
            event = await session.get(
                GuildScheduledEvent,
                (stage.scheduled_event_id, stage.scheduled_event_domain),
                with_for_update=True,
            )
            if event is not None and (event.entity_id, event.entity_domain) == (
                stage.id,
                stage.origin_domain,
            ):
                event.entity_id = None
                event.entity_domain = None
                completed_event = event.status == 2
                if completed_event:
                    event.status = 3
                await materialize_updated_at(session, event)
                creator = await event_creator(session, event)
                rendered_event = scheduled_event_payload(
                    event,
                    creator=creator,
                    user_count=await subscriber_count(session, event),
                )
                await queue_guild_mutation(
                    session,
                    settings,
                    guild,
                    author,
                    "guild.scheduled_event.update",
                    {"scheduled_event": rendered_event},
                    channel=channel,
                )
                queue_postcommit_dispatch(
                    session,
                    guild_topic(guild.origin_domain, guild.id),
                    "GUILD_SCHEDULED_EVENT_UPDATE",
                    rendered_event,
                )
                if completed_event and event.recurrence_rule is not None:
                    if creator is None:
                        raise RuntimeError("scheduled event creator is unavailable")
                    await materialize_next_recurrence(
                        session,
                        settings,
                        snowflake,
                        guild=guild,
                        event=event,
                        creator=creator,
                        channel=channel,
                        after=current,
                    )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            author,
            "guild.stage.instance.delete",
            {"stage_instance": rendered_stage},
            channel=channel,
        )
        queue_postcommit_dispatch(
            session,
            guild_topic(guild.origin_domain, guild.id),
            "STAGE_INSTANCE_DELETE",
            rendered_stage,
        )
        await session.delete(stage)
        # One final read minimizes the remaining external-store/SQL commit
        # window. A returning speaker aborts every staged message/event row.
        if any(not item.suppressed for item in await room_occupants(redis, settings.domain, room)):
            await session.rollback()
            continue
        await session.commit()
        await publish_committed_dispatches(session, redis)
        await wake_queued_guild_federation(guild)
        closed += 1
    return closed
