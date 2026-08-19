from __future__ import annotations

import base64
import hashlib
import json
from typing import cast
from urllib.parse import urlsplit

import structlog
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.payloads import public_user_display_name
from app.chat.permissions import get_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.db.models import Channel, E2EEDevice, Guild, GuildMember, User
from app.voice.livekit import LiveKitControl, LiveKitError, mint_join_token
from app.voice.rooms import guild_room_name, parse_room_name, participant_identity
from app.voice.schemas import VoiceTokenResponse
from app.voice.state import bump_generation, current_generation, remove_occupant

log = structlog.get_logger()

VOICE_SERVER_MUTE = 1 << 0
VOICE_SERVER_DEAF = 1 << 1
VOICE_FLAG_MASK = VOICE_SERVER_MUTE | VOICE_SERVER_DEAF
MEDIA_E2EE_PROTOCOL = "livekit-e2ee-v1"
MEDIA_E2EE_SUITE = "AES-256-GCM"


def valid_federated_voice_url(value: str, authority_domain: str) -> bool:
    """Accept only a configured TLS LiveKit endpoint on the signed authority."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "wss"
        and parsed.hostname == authority_domain
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/", "/livekit"}
        and "?" not in value
        and "#" not in value
        and not parsed.query
        and not parsed.fragment
    )


def federated_voice_grant_matches(
    grant: VoiceTokenResponse,
    channel: Channel,
    *,
    expected_room: str,
    authority_domain: str,
) -> bool:
    """Bind a federated media grant to the exact requested room and policy.

    The channel reference and endpoint are authorization context even for a
    plaintext room.  Keeping those checks outside the encrypted-only branch
    prevents an authority response from substituting a different room or media
    host while still returning ``e2ee=false``.
    """

    encrypted = channel.encryption_mode == "e2ee"
    if (
        grant.room != expected_room
        or not valid_federated_voice_url(grant.url, authority_domain)
        or grant.channel_id != str(channel.id)
        or grant.channel_domain != channel.origin_domain
        or grant.e2ee != encrypted
    ):
        return False
    if not encrypted:
        # VoiceTokenResponse already requires every encryption-context field to
        # be absent for a plaintext grant.
        return True
    return bool(
        channel.encryption_state == "active"
        and channel.encryption_epoch is not None
        and grant.encryption_policy_generation == str(channel.encryption_policy_generation)
        and grant.encryption_epoch == str(channel.encryption_epoch)
        and grant.media_protocol == MEDIA_E2EE_PROTOCOL
        and grant.media_suite == MEDIA_E2EE_SUITE
        and grant.media_session_id == media_session_id(channel, expected_room)
        and grant.media_epoch == str(channel.encryption_epoch)
    )


def media_session_id(channel: Channel, room: str) -> str:
    if (
        channel.encryption_mode != "e2ee"
        or channel.encryption_group_id is None
        or channel.encryption_epoch is None
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"})
    context = "\0".join(
        (
            "kaede-livekit-session-v1",
            room,
            f"{channel.id}@{channel.origin_domain}",
            channel.encryption_group_id,
            str(channel.encryption_policy_generation),
            str(channel.encryption_epoch),
            MEDIA_E2EE_PROTOCOL,
            MEDIA_E2EE_SUITE,
        )
    ).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(context).digest()).rstrip(b"=").decode()


async def require_e2ee_voice_device(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    actor: User,
    sender_device_id: str | None,
    *,
    remote_device_attested: bool = False,
) -> None:
    if channel.encryption_mode != "e2ee":
        return
    if channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    if sender_device_id is None:
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})
    if actor.origin_domain != settings.domain:
        if remote_device_attested:
            return
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})
    device = await session.scalar(
        select(E2EEDevice).where(
            E2EEDevice.id == sender_device_id,
            E2EEDevice.user_id == actor.id,
            E2EEDevice.user_domain == actor.origin_domain,
            E2EEDevice.revoked_at.is_(None),
        )
    )
    if device is None:
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})


def voice_e2ee_context(channel: Channel, room: str) -> dict[str, str]:
    if channel.encryption_mode != "e2ee":
        return {}
    if channel.encryption_state != "active" or channel.encryption_epoch is None:
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    return {
        "encryption_policy_generation": str(channel.encryption_policy_generation),
        "encryption_epoch": str(channel.encryption_epoch),
        "media_protocol": MEDIA_E2EE_PROTOCOL,
        "media_suite": MEDIA_E2EE_SUITE,
        "media_session_id": media_session_id(channel, room),
        "media_epoch": str(channel.encryption_epoch),
    }


def voice_metadata_matches_policy(
    channel: Channel,
    room: str,
    metadata: dict[str, object],
) -> bool:
    encrypted = channel.encryption_mode == "e2ee"
    if bool(metadata.get("e2ee")) != encrypted:
        return False
    if not encrypted:
        return True
    if channel.encryption_state != "active" or channel.encryption_epoch is None:
        return False
    return metadata == {
        **metadata,
        "e2ee": True,
        "encryption_policy_generation": str(channel.encryption_policy_generation),
        "encryption_epoch": str(channel.encryption_epoch),
        "media_protocol": MEDIA_E2EE_PROTOCOL,
        "media_suite": MEDIA_E2EE_SUITE,
        "media_session_id": media_session_id(channel, room),
        "media_epoch": str(channel.encryption_epoch),
    }


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
    move_session_id: str | None = None,
    disconnect_previous: bool = True,
    sender_device_id: str | None = None,
    remote_device_attested: bool = False,
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
    await require_e2ee_voice_device(
        session,
        settings,
        channel,
        actor,
        sender_device_id,
        remote_device_attested=remote_device_attested,
    )
    e2ee_context = voice_e2ee_context(channel, room)
    identity = participant_identity(actor.id, actor.origin_domain)
    previous_raw = await redis.get(f"voice:user-room:{identity}")
    if disconnect_previous and previous_raw is not None:
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
        "channel_domain": channel.origin_domain,
        "e2ee": bool(e2ee_context),
        "server_mute": server_mute,
        "server_deaf": server_deaf,
        "can_speak": can_speak,
        "can_stream": can_stream,
        "can_use_vad": can_use_vad,
    }
    if move_session_id is not None:
        metadata["move_session_id"] = move_session_id
    if e2ee_context:
        metadata.update({"e2ee": True, **e2ee_context})
    try:
        await LiveKitControl(settings).ensure_room(room)
        token, expires_at = mint_join_token(
            settings,
            room=room,
            identity=identity,
            display_name=public_user_display_name(actor),
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
        e2ee=channel.encryption_mode == "e2ee",
        channel_id=str(channel.id),
        channel_domain=channel.origin_domain,
        encryption_policy_generation=(
            str(channel.encryption_policy_generation) if channel.encryption_mode == "e2ee" else None
        ),
        encryption_epoch=(
            str(channel.encryption_epoch)
            if channel.encryption_mode == "e2ee" and channel.encryption_epoch is not None
            else None
        ),
        media_protocol=e2ee_context.get("media_protocol"),
        media_suite=e2ee_context.get("media_suite"),
        media_session_id=e2ee_context.get("media_session_id"),
        media_epoch=e2ee_context.get("media_epoch"),
        move_session_id=move_session_id,
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
        "channel_domain": str,
        "e2ee": bool,
        "can_speak": bool,
        "can_stream": bool,
        "can_use_vad": bool,
        "server_mute": bool,
        "server_deaf": bool,
    }
    for name, expected in required.items():
        if type(metadata.get(name)) is not expected:
            raise ValueError("invalid voice token metadata")
    e2ee_fields = {
        "encryption_policy_generation",
        "encryption_epoch",
        "media_protocol",
        "media_suite",
        "media_session_id",
        "media_epoch",
    }
    if metadata["e2ee"]:
        if (
            any(not isinstance(metadata.get(field), str) for field in e2ee_fields)
            or metadata.get("media_protocol") != MEDIA_E2EE_PROTOCOL
            or metadata.get("media_suite") != MEDIA_E2EE_SUITE
        ):
            raise ValueError("invalid encrypted voice metadata")
    elif any(metadata.get(field) is not None for field in e2ee_fields):
        raise ValueError("plaintext voice metadata contains encryption context")
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
    move_session_id = metadata.get("move_session_id")
    if move_session_id is not None and (
        not isinstance(move_session_id, str)
        or not 32 <= len(move_session_id) <= 64
        or any(
            not (character.isascii() and (character.isalnum() or character in "_-"))
            for character in move_session_id
        )
    ):
        raise ValueError("invalid voice move correlation")
    return cast(dict[str, object], metadata)
