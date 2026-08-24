from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from redis.asyncio import Redis
from sqlalchemy import and_, case, delete, exists, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from taskiq import SimpleRetryMiddleware
from taskiq_redis import RedisStreamBroker

from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import (
    channel_payload,
    dm_channel_payload,
    guild_payload,
    render_message_payload,
    user_payload,
)
from app.chat.permissions import get_permissions
from app.core.cache_warmup import warm_identify_cache
from app.core.logging import configure_logging
from app.core.metrics import observed_job
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import (
    Attachment,
    AuditLogEntry,
    AuthEvent,
    Ban,
    Channel,
    DMConversation,
    DMParticipant,
    FederatedHistoryMessage,
    FederationEvent,
    Guild,
    GuildEvent,
    GuildHistoryImport,
    GuildInstanceBan,
    GuildMember,
    GuildNotificationSetting,
    Instance,
    MediaTombstoneSource,
    Message,
    MessageProjection,
    OneTimeToken,
    PushDevice,
    PushRelayDelivery,
    PushRelaySubscription,
    PushWakeOutbox,
    ReadState,
    Session,
    ThreadMember,
    User,
    UserSettings,
)
from app.db.partitions import ensure_message_partitions
from app.db.session import create_engine_and_sessionmaker
from app.email.outbox import cleanup_email_outbox, drain_email_outbox
from app.federation.delivery import (
    cleanup_federation_retention,
    drain_destination,
    due_destinations,
    expire_stale_outbox,
    rearm_failed_media_delete_outbox,
)
from app.federation.dm_storage import (
    dm_authority_history_available,
    dm_history_metadata,
    sweep_federated_dm_replica_cache,
)
from app.federation.guild_media_deletions import (
    repair_guild_media_delete_request_acks,
    rotate_guild_media_delete_requests,
)
from app.federation.guilds import (
    purge_orphaned_replicated_guilds,
    purge_stale_remote_guild_membership_intents,
    replicated_guild_sync_candidates,
    synchronize_guild,
)
from app.federation.history import (
    cleanup_history_transfers,
    purge_ineligible_federated_history,
    request_and_import_history,
    user_facing_history_error,
)
from app.federation.network import FederationNetworkError, ensure_peer, normalize_domain
from app.federation.presence import fanout_presence
from app.federation.replica_storage import (
    purge_orphaned_remote_instances,
    purge_orphaned_remote_users,
)
from app.federation.terminal_rooms import (
    repair_terminal_room_delivery_acks,
    rotate_terminal_room_deletions,
)
from app.federation.users import (
    discover_profile_by_ref_capability,
    refresh_remote_user,
    refresh_remote_user_by_ref,
    unresolved_profile_peer_candidates,
    unresolved_profile_refresh_candidates,
)
from app.media.asset_invalidation import (
    TerminalAssetInvalidation,
    invalidate_terminal_asset_binding,
    invalidate_terminal_digest_binding_batch,
)
from app.media.digest_revocation import (
    TERMINAL_ATTACHMENT_STATUSES,
    try_lock_asset_digest,
    valid_content_digest,
)
from app.media.jobs import (
    enforce_remote_cache_limit,
    process_attachment_record,
    purge_local_attachment,
    purge_remote_attachment_cache,
    retention_sweep,
    sweep_orphan_uploads,
    sweep_staging_objects,
)
from app.media.payloads import attachment_update_payload
from app.media.processing import IMAGE_PIPELINE_VERSION
from app.media.tombstones import (
    lock_media_tombstone_ref,
    queue_media_delete_tombstone,
    queue_terminal_attachment_tombstone,
    terminal_attachment_refs_for_messages,
)
from app.push.client import send_relay_wake
from app.push.relay import (
    PUSH_WAKE_TTL_SECONDS,
    decrypt_provider_token,
    decrypt_wake_secret,
    stable_wake_identifier,
    wake_mac,
)
from app.push.service import decrypt_device_token, fcm_client, relay_fcm_client
from app.push.sync import PushSyncEvent, discard_push_sync, issue_push_sync
from app.search.meili import (
    SearchUnavailable,
    process_search_outbox,
    purge_index_for_encryption_transition,
    reconcile_search_index_state,
    seed_search_backfill,
)
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

THREAD_TYPES = frozenset({10, 11, 12})
THREAD_FLAG_PINNED = 1 << 1

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

HISTORY_RETRY_KEY = "federation:history:retry"
HISTORY_STATUS_KEY_PREFIX = "federation:history:status"
# A terminal safety-policy result must not create a hot retry loop. Rechecking
# after a long interval lets an operator's later configuration change recover
# even if no new guild event happens to wake this member-specific import.
HISTORY_TERMINAL_RECHECK_MS = 30 * 24 * 60 * 60 * 1_000
HISTORY_STATUS_TTL_SECONDS = 35 * 24 * 60 * 60
HISTORY_ENQUEUED_RETRY_MS = 5 * 60 * 1_000


def history_status_key(user_domain: str, user_id: int) -> str:
    return f"{HISTORY_STATUS_KEY_PREFIX}:{user_domain}:{user_id}"


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
        for reference in pending.mention_user_refs[:5_000]:
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
                "unread": last_message_id is None
                or (last_message_id, last_message_domain or "") < (latest.id, latest.origin_domain),
            },
        )
    if settings.push_enabled or settings.push_relay_enabled:
        for _, message in messages:
            await enqueue_best_effort(
                mobile_push_message,
                message.id,
                message.origin_domain,
                0,
            )
    return len(mention_states)


@broker.task(task_name="mobile.push_message", retry_on_error=True, max_retries=4)
@observed_job("mobile.push_message")
async def mobile_push_message(
    message_id: int,
    message_domain: str,
    after_user_id: int = 0,
) -> int:
    """Deliver one bounded page of native push notifications.

    The task is deliberately paged so enabling all-message notifications in a
    large guild never turns one message projection into an unbounded worker
    transaction. Each continuation advances a local-user snowflake cursor.
    """

    settings = get_settings()
    if not (settings.push_enabled or settings.push_relay_enabled):
        return 0
    message_domain = normalize_domain(message_domain)
    if not 0 <= message_id <= (1 << 63) - 1 or not 0 <= after_user_id <= (1 << 63) - 1:
        raise ValueError("invalid push message identity")

    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    delivered = 0
    next_cursor: int | None = None
    invalid_device_ids: list[str] = []
    try:
        async with sessionmaker() as session:
            message = await session.get(Message, (message_id, message_domain))
            if message is None or message.deleted_at is not None:
                return 0
            channel = await session.get(Channel, (message.channel_id, message.channel_domain))
            author = await session.get(User, (message.author_id, message.author_domain))
            if channel is None or author is None:
                return 0

            if channel.type == 1:
                candidate_statement = (
                    select(User.id)
                    .join(
                        DMParticipant,
                        (DMParticipant.user_id == User.id)
                        & (DMParticipant.user_domain == User.origin_domain),
                    )
                    .join(
                        PushDevice,
                        (PushDevice.user_id == User.id)
                        & (PushDevice.user_domain == User.origin_domain),
                    )
                    .where(
                        DMParticipant.conversation_id == channel.id,
                        DMParticipant.conversation_domain == channel.origin_domain,
                        User.origin_domain == settings.domain,
                        User.id > after_user_id,
                        PushDevice.enabled.is_(True),
                    )
                )
                if message.author_domain == settings.domain:
                    candidate_statement = candidate_statement.where(User.id != message.author_id)
                guild = None
            elif channel.guild_id is not None and channel.guild_domain is not None:
                guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
                if guild is None or guild.unavailable:
                    return 0
                candidate_statement = (
                    select(User.id)
                    .join(
                        GuildMember,
                        (GuildMember.user_id == User.id)
                        & (GuildMember.user_domain == User.origin_domain),
                    )
                    .join(
                        PushDevice,
                        (PushDevice.user_id == User.id)
                        & (PushDevice.user_domain == User.origin_domain),
                    )
                    .where(
                        GuildMember.guild_id == guild.id,
                        GuildMember.guild_domain == guild.origin_domain,
                        User.origin_domain == settings.domain,
                        User.id > after_user_id,
                        PushDevice.enabled.is_(True),
                    )
                )
                if message.author_domain == settings.domain:
                    candidate_statement = candidate_statement.where(User.id != message.author_id)
            else:
                return 0

            candidate_ids = list(
                await session.scalars(candidate_statement.distinct().order_by(User.id).limit(251))
            )
            if len(candidate_ids) > 250:
                next_cursor = candidate_ids[249]
                candidate_ids = candidate_ids[:250]
            if not candidate_ids:
                return 0

            mentioned = {
                int(item["id"])
                for item in message.mention_user_refs
                if item.get("origin_domain") == settings.domain
                and str(item.get("id", "")).isdigit()
            }
            devices_to_notify: list[tuple[PushDevice, str, int]] = []

            users = {
                user.id: user
                for user in await session.scalars(
                    select(User).where(
                        User.id.in_(candidate_ids),
                        User.origin_domain == settings.domain,
                    )
                )
            }
            user_preferences = {
                item.user_id: item
                for item in await session.scalars(
                    select(UserSettings).where(
                        UserSettings.user_id.in_(candidate_ids),
                        UserSettings.user_domain == settings.domain,
                    )
                )
            }
            devices_by_user: dict[int, list[PushDevice]] = {}
            for device in await session.scalars(
                select(PushDevice).where(
                    PushDevice.user_id.in_(candidate_ids),
                    PushDevice.user_domain == settings.domain,
                    PushDevice.enabled.is_(True),
                )
            ):
                devices_by_user.setdefault(device.user_id, []).append(device)
            guild_preferences: dict[int, GuildNotificationSetting] = {}
            thread_preferences: dict[int, ThreadMember] = {}
            if guild is not None:
                guild_preferences = {
                    item.user_id: item
                    for item in await session.scalars(
                        select(GuildNotificationSetting).where(
                            GuildNotificationSetting.user_id.in_(candidate_ids),
                            GuildNotificationSetting.user_domain == settings.domain,
                            GuildNotificationSetting.guild_id == guild.id,
                            GuildNotificationSetting.guild_domain == guild.origin_domain,
                        )
                    )
                }
                if channel.type in {10, 11, 12}:
                    thread_preferences = {
                        item.user_id: item
                        for item in await session.scalars(
                            select(ThreadMember).where(
                                ThreadMember.thread_id == channel.id,
                                ThreadMember.thread_domain == channel.origin_domain,
                                ThreadMember.user_id.in_(candidate_ids),
                                ThreadMember.user_domain == settings.domain,
                            )
                        )
                    }

            for user_id in candidate_ids:
                user = users.get(user_id)
                if user is None:
                    continue
                user_settings = user_preferences.get(user_id)
                notification_settings = (
                    user_settings.notification_settings if user_settings is not None else {}
                )
                if notification_settings.get("presence_preference") == "dnd":
                    continue

                is_mention = user_id in mentioned
                if guild is None:
                    if not bool(notification_settings.get("direct_messages", True)):
                        continue
                    android_channel = "kaede_dms"
                else:
                    permissions = await get_permissions(
                        session,
                        redis,
                        guild,
                        user,
                        channel=channel,
                    )
                    if not permissions & Permission.VIEW_CHANNEL:
                        continue
                    preference = guild_preferences.get(user_id)
                    level = preference.level if preference is not None else "mentions"
                    thread_preference = thread_preferences.get(user_id)
                    if (
                        channel.type in {10, 11, 12}
                        and thread_preference is None
                        and not is_mention
                    ):
                        # Guild-wide "all" applies to channels, while thread
                        # reply pushes are subscription/member scoped. A direct
                        # mention may still notify a visible non-member.
                        continue
                    if (
                        thread_preference is not None
                        and thread_preference.notification_level != "inherit"
                    ):
                        level = thread_preference.notification_level
                    if level == "none" or (level == "mentions" and not is_mention):
                        continue
                    if is_mention and not bool(notification_settings.get("mentions", True)):
                        continue
                    android_channel = "kaede_mentions" if is_mention else "kaede_guilds"

                for device in devices_by_user.get(user_id, []):
                    devices_to_notify.append(
                        (
                            device,
                            {
                                "kaede_dms": "direct_message",
                                "kaede_mentions": "mention",
                                "kaede_guilds": "guild_message",
                            }[android_channel],
                            user_id,
                        )
                    )

            # Relay wakes enter a durable home outbox before any external
            # request. Provider tokens never enter this instance.
            event_message_id = message.id
            event_message_domain = message.origin_domain
            direct_notifications: list[tuple[PushDevice, str, int]] = []
            for device, notification_kind, user_id in devices_to_notify:
                if device.transport != "relay":
                    # A home can switch from its legacy/custom FCM transport to
                    # the relay without immediately deleting old registrations.
                    # Ignore those registrations unless direct delivery remains
                    # configured; otherwise a missing Firebase credential would
                    # turn an ordinary message projection into a failed job.
                    if settings.push_enabled:
                        direct_notifications.append((device, notification_kind, user_id))
                    continue
                event_token = await issue_push_sync(
                    redis,
                    PushSyncEvent(
                        device_id=device.id,
                        user_id=user_id,
                        user_domain=settings.domain,
                        message_id=event_message_id,
                        message_domain=event_message_domain,
                        kind=notification_kind,
                    ),
                )
                inserted = await session.scalar(
                    pg_insert(PushWakeOutbox)
                    .values(
                        request_id=stable_wake_identifier(
                            settings,
                            purpose="request",
                            device_id=device.id,
                            message_id=event_message_id,
                            message_domain=event_message_domain,
                            kind=notification_kind,
                        ),
                        device_id=device.id,
                        message_id=event_message_id,
                        message_domain=event_message_domain,
                        kind=notification_kind,
                        event_token=event_token,
                        delivery_id=stable_wake_identifier(
                            settings,
                            purpose="delivery",
                            device_id=device.id,
                            message_id=event_message_id,
                            message_domain=event_message_domain,
                            kind=notification_kind,
                        ),
                        expires_at=datetime.now(UTC) + timedelta(seconds=PUSH_WAKE_TTL_SECONDS),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            PushWakeOutbox.device_id,
                            PushWakeOutbox.message_id,
                            PushWakeOutbox.message_domain,
                            PushWakeOutbox.kind,
                        ]
                    )
                    .returning(PushWakeOutbox.request_id)
                )
                if inserted is None:
                    await discard_push_sync(redis, event_token)
            await session.commit()
            if any(device.transport == "relay" for device, _, _ in devices_to_notify):
                await enqueue_best_effort(push_relay_outbox_sweep)

            # Direct/custom-FCM delivery is retained for separately signed
            # community clients. It is intentionally not the official path.
            if direct_notifications:
                client = fcm_client(settings)
            for device, notification_kind, user_id in direct_notifications:
                event_token = await issue_push_sync(
                    redis,
                    PushSyncEvent(
                        device_id=device.id,
                        user_id=user_id,
                        user_domain=settings.domain,
                        message_id=event_message_id,
                        message_domain=event_message_domain,
                        kind=notification_kind,
                    ),
                )
                try:
                    result = await client.send_sync(
                        decrypt_device_token(device, settings),
                        event_token=event_token,
                        platform=device.platform,
                    )
                except httpx.HTTPError:
                    await discard_push_sync(redis, event_token)
                    continue
                if result.delivered:
                    delivered += 1
                else:
                    await discard_push_sync(redis, event_token)
                    if result.token_invalid:
                        invalid_device_ids.append(device.id)

            if invalid_device_ids:
                await session.execute(
                    update(PushDevice)
                    .where(PushDevice.id.in_(invalid_device_ids))
                    .values(enabled=False, updated_at=func.now())
                )
                await session.commit()
    finally:
        await redis.aclose()
        await engine.dispose()

    if next_cursor is not None:
        await enqueue_best_effort(
            mobile_push_message,
            message_id,
            message_domain,
            next_cursor,
        )
    return delivered


@broker.task(task_name="mobile.push_activity")
@observed_job("mobile.push_activity")
async def mobile_push_activity(
    user_id: int,
    user_domain: str,
    event_id: int,
    event_domain: str,
    kind: str,
    title: str,
    body: str,
    event_ref: str,
    channel_ref: str | None = None,
) -> int:
    """Deliver one server-authored, content-free wake for non-message activity."""

    settings = get_settings()
    if not (settings.push_enabled or settings.push_relay_enabled):
        return 0
    if kind not in {"call", "moderation", "relationship"}:
        raise ValueError("unsupported activity push kind")
    user_domain = normalize_domain(user_domain)
    event_domain = normalize_domain(event_domain)
    if user_domain != settings.domain or not 0 <= user_id <= (1 << 63) - 1:
        return 0
    if not 0 <= event_id <= (1 << 63) - 1:
        raise ValueError("invalid activity event identity")

    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    delivered = 0
    invalid_device_ids: list[str] = []
    try:
        async with sessionmaker() as session:
            devices = list(
                await session.scalars(
                    select(PushDevice).where(
                        PushDevice.user_id == user_id,
                        PushDevice.user_domain == user_domain,
                        PushDevice.enabled.is_(True),
                    )
                )
            )
            now = datetime.now(UTC)
            direct: list[PushDevice] = []
            for device in devices:
                if device.transport != "relay":
                    if settings.push_enabled:
                        direct.append(device)
                    continue
                token = await issue_push_sync(
                    redis,
                    PushSyncEvent(
                        device_id=device.id,
                        user_id=user_id,
                        user_domain=user_domain,
                        message_id=event_id,
                        message_domain=event_domain,
                        kind=kind,
                        title=title[:160],
                        body=body[:500],
                        channel_ref=channel_ref,
                        event_ref=event_ref[:286],
                        sent_at=now.isoformat(),
                    ),
                )
                inserted = await session.scalar(
                    pg_insert(PushWakeOutbox)
                    .values(
                        request_id=stable_wake_identifier(
                            settings,
                            purpose="request",
                            device_id=device.id,
                            message_id=event_id,
                            message_domain=event_domain,
                            kind=kind,
                        ),
                        device_id=device.id,
                        message_id=event_id,
                        message_domain=event_domain,
                        kind=kind,
                        event_token=token,
                        delivery_id=stable_wake_identifier(
                            settings,
                            purpose="delivery",
                            device_id=device.id,
                            message_id=event_id,
                            message_domain=event_domain,
                            kind=kind,
                        ),
                        expires_at=now + timedelta(seconds=PUSH_WAKE_TTL_SECONDS),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            PushWakeOutbox.device_id,
                            PushWakeOutbox.message_id,
                            PushWakeOutbox.message_domain,
                            PushWakeOutbox.kind,
                        ]
                    )
                    .returning(PushWakeOutbox.request_id)
                )
                if inserted is None:
                    await discard_push_sync(redis, token)
            await session.commit()
            if any(device.transport == "relay" for device in devices):
                await enqueue_best_effort(push_relay_outbox_sweep)

            if direct:
                client = fcm_client(settings)
            for device in direct:
                token = await issue_push_sync(
                    redis,
                    PushSyncEvent(
                        device_id=device.id,
                        user_id=user_id,
                        user_domain=user_domain,
                        message_id=event_id,
                        message_domain=event_domain,
                        kind=kind,
                        title=title[:160],
                        body=body[:500],
                        channel_ref=channel_ref,
                        event_ref=event_ref[:286],
                        sent_at=now.isoformat(),
                    ),
                )
                try:
                    result = await client.send_sync(
                        decrypt_device_token(device, settings),
                        event_token=token,
                        platform=device.platform,
                    )
                except httpx.HTTPError:
                    await discard_push_sync(redis, token)
                    continue
                if result.delivered:
                    delivered += 1
                else:
                    await discard_push_sync(redis, token)
                    if result.token_invalid:
                        invalid_device_ids.append(device.id)
            if invalid_device_ids:
                await session.execute(
                    update(PushDevice)
                    .where(PushDevice.id.in_(invalid_device_ids))
                    .values(enabled=False, updated_at=func.now())
                )
                await session.commit()
    finally:
        await redis.aclose()
        await engine.dispose()
    return delivered


@broker.task(task_name="mobile.push_relay_outbox", schedule=[{"cron": "* * * * *"}])
@observed_job("mobile.push_relay_outbox")
async def push_relay_outbox_sweep() -> int:
    """Drain content-free home wakes without keeping a DB lock over HTTP."""

    settings = get_settings()
    if not settings.push_relay_enabled:
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    accepted = 0
    try:
        async with sessionmaker() as session:
            now = datetime.now(UTC)
            rows = list(
                await session.scalars(
                    select(PushWakeOutbox)
                    .where(PushWakeOutbox.next_attempt_at <= now)
                    .order_by(PushWakeOutbox.next_attempt_at, PushWakeOutbox.created_at)
                    .limit(250)
                    .with_for_update(skip_locked=True)
                )
            )
            claimed: list[dict[str, Any]] = []
            for row in rows:
                device = await session.get(PushDevice, row.device_id)
                if row.expires_at <= now or device is None or not device.enabled:
                    if row.event_token is not None:
                        await discard_push_sync(redis, row.event_token)
                    await session.delete(row)
                    continue
                if (
                    device.transport != "relay"
                    or device.relay_subscription_id is None
                    or device.relay_route_id is None
                    or device.relay_wake_secret_encrypted is None
                    or device.relay_origin != settings.push_relay_origin
                    or row.event_token is None
                ):
                    if row.event_token is not None:
                        await discard_push_sync(redis, row.event_token)
                    await session.delete(row)
                    continue
                expires = int(row.expires_at.timestamp())
                claimed.append(
                    {
                        "row": row,
                        "payload": {
                            "version": 2,
                            "request_id": row.request_id,
                            "subscription_id": device.relay_subscription_id,
                            "route_id": device.relay_route_id,
                            "event_token": row.event_token,
                            "delivery_id": row.delivery_id,
                            "expires_at": expires,
                            "priority": "urgent" if row.kind == "call" else "normal",
                            "wake_mac": wake_mac(
                                decrypt_wake_secret(device.relay_wake_secret_encrypted, settings),
                                route_id=device.relay_route_id,
                                event_token=row.event_token,
                                delivery_id=row.delivery_id,
                                expires_at=expires,
                            ),
                        },
                    }
                )
                row.attempts += 1
                # Lease the delivery long enough to prevent a concurrent sweep
                # from issuing the same provider request while this one is in flight.
                row.next_attempt_at = now + timedelta(seconds=60)
            await session.commit()

            for item in claimed:
                row = cast(PushWakeOutbox, item["row"])
                try:
                    response = await send_relay_wake(session, settings, item["payload"])
                except FederationNetworkError as exc:
                    current = await session.get(PushWakeOutbox, row.request_id)
                    if current is not None:
                        current.last_error = str(exc)[:200]
                        await session.commit()
                    continue
                current = await session.get(PushWakeOutbox, row.request_id)
                if current is None:
                    continue
                if response.status_code in {200, 202}:
                    await session.delete(current)
                    accepted += 1
                elif response.status_code == 410:
                    device = await session.get(PushDevice, current.device_id)
                    if device is not None:
                        device.enabled = False
                    if current.event_token is not None:
                        await discard_push_sync(redis, current.event_token)
                    await session.delete(current)
                elif response.status_code == 429:
                    try:
                        delay = max(1, min(600, int(response.headers.get("Retry-After", "30"))))
                    except ValueError:
                        delay = 30
                    current.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                    current.last_error = "relay rate limited"
                elif response.status_code < 500:
                    if current.event_token is not None:
                        await discard_push_sync(redis, current.event_token)
                    await session.delete(current)
                else:
                    current.last_error = f"relay HTTP {response.status_code}"
                await session.commit()
    finally:
        await redis.aclose()
        await engine.dispose()
    return accepted


@broker.task(task_name="mobile.push_relay_provider", schedule=[{"cron": "* * * * *"}])
@observed_job("mobile.push_relay_provider")
async def push_relay_provider_sweep() -> int:
    """Relay-side provider sender; only this worker needs Firebase credentials."""

    settings = get_settings()
    if not settings.push_relay_service_enabled:
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    delivered = 0
    try:
        async with sessionmaker() as session:
            now = datetime.now(UTC)
            rows = list(
                await session.scalars(
                    select(PushRelayDelivery)
                    .where(
                        PushRelayDelivery.state == "pending",
                        PushRelayDelivery.next_attempt_at <= now,
                    )
                    .order_by(PushRelayDelivery.next_attempt_at)
                    .limit(500)
                    .with_for_update(skip_locked=True)
                )
            )
            claimed: list[tuple[str, str]] = []
            for row in rows:
                if row.expires_at <= now:
                    row.state = "expired"
                    continue
                row.attempts += 1
                # This is an in-flight lease, not the retry schedule. A provider
                # call can consume most of the HTTP timeout, so a short
                # exponential value here would allow another worker to send the
                # same wake concurrently. Failures below replace the lease with
                # their actual backoff.
                row.next_attempt_at = now + timedelta(seconds=60)
                claimed.append((row.home_origin, row.request_id))
            await session.commit()
            client = relay_fcm_client(settings)
            for identity in claimed:
                delivery = await session.get(PushRelayDelivery, identity)
                if delivery is None or delivery.state != "pending":
                    continue
                subscription = await session.get(PushRelaySubscription, delivery.subscription_id)
                if (
                    subscription is None
                    or not subscription.enabled
                    or subscription.expires_at <= datetime.now(UTC)
                ):
                    delivery.state = "invalid"
                    await session.commit()
                    continue
                try:
                    result = await client.send_relay(
                        decrypt_provider_token(subscription.provider_token_encrypted, settings),
                        route_id=delivery.route_id,
                        event_token=delivery.event_token,
                        delivery_id=delivery.delivery_id,
                        expires_at=int(delivery.expires_at.timestamp()),
                        wake_mac=delivery.wake_mac,
                        platform=subscription.platform,
                    )
                except httpx.HTTPError as exc:
                    delivery.last_error = str(exc)[:200]
                    delivery.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=min(120, 2 ** min(delivery.attempts, 6))
                    )
                    await session.commit()
                    continue
                if result.delivered:
                    delivery.state = "delivered"
                    delivered += 1
                elif result.token_invalid:
                    delivery.state = "invalid"
                    subscription.enabled = False
                else:
                    delivery.last_error = "provider rejected wake"
                    delivery.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=min(120, 2 ** min(delivery.attempts, 6))
                    )
                await session.commit()
            await session.execute(
                delete(PushRelayDelivery).where(
                    PushRelayDelivery.state.in_(["delivered", "expired", "invalid"]),
                    PushRelayDelivery.created_at < datetime.now(UTC) - timedelta(days=1),
                )
            )
            await session.execute(
                delete(PushRelaySubscription).where(
                    (PushRelaySubscription.expires_at <= datetime.now(UTC))
                    | (
                        PushRelaySubscription.enabled.is_(False)
                        & (
                            PushRelaySubscription.last_seen_at
                            < datetime.now(UTC) - timedelta(days=1)
                        )
                    )
                )
            )
            await session.commit()
    finally:
        await engine.dispose()
    return delivered


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


async def thread_auto_archive_sweep_in_session(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Archive one bounded page using the global Guild -> Channel lock order."""

    current = now or datetime.now(UTC)
    candidate_rows = list(
        (
            await session.execute(
                select(
                    Channel.id,
                    Channel.origin_domain,
                    Channel.guild_id,
                    Channel.guild_domain,
                )
                .join(
                    Guild,
                    (Guild.id == Channel.guild_id) & (Guild.origin_domain == Channel.guild_domain),
                )
                .where(
                    Guild.origin_domain == settings.domain,
                    Guild.unavailable.is_(False),
                    Channel.type.in_(THREAD_TYPES),
                    Channel.unavailable.is_(False),
                    Channel.archived.is_(False),
                    Channel.flags.op("&")(THREAD_FLAG_PINNED) == 0,
                    Channel.last_activity_at
                    + Channel.auto_archive_duration * text("INTERVAL '1 minute'")
                    <= current,
                )
                .order_by(Channel.last_activity_at, Channel.origin_domain, Channel.id)
                .limit(limit)
            )
        ).all()
    )
    if not candidate_rows:
        return 0

    candidates_by_guild: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for thread_id, thread_domain, guild_id, guild_domain in candidate_rows:
        if guild_id is None or guild_domain is None:
            continue
        candidates_by_guild.setdefault((int(guild_id), str(guild_domain)), []).append(
            (int(thread_id), str(thread_domain))
        )

    guilds: dict[tuple[int, str], Guild] = {}
    archived: list[Channel] = []
    for guild_ref in sorted(candidates_by_guild, key=lambda item: (item[1], item[0])):
        # Every ordinary guild mutation locks this same row before its channel.
        # Selecting candidates lock-free and revalidating after the guild lock
        # avoids the sweep's former Channel -> Guild inversion.
        guild = await session.scalar(
            select(Guild)
            .where(
                Guild.id == guild_ref[0],
                Guild.origin_domain == guild_ref[1],
                Guild.unavailable.is_(False),
            )
            .with_for_update()
        )
        if guild is None or guild.origin_domain != settings.domain:
            continue
        actor = await session.get(User, (guild.owner_id, guild.owner_domain))
        if actor is None or not actor.is_local:
            raise RuntimeError("local guild owner is unavailable for thread auto-archive")
        refs = candidates_by_guild[guild_ref]
        due = list(
            await session.scalars(
                select(Channel)
                .where(
                    tuple_(Channel.id, Channel.origin_domain).in_(refs),
                    Channel.guild_id == guild.id,
                    Channel.guild_domain == guild.origin_domain,
                    Channel.type.in_(THREAD_TYPES),
                    Channel.unavailable.is_(False),
                    Channel.archived.is_(False),
                    Channel.flags.op("&")(THREAD_FLAG_PINNED) == 0,
                    Channel.last_activity_at
                    + Channel.auto_archive_duration * text("INTERVAL '1 minute'")
                    <= current,
                )
                .order_by(Channel.last_activity_at, Channel.origin_domain, Channel.id)
                .with_for_update()
            )
        )
        guilds[guild_ref] = guild
        for thread in due:
            thread.archived = True
            thread.archive_timestamp = current
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.channel.update",
                {"channel": federation_channel_state(thread)},
                channel=thread,
            )
            archived.append(thread)

    if not archived:
        await session.rollback()
        return 0

    await session.commit()
    for guild in guilds.values():
        await wake_queued_guild_federation(guild)
    for thread in archived:
        if thread.guild_id is None or thread.guild_domain is None:
            continue
        await publish_dispatch(
            redis,
            guild_topic(str(thread.guild_domain), int(thread.guild_id)),
            "THREAD_UPDATE",
            channel_payload(thread),
        )
    return len(archived)


@broker.task(task_name="threads.auto_archive_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("threads.auto_archive_sweep")
async def thread_auto_archive_sweep() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        async with sessionmaker() as session:
            return await thread_auto_archive_sweep_in_session(session, redis, settings)
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="search.index_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("search.index_sweep")
async def search_index_sweep() -> int:
    """Drain the durable SQL desired-state queue into private Meilisearch."""

    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            if not await reconcile_search_index_state(session, settings):
                return 0
            try:
                await purge_index_for_encryption_transition(session, settings)
                await seed_search_backfill(session, settings)
                return await process_search_outbox(session, settings)
            except SearchUnavailable:
                # The durable queue owns retry state. A routine search outage
                # must not make Taskiq create a parallel retry storm.
                return 0
    finally:
        await engine.dispose()


@broker.task(task_name="federation.dm_cache_sweep", schedule=[{"cron": "*/5 * * * *"}])
@observed_job("federation.dm_cache_sweep")
async def federation_dm_cache_sweep() -> int:
    """Converge reduced rolling-cache targets and notify connected clients."""

    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        dispatches: list[tuple[int, dict[str, object]]] = []
        async with sessionmaker() as session:
            changed = await sweep_federated_dm_replica_cache(session, settings)
            for conversation_id, conversation_domain in changed:
                conversation = await session.get(
                    DMConversation, (conversation_id, conversation_domain)
                )
                channel = await session.get(Channel, (conversation_id, conversation_domain))
                if conversation is None or channel is None:
                    continue
                users = list(
                    await session.scalars(
                        select(User)
                        .join(
                            DMParticipant,
                            (DMParticipant.user_id == User.id)
                            & (DMParticipant.user_domain == User.origin_domain),
                        )
                        .where(
                            DMParticipant.conversation_id == conversation_id,
                            DMParticipant.conversation_domain == conversation_domain,
                        )
                    )
                )
                history = dm_history_metadata(
                    conversation,
                    local_domain=settings.domain,
                    remote_available=await dm_authority_history_available(
                        session, conversation, local_domain=settings.domain
                    ),
                )
                for local_user in users:
                    if local_user.origin_domain != settings.domain or not local_user.is_local:
                        continue
                    dispatches.append(
                        (
                            local_user.id,
                            dm_channel_payload(
                                channel,
                                [
                                    user
                                    for user in users
                                    if (user.id, user.origin_domain)
                                    != (local_user.id, local_user.origin_domain)
                                ],
                                conversation=conversation,
                                history=history,
                            ),
                        )
                    )
            await session.commit()
        for user_id, payload in dispatches:
            await publish_dispatch(
                redis,
                user_topic(settings.domain, user_id),
                "CHANNEL_UPDATE",
                payload,
            )
        return len(changed)
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
                # A delayed task may outlive the membership that created it.
                # Remove both projections so departed guilds cannot retain
                # retry/status entries indefinitely.
                await redis.zrem(
                    HISTORY_RETRY_KEY,
                    f"{guild_id}@{guild_domain}:{user_id}",
                )
                await redis.hdel(
                    history_status_key(settings.domain, user_id),
                    f"{guild_id}@{guild_domain}",
                )
                return 0
            previous_sync_state = (guild.sync_status, guild.sync_error_code)
            await publish_dispatch(
                redis,
                user_topic(settings.domain, user_id),
                "GUILD_HISTORY_SYNC_UPDATE",
                {
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "status": "syncing",
                },
            )
            history_tombstone_wakes: set[str] = set()
            try:
                imported = await request_and_import_history(
                    session,
                    settings,
                    guild,
                    user,
                    tombstone_delivery_wakes=history_tombstone_wakes,
                )
            except Exception as caught:
                for destination in sorted(history_tombstone_wakes):
                    await enqueue_best_effort(federation_deliver, destination)
                exc = user_facing_history_error(caught)
                await session.refresh(guild)
                if (guild.sync_status, guild.sync_error_code) != previous_sync_state:
                    await publish_dispatch(
                        redis,
                        guild_topic(guild.origin_domain, guild.id),
                        "GUILD_UPDATE",
                        guild_payload(guild),
                    )
                status = "retrying" if exc.retryable else "failed"
                payload = {
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "status": status,
                    **exc.dispatch_payload(),
                }
                await publish_dispatch(
                    redis,
                    user_topic(settings.domain, user_id),
                    "GUILD_HISTORY_SYNC_UPDATE",
                    payload,
                )
                retry_member = f"{guild.id}@{guild.origin_domain}:{user_id}"
                await redis.hset(
                    history_status_key(settings.domain, user_id),
                    f"{guild.id}@{guild.origin_domain}",
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                )
                await redis.expire(
                    history_status_key(settings.domain, user_id),
                    HISTORY_STATUS_TTL_SECONDS,
                )
                if exc.retryable:
                    retry_after_ms = max(1_000, int(exc.retry_after_ms or 2_000))
                    retry_at_ms = int(datetime.now(UTC).timestamp() * 1_000) + retry_after_ms
                    await redis.zadd(HISTORY_RETRY_KEY, {retry_member: retry_at_ms})
                else:
                    await redis.zadd(
                        HISTORY_RETRY_KEY,
                        {
                            retry_member: int(datetime.now(UTC).timestamp() * 1_000)
                            + HISTORY_TERMINAL_RECHECK_MS
                        },
                    )
                return 0
            for destination in sorted(history_tombstone_wakes):
                await enqueue_best_effort(federation_deliver, destination)
            await session.refresh(guild)
            if (guild.sync_status, guild.sync_error_code) != previous_sync_state:
                # Replica admission may clear a previous quota pause. Publish
                # that recovery immediately so already-online clients remove
                # their persistent cache-capacity banner without reconnecting.
                await publish_dispatch(
                    redis,
                    guild_topic(guild.origin_domain, guild.id),
                    "GUILD_UPDATE",
                    guild_payload(guild),
                )
            await redis.zrem(
                HISTORY_RETRY_KEY,
                f"{guild.id}@{guild.origin_domain}:{user_id}",
            )
            await redis.hdel(
                history_status_key(settings.domain, user_id),
                f"{guild.id}@{guild.origin_domain}",
            )
            await publish_dispatch(
                redis,
                user_topic(settings.domain, user_id),
                "GUILD_HISTORY_SYNC_UPDATE",
                {
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "status": "ready",
                },
            )
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


@broker.task(task_name="federation.history_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("federation.history_sweep")
async def federation_history_sweep() -> int:
    settings = get_settings()
    if not settings.federation_history_import_enabled:
        return 0
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
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
        now_ms = int(datetime.now(UTC).timestamp() * 1_000)
        for guild_id, guild_domain, user_id in candidates:
            retry_member = f"{guild_id}@{guild_domain}:{user_id}"
            retry_at = await redis.zscore(HISTORY_RETRY_KEY, retry_member)
            if retry_at is not None and retry_at > now_ms:
                continue
            enqueued = await enqueue_best_effort(
                federation_history_sync,
                guild_id,
                guild_domain,
                user_id,
            )
            if enqueued:
                # Hold a short claim while the worker starts. The worker
                # replaces this with the authority's Retry-After on failure or
                # removes it on success, preventing duplicate minute-sweep
                # jobs when an import takes longer than one sweep interval.
                await redis.zadd(
                    HISTORY_RETRY_KEY,
                    {retry_member: now_ms + HISTORY_ENQUEUED_RETRY_MS},
                )
        return len(candidates)
    finally:
        await redis.aclose()
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


async def _publish_terminal_asset_invalidation(
    session: AsyncSession,
    redis: Redis,
    invalidation: TerminalAssetInvalidation,
) -> None:
    """Publish and wake one asset projection only after its verdict commits."""

    if invalidation.user is not None:
        user = invalidation.user
        await session.refresh(user)
        await publish_dispatch(
            redis,
            user_topic(user.origin_domain, user.id),
            "USER_UPDATE",
            user_payload(user),
        )
        for destination in sorted(invalidation.friend_destinations):
            await enqueue_best_effort(federation_deliver, destination)
    if invalidation.guild is not None:
        guild = invalidation.guild
        await session.refresh(guild)
        await wake_queued_guild_federation(guild)
        if invalidation.dispatch_type is None:
            raise RuntimeError("guild asset invalidation has no dispatch type")
        dispatch_payload = (
            guild_payload(guild)
            if invalidation.dispatch_payload is None
            else invalidation.dispatch_payload
        )
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            invalidation.dispatch_type,
            dispatch_payload,
        )


@broker.task(
    task_name="media.terminal_asset_duplicate_purge",
    retry_on_error=True,
    max_retries=5,
)
@observed_job("media.terminal_asset_duplicate_purge")
async def media_terminal_asset_duplicate_purge(
    attachment_id: int,
    origin_domain: str,
    digest: str,
) -> str:
    """Durably stage one clean same-digest public upload for local purge.

    The bounded projection worker cannot safely create a media tombstone while
    it owns digest -> Attachment. This task starts a fresh transaction in the
    canonical media-ref -> Attachment -> try-digest order, revalidates the
    retained terminal evidence, and commits the signed deletion source before
    either federation delivery or physical object purge is woken.
    """

    settings = get_settings()
    if (
        origin_domain != settings.domain
        or not 0 <= attachment_id <= (1 << 63) - 1
        or not valid_content_digest(digest)
    ):
        raise ValueError("invalid terminal duplicate attachment")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    delivery_wakes: set[str] = set()
    try:
        async with sessionmaker() as session:
            await lock_media_tombstone_ref(session, attachment_id, origin_domain)
            attachment = await session.scalar(
                select(Attachment)
                .where(
                    Attachment.id == attachment_id,
                    Attachment.origin_domain == origin_domain,
                )
                .with_for_update()
            )
            if attachment is None:
                return "missing"
            if (
                attachment.content_sha256 != digest
                or attachment.purpose == "attachment"
                or attachment.scan_status not in TERMINAL_ATTACHMENT_STATUSES
                or attachment.deleted_at is not None
            ):
                return "ineligible"
            if attachment.asset_binding is not None:
                # The bounded projection repair must clear the public metadata
                # and binding first. Its durable hourly discovery will retry.
                return "bound"
            if not await try_lock_asset_digest(session, digest):
                # This transaction already owns the media ref and Attachment;
                # waiting behind a digest owner that may need either can
                # deadlock. Taskiq rolls the transaction back and retries.
                raise RuntimeError("terminal duplicate digest fence is busy")
            terminal_evidence = await session.scalar(
                select(Attachment.id)
                .where(
                    Attachment.origin_domain == settings.domain,
                    Attachment.content_sha256 == digest,
                    Attachment.scan_status.in_(TERMINAL_ATTACHMENT_STATUSES),
                )
                .limit(1)
            )
            if terminal_evidence is None:
                return "no_terminal_evidence"
            delivery_wakes.update(
                await queue_terminal_attachment_tombstone(
                    session,
                    settings,
                    attachment,
                    force_authoritative=True,
                )
            )
            await session.commit()
        for destination in sorted(delivery_wakes):
            await enqueue_best_effort(federation_deliver, destination)
        await enqueue_best_effort(media_local_purge, attachment_id, origin_domain)
        return "queued"
    finally:
        await engine.dispose()


@broker.task(
    task_name="media.terminal_asset_digest_repair",
    retry_on_error=True,
    max_retries=5,
)
@observed_job("media.terminal_asset_digest_repair")
async def media_terminal_asset_digest_repair(digest: str) -> int:
    """Boundedly remove projections that share a retained terminal digest."""

    if not valid_content_digest(digest):
        raise ValueError("invalid terminal asset digest")
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis: Redis | None = None
    try:
        async with sessionmaker() as session:
            (
                invalidations,
                duplicate_purge_refs,
                processed,
                more,
            ) = await invalidate_terminal_digest_binding_batch(
                session,
                settings,
                digest,
            )
            await session.commit()
            for attachment_id, origin_domain in duplicate_purge_refs:
                await enqueue_best_effort(
                    media_terminal_asset_duplicate_purge,
                    attachment_id,
                    origin_domain,
                    digest,
                )
            if more and processed:
                await enqueue_best_effort(media_terminal_asset_digest_repair, digest)
            if invalidations:
                redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
                for invalidation in invalidations:
                    await _publish_terminal_asset_invalidation(
                        session,
                        redis,
                        invalidation,
                    )
        # Each transaction processes at most 25 bindings. The retained
        # terminal row makes an omitted/lost wake discoverable by the hourly
        # tombstone sweep, so a fully locked page must not busy-loop here.
        return processed
    finally:
        if redis is not None:
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
            tombstone_destinations: set[str] = set()
            asset_invalidations: list[TerminalAssetInvalidation] = []

            async def queue_tombstone_before_terminal_commit(
                attachment: Attachment,
            ) -> None:
                # Projection invalidation and its signed federation outbox
                # belong to the same transaction as the terminal verdict.
                # Do this before tombstone signer lookup: asset mutations lock
                # User/Guild before Attachment, and the invalidation helper's
                # NOWAIT lock turns an inverse-order collision into a task
                # retry instead of a deadlock.
                asset_invalidation = await invalidate_terminal_asset_binding(
                    session,
                    settings,
                    attachment,
                )
                if asset_invalidation is not None:
                    asset_invalidations.append(asset_invalidation)
                tombstone_destinations.update(
                    await queue_terminal_attachment_tombstone(
                        session,
                        settings,
                        attachment,
                    )
                )

            result = await process_attachment_record(
                session,
                settings,
                attachment_id,
                origin_domain,
                before_terminal_commit=queue_tombstone_before_terminal_commit,
            )
            attachment = await session.get(Attachment, (attachment_id, origin_domain))
            # Terminal verdict state, public projection invalidation, and all
            # signed outbox rows were committed atomically by
            # process_attachment_record before these external notifications.
            for destination in sorted(tombstone_destinations):
                await enqueue_best_effort(federation_deliver, destination)
            if (
                result in TERMINAL_ATTACHMENT_STATUSES
                and attachment is not None
                and valid_content_digest(attachment.content_sha256)
            ):
                await enqueue_best_effort(
                    media_terminal_asset_digest_repair,
                    attachment.content_sha256,
                )
            for asset_invalidation in asset_invalidations:
                await _publish_terminal_asset_invalidation(
                    session,
                    redis,
                    asset_invalidation,
                )
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
                payload = attachment_update_payload(
                    attachment,
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    channel_id=channel.id,
                    channel_domain=channel.origin_domain,
                )
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
                        or_(
                            Attachment.scan_status.in_(("pending", "failed")),
                            and_(
                                Attachment.scan_status == "clean",
                                Attachment.detected_content_type.in_(("image/gif", "image/webp")),
                                Attachment.variants["thumbnail_128"]["processing_version"]
                                .as_integer()
                                .is_distinct_from(IMAGE_PIPELINE_VERSION),
                            ),
                        ),
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


@broker.task(
    task_name="media.terminal_tombstone_sweep",
    schedule=[{"cron": "23 * * * *"}],
    retry_on_error=True,
    max_retries=3,
)
@observed_job("media.terminal_tombstone_sweep")
async def media_terminal_tombstone_sweep(after_attachment_id: int = 0) -> int:
    """Repair durable media invalidation state missed by an interrupted worker.

    The normal verdict path queues the signed tombstone in the verdict
    transaction. This bounded traversal also covers rows created before that
    invariant, ordinary message deletions awaiting object purge, attachments
    whose message binding appeared later, and proxy uploads disclosed before a
    binding ever returned. Persistent source events make repeated hourly passes
    idempotent while allowing newly discovered replica destinations and fresh
    signing-key generations to be added.
    """

    if not 0 <= after_attachment_id <= (1 << 63) - 1:
        raise ValueError("invalid terminal attachment repair cursor")
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    processed = 0
    cursor = after_attachment_id
    redis: Redis | None = None
    try:
        async with sessionmaker() as session:
            current_key_id = await session.scalar(
                select(Instance.current_key_id).where(Instance.is_self.is_(True))
            )
        if not isinstance(current_key_id, str) or not current_key_id:
            raise RuntimeError("local federation signing identity is unavailable")
        # First repair authoritative deletions whose transaction predates (or
        # was interrupted before) creation of the independent source row. A
        # terminal public asset may already have a source row backfilled by
        # the f8 migration, so independently select every still-bound terminal
        # asset and repair its public projection in the same transaction.
        missing_source = ~exists(
            select(MediaTombstoneSource.attachment_id).where(
                MediaTombstoneSource.attachment_id == Attachment.id,
                MediaTombstoneSource.attachment_domain == Attachment.origin_domain,
            )
        )
        authoritative_deletion = or_(
            Attachment.deleted_at.is_not(None),
            exists(
                select(Message.id).where(
                    Message.id == Attachment.message_id,
                    Message.origin_domain == Attachment.message_domain,
                    Message.deleted_at.is_not(None),
                )
            ),
        )
        bound_duplicate = aliased(Attachment)
        duplicate_missing_source = ~exists(
            select(MediaTombstoneSource.attachment_id).where(
                MediaTombstoneSource.attachment_id == bound_duplicate.id,
                MediaTombstoneSource.attachment_domain == bound_duplicate.origin_domain,
            )
        )
        terminal_digest_binding = and_(
            Attachment.content_sha256.is_not(None),
            Attachment.scan_status.in_(TERMINAL_ATTACHMENT_STATUSES),
            exists(
                select(bound_duplicate.id).where(
                    bound_duplicate.origin_domain == settings.domain,
                    bound_duplicate.content_sha256 == Attachment.content_sha256,
                    or_(
                        bound_duplicate.asset_binding.is_not(None),
                        and_(
                            bound_duplicate.purpose != "attachment",
                            bound_duplicate.scan_status == "clean",
                            bound_duplicate.deleted_at.is_(None),
                            duplicate_missing_source,
                        ),
                    ),
                )
            ),
        )
        repair_candidate = or_(
            and_(missing_source, authoritative_deletion),
            and_(
                missing_source,
                Attachment.purpose != "attachment",
                Attachment.scan_status.in_(TERMINAL_ATTACHMENT_STATUSES),
            ),
            terminal_digest_binding,
        )
        while True:
            async with sessionmaker() as session:
                candidates = list(
                    await session.scalars(
                        select(Attachment)
                        .where(
                            Attachment.origin_domain == settings.domain,
                            Attachment.id > cursor,
                            repair_candidate,
                        )
                        .order_by(Attachment.id)
                        .limit(100)
                    )
                )
                if not candidates:
                    break
                delivery_wakes: set[str] = set()
                asset_invalidations: list[TerminalAssetInvalidation] = []
                digest_repair_wakes: set[str] = set()
                repaired = 0
                for candidate in candidates:
                    # Match the normal media lifecycle ordering: advisory ref
                    # fence first, then the Attachment row. Re-evaluate the
                    # candidate under FOR UPDATE so an asset replaced while
                    # this page was being scanned is never cleared by stale
                    # rollout-repair state.
                    await lock_media_tombstone_ref(
                        session,
                        candidate.id,
                        candidate.origin_domain,
                    )
                    attachment = await session.scalar(
                        select(Attachment)
                        .where(
                            Attachment.id == candidate.id,
                            Attachment.origin_domain == candidate.origin_domain,
                            Attachment.origin_domain == settings.domain,
                            repair_candidate,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if attachment is None:
                        continue
                    if (
                        attachment.scan_status in TERMINAL_ATTACHMENT_STATUSES
                        and valid_content_digest(attachment.content_sha256)
                    ):
                        # This traversal already owns Attachment, so it must
                        # never wait behind a bind that may be replacing this
                        # same row under the digest fence.
                        if not await try_lock_asset_digest(
                            session,
                            attachment.content_sha256,
                        ):
                            continue
                        digest_repair_wakes.add(attachment.content_sha256)
                        if attachment.asset_binding is not None:
                            invalidation = await invalidate_terminal_asset_binding(
                                session,
                                settings,
                                attachment,
                            )
                            if invalidation is not None:
                                asset_invalidations.append(invalidation)
                    delivery_wakes.update(
                        await queue_terminal_attachment_tombstone(
                            session,
                            settings,
                            attachment,
                            force_authoritative=(
                                attachment.scan_status in TERMINAL_ATTACHMENT_STATUSES
                            ),
                        )
                    )
                    repaired += 1
                await session.commit()
                for destination in sorted(delivery_wakes):
                    await enqueue_best_effort(federation_deliver, destination)
                for digest in sorted(digest_repair_wakes):
                    await enqueue_best_effort(media_terminal_asset_digest_repair, digest)
                if asset_invalidations:
                    if redis is None:
                        redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
                    for invalidation in asset_invalidations:
                        await _publish_terminal_asset_invalidation(
                            session,
                            redis,
                            invalidation,
                        )
            processed += repaired
            cursor = candidates[-1].id
            if len(candidates) < 100:
                break

        # Key rollover is driven by the bounded source table, not Attachment.
        # It therefore survives hard message/channel/guild cascades and queues
        # exactly one fresh generation to every durable destination.
        cursor = after_attachment_id
        while True:
            async with sessionmaker() as session:
                sources = list(
                    await session.scalars(
                        select(MediaTombstoneSource)
                        .where(
                            MediaTombstoneSource.attachment_domain == settings.domain,
                            MediaTombstoneSource.attachment_id > cursor,
                            or_(
                                MediaTombstoneSource.generation == 0,
                                MediaTombstoneSource.key_id != current_key_id,
                                ~exists(
                                    select(FederationEvent.event_id).where(
                                        FederationEvent.origin_domain
                                        == MediaTombstoneSource.attachment_domain,
                                        FederationEvent.event_id == MediaTombstoneSource.event_id,
                                    )
                                ),
                            ),
                        )
                        .order_by(MediaTombstoneSource.attachment_id)
                        .limit(100)
                    )
                )
                if not sources:
                    break
                rotation_wakes: set[str] = set()
                for source in sources:
                    rotation_wakes.update(
                        await queue_media_delete_tombstone(
                            session,
                            settings,
                            attachment_id=source.attachment_id,
                            attachment_domain=source.attachment_domain,
                            destinations=set(),
                        )
                    )
                await session.commit()
            for destination in sorted(rotation_wakes):
                await enqueue_best_effort(federation_deliver, destination)
            processed += len(sources)
            cursor = sources[-1].attachment_id
            if len(sources) < 100:
                break

        # The source row is also a durable local-object deletion intent. This
        # repairs a lost best-effort purge wake after ordinary message or hard
        # guild deletion without rescanning already terminal Attachment rows.
        cursor = after_attachment_id
        while True:
            async with sessionmaker() as session:
                purge_refs = (
                    await session.execute(
                        select(Attachment.id, Attachment.origin_domain)
                        .join(
                            MediaTombstoneSource,
                            (MediaTombstoneSource.attachment_id == Attachment.id)
                            & (MediaTombstoneSource.attachment_domain == Attachment.origin_domain),
                        )
                        .where(
                            Attachment.origin_domain == settings.domain,
                            Attachment.id > cursor,
                            Attachment.deleted_at.is_(None),
                        )
                        .order_by(Attachment.id)
                        .limit(100)
                    )
                ).all()
            if not purge_refs:
                break
            for attachment_id, origin_domain in purge_refs:
                await enqueue_best_effort(media_local_purge, attachment_id, origin_domain)
            processed += len(purge_refs)
            cursor = purge_refs[-1][0]
            if len(purge_refs) < 100:
                break
        return processed
    finally:
        if redis is not None:
            await redis.aclose()
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


@broker.task(task_name="media.cache_gc", schedule=[{"cron": "*/5 * * * *"}])
@observed_job("media.cache_gc")
async def media_cache_gc() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await enforce_remote_cache_limit(session, settings)
    finally:
        await engine.dispose()


async def moderation_expiry_sweep_in_session(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Expire sanctions without making authorization depend on this scheduler."""

    current = now or datetime.now(UTC)
    expired_bans = int(
        getattr(
            await session.execute(delete(Ban).where(Ban.expires_at <= current)),
            "rowcount",
            0,
        )
        or 0
    )
    expired_instance_bans = int(
        getattr(
            await session.execute(
                delete(GuildInstanceBan).where(GuildInstanceBan.expires_at <= current)
            ),
            "rowcount",
            0,
        )
        or 0
    )
    members = list(
        await session.scalars(
            select(GuildMember)
            .join(
                Guild,
                (Guild.id == GuildMember.guild_id)
                & (Guild.origin_domain == GuildMember.guild_domain),
            )
            .where(
                Guild.origin_domain == settings.domain,
                GuildMember.timeout_indefinite.is_(False),
                GuildMember.timeout_until <= current,
            )
            .order_by(GuildMember.timeout_until)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
    )
    touched: list[tuple[Guild, GuildMember]] = []
    for member in members:
        guild = await session.scalar(
            select(Guild)
            .where(
                Guild.id == member.guild_id,
                Guild.origin_domain == member.guild_domain,
            )
            .with_for_update()
        )
        if guild is None:
            continue
        owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or not owner.is_local:
            raise RuntimeError("local guild owner is unavailable for timeout expiry")
        member.timeout_until = None
        member.timeout_indefinite = False
        member.timeout_reason = None
        member.member_version += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            owner,
            "guild.member.update",
            {
                "member": {
                    "user": {
                        "id": str(member.user_id),
                        "origin_domain": member.user_domain,
                    },
                    "nickname": member.nickname,
                    "timeout_until": None,
                    "timeout_indefinite": False,
                    "member_version": str(member.member_version),
                }
            },
            snapshot_required=True,
        )
        touched.append((guild, member))
    purged_replica_private_state = await purge_remote_member_private_state(session, settings)
    await session.commit()
    for guild, member in touched:
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            {
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "user_id": str(member.user_id),
                "user_domain": member.user_domain,
                "timeout_until": None,
                "timeout_indefinite": False,
                "timeout_reason": None,
            },
        )
    return {
        "bans": expired_bans,
        "instance_bans": expired_instance_bans,
        "timeouts": len(touched),
        "replica_private_state": purged_replica_private_state,
    }


async def purge_remote_member_private_state(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 500,
) -> int:
    """Boundedly erase private authority state retained by older replicas."""

    members = list(
        await session.scalars(
            select(GuildMember)
            .join(
                Guild,
                (Guild.id == GuildMember.guild_id)
                & (Guild.origin_domain == GuildMember.guild_domain),
            )
            .where(
                Guild.origin_domain != settings.domain,
                or_(GuildMember.timeout_reason.is_not(None), GuildMember.voice_flags != 0),
            )
            .order_by(GuildMember.guild_domain, GuildMember.guild_id, GuildMember.user_id)
            .limit(max(1, min(limit, 500)))
            .with_for_update(skip_locked=True)
        )
    )
    for member in members:
        member.timeout_reason = None
        member.voice_flags = 0
    if members:
        await session.flush()
    return len(members)


@broker.task(task_name="moderation.expiry_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("moderation.expiry_sweep")
async def moderation_expiry_sweep() -> dict[str, int]:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        async with sessionmaker() as session:
            return await moderation_expiry_sweep_in_session(session, redis, settings)
    finally:
        await redis.aclose()
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
            admitted = await session.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        func.hashtextextended(f"kaede-guild-sync:{origin}:{guild_id}", 0)
                    )
                )
            )
            if admitted is not True:
                # Duplicate gap/sweep wakeups coalesce without pinning a DB
                # connection behind a slow peer. The active sync or next sweep
                # retains responsibility for convergence.
                return 0
            guild = await session.get(Guild, (guild_id, origin))
            if guild is None:
                return 0
            async with asyncio.timeout(30):
                messages = await synchronize_guild(session, settings, guild)
            tombstone_destinations: set[str] = set()
            terminal_attachment_refs = await terminal_attachment_refs_for_messages(
                session,
                settings,
                {(message.id, message.origin_domain) for message in messages},
            )
            for attachment_ref in terminal_attachment_refs:
                terminal_attachment = await session.get(Attachment, attachment_ref)
                if terminal_attachment is not None:
                    tombstone_destinations.update(
                        await queue_terminal_attachment_tombstone(
                            session,
                            settings,
                            terminal_attachment,
                        )
                    )
            await session.commit()
            for destination in sorted(tombstone_destinations):
                await enqueue_best_effort(federation_deliver, destination)
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
            await repair_terminal_room_delivery_acks(session)
            await repair_guild_media_delete_request_acks(session)
            terminal_room_wakes = await rotate_terminal_room_deletions(
                session,
                settings,
            )
            guild_media_delete_wakes = await rotate_guild_media_delete_requests(
                session,
                settings,
            )
            await expire_stale_outbox(session, settings, redis)
            rearmed = await rearm_failed_media_delete_outbox(session)
            await session.commit()
            destinations = sorted(
                terminal_room_wakes
                | guild_media_delete_wakes
                | rearmed
                | set(await due_destinations(session))
            )
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
                (await session.execute(replicated_guild_sync_candidates(settings.domain)))
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


@broker.task(task_name="federation.user_ref_refresh")
@observed_job("federation.user_ref_refresh")
async def federation_user_ref_refresh(user_id: int, domain: str) -> bool:
    """Resolve an opaque history identity directly from its authoritative home."""

    settings = get_settings()
    domain = normalize_domain(domain)
    if domain == settings.domain or not 0 <= user_id < 1 << 63:
        raise ValueError("invalid remote profile reference refresh target")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    refresh_key = f"federation:user-ref-refresh:{domain}:{user_id}"
    try:
        async with sessionmaker() as session:
            return (
                await refresh_remote_user_by_ref(session, settings, redis, user_id, domain)
                is not None
            )
    finally:
        await redis.delete(refresh_key)
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.user_ref_refresh_sweep", schedule=[{"cron": "* * * * *"}])
@observed_job("federation.user_ref_refresh_sweep")
async def federation_user_ref_refresh_sweep() -> int:
    """Queue a bounded, deduplicated batch of unresolved profile proofs."""

    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    queued = 0
    try:
        async with sessionmaker() as session:
            candidates = await unresolved_profile_refresh_candidates(session, settings)
            peer_candidates = await unresolved_profile_peer_candidates(session, settings)
        for user_id, domain in candidates:
            key = f"federation:user-ref-refresh:{domain}:{user_id}"
            if not await redis.set(key, "1", ex=15 * 60, nx=True):
                continue
            if await enqueue_best_effort(federation_user_ref_refresh, user_id, domain):
                queued += 1
            else:
                await redis.delete(key)
        for domain in peer_candidates:
            key = f"federation:profile-capability-refresh:{domain}"
            if not await redis.set(key, "1", ex=60 * 60, nx=True):
                continue
            if not await enqueue_best_effort(federation_profile_capability_refresh, domain):
                await redis.delete(key)
        return queued
    finally:
        await redis.aclose()
        await engine.dispose()


@broker.task(task_name="federation.profile_capability_refresh")
@observed_job("federation.profile_capability_refresh")
async def federation_profile_capability_refresh(domain: str) -> bool:
    """Slowly rediscover legacy peers that may have upgraded profile lookup."""

    settings = get_settings()
    domain = normalize_domain(domain)
    if domain == settings.domain:
        raise ValueError("invalid remote profile capability refresh target")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            supported = await discover_profile_by_ref_capability(session, settings, domain)
            await session.commit()
            return supported
    finally:
        await engine.dispose()


@broker.task(task_name="federation.self_moderation_capability_refresh")
@observed_job("federation.self_moderation_capability_refresh")
async def federation_self_moderation_capability_refresh(domain: str) -> bool:
    """Rediscover one active-timeout peer after a rolling upgrade.

    The queued argument is only the peer domain. It deliberately carries no
    affected user, guild, timeout, or reason metadata.
    """

    settings = get_settings()
    domain = normalize_domain(domain)
    if domain == settings.domain:
        raise ValueError("invalid remote self-moderation capability refresh target")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            try:
                peer = await ensure_peer(session, settings, domain, force=True)
                supported = "member-self-moderation/1" in (peer.capabilities or [])
            except (FederationNetworkError, RuntimeError):
                supported = False
            await session.commit()
            return supported
    finally:
        await engine.dispose()


async def cleanup_federation_retention_cycle(
    session: AsyncSession,
    settings: Settings,
) -> int:
    """Clean protocol records and inaccessible replica data in one job cycle."""

    cleaned = await cleanup_federation_retention(session, settings)
    cleaned += await purge_orphaned_replicated_guilds(session, settings)
    cleaned += await purge_orphaned_remote_users(session, settings)
    # User deletes autoflush before the namespace anti-reference query, so a
    # third-party Instance whose final identity disappeared this cycle can be
    # collected without waiting another day.
    cleaned += await purge_orphaned_remote_instances(session, settings)
    cleaned += await purge_stale_remote_guild_membership_intents(session)
    await session.commit()
    return cleaned


@broker.task(task_name="federation.retention", schedule=[{"cron": "17 3 * * *"}])
@observed_job("federation.retention")
async def federation_retention() -> int:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            return await cleanup_federation_retention_cycle(session, settings)
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
