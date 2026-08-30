from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Iterable
from typing import cast
from urllib.parse import urlsplit

from redis.asyncio import Redis

from app.chat.events import publish_presence, user_topic
from app.db.models import User

PRESENCE_TTL_SECONDS = 90
MAX_BOT_PRESENCE_ACTIVITIES = 16
MAX_PRESENCE_TIMESTAMP = (1 << 53) - 1
BOT_PRESENCE_STATUSES = frozenset({"online", "idle", "dnd", "invisible", "offline"})
BOT_ACTIVITY_TYPES = frozenset(range(6))

SET_PRESENCE_SCRIPT = """
local generation = redis.call('INCR', KEYS[1])
-- Preserve Python's JSON array/object distinction. Dragonfly's bundled cjson
-- encodes an empty decoded array as {}, which makes the strict worker decoder
-- reject otherwise valid presence state. The trusted base is always a compact
-- top-level object; generation and expiry remain atomic additions in Lua.
local state = string.sub(ARGV[1], 1, -2)
    .. ',"generation":' .. tostring(generation)
    .. ',"expires_at":' .. ARGV[2] .. '}'
redis.call('SET', KEYS[2], state)
redis.call('ZADD', KEYS[3], ARGV[2], ARGV[3])
return generation
"""


def encode_presence_state(
    status: str,
    activities: list[dict[str, object]],
    since: int | None,
    afk: bool,
    *,
    generation: int | None = None,
    expires_at: int | None = None,
) -> str:
    """Encode the one cache shape shared by local and federated presence."""

    state: dict[str, object] = {
        "status": status,
        "activities": activities,
        "since": since,
        "afk": afk,
    }
    if generation is not None and expires_at is not None:
        state["generation"] = generation
        state["expires_at"] = expires_at
    elif generation is not None or expires_at is not None:
        raise ValueError("presence generation and expiry must be encoded together")
    return json.dumps(state, separators=(",", ":"))


def _bounded_presence_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"presence activity {field} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"presence activity {field} is invalid") from exc
    if len(encoded) > maximum:
        raise ValueError(f"presence activity {field} is too large")
    return value


def normalize_bot_presence_activities(value: object) -> list[dict[str, object]]:
    """Validate the documented subset of Activity fields writable by bots."""

    if not isinstance(value, list) or len(value) > MAX_BOT_PRESENCE_ACTIVITIES:
        raise ValueError("presence activities must be a bounded array")
    normalized: list[dict[str, object]] = []
    for raw in value:
        if (
            not isinstance(raw, dict)
            or not {"name", "type"} <= set(raw)
            or not set(raw) <= {"name", "state", "type", "url"}
        ):
            raise ValueError("presence activity contains unsupported fields")
        name = _bounded_presence_text(raw.get("name"), field="name", maximum=128)
        activity_type = raw.get("type")
        if type(activity_type) is not int or activity_type not in BOT_ACTIVITY_TYPES:
            raise ValueError("presence activity type is invalid")
        rendered: dict[str, object] = {"name": name, "type": activity_type}
        state = raw.get("state")
        if state is not None:
            rendered["state"] = _bounded_presence_text(
                state,
                field="state",
                maximum=128,
            )
        elif "state" in raw:
            rendered["state"] = None
        url = raw.get("url")
        if url is not None:
            candidate = _bounded_presence_text(url, field="url", maximum=2048)
            try:
                parsed = urlsplit(candidate)
                port = parsed.port
            except ValueError as exc:
                raise ValueError("presence streaming URL is invalid") from exc
            if (
                activity_type != 1
                or parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or port not in {None, 443}
                or parsed.hostname
                not in {
                    "twitch.tv",
                    "www.twitch.tv",
                    "youtube.com",
                    "www.youtube.com",
                }
            ):
                raise ValueError("presence streaming URL is invalid")
            rendered["url"] = candidate
        elif "url" in raw:
            rendered["url"] = None
        normalized.append(rendered)
    return normalized


def normalize_presence_since(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_PRESENCE_TIMESTAMP:
        raise ValueError("presence since must be a non-negative millisecond timestamp")
    return value


def decode_presence_state(
    raw_state: str | bytes | None,
) -> tuple[str, int, list[dict[str, object]], int | None, bool] | None:
    """Strictly decode one Redis presence record for workers and member chunks."""

    if raw_state is None:
        return None
    try:
        state = json.loads(raw_state)
        if not isinstance(state, dict):
            return None
        status = state.get("status")
        generation = state.get("generation")
        afk = state.get("afk", False)
        if (
            status not in BOT_PRESENCE_STATUSES
            or type(generation) is not int
            or generation <= 0
            or type(afk) is not bool
        ):
            return None
        activities = normalize_bot_presence_activities(state.get("activities", []))
        since = normalize_presence_since(state.get("since"))
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return status, generation, activities, since, afk


async def set_presence_state(
    redis: Redis,
    user: User,
    status: str,
    *,
    activities: list[dict[str, object]] | None = None,
    since: int | None = None,
    afk: bool = False,
) -> int:
    expires_at = int(time.time()) + PRESENCE_TTL_SECONDS
    handle = f"{user.origin_domain}:{user.id}"
    encoded_state = encode_presence_state(status, activities or [], since, afk)
    result = await cast(
        Awaitable[object],
        redis.eval(
            SET_PRESENCE_SCRIPT,
            3,
            f"presence:generation:{handle}",
            f"presence:{handle}",
            "presence:expirations",
            encoded_state,
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
    *,
    activities: list[dict[str, object]] | None = None,
    since: int | None = None,
    afk: bool = False,
) -> tuple[str, int]:
    """Set authoritative presence and project it to every active client topic."""

    visible_status = "offline" if status == "invisible" else status
    rendered_activities = activities or []
    generation = await set_presence_state(
        redis,
        user,
        status,
        activities=rendered_activities,
        since=since,
        afk=afk,
    )
    private_topic = user_topic(user.origin_domain, user.id)
    public_presence = {
        "user_id": str(user.id),
        "user_domain": user.origin_domain,
        "status": visible_status,
        "activities": rendered_activities if visible_status != "offline" else [],
        "since": since if visible_status != "offline" else None,
        "afk": afk if visible_status != "offline" else False,
        "client_status": ({"web": visible_status} if visible_status != "offline" else {}),
    }
    for topic in dict.fromkeys(topics):
        if not (topic.startswith("guild:") or topic == private_topic):
            continue
        presence = (
            {
                **public_presence,
                "activities": rendered_activities,
                "since": since,
                "afk": afk,
            }
            if topic == private_topic
            else public_presence
        )
        await publish_presence(
            redis,
            topic,
            {**presence, **({"preference": status} if topic == private_topic else {})},
            user_domain=user.origin_domain,
            user_id=user.id,
            generation=generation,
        )
    return visible_status, generation
