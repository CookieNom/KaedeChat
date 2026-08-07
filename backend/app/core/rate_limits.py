from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import cast

from fastapi import HTTPException, Response
from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class ClientRateLimit:
    bucket: str
    limit: int
    period_seconds: int

    def __post_init__(self) -> None:
        if not self.bucket or self.limit < 1 or self.period_seconds < 1:
            raise ValueError("client rate limits require a name and positive bounds")


CLIENT_RATE_LIMITS = {
    "message_send": ClientRateLimit("message-send", 5, 5),
    "typing": ClientRateLimit("typing", 2, 10),
    "dm_open": ClientRateLimit("dm-open", 5, 60),
    "friend_request": ClientRateLimit("friend-request", 10, 60),
    "reaction": ClientRateLimit("reaction", 10, 10),
    "invite_create": ClientRateLimit("invite-create", 5, 60),
    "invite_accept": ClientRateLimit("invite-accept", 10, 60),
    "invite_preview": ClientRateLimit("invite-preview", 10, 60),
    "invite_preview_destination": ClientRateLimit("invite-preview-destination", 20, 60),
    "invite_preview_global": ClientRateLimit("invite-preview-global", 120, 60),
    "guild_create": ClientRateLimit("guild-create", 2, 3600),
    "upload_ticket": ClientRateLimit("upload-ticket", 10, 60),
    "remote_media_fetch": ClientRateLimit("remote-media-fetch", 10, 60),
    "gif_search": ClientRateLimit("gif-search", 30, 60),
}

# Integer milli-tokens and Dragonfly's own clock make this bucket atomic and
# independent of API-worker clock skew. Keys expire after two idle periods.
CLIENT_BUCKET_LUA = """
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local capacity = tonumber(ARGV[1]) * 1000
local period_ms = tonumber(ARGV[2])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens') or capacity)
local updated_ms = tonumber(redis.call('HGET', KEYS[1], 'updated_ms') or now_ms)
local elapsed = math.max(0, now_ms - updated_ms)
local refill = math.floor(elapsed * capacity / period_ms)
tokens = math.min(capacity, tokens + refill)
local allowed = 0
if tokens >= 1000 then
    tokens = tokens - 1000
    allowed = 1
end
local missing = math.max(0, 1000 - tokens)
local retry_ms = math.ceil(missing * period_ms / capacity)
local reset_ms = math.ceil((capacity - tokens) * period_ms / capacity)
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], math.max(1000, period_ms * 2))
return {allowed, math.floor(tokens / 1000), retry_ms, reset_ms}
"""


async def enforce_client_rate_limit(
    redis: Redis,
    response: Response,
    limit: ClientRateLimit,
    *,
    user_id: int,
    user_domain: str,
) -> None:
    await enforce_keyed_rate_limit(
        redis,
        response,
        limit,
        identity=f"{user_domain}:{user_id}",
    )


async def enforce_keyed_rate_limit(
    redis: Redis,
    response: Response,
    limit: ClientRateLimit,
    *,
    identity: str,
) -> None:
    if not identity or len(identity) > 320:
        raise ValueError("rate-limit identity is invalid")
    result = await cast(
        Awaitable[object],
        redis.eval(
            CLIENT_BUCKET_LUA,
            1,
            f"rate:client:{limit.bucket}:{identity}",
            str(limit.limit),
            str(limit.period_seconds * 1000),
        ),
    )
    if not isinstance(result, (list, tuple)) or len(result) != 4:
        raise RuntimeError("Dragonfly returned an invalid client rate-limit result")
    allowed, remaining, retry_ms, reset_ms = (int(item) for item in result)
    headers = {
        "X-RateLimit-Bucket": limit.bucket,
        "X-RateLimit-Limit": str(limit.limit),
        "X-RateLimit-Remaining": str(max(0, remaining)),
        "X-RateLimit-Reset-After": f"{max(0, reset_ms) / 1000:.3f}",
    }
    response.headers.update(headers)
    if not allowed:
        retry_after = max(1, retry_ms)
        raise HTTPException(
            status_code=429,
            detail={"code": "RATE_LIMITED", "retry_after_ms": retry_after},
            headers={**headers, "Retry-After": f"{retry_after / 1000:.3f}"},
        )
