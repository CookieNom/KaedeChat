from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import (
    installation_for_channel,
    installation_for_guild,
    require_installation_scope,
)
from app.api.calls import require_call_bot_capability
from app.api.dependencies import get_redis, get_session
from app.bots.auth import BotPrincipal, require_bot
from app.chat.permissions import get_permissions
from app.core.channel_types import GUILD_VOICE_CHANNEL_TYPES
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.bot_models import BotDMCapability, BotInstallation
from app.db.models import Guild
from app.voice.e2ee import bot_voice_lineage_metadata
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.regions import configured_voice_regions
from app.voice.rooms import guild_room_name, participant_identity
from app.voice.schemas import (
    BotVoiceDisconnectRequest,
    BotVoiceSelfStateRequest,
    BotVoiceTokenRequest,
    VoiceOccupantState,
    VoiceRegion,
    VoiceSelfStateFederationResponse,
    VoiceTokenResponse,
)
from app.voice.service import (
    STAGE_CHANNEL_TYPE,
    authoritative_guild_token,
    require_voice_enabled,
    update_authoritative_occupant_self_state,
)
from app.voice.state import (
    Occupant,
    bump_generation,
    get_active_call,
    occupant_in_room,
    public_occupant_state,
    release_voice_connection,
    remove_occupant_connection,
    voice_connection_matches,
)

router = APIRouter(prefix="/api/v1/bots", tags=["bot voice"])


def _missing_permissions(required: Permission) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": "MISSING_PERMISSIONS", "permissions": str(int(required))},
    )


def _occupant_has_exact_bot_lineage(
    occupant: Occupant,
    principal: BotPrincipal,
    installation: BotInstallation | BotDMCapability,
) -> bool:
    """Keep post-connect controls bound to the grant that minted the session."""

    expected = bot_voice_lineage_metadata(principal.worker, installation)
    return all(
        occupant.participant_metadata.get(field) == value for field, value in expected.items()
    )


@router.get("/voice/regions", response_model=list[VoiceRegion])
async def bot_voice_regions(
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[VoiceRegion]:
    principal.require_scope("voice.connect")
    return configured_voice_regions(settings)


@router.get("/guilds/{guild_ref}/voice/regions", response_model=list[VoiceRegion])
async def bot_guild_voice_regions(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[VoiceRegion]:
    await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "voice.connect",
    )
    return configured_voice_regions(settings)


@router.post(
    "/channels/{channel_ref}/voice/token",
    response_model=VoiceTokenResponse,
)
async def bot_channel_voice_token(
    channel_ref: EntityRef,
    payload: BotVoiceTokenRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VoiceTokenResponse:
    return await bot_channel_voice_token_service(
        channel_ref,
        payload,
        principal,
        session,
        redis,
        settings,
    )


async def bot_channel_voice_token_service(
    channel_ref: EntityRef,
    payload: BotVoiceTokenRequest,
    principal: BotPrincipal,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    self_mute: bool = False,
    self_deaf: bool = False,
) -> VoiceTokenResponse:
    """Mint one authority-local bot voice grant from REST or Gateway op 4."""

    require_voice_enabled(settings)
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "voice.connect"
    )
    if (
        not isinstance(installation, BotInstallation)
        or channel.type not in GUILD_VOICE_CHANNEL_TYPES
        or channel.guild_id is None
    ):
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    if payload.listen:
        require_installation_scope(principal, installation, "voice.listen")
    if payload.speak:
        require_installation_scope(principal, installation, "voice.speak")
    if payload.stream:
        require_installation_scope(principal, installation, "voice.stream")

    guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, principal.user, channel=channel)
    required = Permission.VIEW_CHANNEL | Permission.CONNECT
    if payload.speak and channel.type != STAGE_CHANNEL_TYPE:
        required |= Permission.SPEAK
    if payload.stream:
        required |= Permission.STREAM
    if permissions & required != required:
        raise _missing_permissions(required)

    connection_id = payload.connection_id or secrets.token_urlsafe(32)
    return await authoritative_guild_token(
        session,
        redis,
        settings,
        channel=channel,
        guild=guild,
        actor=principal.user,
        connection_id=connection_id,
        takeover=payload.takeover,
        client_kind="bot",
        allow_listen=payload.listen,
        allow_speak=payload.speak,
        allow_stream=payload.stream,
        sender_device_id=payload.sender_device_id,
        bot_installation=installation,
        bot_worker=principal.worker,
        self_mute=self_mute,
        self_deaf=self_deaf,
    )


async def bot_active_voice_session(
    channel_ref: EntityRef,
    payload: BotVoiceDisconnectRequest,
    principal: BotPrincipal,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> tuple[str, Occupant]:
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "voice.connect"
    )
    identity = participant_identity(principal.user.id, principal.user.origin_domain)
    if isinstance(installation, BotDMCapability):
        if channel.guild_id is not None or channel.type != 1:
            raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
        record = await get_active_call(redis, channel.origin_domain, channel.id)
        if record is None or record.get("state") == "ended":
            raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
        require_call_bot_capability(record, installation)
        room = str(record["room"])
        occupant = await occupant_in_room(redis, settings.domain, room, identity)
    else:
        if channel.type not in GUILD_VOICE_CHANNEL_TYPES or channel.guild_id is None:
            raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None or guild.unavailable:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        room = guild_room_name(channel.guild_id, channel.id)
        occupant = await occupant_in_room(redis, settings.domain, room, identity)
    if occupant is None or not _occupant_has_exact_bot_lineage(
        occupant,
        principal,
        installation,
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_SUPERSEDED"})
    if not await voice_connection_matches(
        redis,
        settings.domain,
        identity,
        connection_id=payload.connection_id,
        room=room,
        generation=payload.generation,
        client_kind="bot",
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_SUPERSEDED"})
    if occupant.connection_id != payload.connection_id:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_SUPERSEDED"})
    return room, occupant


@router.patch(
    "/channels/{channel_ref}/voice/@me",
    response_model=VoiceSelfStateFederationResponse,
)
async def bot_update_voice_self_state(
    channel_ref: EntityRef,
    payload: BotVoiceSelfStateRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VoiceSelfStateFederationResponse:
    room, occupant = await bot_active_voice_session(
        channel_ref,
        payload,
        principal,
        session,
        redis,
        settings,
    )
    updated = await update_authoritative_occupant_self_state(
        redis,
        settings,
        occupant,
        self_mute=payload.self_mute or payload.self_deaf,
        self_deaf=payload.self_deaf,
    )
    generation = updated.participant_metadata.get("generation")
    if type(generation) is not int:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_SUPERSEDED"})
    from app.tasks import voice_replicate_room

    await enqueue_best_effort(voice_replicate_room, room)
    return VoiceSelfStateFederationResponse(
        state=VoiceOccupantState.model_validate(public_occupant_state(updated)),
        generation=generation,
    )


@router.delete("/channels/{channel_ref}/voice", status_code=status.HTTP_204_NO_CONTENT)
async def bot_disconnect_voice(
    channel_ref: EntityRef,
    payload: BotVoiceDisconnectRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    room, _occupant = await bot_active_voice_session(
        channel_ref,
        payload,
        principal,
        session,
        redis,
        settings,
    )
    identity = participant_identity(principal.user.id, principal.user.origin_domain)

    # Revoke the server-side capability before asking the media control plane
    # to evict the participant.  A LiveKit outage must not strand the bot's
    # single-active-connection claim and prevent a clean reconnect.  The
    # reconciliation worker observes the generation/claim mismatch and retries
    # the physical eviction when control-plane access recovers.
    await bump_generation(redis, settings.domain, room, identity)
    control_error: LiveKitError | None = None
    try:
        await LiveKitControl(settings).remove_participant(room, identity)
    except LiveKitError as exc:
        control_error = exc
    await remove_occupant_connection(redis, settings.domain, room, identity, payload.connection_id)
    await release_voice_connection(
        redis,
        settings.domain,
        identity,
        payload.connection_id,
        room=room,
        client_kind="bot",
    )
    if control_error is not None:
        raise HTTPException(
            status_code=503,
            detail={"code": "VOICE_HOME_UNREACHABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from control_error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
