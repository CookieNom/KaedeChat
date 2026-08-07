from __future__ import annotations

import json
import time
from contextlib import suppress
from dataclasses import asdict
from typing import Any, cast
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
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
from app.chat.audit import add_audit_entry
from app.chat.events import guild_topic, publish_dispatch, publish_ephemeral, user_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.hierarchy import require_can_manage_member
from app.chat.permissions import get_permissions, require_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.models import GuildMember, User
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    bounded_request_body,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)
from app.voice.livekit import LiveKitControl, LiveKitError, receive_webhook
from app.voice.rooms import parse_participant_identity, parse_room_name, participant_identity
from app.voice.schemas import (
    VoiceBrokerRequest,
    VoiceModerationUpdate,
    VoiceMoveFederationRequest,
    VoiceMoveRequest,
    VoiceStateFederationRequest,
    VoiceTokenResponse,
)
from app.voice.service import (
    VOICE_FLAG_MASK,
    VOICE_SERVER_DEAF,
    VOICE_SERVER_MUTE,
    authoritative_guild_token,
    load_voice_channel,
    parse_minted_metadata,
    require_voice_enabled,
)
from app.voice.state import (
    Occupant,
    bump_generation,
    current_generation,
    occupancy_snapshot,
    remove_occupant,
    replace_occupancy,
    room_occupants,
    room_state_key,
    set_occupant,
)

router = APIRouter(tags=["voice"])
log = structlog.get_logger()


def voice_audit_reason(value: str | None) -> str | None:
    if value is not None and len(value) > 512:
        raise HTTPException(status_code=400, detail={"code": "AUDIT_REASON_TOO_LONG"})
    return value


@router.post("/api/v1/channels/{channel_ref}/voice/token", response_model=VoiceTokenResponse)
async def channel_voice_token(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel, guild = await load_voice_channel(session, channel_id, channel_domain)
    # The replica performs the fast cached CONNECT check before contacting the
    # home. The home repeats the same calculation against authoritative state.
    permissions = await get_permissions(session, redis, guild, auth.user, channel=channel)
    if not permissions & Permission.CONNECT:
        raise HTTPException(
            status_code=403,
            detail={"code": "MISSING_PERMISSIONS", "permissions": str(int(Permission.CONNECT))},
        )
    if guild.origin_domain == settings.domain:
        return await authoritative_guild_token(
            session, redis, settings, channel=channel, guild=guild, actor=auth.user
        )
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
                "actor_id": str(auth.user.id),
                "actor_domain": auth.user.origin_domain,
            },
            request_timeout=5,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "VOICE_HOME_UNREACHABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc
    if response.status_code != 200:
        if response.status_code in {401, 403, 404}:
            raise HTTPException(status_code=response.status_code, detail={"code": "VOICE_DENIED"})
        raise HTTPException(
            status_code=503,
            detail={"code": "VOICE_HOME_UNREACHABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        )
    try:
        return VoiceTokenResponse.model_validate(response.json())
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502, detail={"code": "VOICE_HOME_INVALID_RESPONSE"}
        ) from exc


@router.post("/_kaede/v1/voice/token", response_model=VoiceTokenResponse)
async def federation_voice_token(
    payload: VoiceBrokerRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "voice-token", capacity=60, refill_per_minute=60
    )
    if payload.actor_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    channel, guild = await load_voice_channel(session, int(payload.channel_id), settings.domain)
    if guild.id != int(payload.guild_id) or guild.origin_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    actor = await session.get(User, (int(payload.actor_id), payload.actor_domain))
    if actor is None:
        raise HTTPException(status_code=404, detail={"code": "VOICE_USER_NOT_FOUND"})
    return await authoritative_guild_token(
        session, redis, settings, channel=channel, guild=guild, actor=actor
    )


@router.get("/api/v1/channels/{channel_ref}/voice/occupancy")
async def channel_voice_occupancy(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel, guild = await load_voice_channel(session, channel_id, channel_domain)
    permissions = await get_permissions(session, redis, guild, auth.user, channel=channel)
    if not permissions & Permission.VIEW_CHANNEL:
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    from app.voice.rooms import guild_room_name

    return await occupancy_snapshot(
        redis,
        guild.origin_domain,
        guild_room_name(guild.id, channel.id),
        settings,
    )


@router.patch("/api/v1/guilds/{guild_ref}/members/{user_ref}/voice", status_code=204)
async def update_member_voice_moderation(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: VoiceModerationUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    require_voice_enabled(settings)
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    from app.api.guilds import local_guild

    guild = await local_guild(session, settings, guild_ref, for_update=True)
    user_id, user_domain = user_ref.resolve(settings.domain)
    needed = Permission(0)
    if payload.server_mute is not None:
        needed |= Permission.MUTE_MEMBERS
    if payload.server_deaf is not None:
        needed |= Permission.DEAFEN_MEMBERS
    if not needed:
        raise HTTPException(status_code=400, detail={"code": "VOICE_NO_CHANGES"})
    await require_permissions(session, redis, guild, auth.user, needed)
    member = await require_can_manage_member(session, guild, auth.user, user_id, user_domain)
    flags = member.voice_flags & VOICE_FLAG_MASK
    if payload.server_mute is not None:
        flags = flags | VOICE_SERVER_MUTE if payload.server_mute else flags & ~VOICE_SERVER_MUTE
    if payload.server_deaf is not None:
        flags = flags | VOICE_SERVER_DEAF if payload.server_deaf else flags & ~VOICE_SERVER_DEAF
    old_flags = member.voice_flags
    changed = flags != old_flags
    if changed:
        member.voice_flags = flags
        member.member_version += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.update",
            {
                "member": {
                    "user": {"id": str(user_id), "origin_domain": user_domain},
                    "nickname": member.nickname,
                    "timeout_until": (
                        member.timeout_until.isoformat() if member.timeout_until else None
                    ),
                    "timeout_indefinite": member.timeout_indefinite,
                    "timeout_reason": member.timeout_reason,
                    "voice_flags": member.voice_flags,
                    "member_version": str(member.member_version),
                }
            },
            snapshot_required=True,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            24,
            target_type="member",
            target_ref={"id": str(user_id), "origin_domain": user_domain},
            reason=voice_audit_reason(reason),
            changes=[
                {
                    "key": "voice_flags",
                    "old_value": str(old_flags),
                    "new_value": str(flags),
                }
            ],
        )
        await session.commit()
        await wake_queued_guild_federation(guild)
    else:
        # Release the guild lock before contacting Redis or LiveKit even when
        # the requested state is already current.
        await session.commit()
    identity = participant_identity(user_id, user_domain)
    room_raw = await redis.get(f"voice:user-room:{identity}")
    if room_raw is not None:
        room = room_raw.decode() if isinstance(room_raw, bytes) else str(room_raw)
        await bump_generation(redis, settings.domain, room, identity)
        occupants = {
            item.identity: item for item in await room_occupants(redis, settings.domain, room)
        }
        current = occupants.get(identity)
        if current is not None:
            with suppress(LiveKitError):
                await LiveKitControl(settings).update_participant(
                    room,
                    identity,
                    can_speak=current.can_speak and not bool(flags & VOICE_SERVER_MUTE),
                    can_stream=current.can_stream,
                    can_subscribe=not bool(flags & VOICE_SERVER_DEAF),
                )
        await publish_ephemeral(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "VOICE_STATE_UPDATE",
            {
                "guild_id": str(guild.id),
                "user_id": str(user_id),
                "user_domain": user_domain,
                "server_mute": bool(flags & VOICE_SERVER_MUTE),
                "server_deaf": bool(flags & VOICE_SERVER_DEAF),
            },
        )
    return Response(status_code=204)


@router.delete("/api/v1/guilds/{guild_ref}/members/{user_ref}/voice", status_code=204)
async def disconnect_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    from app.api.guilds import local_guild

    guild = await local_guild(session, settings, guild_ref, for_update=True)
    await require_permissions(session, redis, guild, auth.user, Permission.MOVE_MEMBERS)
    user_id, user_domain = user_ref.resolve(settings.domain)
    await require_can_manage_member(session, guild, auth.user, user_id, user_domain)
    identity = participant_identity(user_id, user_domain)
    room_raw = await redis.get(f"voice:user-room:{identity}")
    if room_raw is not None:
        room = room_raw.decode() if isinstance(room_raw, bytes) else str(room_raw)
        kind, room_guild_id, _ = parse_room_name(room)
        if kind != "g" or room_guild_id != guild.id:
            raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            27,
            target_type="member",
            target_ref={"id": str(user_id), "origin_domain": user_domain},
            reason=voice_audit_reason(reason),
        )
        await session.commit()
        await bump_generation(redis, settings.domain, room, identity)
        try:
            await LiveKitControl(settings).remove_participant(room, identity)
        except LiveKitError:
            log.warning("voice_disconnect_control_failed", room=room, identity=identity)
        await remove_occupant(redis, settings.domain, room, identity)
    else:
        await session.commit()
    return Response(status_code=204)


@router.post("/api/v1/guilds/{guild_ref}/members/{user_ref}/voice/move", status_code=204)
async def move_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: VoiceMoveRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    from app.api.guilds import local_guild

    guild = await local_guild(session, settings, guild_ref, for_update=True)
    await require_permissions(session, redis, guild, auth.user, Permission.MOVE_MEMBERS)
    user_id, user_domain = user_ref.resolve(settings.domain)
    await require_can_manage_member(session, guild, auth.user, user_id, user_domain)
    target_id, target_domain = payload.channel_id.resolve(settings.domain)
    target_channel, target_guild = await load_voice_channel(session, target_id, target_domain)
    if target_domain != settings.domain or (target_guild.id, target_guild.origin_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    target_user = await session.get(User, (user_id, user_domain))
    if target_user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    target_permissions = await get_permissions(
        session, redis, guild, target_user, channel=target_channel
    )
    if not target_permissions & Permission.CONNECT:
        raise HTTPException(status_code=403, detail={"code": "TARGET_CANNOT_CONNECT"})
    identity = participant_identity(user_id, user_domain)
    source_raw = await redis.get(f"voice:user-room:{identity}")
    if source_raw is None:
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_CONNECTED"})
    source_room = source_raw.decode() if isinstance(source_raw, bytes) else str(source_raw)
    kind, source_guild_id, source_channel_id = parse_room_name(source_room)
    if kind != "g" or source_guild_id != guild.id:
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
    if source_room == f"g.{guild.id}.{target_channel.id}":
        return Response(status_code=204)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        26,
        target_type="member",
        target_ref={"id": str(user_id), "origin_domain": user_domain},
        reason=voice_audit_reason(reason),
        changes=[
            {
                "key": "channel_id",
                "old_value": str(source_channel_id),
                "new_value": str(target_channel.id),
            }
        ],
    )
    grant = await authoritative_guild_token(
        session,
        redis,
        settings,
        channel=target_channel,
        guild=guild,
        actor=target_user,
    )
    await session.commit()
    await bump_generation(redis, settings.domain, source_room, identity)
    try:
        await LiveKitControl(settings).remove_participant(source_room, identity)
    except LiveKitError:
        log.warning("voice_move_disconnect_failed", room=source_room, identity=identity)
    await remove_occupant(redis, settings.domain, source_room, identity)
    move_data = {
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "channel_id": str(target_channel.id),
        "channel_domain": target_channel.origin_domain,
    }
    if user_domain == settings.domain:
        await publish_dispatch(
            redis, user_topic(user_domain, user_id), "VOICE_CHANNEL_MOVE", move_data
        )
        await publish_dispatch(
            redis,
            user_topic(user_domain, user_id),
            "VOICE_TOKEN",
            {**move_data, "grant": grant.model_dump()},
        )
    else:
        try:
            await signed_request(
                session,
                settings,
                "POST",
                user_domain,
                "/_kaede/v1/voice/move",
                payload={
                    "guild_id": str(guild.id),
                    "channel_id": str(target_channel.id),
                    "target_id": str(user_id),
                    "target_domain": user_domain,
                    "grant": grant.model_dump(),
                },
                request_timeout=5,
                max_response_bytes=32 * 1024,
            )
        except FederationNetworkError:
            log.warning("voice_move_delivery_failed", destination=user_domain)
    return Response(status_code=204)


@router.post("/_kaede/v1/voice/move", status_code=204)
async def federation_voice_move(
    payload: VoiceMoveFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "voice-move", capacity=30, refill_per_minute=30
    )
    if payload.target_domain != settings.domain:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    channel, guild = await load_voice_channel(session, int(payload.channel_id), principal.origin)
    target = await session.get(User, (int(payload.target_id), settings.domain))
    if target is None or guild.id != int(payload.guild_id):
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    permissions = await get_permissions(session, redis, guild, target, channel=channel)
    if not permissions & Permission.CONNECT:
        raise HTTPException(status_code=403, detail={"code": "VOICE_DENIED"})
    expected_room = f"g.{payload.guild_id}.{payload.channel_id}"
    if payload.grant.room != expected_room:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_ROOM"})
    if urlsplit(payload.grant.url).hostname != principal.origin:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"})
    topic = user_topic(settings.domain, int(payload.target_id))
    move_data = {
        "guild_id": payload.guild_id,
        "guild_domain": principal.origin,
        "channel_id": payload.channel_id,
        "channel_domain": principal.origin,
    }
    await publish_dispatch(redis, topic, "VOICE_CHANNEL_MOVE", move_data)
    await publish_dispatch(
        redis,
        topic,
        "VOICE_TOKEN",
        {**move_data, "grant": payload.grant.model_dump()},
    )
    return Response(status_code=204)


@router.post("/internal/livekit/webhook", include_in_schema=False, status_code=204)
async def livekit_webhook(
    request: Request,
    authorization: str = Header(default="", alias="Authorization"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    require_voice_enabled(settings)
    raw = await bounded_request_body(
        request,
        max_bytes=256 * 1024,
        too_large_code="VOICE_WEBHOOK_TOO_LARGE",
    )
    try:
        body = raw.decode("utf-8")
        event = receive_webhook(settings, body, authorization)
    except (UnicodeDecodeError, LiveKitError) as exc:
        raise HTTPException(status_code=401, detail={"code": "VOICE_WEBHOOK_INVALID"}) from exc
    event_id = str(getattr(event, "id", ""))
    dedupe_key = f"voice:webhook:{event_id}" if event_id else None
    if dedupe_key is not None:
        state = await redis.get(dedupe_key)
        if state in {"done", b"done"}:
            return Response(status_code=204)
        if state is not None or not await redis.set(dedupe_key, "processing", ex=60, nx=True):
            raise HTTPException(
                status_code=503,
                detail={"code": "VOICE_WEBHOOK_IN_PROGRESS"},
                headers={"Retry-After": "1"},
            )

    async def completed() -> Response:
        if dedupe_key is not None:
            await redis.set(dedupe_key, "done", ex=24 * 60 * 60)
        return Response(status_code=204)

    event_type = str(event.event)
    room = str(event.room.name) if event.HasField("room") else ""
    if not room:
        return await completed()
    try:
        kind, scope_id, leaf_id = parse_room_name(room)
    except ValueError:
        log.warning("ignored_foreign_livekit_room", room=room)
        return await completed()
    if event_type == "room_finished":
        await redis.delete(
            room_state_key("occupancy", settings.domain, room),
            room_state_key("heartbeat", settings.domain, room),
        )
        await cast(Any, redis.srem("voice:rooms", room))
        return await completed()
    if not event.HasField("participant"):
        return await completed()
    # Track publication webhooks include a participant shell but do not
    # guarantee the token metadata carried by join/leave events. They are not
    # occupancy transitions, so validating them as joins can evict a healthy
    # participant immediately after their microphone is published.
    if event_type not in {"participant_joined", "participant_left"}:
        return await completed()
    identity = str(event.participant.identity)
    try:
        user_id, user_domain = parse_participant_identity(identity)
    except ValueError:
        return await completed()
    if event_type == "participant_joined":
        try:
            metadata = parse_minted_metadata(
                str(event.participant.metadata), room=room, identity=identity
            )
        except ValueError:
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            return await completed()
        generation = int(cast(int, metadata["generation"]))
        if generation != await current_generation(redis, settings.domain, room, identity):
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            return await completed()
        if kind == "g":
            try:
                channel, guild = await load_voice_channel(session, leaf_id, settings.domain)
                actor = await session.get(User, (user_id, user_domain))
                if (
                    actor is None
                    or guild.id != scope_id
                    or str(metadata.get("guild_id")) != str(scope_id)
                    or str(metadata.get("channel_id")) != str(leaf_id)
                ):
                    raise HTTPException(status_code=403, detail={"code": "VOICE_JOIN_REVOKED"})
                member = await session.get(
                    GuildMember,
                    (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
                )
                permissions = await get_permissions(session, redis, guild, actor, channel=channel)
                required = Permission.VIEW_CHANNEL | Permission.CONNECT
                if member is None or permissions & required != required:
                    raise HTTPException(status_code=403, detail={"code": "VOICE_JOIN_REVOKED"})
            except HTTPException:
                # Tokens are short-lived capabilities, not durable permission.
                # Recheck SQL at the actual LiveKit admission event so a token
                # minted before a kick or overwrite denial cannot be replayed.
                with suppress(LiveKitError):
                    await LiveKitControl(settings).remove_participant(room, identity)
                return await completed()
        resolved_channel_id = leaf_id if kind == "g" else scope_id
        occupant = Occupant(
            identity=identity,
            user_id=str(user_id),
            user_domain=user_domain,
            room=room,
            guild_id=str(scope_id) if kind == "g" else None,
            channel_id=str(resolved_channel_id),
            joined_at=int(time.time()),
            server_mute=bool(metadata["server_mute"]),
            server_deaf=bool(metadata["server_deaf"]),
            can_speak=bool(metadata["can_speak"]),
            can_stream=bool(metadata["can_stream"]),
        )
        await set_occupant(redis, settings.domain, occupant)
    elif event_type == "participant_left":
        await remove_occupant(redis, settings.domain, room, identity)
    else:
        return await completed()
    topic = (
        guild_topic(settings.domain, scope_id) if kind == "g" else user_topic(user_domain, user_id)
    )
    await publish_ephemeral(
        redis,
        topic,
        "VOICE_STATE_UPDATE",
        {
            "room": room,
            "guild_id": str(scope_id) if kind == "g" else None,
            "channel_id": str(leaf_id if kind == "g" else scope_id),
            "call_id": str(leaf_id) if kind == "d" else None,
            "user_id": str(user_id),
            "user_domain": user_domain,
            "connected": event_type == "participant_joined",
            **({"state": asdict(occupant)} if event_type == "participant_joined" else {}),
        },
    )
    if kind == "g":
        # Keep LiveKit's webhook response independent from peer latency while
        # ensuring remote member homes see joins and leaves immediately. The
        # 30-second coordinator heartbeat remains the recovery path.
        from app.tasks import voice_replicate_room

        await enqueue_best_effort(voice_replicate_room, room)
    return await completed()


@router.post("/_kaede/v1/voice/state", status_code=204)
async def federation_voice_state(
    payload: VoiceStateFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "voice-state", capacity=180, refill_per_minute=180
    )
    try:
        kind, guild_id, channel_id = parse_room_name(payload.room)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_ROOM"}) from None
    if kind != "g" or guild_id != int(payload.guild_id):
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_ROOM"})
    channel, guild = await load_voice_channel(session, channel_id, principal.origin)
    if guild.id != guild_id or channel.guild_id != guild_id:
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    local_member = await session.scalar(
        select(GuildMember.user_id).where(
            GuildMember.guild_id == guild_id,
            GuildMember.guild_domain == principal.origin,
            GuildMember.user_domain == settings.domain,
        )
    )
    if local_member is None:
        raise HTTPException(status_code=403, detail={"code": "KAED_VOICE_NOT_SUBSCRIBED"})
    now = int(time.time())
    if abs(payload.generated_at - now) > settings.federation_clock_skew_seconds:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"})
    occupants: list[Occupant] = []
    try:
        for item in payload.participants:
            occupant = Occupant(**item.model_dump())
            if (
                occupant.room != payload.room
                or occupant.guild_id != payload.guild_id
                or occupant.channel_id != str(channel_id)
            ):
                raise ValueError
            identity_user_id, identity_domain = parse_participant_identity(occupant.identity)
            if (occupant.user_id, occupant.user_domain) != (
                str(identity_user_id),
                identity_domain,
            ):
                raise ValueError
            occupants.append(occupant)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"}) from None
    await replace_occupancy(
        redis,
        principal.origin,
        payload.room,
        occupants,
        generated_at=payload.generated_at,
    )
    await publish_ephemeral(
        redis,
        guild_topic(principal.origin, guild_id),
        "VOICE_STATE_UPDATE",
        {
            "room": payload.room,
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "channel_domain": principal.origin,
            "participants": [asdict(item) for item in occupants],
            "generated_at": payload.generated_at,
            "heartbeat": True,
        },
    )
    return Response(status_code=204)
