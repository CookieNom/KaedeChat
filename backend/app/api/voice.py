from __future__ import annotations

import secrets
import time
from contextlib import suppress
from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
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
from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.bots.installations import (
    installation_accessible_channel,
    installation_allows_channel,
)
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.events import guild_topic, publish_dispatch, publish_ephemeral, user_topic
from app.chat.hierarchy import require_can_manage_member
from app.chat.permissions import get_permissions, require_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation, BotWorker
from app.db.models import Channel, Guild, GuildMember, User
from app.federation.client import signed_request
from app.federation.guild_management import proxy_remote_guild_management
from app.federation.network import FederationNetworkError
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    bounded_request_body,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)
from app.voice.broker import request_remote_guild_voice_token
from app.voice.e2ee import (
    active_bot_dm_voice_capability,
    active_bot_guild_voice_installation,
)
from app.voice.livekit import LiveKitControl, LiveKitError, receive_webhook
from app.voice.regions import configured_voice_regions
from app.voice.rooms import (
    parse_participant_identity,
    parse_room_name,
    participant_identity,
)
from app.voice.schemas import (
    VoiceBrokerRequest,
    VoiceChannelStatusUpdate,
    VoiceModerationUpdate,
    VoiceMoveFederationRequest,
    VoiceMoveRequest,
    VoiceOccupantState,
    VoiceRegion,
    VoiceSelfStateFederationRequest,
    VoiceSelfStateFederationResponse,
    VoiceStateFederationRequest,
    VoiceTokenRequest,
    VoiceTokenResponse,
)
from app.voice.service import (
    STAGE_CHANNEL_TYPE,
    VOICE_FLAG_MASK,
    VOICE_SERVER_DEAF,
    VOICE_SERVER_MUTE,
    authoritative_guild_token,
    effective_voice_user_limit,
    federated_voice_grant_matches,
    load_voice_channel,
    parse_minted_metadata,
    priority_speaking_granted,
    publish_bot_voice_session_update,
    require_voice_enabled,
    update_authoritative_occupant_grant,
    update_authoritative_occupant_self_state,
    voice_metadata_matches_policy,
    voice_speaking_allowed,
)
from app.voice.state import (
    FederatedVoiceSession,
    Occupant,
    admit_occupant,
    advance_federated_voice_home_session,
    bump_generation,
    confirm_federated_voice_home_session,
    current_generation,
    discard_all_federated_voice_home_sessions,
    discard_federated_voice_session,
    get_call,
    get_federated_voice_session,
    occupancy_snapshot,
    occupant_from_federation_state,
    occupant_in_room,
    public_occupant_state,
    release_voice_connection,
    remove_occupant,
    remove_occupant_connection,
    replace_occupancy,
    room_occupants,
    room_state_key,
    set_federated_voice_authority_session,
    sync_federated_voice_session_generation,
    voice_connection_matches,
    voice_room_registry_key,
    voice_user_room,
)
from app.voice.status import set_voice_channel_status

router = APIRouter(tags=["voice"])


@router.put(
    "/api/v1/channels/{channel_ref}/voice-status",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def update_voice_channel_status(
    channel_ref: EntityRef,
    payload: VoiceChannelStatusUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await update_voice_channel_status_for_actor(
        channel_ref,
        payload,
        auth.user,
        session,
        redis,
        snowflake,
        settings,
        reason=reason,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def update_voice_channel_status_for_actor(
    channel_ref: EntityRef,
    payload: VoiceChannelStatusUpdate,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    *,
    reason: str | None = None,
    expected_guild_ref: EntityRef | None = None,
) -> dict[str, object]:
    """Route one authorized actor to the channel authority.

    Bot routes provide ``expected_guild_ref`` so a guild-scoped installation
    can never be reused as a deputy for a channel in another guild.
    """

    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel, guild = await load_voice_channel(session, channel_id, channel_domain)
    if expected_guild_ref is not None:
        expected_guild = expected_guild_ref.resolve(settings.domain)
        if (guild.id, guild.origin_domain) != expected_guild:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        actor,
        "voice_status.update",
        {
            "channel_ref": f"{channel.id}@{channel.origin_domain}",
            "data": payload.model_dump(mode="json"),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    return await set_voice_channel_status(
        session,
        redis,
        snowflake,
        settings,
        guild,
        channel,
        actor,
        payload.status,
        reason=reason,
    )


@router.get("/api/v1/channels/{channel_ref}/voice-status")
async def get_voice_channel_status(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel, guild = await load_voice_channel(session, channel_id, channel_domain)
    if channel.type != 2:
        raise HTTPException(status_code=400, detail={"code": "VOICE_STATUS_VOICE_ONLY"})
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        auth.user,
        "voice_status.get",
        {"channel_ref": f"{channel.id}@{channel.origin_domain}"},
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        Permission.VIEW_CHANNEL,
        channel=channel,
    )
    from app.voice.channel_info import voice_channel_status
    from app.voice.status import voice_channel_status_payload

    current = await voice_channel_status(
        redis,
        guild.origin_domain,
        guild.id,
        channel.id,
    )
    return voice_channel_status_payload(guild, channel, current)


log = structlog.get_logger()


def _qualified_ref(ref: EntityRef, default_domain: str) -> str:
    entity_id, entity_domain = ref.resolve(default_domain)
    return f"{entity_id}@{entity_domain}"


async def require_bot_voice_member_channel_access(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    user_ref: EntityRef,
    installation: BotInstallation,
    *,
    target_channel_ref: EntityRef | None = None,
) -> None:
    """Fence bot moderation to the member's source and requested destination.

    Server mute/deafen and disconnect remain guild-global for a disconnected
    member. Once a live source exists, both it and any move destination must be
    inside the acting installation's parent-aware channel ceiling.
    """

    user_id, user_domain = user_ref.resolve(guild.origin_domain)
    identity = participant_identity(user_id, user_domain)
    source_room = await voice_user_room(
        redis,
        settings.domain,
        identity,
        guild_id=guild.id,
    )
    if source_room is not None:
        try:
            kind, source_guild_id, source_channel_id = parse_room_name(source_room)
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail={"code": "VOICE_NOT_IN_GUILD"},
            ) from None
        if kind != "g" or source_guild_id != guild.id:
            raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
        source = await installation_accessible_channel(
            session,
            installation,
            guild,
            EntityRef(f"{source_channel_id}@{guild.origin_domain}"),
        )
        if source is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_CHANNEL_RESTRICTED"},
            )
    if target_channel_ref is not None:
        target = await installation_accessible_channel(
            session,
            installation,
            guild,
            target_channel_ref,
        )
        if target is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_CHANNEL_RESTRICTED"},
            )


async def _active_bot_voice_move_lineage(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    target_channel: Channel,
    occupant: Occupant,
) -> tuple[BotInstallation, BotWorker]:
    """Revalidate the exact bot media grant before minting its moved token."""

    installation = await active_bot_guild_voice_installation(
        session,
        settings,
        guild.id,
        occupant.identity,
        occupant.participant_metadata,
    )
    worker_id = occupant.participant_metadata.get("bot_worker_id")
    worker = await session.get(BotWorker, worker_id) if type(worker_id) is int else None
    if installation is None or worker is None:
        raise HTTPException(status_code=409, detail={"code": "VOICE_MOVE_SESSION_STALE"})
    required_scopes = {"voice.connect"}
    if occupant.allow_listen:
        required_scopes.add("voice.listen")
    if occupant.allow_speak:
        required_scopes.add("voice.speak")
    if occupant.allow_stream:
        required_scopes.add("voice.stream")
    if not required_scopes.issubset(installation.granted_scopes) or not required_scopes.issubset(
        worker.scopes
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_MOVE_SESSION_STALE"})
    if not await installation_allows_channel(session, installation, target_channel):
        raise HTTPException(status_code=403, detail={"code": "BOT_CHANNEL_RESTRICTED"})
    return installation, worker


@router.get("/api/v1/voice/regions", response_model=list[VoiceRegion])
async def voice_regions(
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[VoiceRegion]:
    if guild_ref is None:
        return configured_voice_regions(settings)

    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "voice.regions",
    )
    if proxied is not None:
        try:
            return [VoiceRegion.model_validate(item) for item in proxied.body]
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
            ) from None

    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    member = await session.get(
        GuildMember,
        (guild_id, guild_domain, auth.user.id, auth.user.origin_domain),
    )
    if guild is None or guild.unavailable or member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return configured_voice_regions(settings)


async def publish_voice_channel_start_time(
    redis: Redis,
    settings: Settings,
    session: AsyncSession,
    *,
    guild_id: int,
    channel_id: int,
    room: str,
    started_at: int | None,
) -> bool:
    """Publish one durable room-start transition and its matching clear."""

    from app.chat.guild_revision import (
        guild_authority_owner,
        queue_guild_mutation,
        wake_queued_guild_federation,
    )
    from app.voice.channel_info import voice_channel_update_payload

    channel = await session.get(Channel, (channel_id, settings.domain))
    guild = await session.get(Guild, (guild_id, settings.domain))
    if (
        channel is None
        or guild is None
        or channel.guild_id != guild.id
        or channel.guild_domain != guild.origin_domain
        or channel.type not in {2, 13}
    ):
        return False
    key = room_state_key("start-time", settings.domain, room)
    previous: bytes | str | None = None
    if started_at is None:
        stored_start = await redis.get(key)
        if not isinstance(stored_start, (bytes, str)):
            return False
        previous = stored_start
        if not await redis.delete(key):
            return False
    elif not await redis.set(key, str(started_at), nx=True, ex=7 * 24 * 60 * 60):
        current = await redis.get(key)
        current_value = current.decode() if isinstance(current, bytes) else current
        if current_value == str(started_at):
            await publish_dispatch(
                redis,
                guild_topic(settings.domain, guild_id),
                "VOICE_CHANNEL_START_TIME_UPDATE",
                voice_channel_update_payload(
                    guild,
                    channel,
                    "voice_start_time",
                    started_at,
                ),
            )
        return False
    owner = await guild_authority_owner(session, settings, guild, for_update=False)
    try:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            owner,
            "guild.voice_channel_start_time.update",
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "voice_start_time": started_at,
            },
            channel=channel,
            pause_e2ee=False,
        )
        await session.commit()
    except Exception:
        with suppress(Exception):
            await session.rollback()
        with suppress(Exception):
            if started_at is None and previous is not None:
                await redis.set(key, previous, ex=7 * 24 * 60 * 60)
            else:
                await redis.delete(key)
        raise
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(settings.domain, guild_id),
        "VOICE_CHANNEL_START_TIME_UPDATE",
        voice_channel_update_payload(guild, channel, "voice_start_time", started_at),
    )
    return True


@router.post("/api/v1/channels/{channel_ref}/voice/token", response_model=VoiceTokenResponse)
async def channel_voice_token(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    payload: VoiceTokenRequest | None = None,
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
            detail={
                "code": "MISSING_PERMISSIONS",
                "message": "You do not have permission to join this voice channel.",
                "permissions": str(int(Permission.CONNECT)),
            },
        )
    identity = participant_identity(auth.user.id, auth.user.origin_domain)
    sender_device_id = payload.sender_device_id if payload is not None else None
    connection_id = (
        payload.connection_id
        if payload is not None and payload.connection_id
        else secrets.token_urlsafe(32)
    )
    takeover = payload.takeover if payload is not None else False
    client_kind = payload.client_kind if payload is not None else "web"
    if guild.origin_domain == settings.domain:
        grant = await authoritative_guild_token(
            session,
            redis,
            settings,
            channel=channel,
            guild=guild,
            actor=auth.user,
            sender_device_id=sender_device_id,
            connection_id=connection_id,
            takeover=takeover,
            client_kind=client_kind,
        )
        await discard_all_federated_voice_home_sessions(redis, identity)
        return grant
    return await request_remote_guild_voice_token(
        session,
        redis,
        settings,
        channel=channel,
        guild=guild,
        actor=auth.user,
        sender_device_id=sender_device_id,
        connection_id=connection_id,
        takeover=takeover,
        client_kind=client_kind,
    )


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
    if actor.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "VOICE_DENIED"})
    await require_remote_user_creation_allowed(session, actor)
    return await authoritative_guild_token(
        session,
        redis,
        settings,
        channel=channel,
        guild=guild,
        actor=actor,
        move_session_id=payload.move_session_id,
        sender_device_id=payload.sender_device_id,
        remote_device_attested=True,
        connection_id=payload.connection_id,
        takeover=payload.takeover,
        client_kind=payload.client_kind,
        allow_listen=payload.allow_listen,
        allow_speak=payload.allow_speak,
        allow_stream=payload.allow_stream,
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
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "voice_member.update",
        {
            "resource_ref": _qualified_ref(user_ref, settings.domain),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        return Response(status_code=204)
    require_voice_enabled(settings)
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
    identity = participant_identity(user_id, user_domain)
    room = await voice_user_room(redis, settings.domain, identity, guild_id=guild.id)
    moderation_channel = None
    if room is not None:
        kind, room_guild_id, room_channel_id = parse_room_name(room)
        if kind != "g" or room_guild_id != guild.id:
            raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
        moderation_channel, moderation_guild = await load_voice_channel(
            session, room_channel_id, guild.origin_domain
        )
        if (moderation_guild.id, moderation_guild.origin_domain) != (
            guild.id,
            guild.origin_domain,
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
    if (
        payload.server_deaf is not None
        and moderation_channel is not None
        and moderation_channel.type == 13
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "STAGE_SERVER_DEAF_UNSUPPORTED"},
        )
    await require_permissions(session, redis, guild, auth.user, needed, channel=moderation_channel)
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
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            24,
            target_type="member",
            target_ref={"id": str(user_id), "origin_domain": user_domain},
            reason=normalize_audit_reason(reason),
            changes=[
                {
                    "key": "voice_flags",
                    "old_value": str(old_flags),
                    "new_value": str(flags),
                }
            ],
        )
        await session.commit()
    else:
        # Release the guild lock before contacting Redis or LiveKit even when
        # the requested state is already current.
        await session.commit()
    if room is not None:
        occupants = {
            item.identity: item for item in await room_occupants(redis, settings.domain, room)
        }
        current = occupants.get(identity)
        if current is not None:
            target_user = await session.get(User, (user_id, user_domain))
            target_permissions = (
                Permission(
                    await get_permissions(
                        session,
                        redis,
                        guild,
                        target_user,
                        channel=moderation_channel,
                    )
                )
                if target_user is not None and moderation_channel is not None
                else Permission(0)
            )
            can_speak = (
                current.allow_speak
                and moderation_channel is not None
                and voice_speaking_allowed(moderation_channel.type, target_permissions)
                and not bool(flags & VOICE_SERVER_MUTE)
                and not (moderation_channel.type == STAGE_CHANNEL_TYPE and current.suppressed)
            )
            can_stream = (
                current.allow_stream
                and bool(target_permissions & Permission.STREAM)
                and not (
                    moderation_channel is not None
                    and moderation_channel.type == STAGE_CHANNEL_TYPE
                    and current.suppressed
                )
            )
            can_priority_speak = (
                priority_speaking_granted(
                    channel_type=moderation_channel.type,
                    permissions=target_permissions,
                    client_kind=current.client_kind,
                    can_speak=can_speak,
                )
                if moderation_channel is not None
                else False
            )
            with suppress(LiveKitError, HTTPException):
                await update_authoritative_occupant_grant(
                    redis,
                    settings,
                    current,
                    can_speak=can_speak,
                    can_stream=can_stream,
                    can_priority_speak=can_priority_speak,
                    server_mute=bool(flags & VOICE_SERVER_MUTE),
                    server_deaf=bool(flags & VOICE_SERVER_DEAF),
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
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "voice_member.disconnect",
        {
            "resource_ref": _qualified_ref(user_ref, settings.domain),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        return Response(status_code=204)
    from app.api.guilds import local_guild

    guild = await local_guild(session, settings, guild_ref, for_update=True)
    user_id, user_domain = user_ref.resolve(settings.domain)
    await require_can_manage_member(session, guild, auth.user, user_id, user_domain)
    identity = participant_identity(user_id, user_domain)
    room = await voice_user_room(redis, settings.domain, identity, guild_id=guild.id)
    if room is not None:
        current_occupant = next(
            (
                item
                for item in await room_occupants(redis, settings.domain, room)
                if item.identity == identity
            ),
            None,
        )
        kind, room_guild_id, room_channel_id = parse_room_name(room)
        if kind != "g" or room_guild_id != guild.id:
            raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
        source_channel, source_guild = await load_voice_channel(
            session, room_channel_id, guild.origin_domain
        )
        if (source_guild.id, source_guild.origin_domain) != (guild.id, guild.origin_domain):
            raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            Permission.MOVE_MEMBERS,
            channel=source_channel,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            27,
            target_type="member",
            target_ref={"id": str(user_id), "origin_domain": user_domain},
            reason=normalize_audit_reason(reason),
        )
        await session.commit()
        revoked_generation = await bump_generation(redis, settings.domain, room, identity)
        try:
            await LiveKitControl(settings).remove_participant(room, identity)
        except LiveKitError:
            log.warning("voice_disconnect_control_failed", room=room, identity=identity)
        await remove_occupant(redis, settings.domain, room, identity)
        if current_occupant is not None and current_occupant.connection_id:
            await release_voice_connection(
                redis,
                settings.domain,
                identity,
                current_occupant.connection_id,
                room=room,
                client_kind=current_occupant.client_kind,
            )
            await publish_bot_voice_session_update(
                redis,
                settings,
                current_occupant,
                generation=revoked_generation,
                connected=False,
            )
    else:
        await require_permissions(session, redis, guild, auth.user, Permission.MOVE_MEMBERS)
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
    _, guild_domain = guild_ref.resolve(settings.domain)
    move_data = payload.model_dump(mode="json", exclude_unset=True)
    move_data["channel_id"] = _qualified_ref(payload.channel_id, guild_domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "voice_member.move",
        {
            "resource_ref": _qualified_ref(user_ref, settings.domain),
            "data": move_data,
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        return Response(status_code=204)
    from app.api.guilds import local_guild

    guild = await local_guild(session, settings, guild_ref, for_update=True)
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
    source_room = await voice_user_room(redis, settings.domain, identity, guild_id=guild.id)
    if source_room is None:
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_CONNECTED"})
    kind, source_guild_id, source_channel_id = parse_room_name(source_room)
    if kind != "g" or source_guild_id != guild.id:
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
    source_channel, source_guild = await load_voice_channel(
        session, source_channel_id, guild.origin_domain
    )
    if (source_guild.id, source_guild.origin_domain) != (guild.id, guild.origin_domain):
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_IN_GUILD"})
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        Permission.MOVE_MEMBERS,
        channel=source_channel,
    )
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        Permission.MOVE_MEMBERS,
        channel=target_channel,
    )
    if source_room == f"g.{guild.id}.{target_channel.id}":
        return Response(status_code=204)
    source_generation = await current_generation(redis, settings.domain, source_room, identity)
    source_occupant = next(
        (
            item
            for item in await room_occupants(redis, settings.domain, source_room)
            if item.identity == identity
        ),
        None,
    )
    target_is_bot = target_user.account_type == "bot"
    bot_installation: BotInstallation | None = None
    bot_worker: BotWorker | None = None
    if target_is_bot:
        if (
            source_occupant is None
            or source_occupant.client_kind != "bot"
            or not source_occupant.connection_id
            or source_occupant.participant_metadata.get("generation") != source_generation
            or not await voice_connection_matches(
                redis,
                settings.domain,
                identity,
                connection_id=source_occupant.connection_id,
                room=source_room,
                generation=source_generation,
                client_kind="bot",
            )
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "VOICE_MOVE_SESSION_STALE"},
            )
        bot_installation, bot_worker = await _active_bot_voice_move_lineage(
            session,
            settings,
            guild,
            target_channel,
            source_occupant,
        )
    elif source_occupant is not None and source_occupant.client_kind == "bot":
        raise HTTPException(status_code=409, detail={"code": "VOICE_MOVE_SESSION_STALE"})
    remote_human = user_domain != settings.domain and not target_is_bot
    move_session: FederatedVoiceSession | None = None
    if remote_human:
        move_session = await get_federated_voice_session(redis, "authority", identity)
        if move_session is None or not move_session.ready or not move_session.active:
            raise HTTPException(status_code=409, detail={"code": "VOICE_MOVE_SESSION_UNAVAILABLE"})
        if (
            move_session.authority_domain != settings.domain
            or move_session.guild_id != str(guild.id)
            or move_session.room != source_room
            or move_session.generation != source_generation
            or source_occupant is None
            or not source_occupant.connection_id
            or move_session.connection_id != source_occupant.connection_id
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_MOVE_SESSION_STALE"})
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        26,
        target_type="member",
        target_ref={"id": str(user_id), "origin_domain": user_domain},
        reason=normalize_audit_reason(reason),
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
        move_session_id=(move_session.move_session_id if move_session is not None else None),
        disconnect_previous=not remote_human,
        sender_device_id=(
            str(source_occupant.participant_metadata["bot_e2ee_device_id"])
            if target_is_bot
            and source_occupant is not None
            and isinstance(
                source_occupant.participant_metadata.get("bot_e2ee_device_id"),
                str,
            )
            else None
        ),
        bot_installation=bot_installation,
        bot_worker=bot_worker,
        connection_id=(
            source_occupant.connection_id
            if source_occupant is not None and source_occupant.connection_id
            else secrets.token_urlsafe(32)
        ),
        takeover=True,
        client_kind=(
            "bot"
            if target_is_bot
            else (
                source_occupant.client_kind
                if source_occupant is not None
                and source_occupant.client_kind in {"web", "desktop", "mobile"}
                else "web"
            )
        ),
        allow_listen=source_occupant.allow_listen if source_occupant is not None else True,
        allow_speak=source_occupant.allow_speak if source_occupant is not None else True,
        allow_stream=source_occupant.allow_stream if source_occupant is not None else True,
        self_mute=source_occupant.self_mute if source_occupant is not None else False,
        self_deaf=source_occupant.self_deaf if source_occupant is not None else False,
    )
    move_data = {
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "channel_id": str(target_channel.id),
        "channel_domain": target_channel.origin_domain,
    }
    if not remote_human:
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
        if move_session is None:
            raise RuntimeError("remote voice move is missing its correlated session")
        try:
            move_response = await signed_request(
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
                    "move_session_id": move_session.move_session_id,
                    "source_room": source_room,
                    "source_generation": source_generation,
                    "grant": grant.model_dump(),
                },
                request_timeout=5,
                max_response_bytes=32 * 1024,
            )
        except FederationNetworkError as exc:
            await session.rollback()
            log.warning("voice_move_delivery_failed", destination=user_domain)
            raise HTTPException(
                status_code=503,
                detail={"code": "VOICE_MOVE_DELIVERY_FAILED", "retry_after_ms": 1_000},
                headers={"Retry-After": "1"},
            ) from exc
        if move_response.status_code != 204:
            await session.rollback()
            raise HTTPException(
                status_code=409 if move_response.status_code == 409 else 503,
                detail={"code": "VOICE_MOVE_REJECTED"},
            )
    await session.commit()
    if remote_human:
        await bump_generation(redis, settings.domain, source_room, identity)
        try:
            await LiveKitControl(settings).remove_participant(source_room, identity)
        except LiveKitError:
            log.warning("voice_move_disconnect_failed", room=source_room, identity=identity)
        await remove_occupant(redis, settings.domain, source_room, identity)
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
    identity = participant_identity(int(payload.target_id), settings.domain)
    active_voice = await get_federated_voice_session(redis, "home", identity)
    if not federated_voice_grant_matches(
        payload.grant,
        channel,
        expected_room=expected_room,
        authority_domain=principal.origin,
        client_kind=active_voice.client_kind if active_voice is not None else "unknown",
    ):
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"})
    try:
        source_kind, source_guild_id, _source_channel_id = parse_room_name(payload.source_room)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_ROOM"}) from None
    if source_kind != "g" or source_guild_id != int(payload.guild_id):
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_ROOM"})
    rejection = await advance_federated_voice_home_session(
        redis,
        identity,
        move_session_id=payload.move_session_id,
        authority_domain=principal.origin,
        guild_id=payload.guild_id,
        source_room=payload.source_room,
        source_generation=payload.source_generation,
        target_room=payload.grant.room,
        target_generation=payload.grant.generation,
        connection_id=payload.grant.connection_id,
    )
    if rejection is not None:
        code = (
            "KAED_VOICE_MOVE_NOT_EXPECTED"
            if rejection in {"missing", "pending", "inactive"}
            else "KAED_VOICE_MOVE_STALE"
        )
        raise HTTPException(status_code=409, detail={"code": code})
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
        {
            **move_data,
            "move_session_id": payload.move_session_id,
            "grant": payload.grant.model_dump(),
        },
    )
    return Response(status_code=204)


@router.post(
    "/_kaede/v1/voice/self-state",
    response_model=VoiceSelfStateFederationResponse,
)
async def federation_voice_self_state(
    payload: VoiceSelfStateFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> VoiceSelfStateFederationResponse:
    """Apply client-owned mute/deaf flags at the guild voice authority."""

    require_voice_enabled(settings)
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "voice-self-state",
        capacity=180,
        refill_per_minute=180,
    )
    try:
        kind, guild_id, channel_id = parse_room_name(payload.room)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_ROOM"}) from None
    if kind != "g" or guild_id != int(payload.guild_id):
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_ROOM"})
    channel, guild = await load_voice_channel(session, channel_id, settings.domain)
    if guild.id != guild_id or guild.origin_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    actor = await session.get(User, (int(payload.actor_id), principal.origin))
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, int(payload.actor_id), principal.origin),
    )
    if actor is None or actor.account_type != "human" or member is None:
        raise HTTPException(status_code=404, detail={"code": "VOICE_USER_NOT_FOUND"})
    await require_remote_user_creation_allowed(session, actor)
    identity = participant_identity(actor.id, actor.origin_domain)
    voice_session = await get_federated_voice_session(redis, "authority", identity)
    if (
        voice_session is None
        or not voice_session.ready
        or not voice_session.active
        or voice_session.authority_domain != settings.domain
        or voice_session.guild_id != payload.guild_id
        or voice_session.room != payload.room
        or voice_session.move_session_id != payload.move_session_id
        or voice_session.connection_id != payload.connection_id
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    occupant = await occupant_in_room(redis, settings.domain, payload.room, identity)
    if (
        occupant is None
        or occupant.user_id != payload.actor_id
        or occupant.user_domain != principal.origin
        or occupant.guild_id != payload.guild_id
        or occupant.channel_id != str(channel.id)
        or occupant.connection_id != payload.connection_id
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    occupant_generation = occupant.participant_metadata.get("generation")
    if type(occupant_generation) is not int or occupant_generation != voice_session.generation:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    requested_self_mute = payload.self_mute or payload.self_deaf
    if payload.generation == voice_session.generation:
        try:
            updated = await update_authoritative_occupant_self_state(
                redis,
                settings,
                occupant,
                self_mute=requested_self_mute,
                self_deaf=payload.self_deaf,
            )
        except LiveKitError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "VOICE_MEDIA_UNAVAILABLE", "retry_after_ms": 2000},
                headers={"Retry-After": "2"},
            ) from exc
        updated_generation = updated.participant_metadata.get("generation")
        if type(updated_generation) is not int or not await sync_federated_voice_session_generation(
            redis,
            "authority",
            identity,
            move_session_id=voice_session.move_session_id,
            authority_domain=settings.domain,
            room=voice_session.room,
            connection_id=voice_session.connection_id,
            expected_generation=payload.generation,
            generation=updated_generation,
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    elif (
        payload.generation + 1 == voice_session.generation
        and occupant.self_mute == requested_self_mute
        and occupant.self_deaf == payload.self_deaf
    ):
        updated = occupant
        updated_generation = voice_session.generation
    else:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    public_state = VoiceOccupantState.model_validate(public_occupant_state(updated))
    await publish_ephemeral(
        redis,
        guild_topic(settings.domain, guild.id),
        "VOICE_STATE_UPDATE",
        {
            "room": payload.room,
            "guild_id": payload.guild_id,
            "channel_id": str(channel.id),
            "user_id": payload.actor_id,
            "user_domain": principal.origin,
            "self_mute": updated.self_mute,
            "self_deaf": updated.self_deaf,
            "state": public_state.model_dump(mode="json"),
        },
    )
    from app.tasks import voice_replicate_room

    await enqueue_best_effort(voice_replicate_room, payload.room)
    return VoiceSelfStateFederationResponse(
        state=public_state,
        generation=updated_generation,
    )


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
    if event_type == "room_started":
        if kind == "g":
            creation_time = int(getattr(event.room, "creation_time", 0) or 0)
            now = int(time.time())
            if creation_time <= 0 or creation_time > now + settings.federation_clock_skew_seconds:
                creation_time = now
            await publish_voice_channel_start_time(
                redis,
                settings,
                session,
                guild_id=scope_id,
                channel_id=leaf_id,
                room=room,
                started_at=creation_time,
            )
        return await completed()
    if event_type == "room_finished":
        if kind == "g":
            await publish_voice_channel_start_time(
                redis,
                settings,
                session,
                guild_id=scope_id,
                channel_id=leaf_id,
                room=room,
                started_at=None,
            )
            finished_channel = await session.get(Channel, (leaf_id, settings.domain))
            finished_guild = await session.get(Guild, (scope_id, settings.domain))
            if (
                finished_channel is not None
                and finished_guild is not None
                and finished_channel.type == 2
            ):
                from app.voice.status import clear_voice_channel_status_after_room_end

                await clear_voice_channel_status_after_room_end(
                    session,
                    redis,
                    settings,
                    finished_guild,
                    finished_channel,
                )
        await redis.delete(
            room_state_key("occupancy", settings.domain, room),
            room_state_key("heartbeat", settings.domain, room),
            room_state_key("snapshot-version", settings.domain, room),
        )
        await cast(Any, redis.srem(voice_room_registry_key(settings.domain), room))
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

    async def revoke_current_join(connection_id: str, client_kind: str) -> Response:
        # Fence the capability before attempting the control-plane eviction.
        # Reconciliation will retry the eviction if LiveKit is unavailable.
        await bump_generation(redis, settings.domain, room, identity)
        with suppress(LiveKitError):
            await LiveKitControl(settings).remove_participant(room, identity)
        await release_voice_connection(
            redis,
            settings.domain,
            identity,
            connection_id,
            room=room,
            client_kind=client_kind,
        )
        return await completed()

    if event_type == "participant_joined":
        bot_dm_capability = None
        try:
            metadata = parse_minted_metadata(
                str(event.participant.metadata), room=room, identity=identity
            )
        except ValueError:
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            return await completed()
        generation = int(cast(int, metadata["generation"]))
        connection_id = str(metadata["connection_id"])
        if generation != await current_generation(
            redis, settings.domain, room, identity
        ) or not await voice_connection_matches(
            redis,
            settings.domain,
            identity,
            connection_id=connection_id,
            room=room,
            generation=generation,
            client_kind=str(metadata["client_kind"]),
        ):
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            return await completed()
        move_session_id = metadata.get("move_session_id")
        if kind == "g":
            try:
                channel, guild = await load_voice_channel(session, leaf_id, settings.domain)
                actor = await session.get(User, (user_id, user_domain))
                if (
                    actor is None
                    or actor.disabled_at is not None
                    or guild.id != scope_id
                    or str(metadata.get("guild_id")) != str(scope_id)
                    or str(metadata.get("channel_id")) != str(leaf_id)
                    or not voice_metadata_matches_policy(channel, room, metadata)
                ):
                    raise HTTPException(status_code=403, detail={"code": "VOICE_JOIN_REVOKED"})
                if actor.account_type == "bot":
                    if metadata.get("client_kind") != "bot" or (
                        await active_bot_guild_voice_installation(
                            session,
                            settings,
                            guild.id,
                            identity,
                            metadata,
                        )
                        is None
                    ):
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "VOICE_JOIN_REVOKED"},
                        )
                elif metadata.get("client_kind") == "bot" or (
                    user_domain != settings.domain and not isinstance(move_session_id, str)
                ):
                    raise HTTPException(
                        status_code=403,
                        detail={"code": "VOICE_JOIN_REVOKED"},
                    )
                member = await session.get(
                    GuildMember,
                    (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
                )
                permissions = await get_permissions(session, redis, guild, actor, channel=channel)
                required = Permission.VIEW_CHANNEL | Permission.CONNECT
                if member is None or permissions & required != required:
                    raise HTTPException(status_code=403, detail={"code": "VOICE_JOIN_REVOKED"})
                live_priority = priority_speaking_granted(
                    channel_type=channel.type,
                    permissions=Permission(permissions),
                    client_kind=str(metadata["client_kind"]),
                    can_speak=(
                        bool(metadata.get("allow_speak", True))
                        and not bool(member.voice_flags & VOICE_SERVER_MUTE)
                    ),
                )
                if bool(metadata.get("can_priority_speak", False)) and not live_priority:
                    raise HTTPException(status_code=403, detail={"code": "VOICE_JOIN_REVOKED"})
            except HTTPException:
                # Tokens are short-lived capabilities, not durable permission.
                # Recheck SQL at the actual LiveKit admission event so a token
                # minted before a kick or overwrite denial cannot be replayed.
                return await revoke_current_join(connection_id, str(metadata["client_kind"]))
        else:
            call = await get_call(redis, settings.domain, leaf_id)
            if (
                call is None
                or call.get("authority_domain") != settings.domain
                or call.get("room") != room
                or str(call.get("channel_id")) != str(scope_id)
                or call.get("state") == "ended"
                or identity not in cast(list[str], call.get("participants", []))
                or str(metadata.get("channel_domain")) != str(call.get("channel_domain"))
            ):
                return await revoke_current_join(connection_id, str(metadata["client_kind"]))
            dm_channel = await session.get(
                Channel,
                (scope_id, str(call["channel_domain"])),
            )
            if dm_channel is None or not voice_metadata_matches_policy(dm_channel, room, metadata):
                return await revoke_current_join(connection_id, str(metadata["client_kind"]))
            actor = await session.get(User, (user_id, user_domain))
            if actor is None:
                return await revoke_current_join(connection_id, str(metadata["client_kind"]))
            if actor.account_type == "bot":
                if metadata.get("client_kind") != "bot":
                    return await revoke_current_join(connection_id, str(metadata["client_kind"]))
                bot_dm_capability = await active_bot_dm_voice_capability(
                    session,
                    settings,
                    call,
                    identity,
                    metadata,
                )
                if bot_dm_capability is None:
                    return await revoke_current_join(connection_id, str(metadata["client_kind"]))
            elif metadata.get("client_kind") == "bot" or (
                user_domain != settings.domain and not isinstance(move_session_id, str)
            ):
                return await revoke_current_join(connection_id, str(metadata["client_kind"]))
        resolved_channel_id = leaf_id if kind == "g" else scope_id
        self_deaf = bool(metadata.get("self_deaf", False))
        occupant = Occupant(
            identity=identity,
            user_id=str(user_id),
            user_domain=user_domain,
            room=room,
            guild_id=str(scope_id) if kind == "g" else None,
            channel_id=str(resolved_channel_id),
            joined_at=int(time.time()),
            connection_id=connection_id,
            client_kind=str(metadata["client_kind"]),
            self_mute=bool(metadata.get("self_mute", False)) or self_deaf,
            self_deaf=self_deaf,
            server_mute=bool(metadata["server_mute"]),
            server_deaf=bool(metadata["server_deaf"]),
            suppressed=bool(metadata.get("suppressed", False)),
            request_to_speak_timestamp=(
                str(metadata["request_to_speak_timestamp"])
                if metadata.get("request_to_speak_timestamp") is not None
                else None
            ),
            can_speak=bool(metadata["can_speak"]),
            can_stream=bool(metadata["can_stream"]),
            can_priority_speak=bool(metadata.get("can_priority_speak", False)),
            allow_listen=bool(metadata.get("allow_listen", True)),
            allow_speak=bool(metadata.get("allow_speak", True)),
            allow_stream=bool(metadata.get("allow_stream", True)),
            participant_metadata=dict(metadata),
        )
        admitted = await admit_occupant(
            redis,
            settings.domain,
            occupant,
            user_limit=(await effective_voice_user_limit(session, channel) if kind == "g" else 0),
            bypass_limit=bool(permissions & Permission.MOVE_MEMBERS) if kind == "g" else True,
        )
        if not admitted:
            return await revoke_current_join(connection_id, str(metadata["client_kind"]))
        if kind == "g":
            # Older LiveKit versions do not always emit room_started. Only an
            # admitted participant may trigger the idempotent fallback.
            await publish_voice_channel_start_time(
                redis,
                settings,
                session,
                guild_id=scope_id,
                channel_id=leaf_id,
                room=room,
                started_at=int(time.time()),
            )
        if user_domain != settings.domain and isinstance(move_session_id, str):
            await set_federated_voice_authority_session(
                redis,
                identity,
                FederatedVoiceSession(
                    authority_domain=settings.domain,
                    guild_id=str(scope_id) if kind == "g" else "",
                    room=room,
                    generation=generation,
                    move_session_id=move_session_id,
                    ready=True,
                    active=True,
                    call_id=str(leaf_id) if kind == "d" else None,
                    channel_id=str(scope_id) if kind == "d" else None,
                    connection_id=connection_id,
                    client_kind=str(metadata["client_kind"]),
                ),
            )
    elif event_type == "participant_left":
        try:
            metadata = parse_minted_metadata(
                str(event.participant.metadata), room=room, identity=identity
            )
            connection_id = str(metadata["connection_id"])
            generation = int(cast(int, metadata["generation"]))
        except ValueError:
            return await completed()
        await remove_occupant_connection(
            redis,
            settings.domain,
            room,
            identity,
            connection_id,
            generation=generation,
        )
        await release_voice_connection(
            redis,
            settings.domain,
            identity,
            connection_id,
            room=room,
            generation=generation,
            client_kind=str(metadata["client_kind"]),
        )
        move_session_id = metadata.get("move_session_id")
        if user_domain != settings.domain and isinstance(move_session_id, str):
            await discard_federated_voice_session(
                redis,
                "authority",
                identity,
                move_session_id=move_session_id,
                room=room,
                authority_domain=settings.domain,
                connection_id=connection_id,
                generation=generation,
            )
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
            "channel_domain": (
                settings.domain if kind == "g" else str(metadata.get("channel_domain"))
            ),
            "call_id": str(leaf_id) if kind == "d" else None,
            "user_id": str(user_id),
            "user_domain": user_domain,
            "connected": event_type == "participant_joined",
            **(
                {"state": public_occupant_state(occupant)}
                if event_type == "participant_joined"
                else {}
            ),
            **(
                {"bot_dm_capability_id": bot_dm_capability.grant_id}
                if event_type == "participant_joined" and bot_dm_capability is not None
                else {}
            ),
        },
    )
    if kind in {"g", "d"}:
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
    local_member_ids = list(
        await session.scalars(
            select(GuildMember.user_id).where(
                GuildMember.guild_id == guild_id,
                GuildMember.guild_domain == principal.origin,
                GuildMember.user_domain == settings.domain,
            )
        )
    )
    if not local_member_ids:
        raise HTTPException(status_code=403, detail={"code": "KAED_VOICE_NOT_SUBSCRIBED"})
    now = int(time.time())
    if abs(payload.generated_at - now) > settings.federation_clock_skew_seconds:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"})
    occupants: list[Occupant] = []
    by_identity: dict[str, tuple[Occupant, int, str | None]] = {}
    try:
        for item in payload.participants:
            occupant = occupant_from_federation_state(item.model_dump(mode="python"))
            if (
                occupant.room != payload.room
                or occupant.guild_id != payload.guild_id
                or occupant.channel_id != str(channel_id)
                or (occupant.can_priority_speak and channel.type != 2)
            ):
                raise ValueError
            identity_user_id, identity_domain = parse_participant_identity(occupant.identity)
            if (occupant.user_id, occupant.user_domain) != (
                str(identity_user_id),
                identity_domain,
            ):
                raise ValueError
            occupants.append(occupant)
            by_identity[occupant.identity] = (
                occupant,
                item.generation,
                item.move_session_id,
            )
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"}) from None
    for local_user_id in local_member_ids:
        identity = participant_identity(local_user_id, settings.domain)
        active = await get_federated_voice_session(redis, "home", identity)
        projected = by_identity.get(identity)
        if active is None or projected is None:
            continue
        occupant, generation, move_session_id = projected
        if (
            not active.ready
            or active.call_id is not None
            or active.guild_id != payload.guild_id
            or active.authority_domain != principal.origin
            or active.room != payload.room
            or active.move_session_id != move_session_id
            or active.connection_id != occupant.connection_id
            or generation < active.generation
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    replaced = await replace_occupancy(
        redis,
        principal.origin,
        payload.room,
        occupants,
        generated_at=payload.generated_at,
        snapshot_version=payload.snapshot_version,
    )
    if not replaced:
        return Response(status_code=204)
    local_occupant_identities = {
        occupant.identity for occupant in occupants if occupant.user_domain == settings.domain
    }
    for local_user_id in local_member_ids:
        identity = participant_identity(local_user_id, settings.domain)
        active = await get_federated_voice_session(redis, "home", identity)
        projected = by_identity.get(identity)
        if identity in local_occupant_identities and projected is not None:
            occupant, generation, _move_session_id = projected
            confirmed = await confirm_federated_voice_home_session(
                redis,
                identity,
                authority_domain=principal.origin,
                room=payload.room,
                generation=generation,
                connection_id=occupant.connection_id,
            )
            if not confirmed:
                await remove_occupant_connection(
                    redis,
                    principal.origin,
                    payload.room,
                    identity,
                    occupant.connection_id,
                )
                raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
        else:
            # A full authoritative room snapshot fences a session that has
            # actually left. Pending grants remain valid until their short TTL
            # so a heartbeat racing initial LiveKit connection cannot cancel it.
            await discard_federated_voice_session(
                redis,
                "home",
                identity,
                room=payload.room,
                active_only=True,
                authority_domain=principal.origin,
                move_session_id=active.move_session_id if active is not None else None,
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
            "participants": [public_occupant_state(item) for item in occupants],
            "generated_at": payload.generated_at,
            "heartbeat": True,
        },
    )
    return Response(status_code=204)
