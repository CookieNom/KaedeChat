from __future__ import annotations

import time
from collections.abc import Awaitable, Iterable
from typing import cast

from redis.asyncio import Redis

from app.chat.events import publish_presence, user_topic
from app.db.models import User

PRESENCE_TTL_SECONDS = 90

SET_PRESENCE_SCRIPT = """
local generation = redis.call('INCR', KEYS[1])
local state = cjson.encode({
    status = ARGV[1],
    generation = generation,
    expires_at = tonumber(ARGV[2])
})
redis.call('SET', KEYS[2], state)
redis.call('ZADD', KEYS[3], ARGV[2], ARGV[3])
return generation
"""


async def set_presence_state(redis: Redis, user: User, status: str) -> int:
    expires_at = int(time.time()) + PRESENCE_TTL_SECONDS
    handle = f"{user.origin_domain}:{user.id}"
    result = await cast(
        Awaitable[object],
        redis.eval(
            SET_PRESENCE_SCRIPT,
            3,
            f"presence:generation:{handle}",
            f"presence:{handle}",
            "presence:expirations",
            status,
            str(expires_at),
            handle,
        ),
    )
    return int(cast(int | str, result))


async def broadcast_presence_preference(
    redis: Redis,
    user: User,
    status: str,
    topics: Iterable[str],
) -> tuple[str, int]:
    """Set authoritative presence and project it to every active client topic."""

    visible_status = "offline" if status == "invisible" else status
    generation = await set_presence_state(redis, user, status)
    private_topic = user_topic(user.origin_domain, user.id)
    presence = {
        "user_id": str(user.id),
        "user_domain": user.origin_domain,
        "status": visible_status,
    }
    for topic in dict.fromkeys(topics):
        if not (topic.startswith("guild:") or topic == private_topic):
            continue
        await publish_presence(
            redis,
            topic,
            {**presence, **({"preference": status} if topic == private_topic else {})},
            user_domain=user.origin_domain,
            user_id=user.id,
            generation=generation,
        )
    return visible_status, generation
