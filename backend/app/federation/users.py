from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import User
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError, normalize_domain
from app.federation.replication import upsert_remote_user
from app.federation.schemas import RemoteUserProfile

REMOTE_PROFILE_FRESHNESS = timedelta(minutes=5)
REQUESTER_LOOKUPS_PER_MINUTE = 30
TARGET_DOMAIN_LOOKUPS_PER_MINUTE = 120
LOOKUP_RATE_SCRIPT = """
local requester_count = redis.call('INCR', KEYS[1])
if requester_count == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
if requester_count > tonumber(ARGV[2]) then return {requester_count, -1} end
local domain_count = redis.call('INCR', KEYS[2])
if domain_count == 1 then redis.call('EXPIRE', KEYS[2], tonumber(ARGV[1])) end
return {requester_count, domain_count}
"""
TARGET_RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
return count
"""


def split_handle(handle: str) -> tuple[str, str]:
    username, separator, raw_domain = handle.strip().lower().rpartition("@")
    username = username.removeprefix("@")
    if not separator or not username:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    try:
        return username, normalize_domain(raw_domain)
    except FederationNetworkError:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"}) from None


async def resolve_handle(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    requester_key: str,
    handle: str,
) -> User:
    username, domain = split_handle(handle)
    user = await session.scalar(
        select(User).where(
            User.origin_domain == domain,
            func.lower(User.username) == username,
        )
    )
    if domain == settings.domain:
        if user is None or not user.is_local:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
        return user
    if user is not None and datetime.now(UTC) - user.updated_at <= REMOTE_PROFILE_FRESHNESS:
        return user
    if user is not None:
        refresh_key = f"federation:user-lookup:refresh:{domain}:{username}"
        if await redis.set(refresh_key, "1", ex=30, nx=True):
            # Lazy import avoids a task-registration cycle at module import time.
            from app.tasks import federation_user_refresh

            queued = await enqueue_best_effort(federation_user_refresh, username, domain)
            if not queued:
                await redis.delete(refresh_key)
        return user
    negative_key = f"federation:user-lookup:missing:{domain}:{username}"
    if await redis.exists(negative_key):
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    window = int(datetime.now(UTC).timestamp() // 60)
    counts = await cast(Any, redis.eval)(
        LOOKUP_RATE_SCRIPT,
        2,
        f"federation:user-lookup:rate:requester:{requester_key}:{window}",
        f"federation:user-lookup:rate:target:{domain}:{window}",
        "120",
        str(REQUESTER_LOOKUPS_PER_MINUTE),
    )
    requester_count, domain_count = (int(item) for item in counts)
    if (
        requester_count > REQUESTER_LOOKUPS_PER_MINUTE
        or domain_count > TARGET_DOMAIN_LOOKUPS_PER_MINUTE
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "FEDERATION_LOOKUP_RATE_LIMITED"},
            headers={"Retry-After": "60"},
        )
    try:
        response = await signed_request(
            session,
            settings,
            "GET",
            domain,
            "/_kaede/v1/users/lookup",
            query={"handle": f"{username}@{domain}"},
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        if user is not None:
            return user
        raise HTTPException(status_code=503, detail={"code": "FEDERATION_UNAVAILABLE"}) from None
    if response.status_code == 404:
        await redis.set(negative_key, "1", ex=60)
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail={"code": "FEDERATION_LOOKUP_FAILED"})
    try:
        profile = RemoteUserProfile.model_validate(response.json())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=502, detail={"code": "FEDERATION_IDENTITY_MISMATCH"}
        ) from None
    if profile.origin_domain != domain or profile.username.lower() != username:
        raise HTTPException(status_code=502, detail={"code": "FEDERATION_IDENTITY_MISMATCH"})
    user = await upsert_remote_user(session, settings, profile)
    user.updated_at = datetime.now(UTC)
    await session.commit()
    return user


async def refresh_remote_user(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    username: str,
    domain: str,
) -> User | None:
    """Refresh one stale cached profile outside a request's latency path."""

    domain = normalize_domain(domain)
    if domain == settings.domain:
        return None
    window = int(datetime.now(UTC).timestamp() // 60)
    target_count = await cast(Any, redis.eval)(
        TARGET_RATE_SCRIPT,
        1,
        f"federation:user-lookup:rate:target:{domain}:{window}",
        "120",
    )
    if int(target_count) > TARGET_DOMAIN_LOOKUPS_PER_MINUTE:
        return None
    response = await signed_request(
        session,
        settings,
        "GET",
        domain,
        "/_kaede/v1/users/lookup",
        query={"handle": f"{username}@{domain}"},
    )
    if response.status_code == 404:
        await redis.set(f"federation:user-lookup:missing:{domain}:{username}", "1", ex=60)
        return None
    if response.status_code != 200:
        raise FederationNetworkError("remote profile refresh failed")
    try:
        profile = RemoteUserProfile.model_validate(response.json())
    except (TypeError, ValueError):
        raise FederationNetworkError("remote profile refresh returned invalid identity") from None
    if profile.origin_domain != domain or profile.username.lower() != username:
        raise FederationNetworkError("remote profile refresh returned mismatched identity")
    user = await upsert_remote_user(session, settings, profile)
    user.updated_at = datetime.now(UTC)
    await session.commit()
    return user
