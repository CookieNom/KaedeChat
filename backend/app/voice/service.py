from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import replace
from typing import cast
from urllib.parse import urlsplit

import structlog
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.e2ee import BotRuntimeInstallation, require_bot_e2ee_participation
from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import public_user_display_name
from app.chat.permissions import require_permissions
from app.core.base64url import encode_base64url
from app.core.channel_types import GUILD_VOICE_CHANNEL_TYPES
from app.core.permissions import Permission
from app.core.settings import Settings
from app.db.bot_models import BotDMCapability, BotInstallation, BotWorker
from app.db.models import Channel, E2EEDevice, Guild, GuildMember, GuildScheduledEvent, User
from app.voice.e2ee import bot_voice_lineage_metadata
from app.voice.livekit import LiveKitControl, LiveKitError, mint_join_token
from app.voice.permissions import STAGE_INSTANCE_MODERATOR_PERMISSIONS
from app.voice.rooms import guild_room_name, parse_room_name, participant_identity
from app.voice.schemas import VoiceTokenResponse, normalized_voice_timestamp
from app.voice.state import (
    Occupant,
    bump_generation,
    claim_voice_connection,
    claim_voice_grant_transition,
    current_generation,
    discard_federated_voice_session,
    get_federated_voice_session,
    occupant_for_identity,
    public_occupant_state,
    release_voice_connection,
    release_voice_grant_transition,
    remove_occupant,
    remove_occupant_connection,
    room_occupants,
    rotate_occupant_grant,
    sync_federated_voice_session_generation,
    voice_connection_matches,
)

log = structlog.get_logger()

VOICE_SERVER_MUTE = 1 << 0
VOICE_SERVER_DEAF = 1 << 1
VOICE_FLAG_MASK = VOICE_SERVER_MUTE | VOICE_SERVER_DEAF
MEDIA_E2EE_PROTOCOL = "livekit-e2ee-v1"
MEDIA_E2EE_SUITE = "AES-256-GCM"
STAGE_CHANNEL_TYPE = 13
PRIORITY_SPEAKER_CLIENT_KINDS = frozenset({"desktop", "bot"})


async def publish_bot_voice_session_update(
    redis: Redis,
    settings: Settings,
    occupant: Occupant,
    *,
    generation: int,
    connected: bool,
) -> None:
    """Deliver a private bot capability fence after authority-owned state changes."""

    if occupant.client_kind != "bot" or not occupant.connection_id:
        return
    kind, scope_id, leaf_id = parse_room_name(occupant.room)
    metadata = occupant.participant_metadata
    channel_domain = metadata.get("channel_domain")
    if not isinstance(channel_domain, str):
        channel_domain = settings.domain
    payload: dict[str, object] = {
        **public_occupant_state(occupant),
        "guild_id": str(scope_id) if kind == "g" else None,
        "guild_domain": settings.domain if kind == "g" else None,
        "channel_id": str(leaf_id if kind == "g" else scope_id),
        "channel_domain": channel_domain,
        "call_id": str(leaf_id) if kind == "d" else None,
        "connected": connected,
        "connection_id": occupant.connection_id,
        "generation": generation,
        "can_listen": occupant.allow_listen and not occupant.server_deaf and not occupant.self_deaf,
    }
    capability_id = metadata.get("bot_dm_capability_grant_id")
    if isinstance(capability_id, str):
        payload["bot_dm_capability_id"] = capability_id
    await publish_dispatch(
        redis,
        user_topic(occupant.user_domain, int(occupant.user_id)),
        "VOICE_STATE_UPDATE",
        payload,
    )


def is_stage_moderator(permissions: Permission) -> bool:
    return (
        permissions & STAGE_INSTANCE_MODERATOR_PERMISSIONS == STAGE_INSTANCE_MODERATOR_PERMISSIONS
    )


def voice_speaking_allowed(channel_type: int, permissions: Permission) -> bool:
    """Return whether channel permissions permit publishing microphone audio.

    Discord's SPEAK and USE_VAD permissions apply to ordinary voice channels,
    not Stage channels. A Stage participant's speaker capability is instead
    represented by the authoritative voice-state ``suppress`` flag.
    """

    return channel_type == STAGE_CHANNEL_TYPE or bool(permissions & Permission.SPEAK)


def voice_activity_allowed(channel_type: int, permissions: Permission) -> bool:
    """Return whether voice activity may be used for this channel type."""

    return channel_type == STAGE_CHANNEL_TYPE or bool(permissions & Permission.USE_VAD)


def priority_speaking_allowed(channel_type: int, permissions: Permission) -> bool:
    """Return whether an ordinary voice participant has priority speaking."""

    return channel_type == 2 and bool(permissions & Permission.PRIORITY_SPEAKER)


def priority_speaking_granted(
    *,
    channel_type: int,
    permissions: Permission,
    client_kind: str,
    can_speak: bool,
) -> bool:
    """Bind priority speech to its supported client and live speaking grant."""

    return (
        can_speak
        and client_kind in PRIORITY_SPEAKER_CLIENT_KINDS
        and priority_speaking_allowed(channel_type, permissions)
    )


async def effective_voice_user_limit(session: AsyncSession, channel: Channel) -> int:
    """Return the authoritative admission cap for this voice room.

    An active scheduled event hosted in an ordinary voice channel is capped at
    99 participants even when the channel itself is otherwise unlimited.
    """

    configured = int(channel.user_limit or 0)
    if getattr(channel, "type", 2) != 2:
        return configured
    active_event_id = await session.scalar(
        select(GuildScheduledEvent.id)
        .where(
            GuildScheduledEvent.channel_id == channel.id,
            GuildScheduledEvent.channel_domain == channel.origin_domain,
            GuildScheduledEvent.entity_type == 2,
            GuildScheduledEvent.status == 2,
        )
        .limit(1)
    )
    if not isinstance(active_event_id, int) or isinstance(active_event_id, bool):
        return configured
    return min(configured, 99) if configured else 99


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
    client_kind: str = "unknown",
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
        or (grant.can_priority_speak and (channel.type != 2 or not grant.can_speak))
        or (grant.can_priority_speak and client_kind not in PRIORITY_SPEAKER_CLIENT_KINDS)
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
    return encode_base64url(hashlib.sha256(context).digest())


async def require_e2ee_voice_device(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    actor: User,
    sender_device_id: str | None,
    *,
    remote_device_attested: bool = False,
    bot_installation: BotRuntimeInstallation | None = None,
    bot_worker_id: int | None = None,
) -> None:
    if channel.encryption_mode != "e2ee":
        return
    if channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    if sender_device_id is None:
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})
    if actor.account_type == "bot":
        if bot_installation is None or bot_worker_id is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
            )
        await require_bot_e2ee_participation(
            session,
            bot_installation,
            channel,
            sender_device_id,
            worker_id=bot_worker_id,
        )
        return
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
    if bool(metadata.get("e2ee")) != encrypted or (
        bool(metadata.get("can_priority_speak")) and channel.type != 2
    ):
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
            Channel.type.in_(GUILD_VOICE_CHANNEL_TYPES),
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
    bot_installation: BotRuntimeInstallation | None = None,
    bot_worker: BotWorker | None = None,
    connection_id: str,
    takeover: bool = False,
    client_kind: str = "web",
    allow_listen: bool = True,
    allow_speak: bool = True,
    allow_stream: bool = True,
    self_mute: bool = False,
    self_deaf: bool = False,
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    if guild.origin_domain != settings.domain or channel.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_HOME"})
    if getattr(actor, "disabled_at", None) is not None:
        raise HTTPException(status_code=403, detail={"code": "VOICE_DENIED"})
    permissions = await require_permissions(
        session,
        redis,
        guild,
        actor,
        Permission.CONNECT,
        channel=channel,
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
        bot_installation=bot_installation,
        bot_worker_id=bot_worker.id if bot_worker is not None else None,
    )
    e2ee_context = voice_e2ee_context(channel, room)
    identity = participant_identity(actor.id, actor.origin_domain)
    bot_lineage: dict[str, object] | None = None
    if actor.account_type == "bot":
        if (
            not isinstance(bot_installation, (BotInstallation, BotDMCapability))
            or bot_worker is None
        ):
            raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
        bot_lineage = bot_voice_lineage_metadata(bot_worker, bot_installation)
        if sender_device_id is not None:
            bot_lineage["bot_e2ee_device_id"] = sender_device_id
    user_limit = await effective_voice_user_limit(session, channel)
    if user_limit and not permissions & Permission.MOVE_MEMBERS:
        occupants = await room_occupants(redis, settings.domain, room)
        if (
            identity not in {occupant.identity for occupant in occupants}
            and len(occupants) >= user_limit
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VOICE_CHANNEL_FULL",
                    "message": "This voice channel has reached its user limit.",
                    "user_limit": user_limit,
                },
            )
    try:
        await LiveKitControl(settings).ensure_room(room)
    except LiveKitError as exc:
        log.warning("voice_home_unavailable", room=room, error_type=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"code": "VOICE_HOME_UNREACHABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc
    claimed, generation, previous_room, previous_client = await claim_voice_connection(
        redis,
        settings.domain,
        identity,
        connection_id=connection_id,
        room=room,
        client_kind=client_kind,
        takeover=takeover,
        bot_lineage=bot_lineage,
    )
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_ACTIVE_ELSEWHERE",
                "message": "Voice is already active for your account on another device.",
                "active_client": previous_client,
                "active_room": previous_room,
            },
        )
    if takeover and disconnect_previous and previous_room:
        try:
            await LiveKitControl(settings).remove_participant(previous_room, identity)
        except LiveKitError:
            log.warning(
                "voice_previous_connection_disconnect_failed",
                room=previous_room,
                identity=identity,
            )
        await remove_occupant(redis, settings.domain, previous_room, identity)
    server_mute = bool(member.voice_flags & VOICE_SERVER_MUTE)
    server_deaf = bool(member.voice_flags & VOICE_SERVER_DEAF)
    can_listen = allow_listen and not server_deaf
    channel_type = getattr(channel, "type", 2)
    suppressed = channel_type == STAGE_CHANNEL_TYPE and not is_stage_moderator(
        Permission(permissions)
    )
    can_speak = (
        allow_speak
        and voice_speaking_allowed(channel_type, Permission(permissions))
        and not server_mute
        and not suppressed
    )
    can_stream = allow_stream and bool(permissions & Permission.STREAM) and not suppressed
    can_priority_speak = priority_speaking_granted(
        channel_type=channel_type,
        permissions=Permission(permissions),
        client_kind=client_kind,
        can_speak=can_speak,
    )
    can_use_vad = voice_activity_allowed(channel_type, Permission(permissions))
    self_mute = self_mute or self_deaf
    metadata: dict[str, object] = {
        "version": 1,
        "generation": generation,
        "connection_id": connection_id,
        "client_kind": client_kind,
        "user_id": str(actor.id),
        "user_domain": actor.origin_domain,
        "guild_id": str(guild.id),
        "channel_id": str(channel.id),
        "channel_domain": channel.origin_domain,
        "e2ee": bool(e2ee_context),
        "server_mute": server_mute,
        "server_deaf": server_deaf,
        "self_mute": self_mute,
        "self_deaf": self_deaf,
        "suppressed": suppressed,
        "request_to_speak_timestamp": None,
        "can_speak": can_speak,
        "can_stream": can_stream,
        "can_priority_speak": can_priority_speak,
        "can_listen": can_listen,
        "allow_speak": allow_speak,
        "allow_stream": allow_stream,
        "allow_listen": allow_listen,
        "can_use_vad": can_use_vad,
    }
    if bot_lineage is not None:
        metadata.update(bot_lineage)
    if move_session_id is not None:
        metadata["move_session_id"] = move_session_id
    if e2ee_context:
        metadata.update({"e2ee": True, **e2ee_context})
    try:
        token, expires_at = mint_join_token(
            settings,
            room=room,
            identity=identity,
            display_name=public_user_display_name(actor),
            metadata=metadata,
            can_speak=can_speak,
            can_stream=can_stream,
            can_subscribe=can_listen,
            can_publish_data=can_priority_speak,
        )
    except LiveKitError as exc:
        try:
            await release_voice_connection(
                redis,
                settings.domain,
                identity,
                connection_id,
                room=room,
                client_kind=client_kind,
            )
        except Exception:
            log.warning(
                "voice_connection_claim_release_failed",
                room=room,
                identity=identity,
            )
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
        connection_id=connection_id,
        expires_at=expires_at.isoformat(),
        can_speak=can_speak,
        can_stream=can_stream,
        can_priority_speak=can_priority_speak,
        can_listen=can_listen,
        can_use_vad=can_use_vad,
        bitrate=int(channel.bitrate or 64_000),
        user_limit=user_limit,
        rtc_region=channel.rtc_region,
        video_quality_mode=int(channel.video_quality_mode or 1),
        e2ee=channel.encryption_mode == "e2ee",
        channel_id=str(channel.id),
        channel_domain=channel.origin_domain,
        guild_id=str(guild.id),
        guild_domain=guild.origin_domain,
        bot_installation_revision=(
            str(bot_installation.grant_revision)
            if isinstance(bot_installation, BotInstallation)
            else None
        ),
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


async def _apply_authoritative_occupant_transition(
    redis: Redis,
    settings: Settings,
    occupant: Occupant,
    updated: Occupant,
    metadata: dict[str, object],
    *,
    generation: int,
    can_speak: bool,
    can_stream: bool,
    can_priority_speak: bool,
    can_subscribe: bool,
) -> Occupant:
    """Apply a LiveKit grant/metadata change and rotate its reconnect token."""

    await LiveKitControl(settings).update_participant(
        occupant.room,
        occupant.identity,
        can_speak=can_speak,
        can_stream=can_stream,
        can_subscribe=can_subscribe,
        can_publish_data=can_priority_speak,
        metadata=metadata,
    )
    rotated = await rotate_occupant_grant(
        redis,
        settings.domain,
        updated,
        expected_generation=generation,
    )
    if rotated is None:
        # The media grant changed but its Redis authority raced. Fail closed
        # so neither the old nor the partially-updated capability survives.
        await bump_generation(redis, settings.domain, occupant.room, occupant.identity)
        try:
            await LiveKitControl(settings).remove_participant(
                occupant.room,
                occupant.identity,
            )
        except LiveKitError:
            log.warning(
                "voice_grant_conflict_disconnect_failed",
                room=occupant.room,
                identity=occupant.identity,
            )
        await remove_occupant(
            redis,
            settings.domain,
            occupant.room,
            occupant.identity,
        )
        if occupant.connection_id:
            await release_voice_connection(
                redis,
                settings.domain,
                occupant.identity,
                occupant.connection_id,
                room=occupant.room,
                client_kind=occupant.client_kind,
            )
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    metadata["generation"] = rotated
    federated_session = await get_federated_voice_session(redis, "authority", occupant.identity)
    move_session_id = metadata.get("move_session_id")
    if (
        federated_session is not None
        and federated_session.ready
        and federated_session.active
        and federated_session.authority_domain == settings.domain
        and federated_session.room == occupant.room
        and federated_session.connection_id == occupant.connection_id
        and isinstance(move_session_id, str)
        and federated_session.move_session_id == move_session_id
    ):
        # Server moderation, Stage changes, and self-state updates all rotate the
        # same reconnect capability. Keep the authority-side move fence on the
        # exact monotonic generation; the next signed snapshot advances home.
        synchronized = await sync_federated_voice_session_generation(
            redis,
            "authority",
            occupant.identity,
            move_session_id=move_session_id,
            authority_domain=settings.domain,
            room=occupant.room,
            connection_id=occupant.connection_id,
            expected_generation=generation,
            generation=rotated,
        )
        if not synchronized:
            latest_session = await get_federated_voice_session(
                redis,
                "authority",
                occupant.identity,
            )
            concurrent_session_change = latest_session is not None and any(
                (
                    latest_session.move_session_id != move_session_id,
                    latest_session.room != occupant.room,
                    latest_session.connection_id != occupant.connection_id,
                )
            )
            if not concurrent_session_change:
                # A changed correlation belongs to a concurrent move and must
                # not be disconnected from its new room. Missing or unchanged
                # state, however, would leave this rotated participant with an
                # unusable move fence, so revoke only the exact rotated grant.
                await bump_generation(redis, settings.domain, occupant.room, occupant.identity)
                try:
                    await LiveKitControl(settings).remove_participant(
                        occupant.room,
                        occupant.identity,
                    )
                except LiveKitError:
                    log.warning(
                        "voice_federated_generation_conflict_disconnect_failed",
                        room=occupant.room,
                        identity=occupant.identity,
                    )
                await remove_occupant_connection(
                    redis,
                    settings.domain,
                    occupant.room,
                    occupant.identity,
                    occupant.connection_id,
                    generation=rotated,
                )
                await release_voice_connection(
                    redis,
                    settings.domain,
                    occupant.identity,
                    occupant.connection_id,
                    room=occupant.room,
                    generation=rotated,
                    client_kind=occupant.client_kind,
                )
                if latest_session is not None:
                    await discard_federated_voice_session(
                        redis,
                        "authority",
                        occupant.identity,
                        move_session_id=latest_session.move_session_id,
                        room=latest_session.room,
                        authority_domain=latest_session.authority_domain,
                        connection_id=latest_session.connection_id,
                        generation=latest_session.generation,
                    )
                raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    result = replace(updated, participant_metadata=metadata)
    await publish_bot_voice_session_update(
        redis,
        settings,
        result,
        generation=rotated,
        connected=True,
    )
    return result


async def update_authoritative_occupant_grant(
    redis: Redis,
    settings: Settings,
    occupant: Occupant,
    *,
    can_speak: bool,
    can_stream: bool,
    can_priority_speak: bool = False,
    server_mute: bool | None = None,
    server_deaf: bool | None = None,
    suppressed: bool | None = None,
    request_to_speak_timestamp: str | None = None,
    update_request_timestamp: bool = False,
) -> Occupant:
    """Update a connected participant while atomically revoking its old JWT.

    LiveKit grants are capabilities embedded in a token. Merely changing the
    live participant would let the old token reconnect with stale privileges;
    rotating the generation keeps Stage promotion/demotion and server mutes
    durable without disconnecting the active participant.
    """

    if not occupant.connection_id or not occupant.participant_metadata:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    transition_token = secrets.token_urlsafe(24)
    if not await claim_voice_grant_transition(
        redis,
        settings.domain,
        occupant.room,
        occupant.identity,
        transition_token,
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_STATE_BUSY"})
    try:
        generation = await current_generation(
            redis,
            settings.domain,
            occupant.room,
            occupant.identity,
        )
        metadata_generation = occupant.participant_metadata.get("generation")
        if type(metadata_generation) is not int or metadata_generation != generation:
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
        resolved_server_mute = occupant.server_mute if server_mute is None else server_mute
        resolved_server_deaf = occupant.server_deaf if server_deaf is None else server_deaf
        resolved_suppressed = occupant.suppressed if suppressed is None else suppressed
        resolved_request = (
            request_to_speak_timestamp
            if update_request_timestamp
            else occupant.request_to_speak_timestamp
        )
        metadata = dict(occupant.participant_metadata)
        metadata.update(
            {
                "generation": generation + 1,
                "server_mute": resolved_server_mute,
                "server_deaf": resolved_server_deaf,
                "suppressed": resolved_suppressed,
                "request_to_speak_timestamp": resolved_request,
                "can_speak": can_speak,
                "can_stream": can_stream,
                "can_priority_speak": can_priority_speak,
                "can_listen": occupant.allow_listen and not resolved_server_deaf,
            }
        )
        updated = replace(
            occupant,
            server_mute=resolved_server_mute,
            server_deaf=resolved_server_deaf,
            suppressed=resolved_suppressed,
            request_to_speak_timestamp=resolved_request,
            can_speak=can_speak,
            can_stream=can_stream,
            can_priority_speak=can_priority_speak,
            participant_metadata=metadata,
        )
        return await _apply_authoritative_occupant_transition(
            redis,
            settings,
            occupant,
            updated,
            metadata,
            generation=generation,
            can_speak=can_speak,
            can_stream=can_stream,
            can_priority_speak=can_priority_speak,
            can_subscribe=(
                occupant.allow_listen and not resolved_server_deaf and not occupant.self_deaf
            ),
        )
    finally:
        await release_voice_grant_transition(
            redis,
            settings.domain,
            occupant.room,
            occupant.identity,
            transition_token,
        )


async def update_authoritative_occupant_self_state(
    redis: Redis,
    settings: Settings,
    occupant: Occupant,
    *,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant:
    """Persist client-owned voice flags at the room authority and in LiveKit.

    The same transition lock used by grant rotation prevents a self-state
    update from restoring stale participant metadata over a concurrent server
    mute, Stage promotion, or move. Keeping the flags in LiveKit metadata also
    lets coordinator reconciliation survive an API-process restart.
    """

    if not occupant.connection_id or not occupant.participant_metadata:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    # Discord treats self-deaf as also self-muted. Enforce it at the
    # authority so raw Gateway and signed federation callers cannot persist an
    # impossible deafened-but-transmitting state.
    self_mute = self_mute or self_deaf
    transition_token = secrets.token_urlsafe(24)
    if not await claim_voice_grant_transition(
        redis,
        settings.domain,
        occupant.room,
        occupant.identity,
        transition_token,
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_STATE_BUSY"})
    try:
        generation = await current_generation(
            redis,
            settings.domain,
            occupant.room,
            occupant.identity,
        )
        metadata_generation = occupant.participant_metadata.get("generation")
        if (
            type(metadata_generation) is not int
            or metadata_generation != generation
            or not await voice_connection_matches(
                redis,
                settings.domain,
                occupant.identity,
                connection_id=occupant.connection_id,
                room=occupant.room,
                generation=generation,
                client_kind=occupant.client_kind,
            )
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
        metadata = dict(occupant.participant_metadata)
        metadata.update(
            {
                "generation": generation + 1,
                "self_mute": self_mute,
                "self_deaf": self_deaf,
            }
        )
        updated = replace(
            occupant,
            self_mute=self_mute,
            self_deaf=self_deaf,
            participant_metadata=metadata,
        )
        return await _apply_authoritative_occupant_transition(
            redis,
            settings,
            occupant,
            updated,
            metadata,
            generation=generation,
            can_speak=occupant.can_speak,
            can_stream=occupant.can_stream,
            can_priority_speak=occupant.can_priority_speak,
            can_subscribe=occupant.allow_listen and not occupant.server_deaf and not self_deaf,
        )
    finally:
        await release_voice_grant_transition(
            redis,
            settings.domain,
            occupant.room,
            occupant.identity,
            transition_token,
        )


async def update_current_authoritative_occupant_self_state(
    redis: Redis,
    settings: Settings,
    identity: str,
    *,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant | None:
    """Apply self state only when the identity has a live local-authority room."""

    occupant = await occupant_for_identity(redis, settings.domain, identity)
    if occupant is None:
        return None
    return await update_authoritative_occupant_self_state(
        redis,
        settings,
        occupant,
        self_mute=self_mute,
        self_deaf=self_deaf,
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
        "connection_id": str,
        "client_kind": str,
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
    if type(metadata.get("can_priority_speak", False)) is not bool:
        raise ValueError("invalid voice token metadata")
    if "suppressed" in metadata and type(metadata["suppressed"]) is not bool:
        raise ValueError("invalid voice token metadata")
    for field in ("self_mute", "self_deaf"):
        if field in metadata and type(metadata[field]) is not bool:
            raise ValueError("invalid voice token metadata")
    request_timestamp = metadata.get("request_to_speak_timestamp")
    if request_timestamp is not None:
        normalized_voice_timestamp(cast(str, request_timestamp))
    if (
        len(cast(str, metadata["connection_id"])) != 43
        or not cast(str, metadata["connection_id"]).replace("_", "a").replace("-", "a").isalnum()
        or metadata["client_kind"] not in {"web", "desktop", "mobile", "bot"}
    ):
        raise ValueError("invalid voice connection metadata")
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
    bot_common_fields = {
        "bot_application_id",
        "bot_application_domain",
        "bot_worker_id",
    }
    bot_guild_fields = {"bot_installation_id", "bot_installation_revision"}
    bot_dm_fields = {
        "bot_dm_capability_grant_id",
        "bot_dm_capability_revision",
        "bot_installation_ref",
        "bot_installation_type",
    }
    bot_optional_fields = {"bot_e2ee_device_id"}
    bot_fields = bot_common_fields | bot_guild_fields | bot_dm_fields | bot_optional_fields
    if metadata["client_kind"] == "bot":
        application_id = metadata.get("bot_application_id")
        if (
            not isinstance(application_id, str)
            or not application_id.isascii()
            or not application_id.isdecimal()
            or application_id.startswith("0")
            or not isinstance(metadata.get("bot_application_domain"), str)
            or type(metadata.get("bot_worker_id")) is not int
            or (
                metadata.get("bot_e2ee_device_id") is not None
                and not isinstance(metadata.get("bot_e2ee_device_id"), str)
            )
        ):
            raise ValueError("bot voice metadata is missing worker lineage")
        if kind == "g":
            if (
                type(metadata.get("bot_installation_id")) is not int
                or type(metadata.get("bot_installation_revision")) is not int
                or any(metadata.get(field) is not None for field in bot_dm_fields)
            ):
                raise ValueError("bot voice metadata has invalid guild lineage")
        elif (
            not isinstance(metadata.get("bot_dm_capability_grant_id"), str)
            or type(metadata.get("bot_dm_capability_revision")) is not int
            or not isinstance(metadata.get("bot_installation_ref"), str)
            or metadata.get("bot_installation_type") not in {"guild", "user"}
            or any(metadata.get(field) is not None for field in bot_guild_fields)
        ):
            raise ValueError("bot voice metadata has invalid DM lineage")
    elif any(metadata.get(field) is not None for field in bot_fields):
        raise ValueError("human voice metadata contains bot lineage")
    return cast(dict[str, object], metadata)
