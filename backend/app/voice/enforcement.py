from __future__ import annotations

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
from app.voice.service import (
    STAGE_CHANNEL_TYPE,
    priority_speaking_granted,
    update_authoritative_occupant_grant,
    voice_speaking_allowed,
)
from app.voice.state import (
    bump_generation,
    release_voice_connection,
    remove_occupant,
    room_occupants,
)

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
    if (
        guild is None
        or channel is None
        or (channel.guild_id, channel.guild_domain)
        != (
            guild.id,
            guild.origin_domain,
        )
    ):
        return 0
    changed = 0
    for occupant in await room_occupants(redis, settings.domain, room):
        try:
            if channel.encryption_mode == "e2ee" and channel.encryption_state != "active":
                raise HTTPException(status_code=409)
            user_id, user_domain = parse_participant_identity(occupant.identity)
            user = await session.get(User, (user_id, user_domain))
            if user is None or user.disabled_at is not None:
                raise HTTPException(status_code=404)
            permissions, member = await calculate_permissions(session, guild, user, channel=channel)
            can_connect = bool(permissions & Permission.CONNECT)
            base_can_speak = (
                occupant.allow_speak
                and voice_speaking_allowed(channel.type, Permission(permissions))
                and not bool(member.voice_flags & 1)
            )
            suppressed = channel.type == STAGE_CHANNEL_TYPE and (
                occupant.suppressed or not occupant.allow_speak
            )
            can_speak = base_can_speak and not suppressed
            can_stream = (
                occupant.allow_stream and bool(permissions & Permission.STREAM) and not suppressed
            )
            can_priority_speak = priority_speaking_granted(
                channel_type=channel.type,
                permissions=Permission(permissions),
                client_kind=occupant.client_kind,
                can_speak=can_speak,
            )
            can_subscribe = occupant.allow_listen and not bool(member.voice_flags & 2)
        except (HTTPException, ValueError):
            can_connect = can_speak = can_stream = can_priority_speak = can_subscribe = False
        if not can_connect:
            await bump_generation(redis, settings.domain, room, occupant.identity)
            try:
                await LiveKitControl(settings).remove_participant(room, occupant.identity)
            except LiveKitError:
                log.warning("voice_revocation_retry_pending", room=room, identity=occupant.identity)
                continue
            await remove_occupant(redis, settings.domain, room, occupant.identity)
            if occupant.connection_id:
                await release_voice_connection(
                    redis,
                    settings.domain,
                    occupant.identity,
                    occupant.connection_id,
                    room=room,
                    client_kind=occupant.client_kind,
                )
            changed += 1
            continue
        server_mute = bool(member.voice_flags & 1)
        server_deaf = not can_subscribe
        request_to_speak_timestamp = occupant.request_to_speak_timestamp if suppressed else None
        if (
            occupant.can_speak == can_speak
            and occupant.can_stream == can_stream
            and occupant.can_priority_speak == can_priority_speak
            and occupant.server_mute == server_mute
            and occupant.server_deaf == server_deaf
            and occupant.suppressed == suppressed
            and occupant.request_to_speak_timestamp == request_to_speak_timestamp
        ):
            continue
        try:
            await update_authoritative_occupant_grant(
                redis,
                settings,
                occupant,
                can_speak=can_speak,
                can_stream=can_stream,
                can_priority_speak=can_priority_speak,
                server_mute=server_mute,
                server_deaf=server_deaf,
                suppressed=suppressed,
                request_to_speak_timestamp=request_to_speak_timestamp,
                update_request_timestamp=True,
            )
        except (HTTPException, LiveKitError):
            log.warning(
                "voice_permission_update_retry_pending",
                room=room,
                identity=occupant.identity,
            )
            continue
        changed += 1
    return changed
