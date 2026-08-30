from __future__ import annotations

import json
import secrets
from contextlib import suppress
from typing import Literal

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import Channel, Guild, User
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError, decode_federation_response_json
from app.voice.rooms import guild_room_name, participant_identity
from app.voice.schemas import VoiceSelfStateFederationResponse, VoiceTokenResponse
from app.voice.service import federated_voice_grant_matches, require_e2ee_voice_device
from app.voice.state import (
    FederatedVoiceSession,
    Occupant,
    activate_federated_voice_home_session,
    begin_federated_voice_home_session,
    discard_pending_federated_voice_home_session,
    get_federated_voice_session,
    sync_federated_voice_session_generation,
    update_occupant_self_flags,
)


def _remote_voice_error(
    response: object,
    *,
    unavailable_code: str = "VOICE_HOME_UNREACHABLE",
) -> HTTPException:
    status_code = int(getattr(response, "status_code", 503))
    detail: dict[str, object] | None = None
    if 400 <= status_code < 500:
        try:
            body = decode_federation_response_json(response)  # type: ignore[arg-type]
            candidate = body.get("detail") if isinstance(body, dict) else None
            if isinstance(candidate, dict) and isinstance(candidate.get("code"), str):
                detail = candidate
        except (FederationNetworkError, ValueError, json.JSONDecodeError):
            pass
    if detail is not None:
        return HTTPException(status_code=status_code, detail=detail)
    return HTTPException(
        status_code=503,
        detail={"code": unavailable_code, "retry_after_ms": 2000},
        headers={"Retry-After": "2"},
    )


async def request_remote_guild_voice_token(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    channel: Channel,
    guild: Guild,
    actor: User,
    sender_device_id: str | None,
    connection_id: str,
    takeover: bool,
    client_kind: Literal["web", "desktop", "mobile"],
    allow_listen: bool = True,
    allow_speak: bool = True,
    allow_stream: bool = True,
) -> VoiceTokenResponse:
    """Broker one human guild voice grant while binding it to the member home."""

    if guild.origin_domain == settings.domain:
        raise ValueError("remote voice broker requires a remote guild authority")
    if actor.origin_domain != settings.domain or actor.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "VOICE_ACTOR_NOT_HOME"})
    await require_e2ee_voice_device(session, settings, channel, actor, sender_device_id)
    identity = participant_identity(actor.id, actor.origin_domain)
    move_session_id = secrets.token_urlsafe(32)
    expected_room = guild_room_name(guild.id, channel.id)
    await begin_federated_voice_home_session(
        redis,
        identity,
        FederatedVoiceSession(
            authority_domain=guild.origin_domain,
            guild_id=str(guild.id),
            room=expected_room,
            generation=0,
            move_session_id=move_session_id,
            client_kind=client_kind,
        ),
    )
    succeeded = False
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            "/_kaede/v1/voice/token",
            payload={
                "guild_id": str(guild.id),
                "channel_id": str(channel.id),
                "actor_id": str(actor.id),
                "actor_domain": actor.origin_domain,
                "move_session_id": move_session_id,
                "sender_device_id": sender_device_id,
                "connection_id": connection_id,
                "takeover": takeover,
                "client_kind": client_kind,
                "allow_listen": allow_listen,
                "allow_speak": allow_speak,
                "allow_stream": allow_stream,
            },
            request_timeout=5,
            max_response_bytes=16 * 1024,
        )
        if response.status_code != 200:
            raise _remote_voice_error(response)
        try:
            grant = VoiceTokenResponse.model_validate(decode_federation_response_json(response))
        except (FederationNetworkError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=502, detail={"code": "VOICE_HOME_INVALID_RESPONSE"}
            ) from exc
        if grant.move_session_id != move_session_id or not federated_voice_grant_matches(
            grant,
            channel,
            expected_room=expected_room,
            authority_domain=guild.origin_domain,
            client_kind=client_kind,
        ):
            raise HTTPException(status_code=502, detail={"code": "VOICE_HOME_INVALID_RESPONSE"})
        if not await activate_federated_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain=guild.origin_domain,
            guild_id=str(guild.id),
            room=grant.room,
            generation=grant.generation,
            connection_id=grant.connection_id,
            client_kind=client_kind,
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_SUPERSEDED"})
        succeeded = True
        return grant
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "VOICE_HOME_UNREACHABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc
    finally:
        if not succeeded:
            with suppress(Exception):
                await discard_pending_federated_voice_home_session(redis, identity, move_session_id)


async def _request_remote_voice_self_state(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    actor: User,
    active: FederatedVoiceSession,
    path: str,
    payload: dict[str, object],
    expected_guild_id: str | None,
    expected_channel_id: str,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant | None:
    identity = participant_identity(actor.id, actor.origin_domain)
    self_mute = self_mute or self_deaf
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            active.authority_domain,
            path,
            payload={
                **payload,
                "actor_id": str(actor.id),
                "room": active.room,
                "move_session_id": active.move_session_id,
                "generation": active.generation,
                "connection_id": active.connection_id,
                "self_mute": self_mute,
                "self_deaf": self_deaf,
            },
            request_timeout=5,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "VOICE_AUTHORITY_UNREACHABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc
    if response.status_code != 200:
        raise _remote_voice_error(response, unavailable_code="VOICE_AUTHORITY_UNREACHABLE")
    try:
        result = VoiceSelfStateFederationResponse.model_validate(
            decode_federation_response_json(response)
        )
    except (FederationNetworkError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "VOICE_AUTHORITY_INVALID_RESPONSE"},
        ) from exc
    public_state = result.state
    expected = (
        identity,
        str(actor.id),
        actor.origin_domain,
        active.room,
        self_mute,
        self_deaf,
    )
    received = (
        public_state.identity,
        public_state.user_id,
        public_state.user_domain,
        public_state.room,
        public_state.self_mute,
        public_state.self_deaf,
    )
    if (
        received != expected
        or public_state.guild_id != expected_guild_id
        or public_state.channel_id != expected_channel_id
        or result.generation != active.generation + 1
    ):
        raise HTTPException(
            status_code=502,
            detail={"code": "VOICE_AUTHORITY_INVALID_RESPONSE"},
        )
    if not await sync_federated_voice_session_generation(
        redis,
        "home",
        identity,
        move_session_id=active.move_session_id,
        authority_domain=active.authority_domain,
        room=active.room,
        connection_id=active.connection_id,
        expected_generation=active.generation,
        generation=result.generation,
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_SUPERSEDED"})
    # The authoritative room snapshot eventually repairs this projection. An
    # immediate exact-room update keeps the local Gateway state responsive and
    # cannot cross into a different authority namespace.
    projected = await update_occupant_self_flags(
        redis,
        active.authority_domain,
        active.room,
        identity,
        self_mute=self_mute,
        self_deaf=self_deaf,
    )
    return projected or Occupant(**public_state.model_dump(mode="python"))


async def request_remote_guild_voice_self_state(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    actor: User,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant | None:
    """Apply a human's client-owned voice flags at a remote guild authority."""

    if actor.origin_domain != settings.domain or actor.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "VOICE_ACTOR_NOT_HOME"})
    identity = participant_identity(actor.id, actor.origin_domain)
    active = await get_federated_voice_session(redis, "home", identity)
    if (
        active is None
        or not active.ready
        or not active.active
        or active.authority_domain == settings.domain
        or active.call_id is not None
        or not active.connection_id
    ):
        return None
    return await _request_remote_voice_self_state(
        session,
        redis,
        settings,
        actor=actor,
        active=active,
        path="/_kaede/v1/voice/self-state",
        payload={"guild_id": active.guild_id},
        expected_guild_id=active.guild_id,
        expected_channel_id=active.room.rsplit(".", 1)[-1],
        self_mute=self_mute,
        self_deaf=self_deaf,
    )


async def request_remote_dm_voice_self_state(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    actor: User,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant | None:
    """Apply client-owned flags at a remote DM-call authority."""

    if actor.origin_domain != settings.domain or actor.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "VOICE_ACTOR_NOT_HOME"})
    identity = participant_identity(actor.id, actor.origin_domain)
    active = await get_federated_voice_session(redis, "home", identity)
    if (
        active is None
        or not active.ready
        or not active.active
        or active.authority_domain == settings.domain
        or active.call_id is None
        or active.channel_id is None
        or not active.connection_id
    ):
        return None
    return await _request_remote_voice_self_state(
        session,
        redis,
        settings,
        actor=actor,
        active=active,
        path="/_kaede/v1/voice/dm-self-state",
        payload={"call_id": active.call_id, "channel_id": active.channel_id},
        expected_guild_id=None,
        expected_channel_id=active.channel_id,
        self_mute=self_mute,
        self_deaf=self_deaf,
    )
