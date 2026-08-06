from __future__ import annotations

import asyncio
from typing import Any

import structlog
from redis.asyncio import Redis

from app.core.settings import Settings
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.rooms import parse_room_name
from app.voice.state import get_call

log = structlog.get_logger()


async def delete_terminal_call_room(settings: Settings, record: dict[str, Any]) -> bool:
    """Best-effort eviction for a terminal call owned by this instance."""

    if (
        not settings.voice_enabled
        or record.get("authority_domain") != settings.domain
        or record.get("state") != "ended"
    ):
        return False
    room = str(record.get("room", ""))
    try:
        kind, _channel_id, call_id = parse_room_name(room)
        if kind != "d" or call_id != int(str(record["id"])):
            raise ValueError("terminal call room does not match its call identity")
        async with asyncio.timeout(3):
            await LiveKitControl(settings).delete_room(room)
    except (KeyError, TypeError, ValueError, TimeoutError, LiveKitError):
        # Redis is the call authority. LiveKit control-plane failure must not
        # turn a committed terminal transition into a false client failure.
        log.warning("voice_terminal_room_cleanup_failed", room=room)
        return False
    return True


async def cleanup_orphaned_dm_rooms(
    redis: Redis,
    settings: Settings,
    *,
    limit: int = 500,
) -> int:
    """Delete ended or Redis-orphaned local-authority DM rooms."""

    if not settings.voice_enabled or limit < 1:
        return 0
    try:
        control = LiveKitControl(settings)
        async with asyncio.timeout(10):
            rooms = await control.list_rooms()
    except (TimeoutError, LiveKitError):
        log.warning("voice_call_room_listing_failed")
        return 0
    removed = 0
    names = sorted(str(room.name) for room in rooms if getattr(room, "name", None))
    for room in names:
        if removed >= limit:
            break
        try:
            kind, _channel_id, call_id = parse_room_name(room)
        except ValueError:
            continue
        if kind != "d":
            continue
        record = await get_call(redis, settings.domain, call_id)
        if (
            record is not None
            and record.get("authority_domain") == settings.domain
            and record.get("room") == room
            and record.get("state") != "ended"
        ):
            continue
        try:
            async with asyncio.timeout(3):
                await control.delete_room(room)
        except (TimeoutError, LiveKitError):
            log.warning("voice_orphaned_room_cleanup_failed", room=room)
            continue
        removed += 1
    return removed
