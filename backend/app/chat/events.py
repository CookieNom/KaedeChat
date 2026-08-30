from __future__ import annotations

import json
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from redis.asyncio import Redis

from app.core.types import validate_entity_reference

log = structlog.get_logger()

PUBLISH_DISPATCH_SCRIPT = """
local sequence = redis.call('INCR', KEYS[1])
-- Do not round-trip the event through Lua cjson. Redis/Dragonfly's bundled
-- cjson cannot distinguish an empty JSON array from an empty JSON object, so
-- fields such as mention_user_refs and attachments were changed from [] to {}
-- before reaching gateway clients. The Python encoder always gives us a
-- compact top-level object, so append the sequence without decoding its body.
local encoded = string.sub(ARGV[1], 1, -2) .. ',"topic_seq":' .. tostring(sequence) .. '}'
redis.call('XADD', KEYS[2], 'MAXLEN', '~', 1000, '*', 'event', encoded)
redis.call('PUBLISH', KEYS[3], encoded)
return {sequence, encoded}
"""

PUBLISH_EPHEMERAL_ONCE_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then return 1 end
redis.call('PUBLISH', KEYS[1], ARGV[1])
redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
return 1
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


def interaction_dispatch_audience(event: dict[str, Any]) -> str | None:
    """Return the one canonical bot audience for an interaction dispatch.

    Interaction options can contain credentials and other private command
    input.  Treat both the explicit audience and the payload bot reference as
    one immutable authorization binding so a malformed retained dispatch
    cannot add a second recipient during live delivery or replay.
    """

    if event.get("t") != "INTERACTION_CREATE":
        return None
    data = event.get("d")
    audience = event.get("audience_user_refs")
    if not isinstance(data, dict) or not isinstance(audience, list) or len(audience) != 1:
        return None
    bot_ref = data.get("bot_user_ref")
    if not isinstance(bot_ref, str) or audience[0] != bot_ref:
        return None
    try:
        parsed = validate_entity_reference(bot_ref)
    except ValueError:
        return None
    if parsed.domain is None or str(parsed) != bot_ref:
        return None
    return bot_ref


def interaction_response_dispatch_expired(
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Fail closed on retained private-response events past their hard deadline."""

    if event.get("t") not in {
        "INTERACTION_RESPONSE_CREATE",
        "INTERACTION_RESPONSE_UPDATE",
        "INTERACTION_RESPONSE_DELETE",
    }:
        return False
    data = event.get("d")
    raw_expiry = data.get("expires_at") if isinstance(data, dict) else None
    if not isinstance(raw_expiry, str):
        return True
    try:
        expiry = datetime.fromisoformat(raw_expiry)
    except ValueError:
        return True
    return expiry.tzinfo is None or expiry <= (now or datetime.now(UTC))


def _dispatch_event(
    event_type: str,
    data: dict[str, Any],
    audience_user_refs: Sequence[str] | None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"t": event_type, "d": data}
    if audience_user_refs is not None:
        audience = list(dict.fromkeys(audience_user_refs))
        if not audience or not all(isinstance(item, str) and item for item in audience):
            raise ValueError("dispatch audience is invalid")
        event["audience_user_refs"] = audience
    return event


def _decoded_dispatch_event(encoded: object) -> dict[str, Any]:
    if isinstance(encoded, bytes):
        encoded = encoded.decode("utf-8")
    if not isinstance(encoded, str):
        raise RuntimeError("Dragonfly returned an invalid dispatch event")
    event = json.loads(encoded)
    if not isinstance(event, dict) or not isinstance(event.get("topic_seq"), int):
        raise RuntimeError("Dragonfly returned an invalid dispatch event")
    return cast(dict[str, Any], event)


async def publish_dispatch(
    redis: Redis,
    topic: str,
    event_type: str,
    data: dict[str, Any],
    *,
    audience_user_refs: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    try:
        event = _dispatch_event(event_type, data, audience_user_refs)
        result = await cast(
            Awaitable[object],
            redis.eval(
                PUBLISH_DISPATCH_SCRIPT,
                3,
                f"dispatch:seq:{topic}",
                f"dispatch:stream:{topic}",
                f"dispatch:{topic}",
                json.dumps(event, separators=(",", ":")),
            ),
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Dragonfly returned an invalid dispatch result")
        sequence, encoded = result
        decoded = _decoded_dispatch_event(encoded)
        if decoded["topic_seq"] != int(sequence):
            raise RuntimeError("Dragonfly returned an inconsistent dispatch sequence")
        return decoded
    except Exception:
        # Dispatch streams are a recoverable projection of committed SQL/outbox
        # state. Never turn a successful mutation into a false 5xx response.
        log.exception("dispatch_projection_failed", topic=topic, event_type=event_type)
        return None


async def publish_ephemeral_once(
    redis: Redis,
    topic: str,
    event_type: str,
    data: dict[str, Any],
    *,
    idempotency_key: str,
    ttl_seconds: int,
    audience_user_refs: Sequence[str] | None = None,
) -> bool:
    """Publish once without retaining the payload in a mixed Redis stream.

    The idempotency marker contains no event body or callback credential. SQL
    remains the encrypted replay authority for reconnecting bot workers.
    """

    try:
        if (
            not idempotency_key.isascii()
            or not 1 <= len(idempotency_key) <= 256
            or not 1 <= ttl_seconds <= 86_400
        ):
            raise ValueError("dispatch idempotency binding is invalid")
        event = _dispatch_event(event_type, data, audience_user_refs)
        result = await cast(
            Awaitable[object],
            redis.eval(
                PUBLISH_EPHEMERAL_ONCE_SCRIPT,
                2,
                f"dispatch:{topic}",
                f"dispatch:once:{topic}:{idempotency_key}",
                json.dumps(event, separators=(",", ":")),
                str(ttl_seconds),
            ),
        )
        return bool(result)
    except Exception:
        log.exception(
            "dispatch_once_projection_failed",
            topic=topic,
            event_type=event_type,
            idempotency_key=idempotency_key,
        )
        return False


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
