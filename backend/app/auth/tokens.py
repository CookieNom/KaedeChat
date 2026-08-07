from __future__ import annotations

import json
import time
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from typing import cast

from redis.asyncio import Redis

from app.auth.security import new_access_token, token_key

ISSUE_ACCESS_SCRIPT = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SADD', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""
LOGIN_ADMIT_SCRIPT = """
local now = tonumber(ARGV[1])
local function refill(key, capacity, refill_per_ms)
  local values = redis.call('HMGET', key, 'tokens', 'updated')
  local tokens = tonumber(values[1]) or capacity
  local updated = tonumber(values[2]) or now
  return math.min(capacity, tokens + math.max(0, now - updated) * refill_per_ms)
end
local account_tokens = refill(KEYS[1], tonumber(ARGV[2]), tonumber(ARGV[3]))
local ip_tokens = refill(KEYS[2], tonumber(ARGV[4]), tonumber(ARGV[5]))
local allowed = 0
if account_tokens >= 1 and ip_tokens >= 1 then
  account_tokens = account_tokens - 1
  ip_tokens = ip_tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', account_tokens, 'updated', now)
redis.call('HSET', KEYS[2], 'tokens', ip_tokens, 'updated', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) / tonumber(ARGV[3])))
redis.call('PEXPIRE', KEYS[2], math.ceil(tonumber(ARGV[4]) / tonumber(ARGV[5])))
return allowed
"""
LOGIN_FAILURE_SCRIPT = """
local account_fails = redis.call('INCR', KEYS[1])
if account_fails == 1 then redis.call('EXPIRE', KEYS[1], 3600) end
local ip_fails = redis.call('INCR', KEYS[2])
if ip_fails == 1 then redis.call('EXPIRE', KEYS[2], 900) end
if account_fails >= 5 then
  local exponent = math.min(account_fails - 5, 11)
  local delay = math.min(3600, 2 ^ exponent)
  redis.call('SET', KEYS[3], '1', 'EX', delay)
end
if ip_fails >= 30 then redis.call('SET', KEYS[4], '1', 'EX', 900) end
return {account_fails, ip_fails}
"""


@dataclass(frozen=True, slots=True)
class AccessGrant:
    user_id: int
    user_domain: str
    session_id: str


class AccessTokenStore:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    async def issue(self, grant: AccessGrant) -> str:
        token = new_access_token()
        digest = token_key(token)
        await cast(
            Awaitable[object],
            self.redis.eval(
                ISSUE_ACCESS_SCRIPT,
                2,
                f"auth:access:{digest}",
                f"auth:session_access:{grant.session_id}",
                json.dumps(asdict(grant), separators=(",", ":")),
                digest,
                str(self.ttl_seconds),
            ),
        )
        return token

    async def get(self, token: str) -> AccessGrant | None:
        if not token.startswith("kc1_at_"):
            return None
        value = await self.redis.get(f"auth:access:{token_key(token)}")
        if value is None:
            return None
        try:
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise TypeError
            grant = AccessGrant(
                user_id=int(parsed["user_id"]),
                user_domain=str(parsed["user_domain"]),
                session_id=str(parsed["session_id"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            await self.redis.delete(f"auth:access:{token_key(token)}")
            return None
        return grant

    async def revoke_token(self, token: str) -> None:
        await self.redis.delete(f"auth:access:{token_key(token)}")

    async def revoke_session(self, session_id: str) -> None:
        set_key = f"auth:session_access:{session_id}"
        digests = await cast(Awaitable[set[str]], self.redis.smembers(set_key))
        if digests:
            await self.redis.delete(*(f"auth:access:{digest}" for digest in digests))
        await self.redis.delete(set_key)
        await self.redis.publish(f"auth:revoke:{session_id}", "revoked")


class LoginLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def admit(self, account_key: str, ip: str) -> bool:
        """Atomically charge an attempt before database lookup or Argon2 work."""

        result = await cast(
            Awaitable[object],
            self.redis.eval(
                LOGIN_ADMIT_SCRIPT,
                2,
                f"auth:admit:account:{account_key}",
                f"auth:admit:ip:{ip}",
                str(int(time.time() * 1000)),
                # Five immediate attempts per identifier, one token per 5s.
                "5",
                "0.0002",
                # Ten immediate attempts per source, one token per second.
                "10",
                "0.001",
            ),
        )
        return int(cast(int | str, result)) == 1

    async def is_locked(self, account_key: str, ip: str) -> bool:
        account_lock, ip_lock = await self.redis.mget(
            f"auth:lock:account:{account_key}", f"auth:lock:ip:{ip}"
        )
        return account_lock is not None or ip_lock is not None

    @staticmethod
    def _challenge_key(account_key: str, ip: str) -> str:
        return f"auth:turnstile:login:{account_key}:{token_key(ip)}"

    async def challenge_required(self, account_key: str, ip: str) -> bool:
        return await self.redis.get(self._challenge_key(account_key, ip)) is not None

    async def require_challenge(self, account_key: str, ip: str) -> None:
        await self.redis.set(self._challenge_key(account_key, ip), "1", ex=900)

    async def clear_challenge(self, account_key: str, ip: str) -> None:
        await self.redis.delete(self._challenge_key(account_key, ip))

    async def failure(self, account_key: str, ip: str) -> None:
        await cast(
            Awaitable[object],
            self.redis.eval(
                LOGIN_FAILURE_SCRIPT,
                4,
                f"auth:fail:account:{account_key}",
                f"auth:fail:ip:{ip}",
                f"auth:lock:account:{account_key}",
                f"auth:lock:ip:{ip}",
            ),
        )

    async def success(self, account_key: str) -> None:
        await self.redis.delete(
            f"auth:fail:account:{account_key}", f"auth:lock:account:{account_key}"
        )
