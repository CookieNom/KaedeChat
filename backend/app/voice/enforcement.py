from __future__ import annotations

from dataclasses import replace

import structlog
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.permissions import calculate_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.db.models import Channel, Guild, User
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.rooms import parse_participant_identity, parse_room_name
from app.voice.state import bump_generation, remove_occupant, room_occupants, set_occupant

log = structlog.get_logger()


async def enforce_room_permissions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    room: str,
) -> int:
    """Re-evaluate authoritative grants for every connected guild participant.

    This periodic home-side check deliberately does not depend on each mutation
    path remembering a callback. New moderation and permission mutations are
    therefore secure by default, and transient LiveKit failures are retried on
    the next coordinator cycle.
    """

    kind, guild_id, channel_id = parse_room_name(room)
    if kind != "g":
        return 0
    guild = await session.get(Guild, (guild_id, settings.domain))
    channel = await session.get(Channel, (channel_id, settings.domain))
    if guild is None or channel is None or channel.guild_id != guild.id:
        return 0
    changed = 0
    for occupant in await room_occupants(redis, settings.domain, room):
        try:
            if channel.encryption_mode == "e2ee" and channel.encryption_state != "active":
                raise HTTPException(status_code=409)
            user_id, user_domain = parse_participant_identity(occupant.identity)
            user = await session.get(User, (user_id, user_domain))
            if user is None:
                raise HTTPException(status_code=404)
            permissions, member = await calculate_permissions(session, guild, user, channel=channel)
            can_connect = bool(permissions & Permission.CONNECT)
            can_speak = bool(permissions & Permission.SPEAK) and not bool(member.voice_flags & 1)
            can_stream = bool(permissions & Permission.STREAM)
            can_subscribe = not bool(member.voice_flags & 2)
        except (HTTPException, ValueError):
            can_connect = can_speak = can_stream = can_subscribe = False
        if not can_connect:
            await bump_generation(redis, settings.domain, room, occupant.identity)
            try:
                await LiveKitControl(settings).remove_participant(room, occupant.identity)
            except LiveKitError:
                log.warning("voice_revocation_retry_pending", room=room, identity=occupant.identity)
                continue
            await remove_occupant(redis, settings.domain, room, occupant.identity)
            changed += 1
            continue
        server_mute = not can_speak and bool(member.voice_flags & 1)
        server_deaf = not can_subscribe
        if (
            occupant.can_speak == can_speak
            and occupant.can_stream == can_stream
            and occupant.server_mute == server_mute
            and occupant.server_deaf == server_deaf
        ):
            continue
        await bump_generation(redis, settings.domain, room, occupant.identity)
        try:
            await LiveKitControl(settings).update_participant(
                room,
                occupant.identity,
                can_speak=can_speak,
                can_stream=can_stream,
                can_subscribe=can_subscribe,
            )
        except LiveKitError:
            log.warning(
                "voice_permission_update_retry_pending",
                room=room,
                identity=occupant.identity,
            )
            continue
        await set_occupant(
            redis,
            settings.domain,
            replace(
                occupant,
                can_speak=can_speak,
                can_stream=can_stream,
                server_mute=server_mute,
                server_deaf=server_deaf,
            ),
        )
        changed += 1
    return changed
