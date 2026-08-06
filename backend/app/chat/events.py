from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, cast

import structlog
from redis.asyncio import Redis

log = structlog.get_logger()

PUBLISH_DISPATCH_SCRIPT = """
local sequence = redis.call('INCR', KEYS[1])
local event = cjson.decode(ARGV[1])
event['topic_seq'] = sequence
local encoded = cjson.encode(event)
redis.call('XADD', KEYS[2], 'MAXLEN', '~', 1000, '*', 'event', encoded)
redis.call('PUBLISH', KEYS[3], encoded)
return {sequence, encoded}
"""

PUBLISH_EPHEMERAL_SCRIPT = """
redis.call('PUBLISH', KEYS[1], ARGV[1])
return 1
"""

PUBLISH_PRESENCE_SCRIPT = """
local generation = tonumber(ARGV[1])
local latest = tonumber(redis.call('GET', KEYS[1]) or generation)
if latest > generation then
    return 0
end
local current = redis.call('GET', KEYS[2])
if current and tonumber(current) > generation then
    return 0
end
if current and tonumber(current) == generation then return 1 end
redis.call('SET', KEYS[2], ARGV[1], 'EX', 300)
redis.call('PUBLISH', KEYS[3], ARGV[2])
return 1
"""


def guild_topic(domain: str, guild_id: int) -> str:
    return f"guild:{domain}:{guild_id}"


def user_topic(domain: str, user_id: int) -> str:
    return f"user:{domain}:{user_id}"


async def publish_dispatch(
    redis: Redis, topic: str, event_type: str, data: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        result = await cast(
            Awaitable[object],
            redis.eval(
                PUBLISH_DISPATCH_SCRIPT,
                3,
                f"dispatch:seq:{topic}",
                f"dispatch:stream:{topic}",
                f"dispatch:{topic}",
                json.dumps({"t": event_type, "d": data}, separators=(",", ":")),
            ),
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Dragonfly returned an invalid dispatch result")
        sequence, encoded = result
        if isinstance(encoded, bytes):
            encoded = encoded.decode("utf-8")
        if not isinstance(encoded, str):
            raise RuntimeError("Dragonfly returned an invalid dispatch event")
        event = json.loads(encoded)
        if not isinstance(event, dict):
            raise RuntimeError("Dragonfly returned an invalid dispatch event")
        event["topic_seq"] = int(sequence)
        return cast(dict[str, Any], event)
    except Exception:
        # Dispatch streams are a recoverable projection of committed SQL/outbox
        # state. Never turn a successful mutation into a false 5xx response.
        log.exception("dispatch_projection_failed", topic=topic, event_type=event_type)
        return None


async def publish_ephemeral(
    redis: Redis, topic: str, event_type: str, data: dict[str, Any]
) -> bool:
    """Publish a best-effort event without consuming the resumable stream."""
    encoded = json.dumps({"t": event_type, "d": data, "ephemeral": True}, separators=(",", ":"))
    try:
        result = await cast(
            Awaitable[object],
            redis.eval(PUBLISH_EPHEMERAL_SCRIPT, 1, f"dispatch:{topic}", encoded),
        )
        return bool(result)
    except Exception:
        log.exception("ephemeral_dispatch_failed", topic=topic, event_type=event_type)
        return False


async def publish_presence(
    redis: Redis,
    topic: str,
    data: dict[str, Any],
    *,
    user_domain: str,
    user_id: int,
    generation: int,
) -> bool:
    """Publish a generation-fenced, non-resumable presence projection.

    Presence state is deliberately excluded from durable gateway streams.  The
    per-topic generation fence also prevents a delayed reaper notification from
    overwriting a newer online update after a reconnect race.
    """
    event = {
        "t": "PRESENCE_UPDATE",
        "d": {**data, "generation": generation},
        "ephemeral": True,
    }
    encoded = json.dumps(event, separators=(",", ":"))
    try:
        result = await cast(
            Awaitable[object],
            redis.eval(
                PUBLISH_PRESENCE_SCRIPT,
                3,
                f"presence:generation:{user_domain}:{user_id}",
                f"presence:published:{topic}:{user_domain}:{user_id}",
                f"dispatch:{topic}",
                str(generation),
                encoded,
            ),
        )
        return bool(result)
    except Exception:
        log.exception(
            "presence_dispatch_failed",
            topic=topic,
            user_domain=user_domain,
            user_id=user_id,
        )
        return False
