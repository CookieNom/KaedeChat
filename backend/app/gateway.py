from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit

import structlog
from anyio import CapacityLimiter, EndOfStream, WouldBlock
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import AccessGrant, AccessTokenStore
from app.chat.events import guild_topic, publish_ephemeral, publish_presence, user_topic
from app.chat.payloads import (
    channel_payload,
    dm_channel_payload,
    guild_payload,
    member_payload,
    user_payload,
)
from app.chat.permissions import get_permissions
from app.chat.presence import (
    PRESENCE_TTL_SECONDS,
    broadcast_presence_preference,
)
from app.chat.presence import (
    SET_PRESENCE_SCRIPT as SET_PRESENCE_SCRIPT,
)
from app.chat.presence import (
    set_presence_state as set_presence_state,
)
from app.core.cache_warmup import cache_is_ready, maintain_cache_readiness, warm_identify_cache
from app.core.close_codes import GatewayCloseCode
from app.core.gateway_ops import PROTOCOL_VERSION, GatewayOp
from app.core.logging import configure_logging
from app.core.permissions import Permission
from app.core.proxy import resolve_client_ip
from app.core.settings import get_settings
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityReference, validate_entity_reference
from app.db.models import (
    Channel,
    DMConversation,
    DMParticipant,
    Guild,
    GuildMember,
    MemberRole,
    ReadState,
    User,
    UserSettings,
)
from app.db.models import Session as AuthSession
from app.db.session import create_engine_and_sessionmaker
from app.federation.dm_storage import (
    dm_authority_history_available,
    dm_history_metadata,
)
from app.tasks import federation_presence_fanout, history_status_key
from app.voice.rooms import parse_room_name, participant_identity
from app.voice.state import update_self_flags

settings = get_settings()
configure_logging(settings.log_level)
log = structlog.get_logger()
HEARTBEAT_INTERVAL_MS = 41_250
SESSION_TTL_SECONDS = 3600
SESSION_PROGRESS_HISTORY = 64
PREAUTH_CONNECTION_LIMIT = 128
PREAUTH_REDIS_TIMEOUT_SECONDS = 3.0
CLIENT_OP_LIMIT = 120
CLIENT_OP_WINDOW_SECONDS = 60.0
AUTH_REVALIDATION_SECONDS = 30.0
PRESENCE_REAPER_LEASE_SECONDS = 15
CONNECTION_OWNER_TTL_SECONDS = 90
USER_SESSION_LIMIT = 8
presence_fanout_tasks: set[asyncio.Task[bool]] = set()


def schedule_presence_fanout(user: User, status: str, generation: int) -> None:
    task = asyncio.create_task(
        enqueue_best_effort(
            federation_presence_fanout,
            user.id,
            user.origin_domain,
            status,
            generation,
        ),
        name=f"presence-fanout:{user.origin_domain}:{user.id}",
    )
    presence_fanout_tasks.add(task)

    def finished(completed: asyncio.Task[bool]) -> None:
        presence_fanout_tasks.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            log.warning("presence_fanout_failed", error_type=type(completed.exception()).__name__)

    task.add_done_callback(finished)


async def visible_presence_status(redis: Redis, user: User) -> str | None:
    raw = await redis.get(f"presence:{user.origin_domain}:{user.id}")
    if not raw:
        return None
    try:
        state = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    status = state.get("status") if isinstance(state, dict) else None
    if status == "invisible":
        return "offline"
    return str(status) if status in {"online", "idle", "dnd"} else None


async def current_presence_preference(
    sessionmaker: async_sessionmaker[AsyncSession], redis: Redis, user: User
) -> str:
    raw = await redis.get(f"presence:{user.origin_domain}:{user.id}")
    if raw:
        try:
            state = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            state = None
        status = state.get("status") if isinstance(state, dict) else None
        if status in {"online", "idle", "dnd", "invisible"}:
            return str(status)
    async with sessionmaker() as session:
        notification_settings = await session.scalar(
            select(UserSettings.notification_settings).where(
                UserSettings.user_id == user.id,
                UserSettings.user_domain == user.origin_domain,
            )
        )
    if isinstance(notification_settings, dict):
        preference = notification_settings.get("presence_preference")
        if preference in {"online", "idle", "dnd", "invisible"}:
            return str(preference)
    return "online"


IDENTIFY_LIMIT_SCRIPT = """
if redis.call('GET', KEYS[3]) ~= 'ready' then return {0, 1000, 0} end
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local function refill(key, rate, burst)
    local capacity = burst * 1000
    local tokens = tonumber(redis.call('HGET', key, 'tokens') or capacity)
    local updated = tonumber(redis.call('HGET', key, 'updated_ms') or now_ms)
    tokens = math.min(capacity, tokens + math.floor(math.max(0, now_ms - updated) * rate))
    return tokens, capacity
end
local client_tokens, client_capacity = refill(KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]))
local global_tokens, global_capacity = refill(KEYS[2], tonumber(ARGV[3]), tonumber(ARGV[4]))
local client_retry = math.ceil(math.max(0, 1000 - client_tokens) / tonumber(ARGV[1]))
local global_retry = math.ceil(math.max(0, 1000 - global_tokens) / tonumber(ARGV[3]))
local allowed = client_tokens >= 1000 and global_tokens >= 1000
if allowed then
    client_tokens = client_tokens - 1000
    global_tokens = global_tokens - 1000
end
redis.call('HSET', KEYS[1], 'tokens', client_tokens, 'updated_ms', now_ms)
redis.call('HSET', KEYS[2], 'tokens', global_tokens, 'updated_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], math.max(2000, math.ceil(client_capacity / tonumber(ARGV[1])) * 2))
redis.call('PEXPIRE', KEYS[2], math.max(2000, math.ceil(global_capacity / tonumber(ARGV[3])) * 2))
return {allowed and 1 or 0, math.max(client_retry, global_retry), math.floor(global_tokens / 1000)}
"""

CLAIM_USER_SESSION_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[2]))
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[2], 'reserved', '1')
redis.call('EXPIRE', KEYS[2], 15)
redis.call('ZADD', KEYS[1], tonumber(ARGV[2]) + 15000, ARGV[3])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return 1
"""

RENEW_PRESENCE_SCRIPT = """
local raw = redis.call('GET', KEYS[2])
if not raw then return 0 end
local state = cjson.decode(raw)
local generation = redis.call('INCR', KEYS[1])
state['generation'] = generation
state['expires_at'] = tonumber(ARGV[1])
state['claim_until'] = nil
redis.call('SET', KEYS[2], cjson.encode(state))
redis.call('ZADD', KEYS[3], ARGV[1], ARGV[2])
return generation
"""

CLAIM_EXPIRED_PRESENCE_SCRIPT = """
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[2]) then return 0 end
local raw = redis.call('GET', KEYS[1])
if not raw then
    redis.call('ZREM', KEYS[2], ARGV[1])
    return 0
end
local state = cjson.decode(raw)
local expires_at = tonumber(state['expires_at'] or score)
if expires_at > tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[2], expires_at, ARGV[1])
    return 0
end
local generation = redis.call('INCR', KEYS[3])
state['generation'] = generation
state['claim_until'] = tonumber(ARGV[2]) + tonumber(ARGV[3])
redis.call('SET', KEYS[1], cjson.encode(state))
redis.call('ZADD', KEYS[2], state['claim_until'], ARGV[1])
return generation
"""

FINALIZE_EXPIRED_PRESENCE_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    redis.call('ZREM', KEYS[2], ARGV[1])
    return 1
end
local state = cjson.decode(raw)
if tonumber(state['generation'] or 0) ~= tonumber(ARGV[2]) then return 0 end
if tonumber(state['expires_at'] or 0) > tonumber(ARGV[3]) then return 0 end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""

RENEW_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

RELEASE_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class ConnectionOpLimiter:
    """A monotonic sliding-window limiter scoped to one WebSocket."""

    def __init__(self, limit: int = CLIENT_OP_LIMIT, window: float = CLIENT_OP_WINDOW_SECONDS):
        self.limit = limit
        self.window = window
        self._admitted: deque[float] = deque()

    def admit(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window
        while self._admitted and self._admitted[0] <= cutoff:
            self._admitted.popleft()
        if len(self._admitted) >= self.limit:
            return False
        self._admitted.append(current)
        return True


class PreAuthAdmission:
    """Fail-fast, process-local capacity for incomplete WebSocket handshakes."""

    def __init__(self, limit: int = PREAUTH_CONNECTION_LIMIT) -> None:
        if limit < 1:
            raise ValueError("pre-authentication connection limit must be positive")
        self._limiter = CapacityLimiter(limit)

    def try_acquire(self, connection: object) -> bool:
        try:
            self._limiter.acquire_on_behalf_of_nowait(connection)
        except WouldBlock:
            return False
        return True

    def release(self, connection: object) -> None:
        self._limiter.release_on_behalf_of(connection)


class GatewaySubscriptionHub:
    """One process-wide Redis subscription fanout with per-socket queues."""

    def __init__(self, redis: Redis, *, queue_size: int = 1000) -> None:
        self._pubsub = redis.pubsub()
        self._subscribers: dict[str, set[GatewaySubscription]] = {}
        self._io_lock = asyncio.Lock()
        self._queue_size = queue_size
        self._reader: asyncio.Task[None] | None = None
        self._closed = False
        self._has_channels = asyncio.Event()

    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._run(), name="gateway-subscription-hub")

    async def open(self) -> GatewaySubscription:
        if self._closed:
            raise RuntimeError("gateway subscription hub is closed")
        return GatewaySubscription(self, self._queue_size)

    async def subscribe(self, subscriber: GatewaySubscription, channels: tuple[str, ...]) -> None:
        if subscriber.closed:
            raise RuntimeError("gateway subscription is closed")
        async with self._io_lock:
            new_channels: list[str] = []
            for channel in channels:
                listeners = self._subscribers.setdefault(channel, set())
                if not listeners:
                    new_channels.append(channel)
                listeners.add(subscriber)
                subscriber.channels.add(channel)
            if new_channels:
                await self._pubsub.subscribe(*new_channels)
                self._has_channels.set()

    async def unsubscribe(self, subscriber: GatewaySubscription, channels: tuple[str, ...]) -> None:
        async with self._io_lock:
            unused: list[str] = []
            for channel in channels:
                listeners = self._subscribers.get(channel)
                if listeners is None:
                    continue
                listeners.discard(subscriber)
                subscriber.channels.discard(channel)
                if not listeners:
                    self._subscribers.pop(channel, None)
                    unused.append(channel)
            if unused:
                await self._pubsub.unsubscribe(*unused)
            if not self._subscribers:
                self._has_channels.clear()

    async def close_subscription(self, subscriber: GatewaySubscription) -> None:
        if subscriber.closed:
            return
        subscriber.closed = True
        await self.unsubscribe(subscriber, tuple(subscriber.channels))

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self._has_channels.wait()
                async with self._io_lock:
                    message = await self._pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.2
                    )
                    listeners = (
                        tuple(self._subscribers.get(str(message.get("channel")), ()))
                        if isinstance(message, dict) and message.get("type") == "message"
                        else ()
                    )
                if not listeners:
                    await asyncio.sleep(0)
                    continue
                rendered = cast(dict[str, Any], message)
                for subscriber in listeners:
                    subscriber.deliver(rendered)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("gateway_subscription_hub_failed")
                await asyncio.sleep(0.25)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
        async with self._io_lock:
            subscribers = {item for listeners in self._subscribers.values() for item in listeners}
            for subscriber in subscribers:
                subscriber.closed = True
                subscriber.channels.clear()
            self._subscribers.clear()
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]


class GatewaySubscription:
    def __init__(self, hub: GatewaySubscriptionHub, queue_size: int) -> None:
        self._hub = hub
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.channels: set[str] = set()
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        await self._hub.subscribe(self, channels)

    async def unsubscribe(self, *channels: str) -> None:
        await self._hub.unsubscribe(self, channels)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = True,
        timeout: float = 0,  # noqa: ASYNC109 - mirrors redis-py's PubSub contract
    ) -> dict[str, Any] | None:
        del ignore_subscribe_messages
        if self.closed:
            return None
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    def deliver(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait({"type": "overflow"})

    async def aclose(self) -> None:
        await self._hub.close_subscription(self)


async def reject_preauth_connection(websocket: WebSocket) -> None:
    """Deny a handshake with 429 when the ASGI denial extension is available."""

    try:
        await websocket.send_denial_response(
            Response(status_code=429, headers={"Retry-After": "1"})
        )
    except RuntimeError:
        # ASGI servers without the denial-response extension render a close
        # before accept as an HTTP 403. Either path avoids allocating a live
        # WebSocket or waiting for capacity.
        await websocket.close(code=GatewayCloseCode.RATE_LIMITED)


async def renew_presence_state(redis: Redis, user: User) -> int:
    expires_at = int(time.time()) + PRESENCE_TTL_SECONDS
    handle = f"{user.origin_domain}:{user.id}"
    result = await cast(
        Awaitable[object],
        redis.eval(
            RENEW_PRESENCE_SCRIPT,
            3,
            f"presence:generation:{handle}",
            f"presence:{handle}",
            "presence:expirations",
            str(expires_at),
            handle,
        ),
    )
    return int(cast(int | str, result))


async def claim_expired_presence(redis: Redis, handle: str, now: int) -> int:
    result = await cast(
        Awaitable[object],
        redis.eval(
            CLAIM_EXPIRED_PRESENCE_SCRIPT,
            3,
            f"presence:{handle}",
            "presence:expirations",
            f"presence:generation:{handle}",
            handle,
            str(now),
            str(PRESENCE_REAPER_LEASE_SECONDS),
        ),
    )
    return int(cast(int | str, result))


async def finalize_expired_presence(redis: Redis, handle: str, generation: int, now: int) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            FINALIZE_EXPIRED_PRESENCE_SCRIPT,
            2,
            f"presence:{handle}",
            "presence:expirations",
            handle,
            str(generation),
            str(now),
        ),
    )
    return bool(result)


async def renew_reaper_lease(redis: Redis, owner: str) -> bool:
    return await renew_owned_lease(
        redis, "gateway:presence-reaper", owner, PRESENCE_REAPER_LEASE_SECONDS
    )


async def renew_owned_lease(redis: Redis, key: str, owner: str, ttl_seconds: int) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            RENEW_LEASE_SCRIPT,
            1,
            key,
            owner,
            str(ttl_seconds),
        ),
    )
    return bool(result)


async def release_reaper_lease(redis: Redis, owner: str) -> None:
    await release_owned_lease(redis, "gateway:presence-reaper", owner)


async def release_owned_lease(redis: Redis, key: str, owner: str) -> None:
    await cast(
        Awaitable[object],
        redis.eval(
            RELEASE_LEASE_SCRIPT,
            1,
            key,
            owner,
        ),
    )


async def presence_reaper(redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    owner = secrets.token_urlsafe(16)
    while True:
        leader = False
        try:
            leader = bool(
                await redis.set(
                    "gateway:presence-reaper",
                    owner,
                    ex=PRESENCE_REAPER_LEASE_SECONDS,
                    nx=True,
                )
            )
            if leader:
                now = int(time.time())
                expired = await redis.zrangebyscore("presence:expirations", min=0, max=now)
                for raw_handle in expired:
                    if not await renew_reaper_lease(redis, owner):
                        break
                    handle = (
                        raw_handle.decode("utf-8")
                        if isinstance(raw_handle, bytes)
                        else str(raw_handle)
                    )
                    domain, user_id_text = handle.rsplit(":", 1)
                    user_id = int(user_id_text)
                    generation = await claim_expired_presence(redis, handle, now)
                    if generation <= 0:
                        continue
                    async with sessionmaker() as session:
                        guilds = list(
                            await session.scalars(
                                select(Guild)
                                .join(
                                    GuildMember,
                                    (GuildMember.guild_id == Guild.id)
                                    & (GuildMember.guild_domain == Guild.origin_domain),
                                )
                                .where(
                                    GuildMember.user_id == user_id,
                                    GuildMember.user_domain == domain,
                                    Guild.unavailable.is_(False),
                                )
                            )
                        )
                    still_leader = await renew_reaper_lease(redis, owner)
                    delivered = True
                    for guild in guilds:
                        projected = await publish_presence(
                            redis,
                            guild_topic(guild.origin_domain, guild.id),
                            {
                                "user_id": str(user_id),
                                "user_domain": domain,
                                "status": "offline",
                            },
                            user_domain=domain,
                            user_id=user_id,
                            generation=generation,
                        )
                        delivered = delivered and projected
                    if delivered and await finalize_expired_presence(
                        redis, handle, generation, now
                    ):
                        async with sessionmaker() as session:
                            user = await session.get(User, (user_id, domain))
                        if user is not None:
                            schedule_presence_fanout(user, "offline", generation)
                    if not still_leader:
                        break
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("presence_reaper_failed")
            await asyncio.sleep(5)
        finally:
            if leader:
                with suppress(Exception):
                    await release_reaper_lease(redis, owner)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.redis = redis
    app.state.connections = set()
    app.state.preauth_admission = PreAuthAdmission()
    await warm_identify_cache(redis, sessionmaker, settings)
    subscription_hub = GatewaySubscriptionHub(redis)
    subscription_hub.start()
    app.state.subscription_hub = subscription_hub
    reaper = asyncio.create_task(presence_reaper(redis, sessionmaker))
    cache_guard = asyncio.create_task(
        maintain_cache_readiness(redis, sessionmaker, settings), name="gateway-cache-guard"
    )
    try:
        yield
    finally:
        connections = cast(set[WebSocket], app.state.connections)
        for websocket in list(connections):
            with suppress(RuntimeError):
                await websocket.send_json({"op": GatewayOp.RECONNECT, "d": None})
                await websocket.close(code=1001)
        reaper.cancel()
        cache_guard.cancel()
        for task in list(presence_fanout_tasks):
            task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
        with suppress(asyncio.CancelledError):
            await cache_guard
        if presence_fanout_tasks:
            await asyncio.gather(*list(presence_fanout_tasks), return_exceptions=True)
        await subscription_hub.aclose()
        await redis.aclose()
        await engine.dispose()


app = FastAPI(title="Kaede Chat Gateway", version="0.1.0", lifespan=lifespan)


@app.get("/health/live", include_in_schema=False)
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def ready(response: Response) -> dict[str, str]:
    redis = cast(Redis, app.state.redis)
    if not await cache_is_ready(redis):
        response.status_code = 503
        return {"status": "warming"}
    return {"status": "ready"}


async def identify(
    sessionmaker: async_sessionmaker[AsyncSession], redis: Redis, token: str
) -> tuple[User, list[Guild], list[ReadState], list[dict[str, object]], list[str]] | None:
    grant = await AccessTokenStore(redis, settings.access_token_ttl_seconds).get(token)
    if grant is None:
        return None
    async with sessionmaker() as session:
        now = datetime.now(UTC)
        user = await session.scalar(
            select(User)
            .join(AuthSession, AuthSession.id == grant.session_id)
            .where(
                User.id == grant.user_id,
                User.origin_domain == grant.user_domain,
                User.disabled_at.is_(None),
                AuthSession.id == grant.session_id,
                AuthSession.user_id == grant.user_id,
                AuthSession.user_domain == grant.user_domain,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
                AuthSession.absolute_expires_at > now,
            )
        )
        if user is None:
            return None
        guilds = list(
            await session.scalars(
                select(Guild)
                .join(
                    GuildMember,
                    (GuildMember.guild_id == Guild.id)
                    & (GuildMember.guild_domain == Guild.origin_domain),
                )
                .where(
                    GuildMember.user_id == user.id,
                    GuildMember.user_domain == user.origin_domain,
                    Guild.unavailable.is_(False),
                )
            )
        )
        states = await accessible_read_states(session, redis, user, guilds)
        dm_channels: list[dict[str, object]] = []
        channels = list(
            await session.scalars(
                select(Channel)
                .join(
                    DMParticipant,
                    (DMParticipant.conversation_id == Channel.id)
                    & (DMParticipant.conversation_domain == Channel.origin_domain),
                )
                .where(
                    DMParticipant.user_id == user.id,
                    DMParticipant.user_domain == user.origin_domain,
                    Channel.type == 1,
                    Channel.guild_id.is_(None),
                    Channel.unavailable.is_(False),
                )
                .order_by(Channel.updated_at.desc(), Channel.id.desc())
            )
        )
        for channel in channels:
            recipients = list(
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
                        (User.id != user.id) | (User.origin_domain != user.origin_domain),
                    )
                    .order_by(User.origin_domain, User.username)
                )
            )
            conversation = await session.get(
                DMConversation,
                (channel.id, channel.origin_domain),
            )
            dm_channels.append(
                dm_channel_payload(
                    channel,
                    recipients,
                    history=dm_history_metadata(
                        conversation,
                        local_domain=settings.domain,
                        remote_available=await dm_authority_history_available(
                            session,
                            conversation,
                            local_domain=settings.domain,
                        ),
                    ),
                )
            )
    topics = [user_topic(user.origin_domain, user.id)]
    topics.extend(guild_topic(guild.origin_domain, guild.id) for guild in guilds)
    return user, guilds, states, dm_channels, topics


async def accessible_read_states(
    session: AsyncSession,
    redis: Redis,
    user: User,
    guilds: list[Guild],
) -> list[ReadState]:
    """Return only states whose channel is currently visible to the user."""
    rows = (
        await session.execute(
            select(ReadState, Channel)
            .join(
                Channel,
                (Channel.id == ReadState.channel_id)
                & (Channel.origin_domain == ReadState.channel_domain),
            )
            .where(
                ReadState.user_id == user.id,
                ReadState.user_domain == user.origin_domain,
                Channel.unavailable.is_(False),
            )
        )
    ).all()
    if not rows:
        return []
    dm_rows = (
        await session.execute(
            select(DMParticipant.conversation_id, DMParticipant.conversation_domain).where(
                DMParticipant.user_id == user.id,
                DMParticipant.user_domain == user.origin_domain,
            )
        )
    ).all()
    dm_refs = {(conversation_id, domain) for conversation_id, domain in dm_rows}
    guild_by_ref = {(guild.id, guild.origin_domain): guild for guild in guilds}
    visible: list[ReadState] = []
    for state, channel in rows:
        if channel.guild_id is None:
            if (channel.id, channel.origin_domain) in dm_refs:
                visible.append(state)
            continue
        guild = guild_by_ref.get((channel.guild_id, cast(str, channel.guild_domain)))
        if guild is None:
            continue
        try:
            permissions = await get_permissions(session, redis, guild, user, channel=channel)
        except HTTPException:
            continue
        if permissions & Permission.VIEW_CHANNEL:
            visible.append(state)
    return visible


async def gateway_grant_is_current(
    sessionmaker: async_sessionmaker[AsyncSession], grant: AccessGrant
) -> bool:
    """Revalidate the exact SQL session and user behind an access grant."""
    async with sessionmaker() as session:
        now = datetime.now(UTC)
        active = await session.scalar(
            select(AuthSession.id)
            .join(
                User,
                (User.id == AuthSession.user_id) & (User.origin_domain == AuthSession.user_domain),
            )
            .where(
                AuthSession.id == grant.session_id,
                AuthSession.user_id == grant.user_id,
                AuthSession.user_domain == grant.user_domain,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
                AuthSession.absolute_expires_at > now,
                User.disabled_at.is_(None),
            )
        )
    return active is not None


async def guild_history_sync_statuses(
    redis: Redis,
    user: User,
    guilds: list[Guild],
) -> dict[tuple[int, str], dict[str, object]]:
    """Load bounded, user-scoped history warnings for a fresh READY.

    The worker hash is a recoverable projection, so malformed or stale fields
    are ignored rather than allowing cache contents to break gateway login.
    """

    accessible = {(guild.id, guild.origin_domain) for guild in guilds}
    status_key = history_status_key(user.origin_domain, user.id)
    try:
        raw_statuses = await redis.hgetall(status_key)  # type: ignore[misc]
    except Exception as exc:
        # History status is advisory. A Redis projection failure must never
        # prevent an otherwise valid gateway login or hide the guild itself.
        log.warning(
            "guild_history_status_load_failed",
            user_id=str(user.id),
            error_type=type(exc).__name__,
        )
        return {}
    statuses: dict[tuple[int, str], dict[str, object]] = {}
    stale_fields: list[object] = []
    for status_field, raw in raw_statuses.items():
        try:
            encoded_field = (
                status_field.decode("utf-8")
                if isinstance(status_field, bytes)
                else str(status_field)
            )
            encoded_payload = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            guild_ref = validate_entity_reference(encoded_field)
            guild_id, guild_domain = guild_ref.resolve(settings.domain)
            payload = json.loads(encoded_payload)
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            stale_fields.append(status_field)
            continue
        if (guild_id, guild_domain) not in accessible or not isinstance(payload, dict):
            stale_fields.append(status_field)
            continue
        status = payload.get("status")
        code = payload.get("code")
        if status not in {"retrying", "failed"} or not isinstance(code, str):
            stale_fields.append(status_field)
            continue
        projected: dict[str, object] = {
            "history_sync_status": status,
            "history_sync_error_code": code,
        }
        retry_after_ms = payload.get("retry_after_ms")
        if (
            status == "retrying"
            and isinstance(retry_after_ms, int)
            and not isinstance(retry_after_ms, bool)
            and 1_000 <= retry_after_ms <= 86_400_000
        ):
            projected["history_sync_retry_after_ms"] = retry_after_ms
        resource = payload.get("resource")
        if status == "failed" and resource in {
            "pages",
            "messages",
            "bytes",
            "reactions",
            "duration",
            "delta_requests",
        }:
            projected["history_sync_resource"] = resource
        statuses[(guild_id, guild_domain)] = projected
    if stale_fields:
        try:
            await redis.hdel(status_key, *stale_fields)
        except Exception as exc:
            # Cleanup is opportunistic and the hash has a defensive TTL. Keep
            # serving validated statuses even if this Redis write fails.
            log.warning(
                "guild_history_status_cleanup_failed",
                user_id=str(user.id),
                stale_count=len(stale_fields),
                error_type=type(exc).__name__,
            )
    return statuses


def ready_payload(
    user: User,
    guilds: list[Guild],
    states: list[ReadState],
    dm_channels: list[dict[str, object]],
    gateway_session_id: str,
    presence_preference: str,
    history_statuses: dict[tuple[int, str], dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "v": PROTOCOL_VERSION,
        "session_id": gateway_session_id,
        "presence_preference": presence_preference,
        "user": user_payload(user),
        "guilds": [
            {
                **guild_payload(guild),
                **(history_statuses or {}).get((guild.id, guild.origin_domain), {}),
            }
            for guild in guilds
        ],
        "dm_channels": dm_channels,
        "read_states": [
            {
                "channel_id": str(state.channel_id),
                "channel_domain": state.channel_domain,
                "last_message_id": (
                    str(state.last_message_id) if state.last_message_id is not None else None
                ),
                "last_message_domain": state.last_message_domain,
                "mention_count": state.mention_count,
            }
            for state in states
        ],
    }


async def next_pubsub(pubsub: GatewaySubscription) -> dict[str, Any] | None:
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=45)
        if message is None or message.get("type") in {"message", "overflow"}:
            return message


async def identify_admission(redis: Redis, client: str) -> tuple[bool, int]:
    result = await cast(
        Awaitable[object],
        redis.eval(
            IDENTIFY_LIMIT_SCRIPT,
            3,
            f"gateway:identify:client:{client}",
            "gateway:identify:global",
            "gateway:cache:ready",
            str(settings.gateway_identify_ip_rate_per_second),
            str(settings.gateway_identify_ip_burst),
            str(settings.gateway_identify_rate_per_second),
            str(settings.gateway_identify_burst),
        ),
    )
    if not isinstance(result, (list, tuple)) or len(result) != 3:
        raise RuntimeError("Dragonfly returned an invalid identify admission result")
    return bool(int(result[0])), max(0, int(result[1]))


async def identify_admitted(redis: Redis, client: str) -> bool:
    admitted, _retry_after_ms = await identify_admission(redis, client)
    return admitted


def gateway_client_ip(websocket: WebSocket) -> str:
    supplied_secret = websocket.headers.get("X-Kaede-Proxy-Secret")
    configured_secret = (
        settings.proxy_secret.get_secret_value() if settings.proxy_secret is not None else None
    )
    return resolve_client_ip(
        supplied_secret=supplied_secret,
        configured_secret=configured_secret,
        forwarded_for=websocket.headers.get("X-Forwarded-For"),
        direct_host=websocket.client.host if websocket.client is not None else None,
    )


def cookie_origin_allowed(websocket: WebSocket) -> bool:
    parsed = urlsplit(settings.app_url)
    expected = f"{parsed.scheme}://{parsed.netloc}"
    origin = websocket.headers.get("Origin")
    return origin is not None and secrets.compare_digest(origin, expected)


def session_key(session_id: str) -> str:
    return f"gateway:session:{session_id}"


def session_progress_key(session_id: str) -> str:
    return f"{session_key(session_id)}:progress"


def encode_gateway_progress(cursors: dict[str, int], topics: list[str]) -> str:
    return json.dumps(
        {"cursors": cursors, "topics": topics},
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_gateway_progress(raw: object) -> tuple[dict[str, int], list[str]]:
    encoded = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise TypeError
    raw_topics = parsed.get("topics")
    raw_cursors = parsed.get("cursors")
    if not isinstance(raw_topics, list) or not all(isinstance(item, str) for item in raw_topics):
        raise TypeError
    if not isinstance(raw_cursors, dict):
        raise TypeError
    return ({str(key): int(value) for key, value in raw_cursors.items()}, list(raw_topics))


async def claim_user_gateway_session(redis: Redis, user: User, session_id: str) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            CLAIM_USER_SESSION_SCRIPT,
            2,
            f"gateway:user-sessions:{user.origin_domain}:{user.id}",
            session_key(session_id),
            str(USER_SESSION_LIMIT),
            str(int(time.time() * 1000)),
            session_id,
            str(SESSION_TTL_SECONDS + 60),
        ),
    )
    return bool(result)


async def touch_gateway_session(redis: Redis, session_id: str) -> None:
    key = session_key(session_id)
    user_id, domain = await cast(Awaitable[list[Any]], redis.hmget(key, ["user_id", "domain"]))
    if user_id is None or domain is None:
        return
    expires_at_ms = int(time.time() * 1000) + SESSION_TTL_SECONDS * 1000
    user_sessions_key = f"gateway:user-sessions:{domain}:{user_id}"
    await redis.zadd(user_sessions_key, {session_id: expires_at_ms})
    await redis.expire(user_sessions_key, SESSION_TTL_SECONDS + 60)


async def store_gateway_session(
    redis: Redis,
    session_id: str,
    user: User,
    auth_session_id: str,
    topics: list[str],
) -> dict[str, int]:
    cursors: dict[str, int] = {}
    for topic in topics:
        value = await redis.get(f"dispatch:seq:{topic}")
        cursors[topic] = int(value or 0)
    async with redis.pipeline(transaction=True) as pipeline:
        pipeline.hset(
            session_key(session_id),
            mapping={
                "user_id": str(user.id),
                "domain": user.origin_domain,
                "auth_session_id": auth_session_id,
                "topics": json.dumps(topics, separators=(",", ":")),
                "cursors": json.dumps(cursors, separators=(",", ":")),
                "sequence": "0",
            },
        )
        pipeline.hset(
            session_progress_key(session_id),
            "0",
            encode_gateway_progress(cursors, topics),
        )
        pipeline.expire(session_key(session_id), SESSION_TTL_SECONDS)
        pipeline.expire(session_progress_key(session_id), SESSION_TTL_SECONDS)
        await pipeline.execute()
    await touch_gateway_session(redis, session_id)
    return cursors


async def persist_gateway_progress(
    redis: Redis,
    session_id: str,
    sequence: int,
    cursors: dict[str, int],
    topics: list[str],
) -> None:
    async with redis.pipeline(transaction=True) as pipeline:
        pipeline.hset(
            session_key(session_id),
            mapping={
                "sequence": str(sequence),
                "cursors": json.dumps(cursors, separators=(",", ":")),
                "topics": json.dumps(topics, separators=(",", ":")),
            },
        )
        pipeline.hset(
            session_progress_key(session_id),
            str(sequence),
            encode_gateway_progress(cursors, topics),
        )
        expired_sequence = sequence - SESSION_PROGRESS_HISTORY
        if expired_sequence >= 0:
            pipeline.hdel(session_progress_key(session_id), str(expired_sequence))
        pipeline.expire(session_key(session_id), SESSION_TTL_SECONDS)
        pipeline.expire(session_progress_key(session_id), SESSION_TTL_SECONDS)
        await pipeline.execute()
    await touch_gateway_session(redis, session_id)


def stream_id_key(stream_id: str) -> tuple[int, int]:
    milliseconds, sequence = stream_id.split("-", 1)
    return int(milliseconds), int(sequence)


async def replay_topic_events(
    redis: Redis, topics: list[str], cursors: dict[str, int]
) -> list[tuple[str, dict[str, Any]]] | None:
    replay: list[tuple[str, str, dict[str, Any]]] = []
    for topic in topics:
        entries = await redis.xrange(f"dispatch:stream:{topic}", min="-", max="+")
        pending = []
        for entry_id, fields in entries:
            event = cast(dict[str, Any], json.loads(str(fields["event"])))
            if int(event.get("topic_seq", 0)) > cursors.get(topic, 0):
                pending.append((str(entry_id), event))
        if pending and int(pending[0][1].get("topic_seq", 0)) > cursors.get(topic, 0) + 1:
            return None
        replay.extend((entry_id, topic, event) for entry_id, event in pending)
    replay.sort(key=lambda item: stream_id_key(item[0]))
    return [(topic, event) for _, topic, event in replay]


def parse_guild_topic(topic: str) -> EntityReference | None:
    if not topic.startswith("guild:"):
        return None
    try:
        domain, identifier = topic.removeprefix("guild:").rsplit(":", 1)
        return EntityReference(int(identifier), domain)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class VisibilitySummary:
    """Per-connection membership and channel visibility snapshot."""

    guilds: set[tuple[int, str]]
    channels: dict[tuple[int, str], set[tuple[int, str]]]
    acl_fences: dict[tuple[int, str], tuple[int, int]] = field(default_factory=dict)


VISIBILITY_INVALIDATING_EVENTS = {
    "CHANNEL_CREATE",
    "CHANNEL_UPDATE",
    "CHANNEL_DELETE",
    "GUILD_UPDATE",
    "GUILD_ROLE_CREATE",
    "GUILD_ROLE_UPDATE",
    "GUILD_ROLE_DELETE",
    "GUILD_MEMBER_UPDATE",
    "GUILD_MEMBER_REMOVE",
}


async def refresh_visibility_summary(
    summary: VisibilitySummary,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    user: User,
    guild_id: int,
    guild_domain: str,
) -> bool:
    guild_key = (guild_id, guild_domain)
    summary.channels[guild_key] = set()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(Guild, GuildMember.member_version)
                .join(
                    GuildMember,
                    (GuildMember.guild_id == Guild.id)
                    & (GuildMember.guild_domain == Guild.origin_domain),
                )
                .where(
                    Guild.id == guild_id,
                    Guild.origin_domain == guild_domain,
                    GuildMember.user_id == user.id,
                    GuildMember.user_domain == user.origin_domain,
                )
            )
        ).one_or_none()
        if row is None:
            summary.guilds.discard(guild_key)
            summary.channels.pop(guild_key, None)
            summary.acl_fences.pop(guild_key, None)
            return False
        guild, member_version = row
        summary.guilds.add(guild_key)
        summary.acl_fences[guild_key] = (guild.permission_generation, member_version)
        channels = list(
            await session.scalars(
                select(Channel).where(
                    Channel.guild_id == guild_id,
                    Channel.guild_domain == guild_domain,
                    Channel.unavailable.is_(False),
                )
            )
        )
        for channel in channels:
            try:
                permissions = await get_permissions(session, redis, guild, user, channel=channel)
            except HTTPException:
                continue
            if permissions & Permission.VIEW_CHANNEL:
                summary.channels[guild_key].add((channel.id, channel.origin_domain))
    return True


async def current_acl_fence(
    sessionmaker: async_sessionmaker[AsyncSession],
    user: User,
    guild_id: int,
    guild_domain: str,
) -> tuple[int, int] | None:
    """Read the durable authorization generations for one gateway subscriber.

    Dragonfly dispatch is a best-effort projection and may be unavailable after
    a membership or permission transaction commits.  Guild events therefore
    cannot use a cached visibility snapshot until its SQL-backed fence has been
    checked.  A missing membership fails closed immediately.
    """
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(Guild.permission_generation, GuildMember.member_version)
                .join(
                    GuildMember,
                    (GuildMember.guild_id == Guild.id)
                    & (GuildMember.guild_domain == Guild.origin_domain),
                )
                .where(
                    Guild.id == guild_id,
                    Guild.origin_domain == guild_domain,
                    GuildMember.user_id == user.id,
                    GuildMember.user_domain == user.origin_domain,
                    Guild.unavailable.is_(False),
                )
            )
        ).one_or_none()
    if row is None:
        return None
    return int(row[0]), int(row[1])


async def build_visibility_summary(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    user: User,
    guilds: list[Guild],
) -> VisibilitySummary:
    summary = VisibilitySummary(
        guilds={(guild.id, guild.origin_domain) for guild in guilds}, channels={}
    )
    for guild in guilds:
        await refresh_visibility_summary(
            summary,
            sessionmaker,
            redis,
            user,
            guild.id,
            guild.origin_domain,
        )
    return summary


async def event_visibility(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    user: User,
    summary: VisibilitySummary,
    topic: str,
    event: dict[str, Any],
) -> tuple[bool, bool]:
    """Return visibility after fencing the snapshot against durable SQL state."""
    guild_ref = parse_guild_topic(topic)
    if guild_ref is None:
        return True, True
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild_key = (guild_id, guild_domain)
    fence = await current_acl_fence(sessionmaker, user, guild_id, guild_domain)
    if fence is None:
        summary.guilds.discard(guild_key)
        summary.channels.pop(guild_key, None)
        summary.acl_fences.pop(guild_key, None)
        return False, False
    if (
        summary.acl_fences.get(guild_key) != fence
        or event.get("t") in VISIBILITY_INVALIDATING_EVENTS
    ):
        await refresh_visibility_summary(
            summary,
            sessionmaker,
            redis,
            user,
            guild_id,
            guild_domain,
        )
    member = guild_key in summary.guilds
    if not member:
        return False, False
    data = event.get("d")
    if not isinstance(data, dict):
        return False, True
    channel_id = data.get("channel_id")
    channel_domain = data.get("channel_domain")
    if channel_id is None and event.get("t") in {"CHANNEL_CREATE", "CHANNEL_UPDATE"}:
        channel_id = data.get("id")
        channel_domain = data.get("origin_domain")
    if channel_id is None:
        return True, True
    try:
        channel_ref = validate_entity_reference(f"{channel_id}@{channel_domain or guild_domain}")
    except ValueError:
        return False, True
    channel_number, resolved_domain = channel_ref.resolve(guild_domain)
    if event.get("t") == "CHANNEL_DELETE":
        return True, True
    return (channel_number, resolved_domain) in summary.channels.get(
        (guild_id, guild_domain), set()
    ), True


async def apply_user_topic_control(
    pubsub: GatewaySubscription,
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    user: User,
    visibility: VisibilitySummary,
    topic: str,
    event: dict[str, Any],
    topics: list[str],
    cursors: dict[str, int],
) -> None:
    if topic != user_topic(user.origin_domain, user.id):
        return
    data = event.get("d")
    if not isinstance(data, dict) or event.get("t") not in {"GUILD_CREATE", "GUILD_DELETE"}:
        return
    try:
        guild_ref = validate_entity_reference(
            f"{data['id']}@{data.get('origin_domain') or settings.domain}"
        )
    except (KeyError, ValueError):
        return
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    controlled_topic = guild_topic(guild_domain, guild_id)
    if event.get("t") == "GUILD_DELETE":
        visibility.guilds.discard((guild_id, guild_domain))
        visibility.channels.pop((guild_id, guild_domain), None)
        if controlled_topic in topics:
            await pubsub.unsubscribe(f"dispatch:{controlled_topic}")
            topics.remove(controlled_topic)
            cursors.pop(controlled_topic, None)
        return
    async with sessionmaker() as session:
        member = await session.scalar(
            select(GuildMember.user_id).where(
                GuildMember.guild_id == guild_id,
                GuildMember.guild_domain == guild_domain,
                GuildMember.user_id == user.id,
                GuildMember.user_domain == user.origin_domain,
            )
        )
    if member is not None and controlled_topic not in topics:
        await refresh_visibility_summary(
            visibility, sessionmaker, redis, user, guild_id, guild_domain
        )
        await pubsub.subscribe(f"dispatch:{controlled_topic}")
        topics.append(controlled_topic)
        current = await redis.get(f"dispatch:seq:{controlled_topic}")
        cursors[controlled_topic] = int(current or 0)


async def member_payloads(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    user: User,
    guild_ref: EntityReference,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 100,
) -> list[dict[str, object]] | None:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    async with sessionmaker() as session:
        membership = await session.scalar(
            select(GuildMember.user_id).where(
                GuildMember.guild_id == guild_id,
                GuildMember.guild_domain == guild_domain,
                GuildMember.user_id == user.id,
                GuildMember.user_domain == user.origin_domain,
            )
        )
        if membership is None:
            return None
        conditions = [
            GuildMember.guild_id == guild_id,
            GuildMember.guild_domain == guild_domain,
        ]
        if query:
            conditions.append(func.lower(User.username).contains(query.lower()))
        rows = (
            await session.execute(
                select(GuildMember, User)
                .join(
                    User,
                    (User.id == GuildMember.user_id)
                    & (User.origin_domain == GuildMember.user_domain),
                )
                .where(*conditions)
                .order_by(func.lower(User.username), User.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        refs = [(member.user_id, member.user_domain) for member, _ in rows]
        roles: dict[tuple[int, str], list[int]] = {ref: [] for ref in refs}
        if refs:
            assignments = await session.scalars(
                select(MemberRole).where(
                    MemberRole.guild_id == guild_id,
                    MemberRole.guild_domain == guild_domain,
                    tuple_(MemberRole.user_id, MemberRole.user_domain).in_(refs),
                )
            )
            for assignment in assignments:
                roles[(assignment.user_id, assignment.user_domain)].append(assignment.role_id)
        presence_keys = [f"presence:{member.user_domain}:{member.user_id}" for member, _ in rows]
        raw_presences: list[Any] = []
        if presence_keys:
            async with redis.pipeline(transaction=False) as pipeline:
                for key in presence_keys:
                    pipeline.get(key)
                raw_presences = list(await pipeline.execute())

        payloads: list[dict[str, object]] = []
        for index, (member, member_user) in enumerate(rows):
            status = "offline"
            raw = raw_presences[index] if index < len(raw_presences) else None
            if raw is not None:
                try:
                    state = json.loads(raw)
                    candidate = state.get("status") if isinstance(state, dict) else None
                    if candidate in {"online", "idle", "dnd"}:
                        status = candidate
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            payload = member_payload(
                member,
                member_user,
                roles[(member.user_id, member.user_domain)],
                include_private_authority_state=guild_domain == settings.domain,
            )
            payload["presence"] = status
            payloads.append(payload)
        return payloads


async def handle_member_request(
    websocket: WebSocket,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    user: User,
    op: int,
    data: dict[str, Any],
    sequence: int,
) -> int:
    try:
        guild_ref = validate_entity_reference(data.get("guild_id", ""))
        guild_id, guild_domain = guild_ref.resolve(settings.domain)
    except (TypeError, ValueError):
        return sequence
    if op == GatewayOp.REQUEST_MEMBERS:
        try:
            requested_limit = int(data.get("limit", 100))
        except (TypeError, ValueError):
            requested_limit = 100
        members = await member_payloads(
            sessionmaker,
            redis,
            user,
            guild_ref,
            query=str(data.get("query", ""))[:32],
            limit=min(max(requested_limit, 1), 100),
        )
        if members is None:
            return sequence
        sequence += 1
        await websocket.send_json(
            {
                "op": GatewayOp.DISPATCH,
                "t": "GUILD_MEMBERS_CHUNK",
                "s": sequence,
                "d": {
                    "guild_id": str(guild_id),
                    "guild_domain": guild_domain,
                    "members": members,
                    "chunk_index": 0,
                },
            }
        )
        return sequence
    ranges = data.get("ranges") or [[0, 99]]
    if not isinstance(ranges, list) or len(ranges) > 3:
        return sequence
    operations: list[dict[str, object]] = []
    for requested in ranges:
        if not isinstance(requested, list) or len(requested) != 2:
            continue
        try:
            start, end = max(0, int(requested[0])), max(0, int(requested[1]))
        except (TypeError, ValueError):
            continue
        start = min(start, 1_000_000)
        end = min(end, start + 99)
        members = await member_payloads(
            sessionmaker, redis, user, guild_ref, offset=start, limit=end - start + 1
        )
        if members is None:
            return sequence
        operations.append({"op": "SYNC", "range": [start, end], "items": members})
    sequence += 1
    await websocket.send_json(
        {
            "op": GatewayOp.DISPATCH,
            "t": "GUILD_MEMBER_LIST_UPDATE",
            "s": sequence,
            "d": {"guild_id": str(guild_id), "guild_domain": guild_domain, "ops": operations},
        }
    )
    return sequence


async def deliver_topic_event(
    websocket: WebSocket,
    pubsub: GatewaySubscription,
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    user: User,
    visibility: VisibilitySummary,
    topic: str,
    event: dict[str, Any],
    topics: list[str],
    cursors: dict[str, int],
    sequence: int,
) -> int:
    topic_sequence = int(event.get("topic_seq", 0))
    if topic_sequence <= cursors.get(topic, 0):
        return sequence
    guild_ref = parse_guild_topic(topic)
    guild_key = guild_ref.resolve(settings.domain) if guild_ref is not None else None
    before = set(visibility.channels.get(guild_key, set())) if guild_key is not None else set()
    visible, member = await event_visibility(sessionmaker, redis, user, visibility, topic, event)
    after = set(visibility.channels.get(guild_key, set())) if guild_key is not None else set()
    cursors[topic] = topic_sequence
    if not member and topic in topics:
        await pubsub.unsubscribe(f"dispatch:{topic}")
        topics.remove(topic)

    async def send(event_type: str, data: dict[str, object]) -> None:
        nonlocal sequence
        sequence += 1
        await websocket.send_json(
            {"op": GatewayOp.DISPATCH, "t": event_type, "d": data, "s": sequence}
        )

    if guild_key is not None:
        for channel_id, channel_domain in sorted(before - after):
            await send(
                "CHANNEL_ACCESS_REVOKED",
                {
                    "guild_id": str(guild_key[0]),
                    "guild_domain": guild_key[1],
                    "channel_id": str(channel_id),
                    "channel_domain": channel_domain,
                },
            )
        for channel_id, channel_domain in sorted(after - before):
            async with sessionmaker() as session:
                guild = await session.get(Guild, guild_key)
                channel = await session.get(Channel, (channel_id, channel_domain))
                if guild is None or channel is None:
                    continue
                rendered = channel_payload(channel)
                rendered["permissions"] = str(
                    int(await get_permissions(session, redis, guild, user, channel=channel))
                )
            await send("CHANNEL_ACCESS_GRANTED", rendered)
        if event.get("t") in VISIBILITY_INVALIDATING_EVENTS:
            permission_targets = before & after
            event_data = event.get("d")
            if event.get("t") == "CHANNEL_UPDATE" and isinstance(event_data, dict):
                try:
                    changed_ref = validate_entity_reference(
                        f"{event_data.get('id')}@{event_data.get('origin_domain') or guild_key[1]}"
                    ).resolve(guild_key[1])
                    permission_targets &= {changed_ref}
                except ValueError:
                    permission_targets = set()
            for channel_id, channel_domain in sorted(permission_targets):
                async with sessionmaker() as session:
                    guild = await session.get(Guild, guild_key)
                    channel = await session.get(Channel, (channel_id, channel_domain))
                    if guild is None or channel is None:
                        continue
                    permissions = await get_permissions(
                        session, redis, guild, user, channel=channel
                    )
                await send(
                    "CHANNEL_PERMISSION_UPDATE",
                    {
                        "guild_id": str(guild_key[0]),
                        "guild_domain": guild_key[1],
                        "channel_id": str(channel_id),
                        "channel_domain": channel_domain,
                        "permissions": str(int(permissions)),
                    },
                )
    if not visible:
        return sequence
    await send(str(event["t"]), cast(dict[str, object], event["d"]))
    await apply_user_topic_control(
        pubsub,
        redis,
        sessionmaker,
        user,
        visibility,
        topic,
        event,
        topics,
        cursors,
    )
    return sequence


async def deliver_with_gap_recovery(
    websocket: WebSocket,
    pubsub: GatewaySubscription,
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    user: User,
    visibility: VisibilitySummary,
    topic: str,
    event: dict[str, Any],
    topics: list[str],
    cursors: dict[str, int],
    sequence: int,
) -> tuple[int, bool]:
    topic_sequence = int(event.get("topic_seq", 0))
    current = cursors.get(topic, 0)
    if topic_sequence <= current:
        return sequence, True
    pending: list[tuple[str, dict[str, Any]]]
    if topic_sequence > current + 1:
        replay = await replay_topic_events(redis, [topic], cursors)
        if replay is None:
            return sequence, False
        pending = replay
    else:
        pending = [(topic, event)]
    for pending_topic, pending_event in pending:
        sequence = await deliver_topic_event(
            websocket,
            pubsub,
            redis,
            sessionmaker,
            user,
            visibility,
            pending_topic,
            pending_event,
            topics,
            cursors,
            sequence,
        )
    return sequence, True


async def deliver_ephemeral_topic_event(
    websocket: WebSocket,
    pubsub: GatewaySubscription,
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    user: User,
    visibility: VisibilitySummary,
    topic: str,
    event: dict[str, Any],
    topics: list[str],
    cursors: dict[str, int],
    sequence: int,
) -> int:
    """Deliver live-only state without changing a durable topic cursor."""
    visible, member = await event_visibility(sessionmaker, redis, user, visibility, topic, event)
    if not member and topic in topics:
        await pubsub.unsubscribe(f"dispatch:{topic}")
        topics.remove(topic)
    if not visible:
        return sequence
    sequence += 1
    await websocket.send_json(
        {
            "op": GatewayOp.DISPATCH,
            "t": event["t"],
            "d": event["d"],
            "s": sequence,
        }
    )
    await apply_user_topic_control(
        pubsub,
        redis,
        sessionmaker,
        user,
        visibility,
        topic,
        event,
        topics,
        cursors,
    )
    return sequence


async def fanout_loop(
    websocket: WebSocket,
    redis: Redis,
    pubsub: GatewaySubscription,
    token: str,
    topics: list[str],
    gateway_session_id: str,
    user: User,
    visibility: VisibilitySummary,
    sessionmaker: async_sessionmaker[AsyncSession],
    grant: AccessGrant,
    connection_lock_key: str,
    connection_owner: str,
    sequence: int,
    cursors: dict[str, int],
) -> None:
    heartbeat_timeout = (HEARTBEAT_INTERVAL_MS / 1000) + 5
    heartbeat_deadline = asyncio.get_running_loop().time() + heartbeat_timeout
    grant_deadline = asyncio.get_running_loop().time() + settings.access_token_ttl_seconds
    auth_revalidation_deadline = asyncio.get_running_loop().time() + AUTH_REVALIDATION_SECONDS
    op_limiter = ConnectionOpLimiter()
    while True:
        remaining = heartbeat_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await websocket.close(code=GatewayCloseCode.SESSION_TIMED_OUT)
            return
        receive_task = asyncio.create_task(websocket.receive_json())
        event_task = asyncio.create_task(next_pubsub(pubsub))
        try:
            done, pending = await asyncio.wait(
                {receive_task, event_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            receive_task.cancel()
            event_task.cancel()
            await asyncio.gather(receive_task, event_task, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        # Always consume both child-task outcomes. They can complete in the same
        # event-loop turn (for example, a pub/sub dispatch racing a client
        # disconnect); reading only the branch handled first leaks the other
        # exception as an un-retrieved task and can obscure shutdown failures.
        receive_result, event_result = await asyncio.gather(
            receive_task, event_task, return_exceptions=True
        )
        if not done:
            await websocket.close(code=GatewayCloseCode.SESSION_TIMED_OUT)
            return
        if receive_task in done:
            if isinstance(receive_result, BaseException):
                raise receive_result
            payload = receive_result
            if not isinstance(payload, dict):
                await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
                return
            op = payload.get("op")
            if not op_limiter.admit():
                await websocket.close(code=GatewayCloseCode.RATE_LIMITED)
                return
            if op == GatewayOp.HEARTBEAT:
                now = asyncio.get_running_loop().time()
                token_grant = await AccessTokenStore(redis, settings.access_token_ttl_seconds).get(
                    token
                )
                if now >= grant_deadline or token_grant != grant:
                    await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
                    return
                if not await renew_owned_lease(
                    redis,
                    connection_lock_key,
                    connection_owner,
                    CONNECTION_OWNER_TTL_SECONDS,
                ):
                    await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                    return
                if now >= auth_revalidation_deadline:
                    if not await gateway_grant_is_current(sessionmaker, grant):
                        await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
                        return
                    auth_revalidation_deadline = now + AUTH_REVALIDATION_SECONDS
                heartbeat_deadline = asyncio.get_running_loop().time() + heartbeat_timeout
                await redis.expire(session_key(gateway_session_id), SESSION_TTL_SECONDS)
                await redis.expire(session_progress_key(gateway_session_id), SESSION_TTL_SECONDS)
                await touch_gateway_session(redis, gateway_session_id)
                renewed = await renew_presence_state(redis, user)
                if renewed:
                    visible_status = await visible_presence_status(redis, user)
                    refresh_claimed = await redis.set(
                        f"presence:federation-refresh:{user.origin_domain}:{user.id}",
                        "1",
                        ex=30,
                        nx=True,
                    )
                    if visible_status is not None and refresh_claimed:
                        schedule_presence_fanout(user, visible_status, renewed)
                await websocket.send_json({"op": GatewayOp.HEARTBEAT_ACK, "d": None})
            elif op == GatewayOp.PRESENCE_UPDATE:
                raw_data = payload.get("d") or {}
                if not isinstance(raw_data, dict):
                    await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
                    return
                data = cast(dict[str, Any], raw_data)
                status_value = str(data.get("status", "online"))
                if status_value not in {"online", "idle", "dnd", "invisible"}:
                    status_value = "online"
                visible_status, generation = await broadcast_presence_preference(
                    redis, user, status_value, topics
                )
                schedule_presence_fanout(user, visible_status, generation)
            elif op == GatewayOp.VOICE_STATE_UPDATE:
                raw_data = payload.get("d")
                if not isinstance(raw_data, dict) or set(raw_data) != {"self_mute", "self_deaf"}:
                    await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
                    return
                if (
                    type(raw_data["self_mute"]) is not bool
                    or type(raw_data["self_deaf"]) is not bool
                ):
                    await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
                    return
                identity = participant_identity(user.id, user.origin_domain)
                occupant = await update_self_flags(
                    redis,
                    settings.domain,
                    identity,
                    self_mute=raw_data["self_mute"],
                    self_deaf=raw_data["self_deaf"],
                )
                if occupant is not None:
                    kind, scope_id, _ = parse_room_name(occupant.room)
                    topic = (
                        guild_topic(settings.domain, scope_id)
                        if kind == "g"
                        else user_topic(user.origin_domain, user.id)
                    )
                    await publish_ephemeral(
                        redis,
                        topic,
                        "VOICE_STATE_UPDATE",
                        {
                            "room": occupant.room,
                            "user_id": str(user.id),
                            "user_domain": user.origin_domain,
                            "self_mute": occupant.self_mute,
                            "self_deaf": occupant.self_deaf,
                        },
                    )
            elif op in {GatewayOp.REQUEST_MEMBERS, GatewayOp.SUBSCRIBE_MEMBER_LIST}:
                raw_data = payload.get("d") or {}
                if not isinstance(raw_data, dict):
                    await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
                    return
                sequence = await handle_member_request(
                    websocket,
                    sessionmaker,
                    redis,
                    user,
                    op,
                    cast(dict[str, Any], raw_data),
                    sequence,
                )
                await persist_gateway_progress(redis, gateway_session_id, sequence, cursors, topics)
            else:
                await websocket.close(code=GatewayCloseCode.UNKNOWN_OPCODE)
                return
        if event_task in done:
            if isinstance(event_result, BaseException):
                raise event_result
            message = event_result
            if message is None:
                continue
            if message.get("type") == "overflow":
                await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                return
            channel = str(message["channel"])
            if channel == f"gateway:connection-owner:{gateway_session_id}":
                if not secrets.compare_digest(str(message["data"]), connection_owner):
                    await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                    return
                continue
            if channel.startswith("auth:revoke:"):
                await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
                return
            try:
                event = json.loads(str(message["data"]))
                if not isinstance(event, dict):
                    raise TypeError
            except (json.JSONDecodeError, KeyError, TypeError):
                await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
                return
            topic = channel.removeprefix("dispatch:")
            if topic not in topics:
                continue
            if event.get("ephemeral") is True:
                sequence = await deliver_ephemeral_topic_event(
                    websocket,
                    pubsub,
                    redis,
                    sessionmaker,
                    user,
                    visibility,
                    topic,
                    cast(dict[str, Any], event),
                    topics,
                    cursors,
                    sequence,
                )
            else:
                sequence, recovered = await deliver_with_gap_recovery(
                    websocket,
                    pubsub,
                    redis,
                    sessionmaker,
                    user,
                    visibility,
                    topic,
                    cast(dict[str, Any], event),
                    topics,
                    cursors,
                    sequence,
                )
                if not recovered:
                    await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                    return
            await persist_gateway_progress(redis, gateway_session_id, sequence, cursors, topics)


@app.websocket("/gateway")
async def gateway(websocket: WebSocket, v: int = PROTOCOL_VERSION, encoding: str = "json") -> None:
    admission = cast(PreAuthAdmission, websocket.app.state.preauth_admission)
    if not admission.try_acquire(websocket):
        await reject_preauth_connection(websocket)
        return
    preauth_held = True
    connections = cast(set[WebSocket], websocket.app.state.connections)
    pubsub: GatewaySubscription | None = None
    connection_lock_key: str | None = None
    connection_owner: str | None = None
    try:
        await websocket.accept()
        connections.add(websocket)
        if v != PROTOCOL_VERSION or encoding != "json":
            await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
            return
        await websocket.send_json(
            {"op": GatewayOp.HELLO, "d": {"heartbeat_interval": HEARTBEAT_INTERVAL_MS}}
        )
        payload = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if not isinstance(payload, dict):
            await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
            return
        op = payload.get("op")
        if op not in {GatewayOp.IDENTIFY, GatewayOp.RESUME}:
            await websocket.close(code=GatewayCloseCode.NOT_AUTHENTICATED)
            return
        data = payload.get("d") or {}
        if not isinstance(data, dict):
            await websocket.close(code=GatewayCloseCode.DECODE_ERROR)
            return
        explicit_token = data.get("token")
        cookie_token = websocket.cookies.get("kc_access")
        if isinstance(explicit_token, str) and explicit_token:
            token = explicit_token
        elif cookie_token is not None:
            if not cookie_origin_allowed(websocket):
                await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
                return
            token = cookie_token
        else:
            token = ""
        sessionmaker = cast(async_sessionmaker[AsyncSession], websocket.app.state.sessionmaker)
        redis = cast(Redis, websocket.app.state.redis)
        admitted, retry_after_ms = await asyncio.wait_for(
            identify_admission(redis, gateway_client_ip(websocket)),
            timeout=PREAUTH_REDIS_TIMEOUT_SECONDS,
        )
        if not admitted:
            await websocket.close(
                code=GatewayCloseCode.RATE_LIMITED,
                reason=json.dumps(
                    {"retry_after_ms": max(1, retry_after_ms)}, separators=(",", ":")
                ),
            )
            return
        admission.release(websocket)
        preauth_held = False
        identity = await identify(sessionmaker, redis, token)
        if identity is None:
            await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
            return
        user, guilds, states, dm_channels, current_topics = identity
        visibility = await build_visibility_summary(sessionmaker, redis, user, guilds)
        grant = await AccessTokenStore(redis, settings.access_token_ttl_seconds).get(token)
        if grant is None:
            await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
            return
        subscription_hub = cast(GatewaySubscriptionHub, websocket.app.state.subscription_hub)
        pubsub = await subscription_hub.open()
        if op == GatewayOp.RESUME:
            gateway_session_id = str(data.get("session_id") or "")
            connection_lock_key = f"gateway:connection-owner:{gateway_session_id}"
            connection_owner = secrets.token_urlsafe(16)
            stored = await redis.hgetall(  # type: ignore[misc]
                session_key(gateway_session_id)
            )
            if (
                not stored
                or str(stored.get("user_id")) != str(user.id)
                or str(stored.get("domain")) != user.origin_domain
                or str(stored.get("auth_session_id")) != grant.session_id
            ):
                log.info("gateway_resume_rejected", reason="session_identity_mismatch")
                await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                return
            try:
                sequence = int(stored.get("sequence", 0))
                requested_sequence = int(str(data.get("seq", "")))
                topics = json.loads(str(stored["topics"]))
                cursors = json.loads(str(stored["cursors"]))
                if not isinstance(topics, list) or not all(
                    isinstance(item, str) for item in topics
                ):
                    raise TypeError
                if not isinstance(cursors, dict):
                    raise TypeError
                topics = list(topics)
                cursors = {str(key): int(value) for key, value in cursors.items()}
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                log.info("gateway_resume_rejected", reason="invalid_session_state")
                await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                return
            if requested_sequence < 0 or requested_sequence > sequence:
                log.info("gateway_resume_rejected", reason="sequence_mismatch")
                await websocket.close(code=GatewayCloseCode.INVALID_SEQUENCE)
                return
            if requested_sequence != sequence:
                raw_progress = await cast(
                    Awaitable[Any],
                    redis.hget(
                        session_progress_key(gateway_session_id),
                        str(requested_sequence),
                    ),
                )
                if raw_progress is None:
                    log.info("gateway_resume_rejected", reason="sequence_out_of_window")
                    await websocket.close(code=GatewayCloseCode.INVALID_SEQUENCE)
                    return
                try:
                    cursors, topics = decode_gateway_progress(raw_progress)
                except (json.JSONDecodeError, TypeError, ValueError):
                    log.info("gateway_resume_rejected", reason="invalid_progress_state")
                    await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                    return
                sequence = requested_sequence
            if set(topics) != set(current_topics):
                log.info("gateway_resume_rejected", reason="topic_membership_changed")
                await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                return
            await redis.set(
                connection_lock_key,
                connection_owner,
                ex=CONNECTION_OWNER_TTL_SECONDS,
            )
            channels = [
                *(f"dispatch:{topic}" for topic in topics),
                f"auth:revoke:{grant.session_id}",
                connection_lock_key,
            ]
            await pubsub.subscribe(*channels)
            await redis.publish(connection_lock_key, connection_owner)
            replay = await replay_topic_events(redis, topics, cursors)
            if replay is None:
                log.info("gateway_resume_rejected", reason="replay_gap")
                await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                return
            start_sequence = sequence
            for topic, event in replay:
                sequence = await deliver_topic_event(
                    websocket,
                    pubsub,
                    redis,
                    sessionmaker,
                    user,
                    visibility,
                    topic,
                    event,
                    topics,
                    cursors,
                    sequence,
                )
            sequence += 1
            presence_preference = await current_presence_preference(sessionmaker, redis, user)
            history_statuses = await guild_history_sync_statuses(redis, user, guilds)
            await websocket.send_json(
                {
                    "op": GatewayOp.DISPATCH,
                    "t": "RESUMED",
                    "s": sequence,
                    "d": {
                        "session_id": gateway_session_id,
                        "replayed": sequence - start_sequence - 1,
                        "presence_preference": presence_preference,
                    },
                }
            )
            await persist_gateway_progress(redis, gateway_session_id, sequence, cursors, topics)
        else:
            topics = list(current_topics)
            gateway_session_id = secrets.token_urlsafe(24)
            if not await claim_user_gateway_session(redis, user, gateway_session_id):
                await websocket.close(
                    code=GatewayCloseCode.RATE_LIMITED,
                    reason=json.dumps({"code": "SESSION_LIMIT", "limit": USER_SESSION_LIMIT}),
                )
                return
            connection_lock_key = f"gateway:connection-owner:{gateway_session_id}"
            connection_owner = secrets.token_urlsafe(16)
            if not await redis.set(
                connection_lock_key,
                connection_owner,
                ex=CONNECTION_OWNER_TTL_SECONDS,
                nx=True,
            ):
                await websocket.close(code=GatewayCloseCode.UNKNOWN_ERROR)
                return
            sequence = 0
            # Subscribe and capture a stream barrier before constructing READY.
            # Sampling cursors after the database snapshot can permanently skip
            # events committed in between. Membership may change while this
            # barrier is established, so rebuild until the topic set and the
            # post-barrier snapshot converge.
            for _attempt in range(3):
                channels = [
                    *(f"dispatch:{topic}" for topic in topics),
                    f"auth:revoke:{grant.session_id}",
                    connection_lock_key,
                ]
                await pubsub.subscribe(*channels)
                cursors = await store_gateway_session(
                    redis, gateway_session_id, user, grant.session_id, topics
                )
                refreshed_identity = await identify(sessionmaker, redis, token)
                if refreshed_identity is None:
                    await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
                    return
                (
                    refreshed_user,
                    refreshed_guilds,
                    refreshed_states,
                    refreshed_dm_channels,
                    refreshed_topics,
                ) = refreshed_identity
                if (refreshed_user.id, refreshed_user.origin_domain) != (
                    user.id,
                    user.origin_domain,
                ):
                    await websocket.close(code=GatewayCloseCode.AUTHENTICATION_FAILED)
                    return
                if set(refreshed_topics) == set(topics):
                    user = refreshed_user
                    guilds = refreshed_guilds
                    states = refreshed_states
                    dm_channels = refreshed_dm_channels
                    topics = list(refreshed_topics)
                    break
                removed_topics = set(topics) - set(refreshed_topics)
                if removed_topics:
                    await pubsub.unsubscribe(*(f"dispatch:{topic}" for topic in removed_topics))
                topics = list(refreshed_topics)
            else:
                await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                return
            visibility = await build_visibility_summary(sessionmaker, redis, user, guilds)
            presence_preference = await current_presence_preference(sessionmaker, redis, user)
            await websocket.send_json(
                {
                    "op": GatewayOp.DISPATCH,
                    "t": "READY",
                    "s": 0,
                    "d": ready_payload(
                        user,
                        guilds,
                        states,
                        dm_channels,
                        gateway_session_id,
                        presence_preference,
                        history_statuses,
                    ),
                }
            )
            replay = await replay_topic_events(redis, topics, cursors)
            if replay is None:
                await websocket.send_json({"op": GatewayOp.INVALID_SESSION, "d": False})
                return
            for topic, event in replay:
                sequence = await deliver_topic_event(
                    websocket,
                    pubsub,
                    redis,
                    sessionmaker,
                    user,
                    visibility,
                    topic,
                    event,
                    topics,
                    cursors,
                    sequence,
                )
            await persist_gateway_progress(redis, gateway_session_id, sequence, cursors, topics)
        if connection_lock_key is None or connection_owner is None:
            await websocket.close(code=GatewayCloseCode.UNKNOWN_ERROR)
            return
        await fanout_loop(
            websocket,
            redis,
            pubsub,
            token,
            topics,
            gateway_session_id,
            user,
            visibility,
            sessionmaker,
            grant,
            connection_lock_key,
            connection_owner,
            sequence,
            cursors,
        )
    except (
        TimeoutError,
        WebSocketDisconnect,
        EndOfStream,
        RedisConnectionError,
        RuntimeError,
    ):
        with suppress(RuntimeError):
            await websocket.close(code=GatewayCloseCode.SESSION_TIMED_OUT)
    finally:
        if preauth_held:
            admission.release(websocket)
        connections.discard(websocket)
        if connection_lock_key is not None and connection_owner is not None:
            with suppress(Exception):
                await release_owned_lease(redis, connection_lock_key, connection_owner)
        if pubsub is not None:
            await pubsub.aclose()
