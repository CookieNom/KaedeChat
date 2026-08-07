from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import and_, case, delete, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import SimpleRetryMiddleware
from taskiq_redis import RedisStreamBroker

from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.payloads import guild_payload, render_message_payload
from app.core.cache_warmup import warm_identify_cache
from app.core.logging import configure_logging
from app.core.metrics import observed_job
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import (
    Attachment,
    AuditLogEntry,
    AuthEvent,
    Channel,
    DMParticipant,
    FederatedHistoryMessage,
    Guild,
    GuildEvent,
    GuildHistoryImport,
    GuildMember,
    Message,
    MessageProjection,
    OneTimeToken,
    ReadState,
    Session,
    User,
)
from app.db.partitions import ensure_message_partitions
from app.db.session import create_engine_and_sessionmaker
from app.email.outbox import cleanup_email_outbox, drain_email_outbox
from app.federation.delivery import (
    cleanup_federation_retention,
    drain_destination,
    due_destinations,
    expire_stale_outbox,
)
from app.federation.guilds import synchronize_guild
from app.federation.history import (
    cleanup_history_transfers,
    purge_ineligible_federated_history,
    request_and_import_history,
)
from app.federation.network import normalize_domain
from app.federation.presence import fanout_presence
from app.federation.users import refresh_remote_user
from app.media.jobs import (
    enforce_remote_cache_limit,
    process_attachment_record,
    purge_local_attachment,
    purge_remote_attachment_cache,
    retention_sweep,
    sweep_orphan_uploads,
    sweep_staging_objects,
)
from app.media.service import attachment_payload
from app.voice.background import replicate_room
from app.voice.cleanup import cleanup_orphaned_dm_rooms
from app.voice.rooms import parse_room_name

configure_logging(os.environ.get("KAEDE_LOG_LEVEL", "INFO"))
broker_url = os.environ.get("KAEDE_DRAGONFLY_URL")
if not broker_url:
    raise RuntimeError("KAEDE_DRAGONFLY_URL is required for Taskiq")
broker = RedisStreamBroker(url=broker_url).with_middlewares(
    SimpleRetryMiddleware(default_retry_count=5)
)

SET_LATEST_MESSAGE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
    local separator = string.find(current, '@', 1, true)
    if separator then
        local current_id = string.sub(current, 1, separator - 1)
        local current_domain = string.sub(current, separator + 1)
        local incoming_id = ARGV[1]
        local newer = string.len(incoming_id) > string.len(current_id)
            or (string.len(incoming_id) == string.len(current_id) and incoming_id > current_id)
            or (incoming_id == current_id and ARGV[2] > current_domain)
        if not newer then return current end
    end
end
local rendered = ARGV[1] .. '@' .. ARGV[2]
redis.call('SET', KEYS[1], rendered)
return rendered
"""


@broker.task(task_name="voice.call_room_gc", schedule=[{"cron": "*/5 * * * *"}])
@observed_job("voice.call_room_gc")
async def voice_call_room_gc() -> int:
    settings = get_settings()
    if not settings.voice_enabled:
        return 0
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        return await cleanup_orphaned_dm_rooms(redis, settings)
    finally:
        await redis.aclose()


@broker.task(task_name="voice.replicate_room", retry_on_error=True, max_retries=3)
@observed_job("voice.replicate_room")
async def voice_replicate_room(room: str) -> int:
    """Immediately copy an authoritative guild room snapshot to member homes."""

    settings = get_settings()
    kind, _, _ = parse_room_name(room)
    if not settings.voice_enabled or kind != "g":
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        return await replicate_room(redis, sessionmaker, settings, room)
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.presence_fanout", retry_on_error=True, max_retries=2)
@observed_job("federation.presence_fanout")
async def federation_presence_fanout(
    user_id: int,
    user_domain: str,
    status: str,
    generation: int,
) -> int:
    """Sign ephemeral presence inside the worker's federation trust boundary."""

    settings = get_settings()
    user_domain = normalize_domain(user_domain)
    if (
        user_domain != settings.domain
        or status not in {"online", "idle", "dnd", "offline"}
        or not 0 <= user_id <= (1 << 63) - 1
        or generation <= 0
    ):
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        current_generation = await redis.get(f"presence:generation:{user_domain}:{user_id}")
        if current_generation is None or int(current_generation) != generation:
            return 0
        raw_state = await redis.get(f"presence:{user_domain}:{user_id}")
        if not presence_fanout_state_is_current(raw_state, status, generation):
            return 0
        async with sessionmaker() as session:
            user = await session.get(User, (user_id, user_domain))
            if user is None or not user.is_local:
                return 0
        await fanout_presence(sessionmaker, settings, user, status)
        return 1
    finally:
        await redis.aclose()
        await engine.dispose()


def presence_fanout_state_is_current(
    raw_state: str | bytes | None,
    status: str,
    generation: int,
) -> bool:
    if raw_state is None:
        return status == "offline"
    try:
        state = json.loads(raw_state)
        stored_status = str(state["status"])
        stored_generation = int(state["generation"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    visible_status = "offline" if stored_status == "invisible" else stored_status
    return stored_generation == generation and visible_status == status


async def project_message_record(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    message_id: int,
    message_domain: str,
) -> int:
    projection = await session.scalar(
        select(MessageProjection)
        .where(
            MessageProjection.message_id == message_id,
            MessageProjection.message_domain == message_domain,
        )
        .with_for_update()
    )
    if projection is None or projection.processed_at is not None:
        return 0
    projections = list(
        await session.scalars(
            select(MessageProjection)
            .where(
                MessageProjection.channel_id == projection.channel_id,
                MessageProjection.channel_domain == projection.channel_domain,
                MessageProjection.processed_at.is_(None),
            )
            .order_by(MessageProjection.message_id, MessageProjection.message_domain)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    )
    if projection not in projections:
        projections.append(projection)
    messages: list[tuple[MessageProjection, Message]] = []
    for pending in projections:
        message = await session.get(Message, (pending.message_id, pending.message_domain))
        if message is not None:
            messages.append((pending, message))
    now = datetime.now(UTC)
    if not messages:
        for pending in projections:
            pending.processed_at = now
        await session.commit()
        return 0
    latest = max(messages, key=lambda item: (item[1].id, item[1].origin_domain))[1]
    await session.execute(
        update(Channel)
        .where(
            Channel.id == projection.channel_id,
            Channel.origin_domain == projection.channel_domain,
            Channel.last_message_id.is_(None)
            | (
                tuple_(Channel.last_message_id, Channel.last_message_domain)
                < (latest.id, latest.origin_domain)
            ),
        )
        .values(
            last_message_id=latest.id,
            last_message_domain=latest.origin_domain,
            updated_at=func.now(),
        )
    )
    mention_states: list[tuple[int, int | None, str | None, int]] = []
    for pending, message in messages:
        seen: set[int] = set()
        for reference in pending.mention_user_refs[:100]:
            try:
                user_id = int(reference["id"])
                domain = str(reference["origin_domain"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                domain != settings.domain
                or user_id in seen
                or (user_id, domain) == (message.author_id, message.author_domain)
            ):
                continue
            seen.add(user_id)
            statement = pg_insert(ReadState).values(
                user_id=user_id,
                user_domain=settings.domain,
                user_is_local=True,
                channel_id=pending.channel_id,
                channel_domain=pending.channel_domain,
                last_message_id=None,
                last_message_domain=None,
                mention_count=1,
            )
            unread = ReadState.last_message_id.is_(None) | (
                tuple_(ReadState.last_message_id, ReadState.last_message_domain)
                < (message.id, message.origin_domain)
            )
            row = (
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            "user_id",
                            "user_domain",
                            "channel_id",
                            "channel_domain",
                        ],
                        set_={
                            "mention_count": case(
                                (unread, ReadState.mention_count + 1),
                                else_=ReadState.mention_count,
                            ),
                            "updated_at": func.now(),
                        },
                    ).returning(
                        ReadState.user_id,
                        ReadState.last_message_id,
                        ReadState.last_message_domain,
                        ReadState.mention_count,
                    )
                )
            ).one()
            mention_states.append((row[0], row[1], row[2], row[3]))
    for pending in projections:
        pending.processed_at = now
    await session.commit()
    await cast(
        Awaitable[object],
        redis.eval(
            SET_LATEST_MESSAGE_SCRIPT,
            1,
            f"channel:last_message:{projection.channel_domain}:{projection.channel_id}",
            str(latest.id),
            latest.origin_domain,
        ),
    )
    for user_id, last_message_id, last_message_domain, mention_count in mention_states:
        await publish_dispatch(
            redis,
            user_topic(settings.domain, user_id),
            "READ_STATE_UPDATE",
            {
                "channel_id": str(projection.channel_id),
                "channel_domain": projection.channel_domain,
                "last_message_id": (str(last_message_id) if last_message_id is not None else None),
                "last_message_domain": last_message_domain,
                "mention_count": mention_count,
            },
        )
    return len(mention_states)


@broker.task(task_name="mentions.fanout", retry_on_error=True, max_retries=5)
@observed_job("mentions.fanout")
async def mentions_fanout(message_id: int, message_domain: str) -> int:
    settings = get_settings()
    message_domain = normalize_domain(message_domain)
    if not 0 <= message_id <= (1 << 63) - 1:
        raise ValueError("invalid message identity")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        async with sessionmaker() as session:
            return await project_message_record(
                session, redis, settings, message_id, message_domain
            )
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="messages.projection_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("messages.projection_sweep")
async def message_projection_sweep() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    projected = 0
    try:
        async with sessionmaker() as session:
            await sweep_staging_objects(session, settings)
            refs = (
                await session.execute(
                    select(MessageProjection.message_id, MessageProjection.message_domain)
                    .where(MessageProjection.processed_at.is_(None))
                    .order_by(MessageProjection.created_at)
                    .limit(500)
                )
            ).all()
            for message_id, message_domain in refs:
                await project_message_record(session, redis, settings, message_id, message_domain)
                projected += 1
        return projected
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.history_sync", retry_on_error=True, max_retries=5)
@observed_job("federation.history_sync")
async def federation_history_sync(guild_id: int, guild_domain: str, user_id: int) -> int:
    settings = get_settings()
    guild_domain = normalize_domain(guild_domain)
    if guild_domain == settings.domain:
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        async with sessionmaker() as session:
            guild = await session.get(Guild, (guild_id, guild_domain))
            user = await session.get(User, (user_id, settings.domain))
            membership = await session.get(
                GuildMember,
                (guild_id, guild_domain, user_id, settings.domain),
            )
            if guild is None or user is None or membership is None or guild.unavailable:
                return 0
            imported = await request_and_import_history(session, settings, guild, user)
            if imported:
                await publish_dispatch(
                    redis,
                    guild_topic(guild.origin_domain, guild.id),
                    "GUILD_HISTORY_SYNC_COMPLETE",
                    {
                        "guild_id": str(guild.id),
                        "guild_domain": guild.origin_domain,
                        "imported_messages": imported,
                    },
                )
            return imported
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.history_sync_guild", retry_on_error=True, max_retries=5)
@observed_job("federation.history_sync_guild")
async def federation_history_sync_guild(
    guild_id: int,
    guild_domain: str,
    after_user_id: int = 0,
) -> int:
    """Fan out history eligibility checks without an unbounded request task."""

    settings = get_settings()
    guild_domain = normalize_domain(guild_domain)
    if guild_domain == settings.domain or not 0 <= after_user_id <= (1 << 63) - 1:
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            user_ids = list(
                await session.scalars(
                    select(GuildMember.user_id)
                    .where(
                        GuildMember.guild_id == guild_id,
                        GuildMember.guild_domain == guild_domain,
                        GuildMember.user_domain == settings.domain,
                        GuildMember.user_id > after_user_id,
                    )
                    .order_by(GuildMember.user_id)
                    .limit(251)
                )
            )
        page = user_ids[:250]
        for user_id in page:
            await enqueue_best_effort(
                federation_history_sync,
                guild_id,
                guild_domain,
                user_id,
            )
        if len(user_ids) > len(page):
            await enqueue_best_effort(
                federation_history_sync_guild,
                guild_id,
                guild_domain,
                page[-1],
            )
        return len(page)
    finally:
        await engine.dispose()


@broker.task(task_name="federation.history_sweep", schedule=[{"cron": "*/5 * * * *"}])
@observed_job("federation.history_sweep")
async def federation_history_sweep() -> int:
    settings = get_settings()
    if not settings.federation_history_import_enabled:
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            candidates = (
                await session.execute(
                    select(Guild.id, Guild.origin_domain, GuildMember.user_id)
                    .join(
                        GuildMember,
                        (GuildMember.guild_id == Guild.id)
                        & (GuildMember.guild_domain == Guild.origin_domain),
                    )
                    .where(
                        Guild.origin_domain != settings.domain,
                        Guild.unavailable.is_(False),
                        GuildMember.user_domain == settings.domain,
                        ~exists(
                            select(GuildHistoryImport.export_id).where(
                                GuildHistoryImport.guild_id == Guild.id,
                                GuildHistoryImport.guild_domain == Guild.origin_domain,
                                GuildHistoryImport.requester_user_id == GuildMember.user_id,
                                or_(
                                    GuildHistoryImport.status.in_(
                                        ("pending", "downloading", "reconciling")
                                    ),
                                    and_(
                                        GuildHistoryImport.status == "completed",
                                        GuildHistoryImport.requester_member_version
                                        == GuildMember.member_version,
                                        GuildHistoryImport.permission_generation
                                        == Guild.permission_generation,
                                        GuildHistoryImport.history_policy_generation
                                        == Guild.history_policy_generation,
                                    ),
                                ),
                            )
                        ),
                    )
                    .order_by(Guild.updated_at, Guild.id, GuildMember.user_id)
                    .limit(100)
                )
            ).all()
        for guild_id, guild_domain, user_id in candidates:
            await enqueue_best_effort(
                federation_history_sync,
                guild_id,
                guild_domain,
                user_id,
            )
        return len(candidates)
    finally:
        await engine.dispose()


@broker.task(task_name="federation.history_revocation_sweep", schedule=[{"cron": "*/5 * * * *"}])
@observed_job("federation.history_revocation_sweep")
async def federation_history_revocation_sweep() -> int:
    """Converge best-effort purges even if a live policy event was missed."""

    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
    removed = 0
    try:
        async with sessionmaker() as session:
            refs = (
                await session.execute(
                    select(Guild.id, Guild.origin_domain)
                    .join(
                        GuildHistoryImport,
                        (GuildHistoryImport.guild_id == Guild.id)
                        & (GuildHistoryImport.guild_domain == Guild.origin_domain),
                    )
                    .join(
                        FederatedHistoryMessage,
                        (FederatedHistoryMessage.export_id == GuildHistoryImport.export_id)
                        & (
                            FederatedHistoryMessage.export_domain
                            == GuildHistoryImport.export_domain
                        ),
                    )
                    .where(Guild.origin_domain != settings.domain)
                    .distinct()
                    .limit(100)
                )
            ).all()
            for guild_id, guild_domain in refs:
                guild = await session.scalar(
                    select(Guild)
                    .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
                    .with_for_update(skip_locked=True)
                )
                if guild is None:
                    continue
                guild_removed = await purge_ineligible_federated_history(
                    session,
                    settings,
                    guild,
                )
                removed += guild_removed
                await session.commit()
                if guild_removed:
                    channel_ids = list(
                        await session.scalars(
                            select(Channel.id).where(
                                Channel.guild_id == guild.id,
                                Channel.guild_domain == guild.origin_domain,
                            )
                        )
                    )
                    if channel_ids:
                        await redis.delete(
                            *(
                                f"channel:last_message:{guild.origin_domain}:{channel_id}"
                                for channel_id in channel_ids
                            )
                        )
        return removed
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="media.process", retry_on_error=True, max_retries=3)
@observed_job("media.process")
async def media_process(attachment_id: int, origin_domain: str) -> str:
    settings = get_settings()
    if origin_domain != settings.domain or not 0 <= attachment_id <= (1 << 63) - 1:
        raise ValueError("invalid local attachment identity")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            result = await process_attachment_record(
                session, settings, attachment_id, origin_domain
            )
            attachment = await session.get(Attachment, (attachment_id, origin_domain))
            message = (
                await session.get(
                    Message,
                    (attachment.message_id, attachment.message_domain),
                )
                if attachment is not None
                and attachment.message_id is not None
                and attachment.message_domain is not None
                else None
            )
            channel = (
                await session.get(Channel, (message.channel_id, message.channel_domain))
                if message is not None
                else None
            )
            if attachment is not None and message is not None and channel is not None:
                payload = {
                    "message_id": str(message.id),
                    "message_domain": message.origin_domain,
                    "attachment": attachment_payload(attachment),
                }
                if channel.guild_id is not None and channel.guild_domain is not None:
                    await publish_dispatch(
                        redis,
                        guild_topic(channel.guild_domain, channel.guild_id),
                        "ATTACHMENT_UPDATE",
                        payload,
                    )
                else:
                    participants = await session.execute(
                        select(DMParticipant.user_id, DMParticipant.user_domain).where(
                            DMParticipant.conversation_id == channel.id,
                            DMParticipant.conversation_domain == channel.origin_domain,
                            DMParticipant.user_domain == settings.domain,
                        )
                    )
                    for user_id, user_domain in participants:
                        await publish_dispatch(
                            redis,
                            user_topic(user_domain, user_id),
                            "ATTACHMENT_UPDATE",
                            payload,
                        )
            return result
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="media.processing_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("media.processing_sweep")
async def media_processing_sweep() -> int:
    """Recover finalized uploads whose best-effort processing wake was lost."""

    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            refs = (
                await session.execute(
                    select(Attachment.id, Attachment.origin_domain)
                    .where(
                        Attachment.origin_domain == settings.domain,
                        Attachment.finalized_at.is_not(None),
                        Attachment.deleted_at.is_(None),
                        Attachment.scan_status.in_(("pending", "failed")),
                    )
                    .order_by(Attachment.finalized_at, Attachment.id)
                    .limit(100)
                )
            ).all()
        for attachment_id, origin_domain in refs:
            await enqueue_best_effort(media_process, attachment_id, origin_domain)
        return len(refs)
    finally:
        await engine.dispose()


@broker.task(task_name="media.orphan_gc", schedule=[{"cron": "*/15 * * * *"}])
@observed_job("media.orphan_gc")
async def media_orphan_gc() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await sweep_orphan_uploads(session, settings)
    finally:
        await engine.dispose()


@broker.task(task_name="media.cache_gc", schedule=[{"cron": "23 * * * *"}])
@observed_job("media.cache_gc")
async def media_cache_gc() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await enforce_remote_cache_limit(session, settings)
    finally:
        await engine.dispose()


@broker.task(task_name="media.remote_purge", retry_on_error=True, max_retries=5)
@observed_job("media.remote_purge")
async def media_remote_purge(origin_domain: str, attachment_id: int) -> int:
    settings = get_settings()
    origin_domain = normalize_domain(origin_domain)
    if origin_domain == settings.domain or not 0 <= attachment_id <= (1 << 63) - 1:
        raise ValueError("invalid remote attachment identity")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await purge_remote_attachment_cache(
                session, settings, origin_domain, attachment_id
            )
    finally:
        await engine.dispose()


@broker.task(task_name="media.local_purge", retry_on_error=True, max_retries=5)
@observed_job("media.local_purge")
async def media_local_purge(attachment_id: int, origin_domain: str) -> str:
    settings = get_settings()
    if origin_domain != settings.domain or not 0 <= attachment_id <= (1 << 63) - 1:
        raise ValueError("invalid local attachment identity")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await purge_local_attachment(session, settings, attachment_id, origin_domain)
    finally:
        await engine.dispose()


@broker.task(task_name="media.retention", schedule=[{"cron": "31 4 * * *"}])
@observed_job("media.retention")
async def media_retention() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await retention_sweep(session, settings)
    finally:
        await engine.dispose()


@broker.task(task_name="cache.warmup")
@observed_job("cache.warmup")
async def cache_warmup() -> None:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        await warm_identify_cache(redis, sessionmaker, settings)
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.deliver")
@observed_job("federation.deliver")
async def federation_deliver(destination: str) -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
    try:
        return await drain_destination(sessionmaker, settings, destination, redis)
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.guild_sync", retry_on_error=True, max_retries=5)
@observed_job("federation.guild_sync")
async def federation_guild_sync(origin: str, guild_id: int) -> int:
    """Recover a replica outside the latency-sensitive federation API pool."""

    settings = get_settings()
    origin = normalize_domain(origin)
    if origin == settings.domain or not 0 <= guild_id <= (1 << 63) - 1:
        raise ValueError("invalid replicated guild identity")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            # Coalesce duplicate sender retries for the same replica and bound a
            # malicious/slow home across all history and snapshot pages.
            await session.scalar(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"kaede-guild-sync:{origin}:{guild_id}", 0)
                    )
                )
            )
            guild = await session.get(Guild, (guild_id, origin))
            if guild is None:
                return 0
            async with asyncio.timeout(30):
                messages = await synchronize_guild(session, settings, guild)
            await session.commit()
            channel_ids = list(
                await session.scalars(
                    select(Channel.id).where(
                        Channel.guild_id == guild_id,
                        Channel.guild_domain == origin,
                    )
                )
            )
            if channel_ids:
                await redis.delete(
                    *(f"channel:last_message:{origin}:{channel_id}" for channel_id in channel_ids)
                )
            await publish_dispatch(
                redis,
                guild_topic(origin, guild_id),
                "GUILD_UPDATE",
                guild_payload(guild),
            )
            for message in messages:
                await publish_dispatch(
                    redis,
                    guild_topic(origin, guild_id),
                    "MESSAGE_CREATE",
                    await render_message_payload(session, message),
                )
                await enqueue_best_effort(mentions_fanout, message.id, message.origin_domain)
            await enqueue_best_effort(federation_history_sync_guild, guild_id, origin)
            return len(messages)
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.outbox_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("federation.outbox_sweep")
async def federation_outbox_sweep() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            await expire_stale_outbox(session, settings, redis)
            destinations = await due_destinations(session)
        for destination in destinations:
            await enqueue_best_effort(federation_deliver, destination)
        return len(destinations)
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.guild_sync_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("federation.guild_sync_sweep")
async def federation_guild_sync_sweep() -> int:
    """Retry durable replica reconciliation when a direct task wake was lost."""

    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            replicas = (
                (
                    await session.execute(
                        select(Guild.origin_domain, Guild.id)
                        .where(
                            Guild.origin_domain != settings.domain,
                            Guild.sync_status.in_(("stale", "failed")),
                        )
                        .order_by(Guild.origin_domain, Guild.id)
                        .limit(100)
                    )
                )
                .tuples()
                .all()
            )
        for origin, guild_id in replicas:
            await enqueue_best_effort(federation_guild_sync, origin, guild_id)
        return len(replicas)
    finally:
        await engine.dispose()


@broker.task(task_name="federation.user_refresh", retry_on_error=True, max_retries=3)
@observed_job("federation.user_refresh")
async def federation_user_refresh(username: str, domain: str) -> bool:
    """Refresh stale remote identity data without delaying a user lookup."""

    settings = get_settings()
    domain = normalize_domain(domain)
    if domain == settings.domain or not username:
        raise ValueError("invalid remote profile refresh target")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
    refresh_key = f"federation:user-lookup:refresh:{domain}:{username}"
    try:
        async with sessionmaker() as session:
            return await refresh_remote_user(session, settings, redis, username, domain) is not None
    finally:
        await redis.delete(refresh_key)
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.retention", schedule=[{"cron": "17 3 * * *"}])
@observed_job("federation.retention")
async def federation_retention() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await cleanup_federation_retention(session, settings)
    finally:
        await engine.dispose()


async def _run_email_outbox_cycle(*, cleanup: bool) -> dict[str, int]:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        result = await drain_email_outbox(sessionmaker, settings)
        if cleanup:
            async with sessionmaker() as session:
                result["cleaned"] = await cleanup_email_outbox(session)
        return result
    finally:
        await engine.dispose()


@broker.task(task_name="email.outbox_drain", retry_on_error=True, max_retries=5)
@observed_job("email.outbox_drain")
async def email_outbox_drain() -> dict[str, int]:
    return await _run_email_outbox_cycle(cleanup=False)


@broker.task(
    task_name="email.outbox_sweep",
    retry_on_error=True,
    max_retries=5,
    schedule=[{"cron": "* * * * *"}],
)
@observed_job("email.outbox_sweep")
async def email_outbox_sweep() -> dict[str, int]:
    """Recover missed Redis wakes and prune old terminal delivery metadata."""

    return await _run_email_outbox_cycle(cleanup=True)


@broker.task(task_name="partitions.ensure", schedule=[{"cron": "17 3 * * *"}])
@observed_job("partitions.ensure")
async def partitions_ensure() -> None:
    settings = get_settings()
    engine, _ = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with engine.begin() as connection:
            await ensure_message_partitions(connection)
    finally:
        await engine.dispose()


@broker.task(task_name="accounts.purge_unverified", schedule=[{"cron": "43 3 * * *"}])
@observed_job("accounts.purge_unverified")
async def purge_unverified_accounts() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await purge_unverified_accounts_in_session(session, settings)
    finally:
        await engine.dispose()


async def purge_unverified_accounts_in_session(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """Delete expired registrations without invalidating a freshly resent link."""

    if settings.email_backend == "disabled":
        await session.commit()
        return 0
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(hours=settings.verification_ttl_hours)
    active_verification = exists(
        select(OneTimeToken.id).where(
            OneTimeToken.user_id == User.id,
            OneTimeToken.user_domain == User.origin_domain,
            OneTimeToken.purpose == "email_verify",
            OneTimeToken.consumed_at.is_(None),
            OneTimeToken.expires_at > current_time,
        )
    )
    # Token issuance takes this same User lock before replacing a credential.
    # Locking candidate accounts first means a concurrent resend either commits
    # its fresh token before this predicate is evaluated or causes this sweep to
    # skip the account; a set-based DELETE alone can miss that serialization.
    candidate_refs = list(
        (
            await session.execute(
                select(User.id, User.origin_domain)
                .where(
                    User.is_local.is_(True),
                    User.email.is_not(None),
                    User.email_verified_at.is_(None),
                    User.created_at < cutoff,
                    ~active_verification,
                )
                .with_for_update(skip_locked=True)
            )
        ).tuples()
    )
    if not candidate_refs:
        await session.commit()
        return 0
    result = await session.execute(
        delete(User)
        .where(
            tuple_(User.id, User.origin_domain).in_(candidate_refs),
            # DELETE is a fresh READ COMMITTED statement after the User locks
            # were acquired, so it observes any token issuer that committed
            # while the candidate query waited.
            ~active_verification,
        )
        .returning(User.id)
    )
    await session.commit()
    return len(result.scalars().all())


@broker.task(task_name="retention.prune", schedule=[{"cron": "31 4 * * *"}])
@observed_job("retention.prune")
async def retention_prune() -> dict[str, int]:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    now = datetime.now(UTC)
    audit_cutoff = now - timedelta(days=settings.audit_retention_days)
    federation_cutoff = now - timedelta(days=settings.federation_event_retention_days)
    revoked_session_cutoff = now - timedelta(days=7)
    projection_cutoff = now - timedelta(days=7)
    try:
        async with sessionmaker() as session:
            results = {
                "one_time_tokens": await session.execute(
                    delete(OneTimeToken).where(OneTimeToken.expires_at < now)
                ),
                "sessions": await session.execute(
                    delete(Session).where(
                        or_(
                            Session.absolute_expires_at < now,
                            Session.revoked_at < revoked_session_cutoff,
                        )
                    )
                ),
                "audit_log_entries": await session.execute(
                    delete(AuditLogEntry).where(AuditLogEntry.created_at < audit_cutoff)
                ),
                "auth_events": await session.execute(
                    delete(AuthEvent).where(AuthEvent.created_at < audit_cutoff)
                ),
                "guild_events": await session.execute(
                    delete(GuildEvent).where(GuildEvent.created_at < federation_cutoff)
                ),
                "message_projections": await session.execute(
                    delete(MessageProjection).where(
                        MessageProjection.processed_at.is_not(None),
                        MessageProjection.processed_at < projection_cutoff,
                    )
                ),
            }
            history_cleanup = await cleanup_history_transfers(session, now=now)
            await session.commit()
            cleaned = {
                name: cast(CursorResult[Any], result).rowcount or 0
                for name, result in results.items()
            }
            cleaned.update(history_cleanup)
            return cleaned
    finally:
        await engine.dispose()
