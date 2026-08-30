from __future__ import annotations

from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import resource_version, user_payload
from app.chat.postcommit import publish_committed_dispatches, queue_postcommit_dispatch
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Attachment,
    Channel,
    Guild,
    GuildScheduledEvent,
    GuildScheduledEventSubscription,
    User,
)
from app.scheduled_events.recurrence import next_recurrence_start
from app.voice.rooms import guild_room_name
from app.voice.state import room_occupants

SCHEDULED = 1
ACTIVE = 2
COMPLETED = 3
CANCELED = 4
STAGE_INSTANCE = 1
VOICE = 2
EXTERNAL = 3
CHANNEL_EVENT_TYPES = frozenset({STAGE_INSTANCE, VOICE})
CHANNEL_EVENT_START_GRACE = timedelta(hours=3)
CHANNEL_EVENT_EMPTY_GRACE = timedelta(minutes=5)
CHANNEL_EVENT_EMPTY_KEY_TTL_SECONDS = 24 * 60 * 60


def scheduled_event_image_binding(event: GuildScheduledEvent) -> str:
    return f"scheduled_event:{event.origin_domain}:{event.id}:image"


def scheduled_event_payload(
    event: GuildScheduledEvent,
    *,
    creator: User | None = None,
    user_count: int | None = None,
    me_subscribed: bool | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(event.id),
        "origin_domain": event.origin_domain,
        "guild_id": str(event.guild_id),
        "guild_domain": event.guild_domain,
        "channel_id": str(event.channel_id) if event.channel_id is not None else None,
        "channel_domain": event.channel_domain,
        "creator_id": str(event.creator_id),
        "creator_domain": event.creator_domain,
        "name": event.name,
        "description": event.description,
        "scheduled_start_time": event.scheduled_start_time.isoformat(),
        "scheduled_end_time": (
            event.scheduled_end_time.isoformat() if event.scheduled_end_time else None
        ),
        "privacy_level": event.privacy_level,
        "status": event.status,
        "entity_type": event.entity_type,
        "entity_id": str(event.entity_id) if event.entity_id is not None else None,
        "entity_domain": event.entity_domain,
        "entity_metadata": event.entity_metadata,
        "recurrence_rule": event.recurrence_rule,
        "image": event.image_hash,
        "created_at": event.created_at.isoformat(),
        "updated_at": event.updated_at.isoformat(),
        "version": resource_version(event),
    }
    if creator is not None:
        payload["creator"] = user_payload(creator)
    if user_count is not None:
        payload["user_count"] = user_count
    if me_subscribed is not None:
        payload["me_subscribed"] = me_subscribed
    return payload


async def subscriber_count(session: AsyncSession, event: GuildScheduledEvent) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(GuildScheduledEventSubscription)
            .where(
                GuildScheduledEventSubscription.event_id == event.id,
                GuildScheduledEventSubscription.event_domain == event.origin_domain,
            )
        )
        or 0
    )


async def viewer_subscribed(
    session: AsyncSession,
    event: GuildScheduledEvent,
    viewer: User,
) -> bool:
    return (
        await session.scalar(
            select(GuildScheduledEventSubscription.user_id)
            .where(
                GuildScheduledEventSubscription.event_id == event.id,
                GuildScheduledEventSubscription.event_domain == event.origin_domain,
                GuildScheduledEventSubscription.user_id == viewer.id,
                GuildScheduledEventSubscription.user_domain == viewer.origin_domain,
            )
            .limit(1)
        )
        is not None
    )


async def event_creator(session: AsyncSession, event: GuildScheduledEvent) -> User | None:
    return await session.get(User, (event.creator_id, event.creator_domain))


async def materialize_next_recurrence(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    *,
    guild: Guild,
    event: GuildScheduledEvent,
    creator: User,
    channel: Channel | None,
    after: datetime | None = None,
) -> GuildScheduledEvent | None:
    """Create the next occurrence while leaving this terminal row immutable."""

    if event.status not in {COMPLETED, CANCELED} or event.recurrence_rule is None:
        return None
    next_start = next_recurrence_start(
        event.recurrence_rule,
        current_start=event.scheduled_start_time,
        after=after,
    )
    duration = (
        event.scheduled_end_time - event.scheduled_start_time
        if event.scheduled_end_time is not None
        else None
    )
    subscriptions = list(
        await session.scalars(
            select(GuildScheduledEventSubscription)
            .where(
                GuildScheduledEventSubscription.event_id == event.id,
                GuildScheduledEventSubscription.event_domain == event.origin_domain,
            )
            .with_for_update(read=True)
        )
    )
    recurrence_rule = {**event.recurrence_rule, "start": next_start.isoformat()}
    next_event = GuildScheduledEvent(
        id=await snowflake.mint(),
        origin_domain=event.origin_domain,
        guild_id=event.guild_id,
        guild_domain=event.guild_domain,
        channel_id=event.channel_id,
        channel_domain=event.channel_domain,
        creator_id=event.creator_id,
        creator_domain=event.creator_domain,
        name=event.name,
        description=event.description,
        scheduled_start_time=next_start,
        scheduled_end_time=next_start + duration if duration is not None else None,
        privacy_level=event.privacy_level,
        status=SCHEDULED,
        entity_type=event.entity_type,
        entity_id=None,
        entity_domain=None,
        entity_metadata=event.entity_metadata,
        recurrence_rule=recurrence_rule,
        image_hash=event.image_hash,
    )
    session.add(next_event)
    if event.image_hash is not None:
        image = await session.scalar(
            select(Attachment)
            .where(Attachment.asset_binding == scheduled_event_image_binding(event))
            .with_for_update()
        )
        if image is not None:
            # The public cover URL is content-addressed, but one durable asset
            # row owns its storage lifecycle. Move that ownership forward so
            # deleting an older terminal occurrence cannot purge the cover
            # still advertised by the next occurrence.
            image.asset_binding = scheduled_event_image_binding(next_event)
    for subscription in subscriptions:
        session.add(
            GuildScheduledEventSubscription(
                event_id=next_event.id,
                event_domain=next_event.origin_domain,
                guild_id=next_event.guild_id,
                guild_domain=next_event.guild_domain,
                user_id=subscription.user_id,
                user_domain=subscription.user_domain,
            )
        )
    await session.flush()
    rendered = scheduled_event_payload(
        next_event,
        creator=creator,
        user_count=len(subscriptions),
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        creator,
        "guild.scheduled_event.create",
        {"scheduled_event": rendered},
        channel=channel,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SCHEDULED_EVENT_CREATE",
        rendered,
    )
    return next_event


def scheduled_event_lifecycle_status(
    event: GuildScheduledEvent,
    *,
    now: datetime,
) -> int | None:
    if event.entity_type == EXTERNAL:
        if (
            event.scheduled_end_time is not None
            and event.scheduled_end_time <= now
            and event.status in {SCHEDULED, ACTIVE}
        ):
            return COMPLETED
        if event.status == SCHEDULED and event.scheduled_start_time <= now:
            return ACTIVE
    elif (
        event.entity_type in CHANNEL_EVENT_TYPES
        and event.status == SCHEDULED
        and event.scheduled_start_time <= now - CHANNEL_EVENT_START_GRACE
    ):
        return CANCELED
    return None


async def _empty_channel_lifecycle_status(
    redis: Redis,
    settings: Settings,
    event: GuildScheduledEvent,
    *,
    now: datetime,
) -> int | None:
    if (
        event.status != ACTIVE
        or event.entity_type not in CHANNEL_EVENT_TYPES
        or event.channel_id is None
    ):
        return None
    key = f"scheduled-event:empty:{event.origin_domain}:{event.id}"
    room = guild_room_name(event.guild_id, event.channel_id)
    if await room_occupants(redis, settings.domain, room):
        await redis.delete(key)
        return None
    raw_empty_since = await redis.get(key)
    if raw_empty_since is None:
        await redis.set(
            key,
            str(int(now.timestamp())),
            ex=CHANNEL_EVENT_EMPTY_KEY_TTL_SECONDS,
            nx=True,
        )
        return None
    try:
        empty_since = int(
            raw_empty_since.decode("ascii")
            if isinstance(raw_empty_since, bytes)
            else raw_empty_since
        )
    except (TypeError, ValueError):
        await redis.delete(key)
        return None
    if now.timestamp() - empty_since < CHANNEL_EVENT_EMPTY_GRACE.total_seconds():
        return None
    # Recheck at the completion edge so a join racing the grace timer wins.
    if await room_occupants(redis, settings.domain, room):
        await redis.delete(key)
        return None
    return COMPLETED


async def advance_scheduled_event_lifecycle(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator | None = None,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    """Advance authority-owned events whose Discord lifecycle deadline passed."""

    current = now or datetime.now(UTC)
    overdue_channel_start = current - CHANNEL_EVENT_START_GRACE
    due = list(
        await session.scalars(
            select(GuildScheduledEvent)
            .where(
                GuildScheduledEvent.origin_domain == settings.domain,
                (
                    (
                        (GuildScheduledEvent.entity_type == EXTERNAL)
                        & (GuildScheduledEvent.status == SCHEDULED)
                        & (GuildScheduledEvent.scheduled_start_time <= current)
                    )
                    | (
                        (GuildScheduledEvent.entity_type == EXTERNAL)
                        & (GuildScheduledEvent.status == ACTIVE)
                        & (GuildScheduledEvent.scheduled_end_time <= current)
                    )
                    | (
                        GuildScheduledEvent.entity_type.in_(CHANNEL_EVENT_TYPES)
                        & (GuildScheduledEvent.status == SCHEDULED)
                        & (GuildScheduledEvent.scheduled_start_time <= overdue_channel_start)
                    )
                    | (
                        GuildScheduledEvent.entity_type.in_(CHANNEL_EVENT_TYPES)
                        & (GuildScheduledEvent.status == ACTIVE)
                    )
                ),
            )
            .order_by(GuildScheduledEvent.scheduled_start_time, GuildScheduledEvent.id)
            .with_for_update(skip_locked=True)
            .limit(batch_size)
        )
    )
    changed = 0
    changed_guilds: dict[tuple[int, str], Guild] = {}
    for event in due:
        next_status = scheduled_event_lifecycle_status(event, now=current)
        if next_status is None:
            next_status = await _empty_channel_lifecycle_status(
                redis,
                settings,
                event,
                now=current,
            )
        if next_status is None:
            continue
        event.status = next_status
        changed += 1
        await materialize_updated_at(session, event)
        guild = await session.get(Guild, (event.guild_id, event.guild_domain))
        creator = await event_creator(session, event)
        if guild is None or creator is None:
            raise RuntimeError("scheduled event authority state is incomplete")
        channel = (
            await session.get(Channel, (event.channel_id, event.channel_domain))
            if event.channel_id is not None and event.channel_domain is not None
            else None
        )
        rendered = scheduled_event_payload(
            event,
            creator=creator,
            user_count=await subscriber_count(session, event),
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            creator,
            "guild.scheduled_event.update",
            {"scheduled_event": rendered},
            channel=channel,
        )
        changed_guilds[(guild.id, guild.origin_domain)] = guild
        queue_postcommit_dispatch(
            session,
            guild_topic(event.guild_domain, event.guild_id),
            "GUILD_SCHEDULED_EVENT_UPDATE",
            rendered,
        )
        if event.recurrence_rule is not None:
            if snowflake is None:
                raise RuntimeError("scheduled event recurrence requires a Snowflake lease")
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
    if not changed:
        await session.rollback()
        return 0
    await session.commit()
    await publish_committed_dispatches(session, redis)
    for guild in changed_guilds.values():
        await wake_queued_guild_federation(guild)
    return changed
