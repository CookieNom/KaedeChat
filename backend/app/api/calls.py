from __future__ import annotations

import json
import time
from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.chat.channel_access import load_channel_access
from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import public_user_display_name
from app.chat.privacy import blocked_between, require_can_direct_message
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.models import Channel, DMConversation, DMParticipant, User
from app.federation.client import signed_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
)
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)
from app.voice.cleanup import delete_terminal_call_room
from app.voice.livekit import LiveKitControl, LiveKitError, mint_join_token
from app.voice.rooms import dm_room_name, parse_participant_identity, participant_identity
from app.voice.schemas import (
    ActiveCallResponse,
    CallAction,
    CallCreate,
    CallFederationRequest,
    CallResponse,
    CallStateFederationRequest,
    DMVoiceBrokerRequest,
    VoiceTokenResponse,
)
from app.voice.service import require_voice_enabled
from app.voice.state import (
    apply_authoritative_call,
    create_call,
    current_generation,
    discard_all_federated_voice_home_sessions,
    get_active_call,
    get_call,
    is_call_accepted,
    transition_call,
)

router = APIRouter(tags=["calls"])
log = structlog.get_logger()

CALL_IDENTITY_FIELDS = (
    "id",
    "channel_id",
    "channel_domain",
    "authority_domain",
    "room",
    "created_at",
    "caller",
    "participants",
)


def call_response(record: dict[str, Any]) -> CallResponse:
    return CallResponse.model_validate(record)


def same_call_identity(current: dict[str, Any], incoming: dict[str, Any]) -> bool:
    return all(current.get(field) == incoming.get(field) for field in CALL_IDENTITY_FIELDS)


def authoritative_call_record(
    current: dict[str, Any], response: CallResponse, action: str
) -> dict[str, Any]:
    incoming = response.model_dump(mode="json")
    if not same_call_identity(current, incoming):
        raise ValueError("call authority changed immutable call identity")
    required_state = "active" if action == "accept" else "ended"
    if incoming["state"] != required_state:
        raise ValueError("call authority returned a state inconsistent with the action")
    return incoming


async def notify_call(
    redis: Redis,
    participants: list[str],
    event: str,
    record: dict[str, Any],
    settings: Settings,
) -> None:
    for identity in participants:
        try:
            user_id, domain = parse_participant_identity(identity)
            if domain == settings.domain:
                await publish_dispatch(redis, user_topic(domain, user_id), event, record)
        except Exception:
            # Call state is authoritative; gateway delivery is a recoverable
            # projection and must not turn a committed transition into a 5xx.
            log.exception("call_dispatch_failed", identity=identity, call_event=event)


async def propagate_terminal_call(
    session: AsyncSession,
    settings: Settings,
    record: dict[str, Any],
) -> None:
    response = call_response(record)
    if response.authority_domain != settings.domain or response.state != "ended":
        return
    destinations = {
        domain
        for identity in response.participants
        if (domain := parse_participant_identity(identity)[1]) != settings.domain
    }
    for destination in sorted(destinations):
        try:
            await signed_request(
                session,
                settings,
                "POST",
                destination,
                "/_kaede/v1/calls/state",
                payload={"call": response.model_dump(mode="json")},
                request_timeout=5,
                max_response_bytes=16 * 1024,
            )
        except FederationNetworkError:
            # Calls are deliberately ephemeral. A later idempotent action can
            # still retrieve the terminal authority state if this push drops.
            continue
        except Exception:
            # Federation propagation is also an ephemeral projection. Retrying
            # the terminal action will make another idempotent delivery attempt.
            log.exception("call_terminal_propagation_failed", destination=destination)


async def project_call_transition(
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    record: dict[str, Any],
    event: str,
    *,
    changed: bool,
) -> None:
    if changed or record.get("state") == "ended":
        try:
            await notify_call(
                redis,
                cast(list[str], record["participants"]),
                event,
                record,
                settings,
            )
        except Exception:
            log.exception("call_transition_dispatch_failed", call_event=event)
    if record.get("state") == "ended" and record.get("authority_domain") == settings.domain:
        # These operations are safe to repeat. In particular, a replay must
        # retry them after a lost response or a transient control-plane outage.
        try:
            await delete_terminal_call_room(settings, record)
        except Exception:
            log.exception("call_terminal_cleanup_failed", call_id=record.get("id"))
        try:
            await propagate_terminal_call(session, settings, record)
        except Exception:
            log.exception("call_terminal_projection_failed", call_id=record.get("id"))


async def local_dm_participants(
    session: AsyncSession, channel_id: int, channel_domain: str
) -> list[User]:
    return list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == channel_id,
                DMParticipant.conversation_domain == channel_domain,
            )
            .order_by(User.origin_domain, User.id)
        )
    )


async def require_call_policy(
    session: AsyncSession,
    settings: Settings,
    record: dict[str, Any],
    actor: User,
    participants: list[User] | None = None,
) -> list[User]:
    """Recheck DM membership, blocks, and recipient privacy at effect time."""
    if participants is None:
        participants = await local_dm_participants(
            session, int(record["channel_id"]), str(record["channel_domain"])
        )
    identities = {
        participant_identity(participant.id, participant.origin_domain): participant
        for participant in participants
    }
    actor_identity = participant_identity(actor.id, actor.origin_domain)
    if actor_identity not in identities or actor_identity not in record.get("participants", []):
        raise HTTPException(status_code=403, detail={"code": "CALL_FORBIDDEN"})
    caller_identity = str(record.get("caller", ""))
    caller = identities.get(caller_identity)
    if caller is None:
        raise HTTPException(status_code=403, detail={"code": "CALL_FORBIDDEN"})
    conversation = await session.get(
        DMConversation, (int(record["channel_id"]), str(record["channel_domain"]))
    )
    if conversation is not None and conversation.type == "group":
        return participants
    peers = [
        participant for identity, participant in identities.items() if identity != actor_identity
    ]
    for peer in peers:
        if await blocked_between(session, actor, peer):
            raise HTTPException(status_code=403, detail={"code": "DM_PRIVACY_REJECTED"})
    if actor_identity == caller_identity:
        # The caller must satisfy each locally authoritative recipient policy.
        for peer in peers:
            if peer.is_local and peer.origin_domain == settings.domain:
                await require_can_direct_message(session, actor, peer)
    elif actor.is_local and actor.origin_domain == settings.domain:
        # Accepting/joining rechecks the local recipient's current policy.
        await require_can_direct_message(session, caller, actor)
    return participants


@router.post("/api/v1/channels/{channel_ref}/calls", response_model=CallResponse)
async def start_call(
    channel_ref: EntityRef,
    payload: CallCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    require_voice_enabled(settings)
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.guild is not None or access.channel.type != 1:
        raise HTTPException(status_code=400, detail={"code": "CALL_REQUIRES_DM"})
    participants = await local_dm_participants(
        session, access.channel.id, access.channel.origin_domain
    )
    identities = {participant_identity(item.id, item.origin_domain) for item in participants}
    caller = participant_identity(auth.user.id, auth.user.origin_domain)
    call_id = await snowflake.mint()
    created_at = int(time.time())
    record: dict[str, Any] = {
        "id": str(call_id),
        "channel_id": str(access.channel.id),
        "channel_domain": access.channel.origin_domain,
        "authority_domain": settings.domain,
        "room": dm_room_name(access.channel.id, call_id),
        "state": "ringing",
        "created_at": created_at,
        "ended_at": None,
        "caller": caller,
        "participants": sorted(identities),
    }
    await require_call_policy(session, settings, record, auth.user, participants)
    if not await create_call(redis, record, identities, settings, accepted={caller}):
        raise HTTPException(status_code=409, detail={"code": "CALL_ALREADY_ACTIVE"})
    await notify_call(redis, sorted(identities), "CALL_CREATE", record, settings)
    if payload.ring:
        await notify_call(redis, sorted(identities - {caller}), "CALL_RING", record, settings)
    for destination in sorted({item.origin_domain for item in participants} - {settings.domain}):
        try:
            await signed_request(
                session,
                settings,
                "POST",
                destination,
                "/_kaede/v1/calls",
                payload={
                    "call_id": str(call_id),
                    "channel_id": str(access.channel.id),
                    "channel_domain": access.channel.origin_domain,
                    "authority_domain": settings.domain,
                    "actor_id": str(auth.user.id),
                    "actor_domain": auth.user.origin_domain,
                    "action": "create",
                    "created_at": created_at,
                },
                request_timeout=5,
                max_response_bytes=16 * 1024,
            )
        except FederationNetworkError:
            # The durable chat relationship is unaffected. A missed ephemeral
            # ring is surfaced as peer unavailability and may be retried by a
            # fresh call without creating phantom call state remotely.
            continue
        except Exception:
            log.exception("call_create_projection_failed", destination=destination)
    return call_response(record)


@router.get(
    "/api/v1/channels/{channel_ref}/calls/active",
    response_model=ActiveCallResponse,
)
async def active_call(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ActiveCallResponse:
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.guild is not None or access.channel.type != 1:
        raise HTTPException(status_code=400, detail={"code": "CALL_REQUIRES_DM"})
    record = await get_active_call(redis, access.channel.origin_domain, access.channel.id)
    if record is None or record.get("state") == "ended":
        return ActiveCallResponse(call=None)
    identity = participant_identity(auth.user.id, auth.user.origin_domain)
    participants = cast(list[str], record.get("participants", []))
    if identity not in participants:
        return ActiveCallResponse(call=None)
    await require_call_policy(session, settings, record, auth.user)
    authority = str(record["authority_domain"])
    call_id = int(record["id"])
    joined = identity == record.get("caller") or await is_call_accepted(
        redis, authority, call_id, identity
    )
    return ActiveCallResponse(call=call_response(record), joined=joined)


@router.post("/api/v1/calls/{call_ref}", response_model=CallResponse)
async def act_on_call(
    call_ref: EntityRef,
    payload: CallAction,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    call_id, authority = call_ref.resolve(settings.domain)
    record = await get_call(redis, authority, call_id)
    if record is None or record.get("authority_domain") != authority:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    identity = participant_identity(auth.user.id, auth.user.origin_domain)
    await require_call_policy(session, settings, record, auth.user)
    if authority != settings.domain:
        try:
            response = await signed_request(
                session,
                settings,
                "POST",
                authority,
                "/_kaede/v1/calls",
                payload={
                    "call_id": str(call_id),
                    "channel_id": str(record["channel_id"]),
                    "channel_domain": str(record["channel_domain"]),
                    "authority_domain": authority,
                    "actor_id": str(auth.user.id),
                    "actor_domain": auth.user.origin_domain,
                    "action": payload.action,
                    "created_at": int(record["created_at"]),
                },
                request_timeout=5,
                max_response_bytes=16 * 1024,
            )
        except FederationNetworkError as exc:
            raise HTTPException(status_code=503, detail={"code": "CALL_HOME_UNREACHABLE"}) from exc
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail={"code": "CALL_REJECTED"})
        try:
            authority_response = CallResponse.model_validate(
                decode_federation_response_json(response)
            )
            incoming = authoritative_call_record(record, authority_response, payload.action)
        except (FederationNetworkError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail={"code": "CALL_HOME_INVALID_RESPONSE"}
            ) from exc
        accepted, changed, result = await apply_authoritative_call(
            redis,
            incoming,
            settings,
            action=payload.action,
            identity=identity,
        )
        if not accepted:
            code = "CALL_NOT_FOUND" if result == "missing" else "CALL_HOME_INVALID_RESPONSE"
            raise HTTPException(
                status_code=404 if result == "missing" else 502, detail={"code": code}
            )
    else:
        accepted, changed, result = await transition_call(
            redis, authority, call_id, identity, payload.action, settings
        )
        if not accepted:
            code = "CALL_NOT_FOUND" if result == "missing" else "CALL_INVALID_TRANSITION"
            raise HTTPException(
                status_code=404 if result == "missing" else 409, detail={"code": code}
            )
    updated = cast(dict[str, Any], result)
    event = {"accept": "CALL_ACCEPT", "decline": "CALL_DECLINE", "end": "CALL_END"}[payload.action]
    await project_call_transition(
        redis,
        session,
        settings,
        updated,
        event,
        changed=changed,
    )
    return call_response(updated)


async def mint_dm_call_token(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    record: dict[str, Any],
    user: User,
) -> VoiceTokenResponse:
    await require_call_policy(session, settings, record, user)
    identity = participant_identity(user.id, user.origin_domain)
    participants = cast(list[str], record.get("participants", []))
    if identity not in participants or record.get("state") == "ended":
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    call_id = int(record["id"])
    authority = str(record["authority_domain"])
    accepted = await is_call_accepted(redis, authority, call_id, identity)
    if identity != record.get("caller") and not accepted:
        raise HTTPException(status_code=409, detail={"code": "CALL_NOT_ACCEPTED"})
    room = str(record["room"])
    generation = await current_generation(redis, authority, room, identity)
    metadata: dict[str, object] = {
        "version": 1,
        "generation": generation,
        "user_id": str(user.id),
        "user_domain": user.origin_domain,
        "channel_id": str(record["channel_id"]),
        "call_id": str(call_id),
        "server_mute": False,
        "server_deaf": False,
        "can_speak": True,
        "can_stream": True,
        "can_use_vad": True,
    }
    try:
        await LiveKitControl(settings).ensure_room(room)
        token, expires_at = mint_join_token(
            settings,
            room=room,
            identity=identity,
            display_name=public_user_display_name(user),
            metadata=metadata,
            can_speak=True,
            can_stream=True,
        )
    except LiveKitError as exc:
        raise HTTPException(status_code=503, detail={"code": "VOICE_HOME_UNREACHABLE"}) from exc
    return VoiceTokenResponse(
        token=token,
        url=cast(str, settings.voice_public_url),
        room=room,
        generation=generation,
        expires_at=expires_at.isoformat(),
        can_speak=True,
        can_stream=True,
        can_use_vad=True,
    )


@router.post("/api/v1/calls/{call_ref}/voice/token", response_model=VoiceTokenResponse)
async def call_voice_token(
    call_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    call_id, authority = call_ref.resolve(settings.domain)
    record = await get_call(redis, authority, call_id)
    if record is None or record.get("authority_domain") != authority:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    identity = participant_identity(auth.user.id, auth.user.origin_domain)
    if authority == settings.domain:
        grant = await mint_dm_call_token(session, redis, settings, record, auth.user)
        await discard_all_federated_voice_home_sessions(redis, identity)
        return grant
    await require_call_policy(session, settings, record, auth.user)
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            authority,
            "/_kaede/v1/voice/dm-token",
            payload={
                "call_id": str(call_id),
                "actor_id": str(auth.user.id),
                "actor_domain": auth.user.origin_domain,
            },
            request_timeout=5,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError as exc:
        raise HTTPException(status_code=503, detail={"code": "VOICE_HOME_UNREACHABLE"}) from exc
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail={"code": "VOICE_DENIED"})
    try:
        grant = VoiceTokenResponse.model_validate(decode_federation_response_json(response))
    except (FederationNetworkError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502, detail={"code": "VOICE_HOME_INVALID_RESPONSE"}
        ) from exc
    await discard_all_federated_voice_home_sessions(redis, identity)
    return grant


@router.post("/_kaede/v1/voice/dm-token", response_model=VoiceTokenResponse)
async def federation_dm_voice_token(
    payload: DMVoiceBrokerRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-voice-token", capacity=60, refill_per_minute=60
    )
    if payload.actor_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    record = await get_call(redis, settings.domain, int(payload.call_id))
    user = await session.get(User, (int(payload.actor_id), payload.actor_domain))
    if record is None or user is None or record.get("authority_domain") != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    await require_call_policy(session, settings, record, user)
    return await mint_dm_call_token(session, redis, settings, record, user)


@router.post("/_kaede/v1/calls", response_model=CallResponse)
async def federation_call_signal(
    payload: CallFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-call", capacity=120, refill_per_minute=120
    )
    if payload.actor_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    if payload.action == "create":
        if payload.authority_domain != principal.origin:
            raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    elif payload.authority_domain != settings.domain or payload.action == "ring":
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    if (
        payload.action == "create"
        and abs(payload.created_at - int(time.time())) > settings.federation_clock_skew_seconds
    ):
        raise HTTPException(status_code=400, detail={"code": "KAED_CALL_INVALID_TIMESTAMP"})
    call_id = int(payload.call_id)
    existing_call: dict[str, Any] | None = None
    channel_id = payload.channel_id
    channel_domain = payload.channel_domain
    if payload.action != "create":
        existing_call = await get_call(redis, settings.domain, call_id)
        if existing_call is None or existing_call.get("authority_domain") != settings.domain:
            raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
        if (
            str(existing_call.get("channel_id")) != payload.channel_id
            or existing_call.get("channel_domain") != payload.channel_domain
            or existing_call.get("created_at") != payload.created_at
        ):
            raise HTTPException(status_code=409, detail={"code": "CALL_CONTEXT_MISMATCH"})
        channel_id = str(existing_call["channel_id"])
        channel_domain = str(existing_call["channel_domain"])
    channel = await session.get(Channel, (int(channel_id), channel_domain))
    actor = await session.get(User, (int(payload.actor_id), payload.actor_domain))
    if channel is None or channel.type != 1 or actor is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    participants = await local_dm_participants(session, channel.id, channel.origin_domain)
    identities = {participant_identity(item.id, item.origin_domain) for item in participants}
    identity = participant_identity(actor.id, actor.origin_domain)
    if identity not in identities or (
        existing_call is not None and identity not in existing_call.get("participants", [])
    ):
        raise HTTPException(status_code=403, detail={"code": "CALL_FORBIDDEN"})
    if payload.action == "create":
        record = call_response(
            {
                "id": payload.call_id,
                "channel_id": payload.channel_id,
                "channel_domain": payload.channel_domain,
                "authority_domain": payload.authority_domain,
                "room": dm_room_name(channel.id, call_id),
                "state": "ringing",
                "created_at": payload.created_at,
                "ended_at": None,
                "caller": identity,
                "participants": sorted(identities),
            }
        ).model_dump(mode="json")
        await require_call_policy(session, settings, record, actor, participants)
        created = await create_call(redis, record, identities, settings, accepted={identity})
        if not created:
            existing = await get_call(redis, payload.authority_domain, call_id)
            if existing is None or not same_call_identity(existing, record):
                raise HTTPException(status_code=409, detail={"code": "CALL_ALREADY_ACTIVE"})
            return call_response(existing)
        await notify_call(redis, sorted(identities), "CALL_RING", record, settings)
        return call_response(record)
    if existing_call is None:
        raise RuntimeError("non-create call action lost its validated authority record")
    await require_call_policy(session, settings, existing_call, actor, participants)
    accepted, changed, result = await transition_call(
        redis, settings.domain, call_id, identity, payload.action, settings
    )
    if not accepted:
        code = "CALL_NOT_FOUND" if result == "missing" else "CALL_INVALID_TRANSITION"
        raise HTTPException(status_code=404 if result == "missing" else 409, detail={"code": code})
    updated = cast(dict[str, Any], result)
    event = {"accept": "CALL_ACCEPT", "decline": "CALL_DECLINE", "end": "CALL_END"}[payload.action]
    await project_call_transition(
        redis,
        session,
        settings,
        updated,
        event,
        changed=changed,
    )
    return call_response(updated)


@router.post("/_kaede/v1/calls/state", response_model=CallResponse)
async def federation_call_state(
    payload: CallStateFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-call-state", capacity=120, refill_per_minute=120
    )
    incoming = payload.call.model_dump(mode="json")
    if payload.call.authority_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    call_id = int(payload.call.id)
    existing = await get_call(redis, principal.origin, call_id)
    if existing is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    if not same_call_identity(existing, incoming):
        raise HTTPException(status_code=409, detail={"code": "CALL_CONTEXT_MISMATCH"})
    accepted, _changed, result = await apply_authoritative_call(
        redis,
        incoming,
        settings,
        action="sync",
    )
    if not accepted:
        code = "CALL_NOT_FOUND" if result == "missing" else "CALL_INVALID_TRANSITION"
        raise HTTPException(status_code=404 if result == "missing" else 409, detail={"code": code})
    updated = cast(dict[str, Any], result)
    await notify_call(
        redis,
        cast(list[str], updated["participants"]),
        "CALL_END",
        updated,
        settings,
    )
    return call_response(updated)
