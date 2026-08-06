from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import suppress
from typing import cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import GuildMember, User

WARMUP_READY_KEY = "gateway:cache:ready"
WARMUP_SENTINEL_KEY = "gateway:cache:snapshot-sentinel"
WARMUP_LOCK_KEY = "gateway:cache:warmup-lock"
WARMUP_MANIFEST_KEY = "gateway:cache:warmup-manifest"


async def cache_is_ready(redis: Redis) -> bool:
    return bool(await redis.get(WARMUP_READY_KEY) == "ready")


async def _prime_identify_queries(session: AsyncSession, settings: Settings) -> tuple[int, int]:
    users = list(
        await session.scalars(
            select(User.id)
            .where(User.origin_domain == settings.domain, User.disabled_at.is_(None))
            .order_by(User.id.desc())
            .limit(settings.gateway_warmup_max_rows)
        )
    )
    memberships = (
        await session.execute(
            select(GuildMember.user_id, GuildMember.user_domain)
            .where(GuildMember.user_domain == settings.domain)
            .order_by(GuildMember.user_id.desc())
            .limit(settings.gateway_warmup_max_rows)
        )
    ).all()
    return len(users), len(memberships)


async def warm_identify_cache(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if await cache_is_ready(redis) and await redis.exists(WARMUP_SENTINEL_KEY):
        return
    owner = secrets.token_urlsafe(18)
    acquired = await redis.set(
        WARMUP_LOCK_KEY,
        owner,
        ex=settings.gateway_warmup_timeout_seconds + 10,
        nx=True,
    )
    if not acquired:
        deadline = time.monotonic() + settings.gateway_warmup_timeout_seconds
        while time.monotonic() < deadline:
            if await cache_is_ready(redis):
                return
            await asyncio.sleep(0.25)
        raise RuntimeError("gateway cache warmup timed out")
    try:
        await redis.set(WARMUP_READY_KEY, "warming")
        async with sessionmaker() as session:
            users, memberships = await _prime_identify_queries(session, settings)
        pipeline = redis.pipeline(transaction=True)
        pipeline.set(WARMUP_SENTINEL_KEY, secrets.token_hex(16))
        pipeline.set(
            WARMUP_MANIFEST_KEY,
            json.dumps(
                {"users": users, "memberships": memberships, "completed_at": int(time.time())},
                separators=(",", ":"),
            ),
        )
        pipeline.set(WARMUP_READY_KEY, "ready")
        await pipeline.execute()
    except Exception:
        await redis.delete(WARMUP_READY_KEY)
        raise
    finally:
        with suppress(Exception):
            current = await redis.get(WARMUP_LOCK_KEY)
            if current == owner:
                await redis.delete(WARMUP_LOCK_KEY)


async def maintain_cache_readiness(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    while True:
        try:
            if not await cache_is_ready(redis) or not await redis.exists(WARMUP_SENTINEL_KEY):
                await warm_identify_cache(redis, sessionmaker, settings)
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


async def warmup_manifest(redis: Redis) -> dict[str, int]:
    raw = await redis.get(WARMUP_MANIFEST_KEY)
    if not isinstance(raw, str):
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {}
    return cast(dict[str, int], parsed)
