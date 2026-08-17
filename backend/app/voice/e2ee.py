from __future__ import annotations

from redis.asyncio import Redis

from app.core.settings import Settings
from app.db.models import Channel, DMConversation
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.rooms import guild_room_name
from app.voice.state import (
    bump_generation,
    get_active_call,
    remove_occupant,
    room_occupants,
)


class MediaSessionRotationError(RuntimeError):
    """The old media session could not be safely fenced."""


async def evict_channel_media_sessions(
    redis: Redis,
    settings: Settings,
    channel: Channel,
    *,
    conversation: DMConversation | None = None,
) -> None:
    """Fence every grant and connection that can carry the channel's old key."""

    if not settings.voice_enabled:
        return
    rooms: set[str] = set()
    if channel.type == 2 and channel.guild_id is not None:
        rooms.add(guild_room_name(channel.guild_id, channel.id))
    elif conversation is not None and conversation.authority_domain == settings.domain:
        record = await get_active_call(redis, settings.domain, channel.id)
        if record is not None and record.get("state") != "ended":
            rooms.add(str(record["room"]))
    if not rooms:
        return

    try:
        control = LiveKitControl(settings)
        existing = {str(room.name) for room in await control.list_rooms()}
        for room in rooms:
            if room not in existing:
                continue
            occupants = await room_occupants(redis, settings.domain, room)
            # Fence the short-lived JWT before deleting the room. A delayed or
            # replayed join is then rejected by the admission webhook.
            for occupant in occupants:
                await bump_generation(redis, settings.domain, room, occupant.identity)
            await control.delete_room(room)
            for occupant in occupants:
                await remove_occupant(redis, settings.domain, room, occupant.identity)
    except LiveKitError as exc:
        raise MediaSessionRotationError("could not fence the old media session") from exc
