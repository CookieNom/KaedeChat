from __future__ import annotations

import json
from typing import cast

import structlog
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.permissions import get_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.db.models import Channel, Guild, GuildMember, User
from app.voice.livekit import LiveKitControl, LiveKitError, mint_join_token
from app.voice.rooms import guild_room_name, parse_room_name, participant_identity
from app.voice.schemas import VoiceTokenResponse
from app.voice.state import bump_generation, current_generation, remove_occupant

log = structlog.get_logger()

VOICE_SERVER_MUTE = 1 << 0
VOICE_SERVER_DEAF = 1 << 1
VOICE_FLAG_MASK = VOICE_SERVER_MUTE | VOICE_SERVER_DEAF


def require_voice_enabled(settings: Settings) -> None:
    if not settings.voice_enabled:
        raise HTTPException(status_code=503, detail={"code": "VOICE_DISABLED"})


async def load_voice_channel(
    session: AsyncSession,
    channel_id: int,
    channel_domain: str,
) -> tuple[Channel, Guild]:
    channel = await session.scalar(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.origin_domain == channel_domain,
            Channel.type == 2,
            Channel.unavailable.is_(False),
        )
    )
    if channel is None or channel.guild_id is None or channel.guild_domain is None:
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    return channel, guild


async def authoritative_guild_token(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    channel: Channel,
    guild: Guild,
    actor: User,
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    if guild.origin_domain != settings.domain or channel.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_HOME"})
    permissions = await get_permissions(session, redis, guild, actor, channel=channel)
    if not permissions & Permission.CONNECT:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MISSING_PERMISSIONS",
                "message": "You do not have permission to join this voice channel.",
                "permissions": str(int(Permission.CONNECT)),
            },
        )
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    room = guild_room_name(guild.id, channel.id)
    identity = participant_identity(actor.id, actor.origin_domain)
    previous_raw = await redis.get(f"voice:user-room:{identity}")
    if previous_raw is not None:
        previous_room = (
            previous_raw.decode() if isinstance(previous_raw, bytes) else str(previous_raw)
        )
        if previous_room != room:
            await bump_generation(redis, settings.domain, previous_room, identity)
            try:
                await LiveKitControl(settings).remove_participant(previous_room, identity)
            except LiveKitError:
                log.warning(
                    "voice_previous_room_disconnect_failed",
                    room=previous_room,
                    identity=identity,
                )
            await remove_occupant(redis, settings.domain, previous_room, identity)
    generation = await current_generation(redis, settings.domain, room, identity)
    server_mute = bool(member.voice_flags & VOICE_SERVER_MUTE)
    server_deaf = bool(member.voice_flags & VOICE_SERVER_DEAF)
    can_speak = bool(permissions & Permission.SPEAK) and not server_mute
    can_stream = bool(permissions & Permission.STREAM)
    can_use_vad = bool(permissions & Permission.USE_VAD)
    metadata: dict[str, object] = {
        "version": 1,
        "generation": generation,
        "user_id": str(actor.id),
        "user_domain": actor.origin_domain,
        "guild_id": str(guild.id),
        "channel_id": str(channel.id),
        "server_mute": server_mute,
        "server_deaf": server_deaf,
        "can_speak": can_speak,
        "can_stream": can_stream,
        "can_use_vad": can_use_vad,
    }
    try:
        await LiveKitControl(settings).ensure_room(room)
        token, expires_at = mint_join_token(
            settings,
            room=room,
            identity=identity,
            display_name=actor.display_name or actor.username,
            metadata=metadata,
            can_speak=can_speak,
            can_stream=can_stream,
            can_subscribe=not server_deaf,
        )
    except LiveKitError as exc:
        log.warning("voice_home_unavailable", room=room, error_type=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"code": "VOICE_HOME_UNREACHABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc
    return VoiceTokenResponse(
        token=token,
        url=cast(str, settings.voice_public_url),
        room=room,
        generation=generation,
        expires_at=expires_at.isoformat(),
        can_speak=can_speak,
        can_stream=can_stream,
        can_use_vad=can_use_vad,
    )


def parse_minted_metadata(raw: str, *, room: str, identity: str) -> dict[str, object]:
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid voice token metadata") from exc
    if not isinstance(metadata, dict):
        raise ValueError("invalid voice token metadata")
    required = {
        "generation": int,
        "user_id": str,
        "user_domain": str,
        "channel_id": str,
        "can_speak": bool,
        "can_stream": bool,
        "can_use_vad": bool,
        "server_mute": bool,
        "server_deaf": bool,
    }
    for name, expected in required.items():
        if type(metadata.get(name)) is not expected:
            raise ValueError("invalid voice token metadata")
    metadata_identity = participant_identity(
        int(cast(str, metadata["user_id"])), str(metadata["user_domain"])
    )
    if metadata_identity != identity:
        raise ValueError("voice metadata identity mismatch")
    kind, scope_id, leaf_id = parse_room_name(room)
    channel_id = int(cast(str, metadata["channel_id"]))
    room_matches = (kind == "g" and channel_id == leaf_id) or (
        kind == "d"
        and channel_id == scope_id
        and int(cast(str, metadata.get("call_id", "-1"))) == leaf_id
    )
    if not room_matches:
        raise ValueError("voice metadata room mismatch")
    return cast(dict[str, object], metadata)
