from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import cast

import structlog
from redis.asyncio import Redis
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.chat.events import guild_topic, publish_presence, user_topic
from app.chat.presence import encode_presence_state
from app.core.settings import Settings
from app.db.models import Guild, GuildMember, Relationship, User
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError
from app.federation.schemas import PresenceFederationRequest
from app.federation.security import matching_block

log = structlog.get_logger()
PRESENCE_TTL_SECONDS = 90
MAX_PRESENCE_CLOCK_SKEW_SECONDS = 60

SET_REMOTE_PRESENCE_SCRIPT = """
local incoming = tonumber(ARGV[1])
local latest = tonumber(redis.call('GET', KEYS[1]) or 0)
if incoming < latest then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', 300)
-- Store Python's validated JSON verbatim so an empty activities array cannot
-- be changed into an object by Dragonfly's bundled Lua cjson implementation.
redis.call('SET', KEYS[2], ARGV[2])
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[4])
return 1
"""


async def presence_destinations(session: AsyncSession, settings: Settings, user: User) -> set[str]:
    memberships = (
        select(GuildMember.guild_id, GuildMember.guild_domain)
        .where(
            GuildMember.user_id == user.id,
            GuildMember.user_domain == user.origin_domain,
        )
        .subquery()
    )
    guild_peers = set(
        await session.scalars(
            select(distinct(GuildMember.user_domain))
            .join(
                memberships,
                (memberships.c.guild_id == GuildMember.guild_id)
                & (memberships.c.guild_domain == GuildMember.guild_domain),
            )
            .where(
                GuildMember.user_domain != settings.domain,
            )
        )
    )
    guild_peers.update(
        await session.scalars(
            select(distinct(Guild.origin_domain))
            .join(
                memberships,
                (memberships.c.guild_id == Guild.id)
                & (memberships.c.guild_domain == Guild.origin_domain),
            )
            .where(Guild.origin_domain != settings.domain)
        )
    )
    friend_peers = set(
        await session.scalars(
            select(distinct(Relationship.target_domain)).where(
                Relationship.user_id == user.id,
                Relationship.user_domain == user.origin_domain,
                Relationship.type == "friend",
                Relationship.target_domain != settings.domain,
            )
        )
    )
    destinations = set(friend_peers)
    for domain in guild_peers:
        block = await matching_block(session, domain)
        if block is None or block.level != "silence" or domain in friend_peers:
            destinations.add(domain)
    return destinations


async def fanout_presence(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User,
    status: str,
    *,
    activities: list[dict[str, object]] | None = None,
    since: int | None = None,
    afk: bool = False,
) -> None:
    """Best-effort signed fanout; presence never enters the durable outbox."""

    observed_at = time.time_ns() // 1_000
    expires_at = int(time.time()) + PRESENCE_TTL_SECONDS
    payload = {
        "user_id": str(user.id),
        "user_domain": user.origin_domain,
        "status": status,
        "activities": activities or [],
        "since": since,
        "afk": afk,
        "observed_at": observed_at,
        "expires_at": expires_at,
    }
    async with sessionmaker() as session:
        destinations = await presence_destinations(session, settings, user)

    async def deliver(domain: str) -> None:
        try:
            async with sessionmaker() as delivery_session:
                response = await signed_request(
                    delivery_session,
                    settings,
                    "POST",
                    domain,
                    "/_kaede/v1/presence",
                    payload=payload,
                    request_timeout=3,
                    max_response_bytes=4096,
                )
            if response.status_code not in {200, 204}:
                log.info(
                    "federated_presence_rejected",
                    destination=domain,
                    status=response.status_code,
                )
        except (FederationNetworkError, RuntimeError):
            log.info("federated_presence_unavailable", destination=domain)

    ordered = sorted(destinations)
    for offset in range(0, len(ordered), 8):
        await asyncio.gather(*(deliver(domain) for domain in ordered[offset : offset + 8]))


async def receive_presence(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    payload: PresenceFederationRequest,
    *,
    include_guilds: bool = True,
) -> bool:
    now = int(time.time())
    if payload.expires_at <= now or payload.expires_at > now + PRESENCE_TTL_SECONDS + 5:
        return False
    observed_seconds = payload.observed_at // 1_000_000
    if abs(observed_seconds - now) > MAX_PRESENCE_CLOCK_SKEW_SECONDS:
        return False
    user = await session.get(User, (int(payload.user_id), payload.user_domain))
    if user is None or user.is_local:
        return False
    guilds = (
        list(
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
        if include_guilds
        else []
    )
    local_friend_ids = set(
        await session.scalars(
            select(Relationship.user_id).where(
                Relationship.user_domain == settings.domain,
                Relationship.target_id == user.id,
                Relationship.target_domain == user.origin_domain,
                Relationship.type == "friend",
            )
        )
    )
    if not guilds and not local_friend_ids:
        return False
    await require_remote_user_creation_allowed(session, user)
    handle = f"{user.origin_domain}:{user.id}"
    encoded_state = encode_presence_state(
        payload.status,
        payload.activities,
        payload.since,
        payload.afk,
        generation=payload.observed_at,
        expires_at=payload.expires_at,
    )
    accepted = bool(
        await cast(
            Awaitable[object],
            redis.eval(
                SET_REMOTE_PRESENCE_SCRIPT,
                3,
                f"presence:generation:{handle}",
                f"presence:{handle}",
                "presence:expirations",
                str(payload.observed_at),
                encoded_state,
                str(payload.expires_at),
                handle,
            ),
        )
    )
    if not accepted:
        return False
    projection = {
        "user_id": payload.user_id,
        "user_domain": payload.user_domain,
        "status": payload.status,
        "activities": payload.activities,
        "since": payload.since,
        "afk": payload.afk,
        "client_status": ({"web": payload.status} if payload.status != "offline" else {}),
    }
    for guild in guilds:
        await publish_presence(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            projection,
            user_domain=user.origin_domain,
            user_id=user.id,
            generation=payload.observed_at,
        )
    for local_friend_id in sorted(local_friend_ids):
        await publish_presence(
            redis,
            user_topic(settings.domain, local_friend_id),
            projection,
            user_domain=user.origin_domain,
            user_id=user.id,
            generation=payload.observed_at,
        )
    return True
