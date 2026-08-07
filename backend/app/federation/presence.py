from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import cast

import structlog
from redis.asyncio import Redis
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.events import guild_topic, publish_presence
from app.core.settings import Settings
from app.db.models import Guild, GuildMember, User
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError
from app.federation.schemas import PresenceFederationRequest

log = structlog.get_logger()
PRESENCE_TTL_SECONDS = 90
MAX_PRESENCE_CLOCK_SKEW_SECONDS = 60

SET_REMOTE_PRESENCE_SCRIPT = """
local incoming = tonumber(ARGV[1])
local latest = tonumber(redis.call('GET', KEYS[1]) or 0)
if incoming < latest then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', 300)
redis.call('SET', KEYS[2], cjson.encode({
    status = ARGV[2], generation = incoming, expires_at = tonumber(ARGV[3])
}))
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
    peers = set(
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
    peers.update(
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
    return peers


async def fanout_presence(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: User,
    status: str,
) -> None:
    """Best-effort signed fanout; presence never enters the durable outbox."""

    observed_at = time.time_ns() // 1_000
    expires_at = int(time.time()) + PRESENCE_TTL_SECONDS
    payload = {
        "user_id": str(user.id),
        "user_domain": user.origin_domain,
        "status": status,
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
    if not guilds:
        return False
    handle = f"{user.origin_domain}:{user.id}"
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
                payload.status,
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
    return True
