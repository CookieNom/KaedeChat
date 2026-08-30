from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any, cast

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.events import guild_topic, publish_ephemeral, user_topic
from app.core.channel_types import GUILD_VOICE_CHANNEL_TYPES
from app.core.settings import Settings
from app.db.models import Channel, Guild, GuildMember
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError
from app.voice.e2ee import (
    active_bot_dm_voice_capability,
    active_bot_guild_voice_installation,
)
from app.voice.enforcement import enforce_room_permissions
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.rooms import parse_participant_identity, parse_room_name
from app.voice.service import parse_minted_metadata
from app.voice.state import (
    FederatedVoiceSession,
    Occupant,
    call_bot_capability_bindings,
    current_generation,
    federation_occupant_state,
    get_call,
    public_occupant_state,
    release_voice_connection,
    remove_occupant,
    room_occupants,
    room_state_key,
    set_federated_voice_authority_session,
    set_occupant,
    voice_connection_matches,
    voice_grant_transition_active,
    voice_room_registry_key,
)

log = structlog.get_logger()
COORDINATOR_LEASE_SECONDS = 20
RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""
RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


async def migrate_legacy_voice_room_registry(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Rebuild the authority-scoped registry from the local media control plane.

    The legacy ``voice:rooms`` members contain no authority provenance, so they
    are never copied directly. A room is admitted only when LiveKit lists it
    locally and the exact local-authority SQL/Redis owner still exists.
    """

    try:
        async with asyncio.timeout(10):
            listed = await LiveKitControl(settings).list_rooms()
    except (TimeoutError, LiveKitError):
        return 0
    valid: set[str] = set()
    async with sessionmaker() as session:
        for listed_room in listed:
            room = str(getattr(listed_room, "name", ""))
            try:
                kind, scope_id, leaf_id = parse_room_name(room)
            except ValueError:
                continue
            if kind == "g":
                guild = await session.get(Guild, (scope_id, settings.domain))
                channel = await session.get(Channel, (leaf_id, settings.domain))
                if (
                    guild is not None
                    and channel is not None
                    and channel.type in GUILD_VOICE_CHANNEL_TYPES
                    and (channel.guild_id, channel.guild_domain) == (guild.id, guild.origin_domain)
                ):
                    valid.add(room)
                continue
            record = await get_call(redis, settings.domain, leaf_id)
            if (
                record is not None
                and record.get("authority_domain") == settings.domain
                and record.get("room") == room
                and str(record.get("channel_id")) == str(scope_id)
                and record.get("state") != "ended"
            ):
                valid.add(room)
    if valid:
        await cast(
            Awaitable[object],
            redis.sadd(voice_room_registry_key(settings.domain), *sorted(valid)),
        )
    await redis.delete("voice:rooms")
    return len(valid)


async def replicate_room(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    room: str,
    *,
    wait_seconds: float = 5,
) -> int:
    """Serialize and replicate the newest room state across API/worker processes."""

    kind, scope_id, leaf_id = parse_room_name(room)
    owner = secrets.token_urlsafe(24)
    lease_key = f"voice:replication-lock:{settings.domain}:{room}"
    deadline = time.monotonic() + wait_seconds
    while not await redis.set(lease_key, owner, ex=30, nx=True):
        if time.monotonic() >= deadline:
            raise RuntimeError("voice replication lease remained busy")
        await asyncio.sleep(0.1)
    try:
        # Read after acquiring the lease. Every queued transition therefore
        # sends the latest state, even when workers execute join/leave tasks in
        # a different order from their LiveKit webhooks.
        occupants = await room_occupants(redis, settings.domain, room)
        generated_at = int(time.time())
        snapshot_version = int(
            await redis.incr(room_state_key("snapshot-version", settings.domain, room))
        )
        await redis.set(
            room_state_key("heartbeat", settings.domain, room),
            str(generated_at),
            ex=300,
        )
        record: dict[str, Any] | None = None
        if kind == "g":
            async with sessionmaker() as session:
                destinations = set(
                    await session.scalars(
                        select(GuildMember.user_domain).where(
                            GuildMember.guild_id == scope_id,
                            GuildMember.guild_domain == settings.domain,
                            GuildMember.user_domain != settings.domain,
                        )
                    )
                )
            path = "/_kaede/v1/voice/state"
            payload = {
                "guild_id": str(scope_id),
                "room": room,
                "generated_at": generated_at,
                "snapshot_version": snapshot_version,
                "participants": [federation_occupant_state(item) for item in occupants],
            }
        else:
            record = await get_call(redis, settings.domain, leaf_id)
            if (
                record is None
                or record.get("authority_domain") != settings.domain
                or record.get("room") != room
                or str(record.get("channel_id")) != str(scope_id)
                or record.get("state") == "ended"
            ):
                return 0
            participant_domains = {
                parse_participant_identity(identity)[1]
                for identity in cast(list[str], record.get("participants", []))
            }
            destinations = participant_domains - {settings.domain}
            path = "/_kaede/v1/voice/dm-state"
            payload = {
                "call_id": str(leaf_id),
                "channel_id": str(scope_id),
                "room": room,
                "generated_at": generated_at,
                "snapshot_version": snapshot_version,
                "participants": [federation_occupant_state(item) for item in occupants],
            }
            await _publish_local_room_snapshot(
                redis,
                settings,
                room,
                occupants,
                generated_at,
                call_record=record,
            )

        async def deliver(destination: str) -> None:
            try:
                async with sessionmaker() as session:
                    response = await signed_request(
                        session,
                        settings,
                        "POST",
                        destination,
                        path,
                        payload=payload,
                        request_timeout=5,
                        max_response_bytes=16 * 1024,
                    )
                    if response.status_code == 204:
                        await session.commit()
                    else:
                        await session.rollback()
                        log.warning(
                            "voice_heartbeat_rejected",
                            destination=destination,
                            status_code=response.status_code,
                        )
            except FederationNetworkError:
                log.warning("voice_heartbeat_unreachable", destination=destination)

        ordered = sorted(destinations)
        for offset in range(0, len(ordered), 8):
            renewed = await cast(Any, redis.eval)(
                RENEW_LEASE_LUA,
                1,
                lease_key,
                owner,
                "30",
            )
            if int(renewed) != 1:
                raise RuntimeError("voice replication lease was lost")
            await asyncio.gather(
                *(deliver(destination) for destination in ordered[offset : offset + 8])
            )
        return len(occupants)
    finally:
        await cast(Any, redis.eval)(RELEASE_LEASE_LUA, 1, lease_key, owner)


async def _publish_local_room_snapshot(
    redis: Redis,
    settings: Settings,
    room: str,
    occupants: list[Occupant],
    generated_at: int,
    *,
    call_record: dict[str, Any] | None = None,
) -> None:
    """Publish an authoritative room snapshot to connected local clients.

    Webhooks keep the UI responsive, while these snapshots guarantee eventual
    convergence if an ephemeral join/leave event is lost during a gateway
    reconnect or process restart.
    """

    kind, scope_id, leaf_id = parse_room_name(room)
    if kind == "g":
        await publish_ephemeral(
            redis,
            guild_topic(settings.domain, scope_id),
            "VOICE_STATE_UPDATE",
            {
                "room": room,
                "guild_id": str(scope_id),
                "channel_id": str(leaf_id),
                "channel_domain": settings.domain,
                "participants": [public_occupant_state(item) for item in occupants],
                "generated_at": generated_at,
                "heartbeat": True,
            },
        )
        return
    record = call_record or await get_call(redis, settings.domain, leaf_id)
    if (
        record is None
        or record.get("authority_domain") != settings.domain
        or record.get("room") != room
        or str(record.get("channel_id")) != str(scope_id)
        or record.get("state") == "ended"
    ):
        return
    payload = {
        "room": room,
        "guild_id": None,
        "channel_id": str(scope_id),
        "channel_domain": str(record["channel_domain"]),
        "call_id": str(leaf_id),
        "participants": [public_occupant_state(item) for item in occupants],
        "generated_at": generated_at,
        "heartbeat": True,
    }
    capability_bindings = call_bot_capability_bindings(record)
    for identity in cast(list[str], record.get("participants", [])):
        user_id, user_domain = parse_participant_identity(identity)
        if user_domain == settings.domain or identity in capability_bindings:
            rendered = dict(payload)
            if binding := capability_bindings.get(identity):
                rendered["bot_dm_capability_id"] = str(binding["grant_id"])
            await publish_ephemeral(
                redis,
                user_topic(user_domain, user_id),
                "VOICE_STATE_UPDATE",
                rendered,
            )


async def _reconcile_room(
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    room: str,
) -> None:
    try:
        participants = await LiveKitControl(settings).list_participants(room)
    except LiveKitError:
        log.warning("voice_reconciliation_failed", room=room)
        return
    seen: set[str] = set()
    kind, scope_id, leaf_id = parse_room_name(room)
    call_record = await get_call(redis, settings.domain, leaf_id) if kind == "d" else None
    call_participants = (
        set(cast(list[str], call_record.get("participants", [])))
        if call_record is not None
        and call_record.get("authority_domain") == settings.domain
        and call_record.get("room") == room
        and str(call_record.get("channel_id")) == str(scope_id)
        and call_record.get("state") != "ended"
        else set()
    )
    for participant in participants:
        identity = str(participant.identity)
        try:
            user_id, user_domain = parse_participant_identity(identity)
            metadata = parse_minted_metadata(
                str(participant.metadata), room=room, identity=identity
            )
            generation = int(cast(int, metadata["generation"]))
        except (TypeError, ValueError):
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            continue
        if kind == "d" and (
            identity not in call_participants
            or call_record is None
            or str(metadata.get("channel_domain")) != str(call_record.get("channel_domain"))
        ):
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            continue
        move_session_id = metadata.get("move_session_id")
        bot_grant = None
        if metadata.get("client_kind") == "bot":
            bot_grant = (
                await active_bot_guild_voice_installation(
                    session,
                    settings,
                    scope_id,
                    identity,
                    metadata,
                )
                if kind == "g"
                else (
                    await active_bot_dm_voice_capability(
                        session,
                        settings,
                        call_record,
                        identity,
                        metadata,
                    )
                    if call_record is not None
                    else None
                )
            )
        if (metadata.get("client_kind") == "bot" and bot_grant is None) or (
            metadata.get("client_kind") != "bot"
            and user_domain != settings.domain
            and not isinstance(move_session_id, str)
        ):
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            continue
        connection_id = str(metadata["connection_id"])
        if await voice_grant_transition_active(
            redis,
            settings.domain,
            room,
            identity,
        ):
            # A REST moderation transition updates LiveKit before atomically
            # rotating Redis. Retain the old occupancy for this one pass.
            seen.add(identity)
            continue
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
            continue
        seen.add(identity)
        resolved_channel_id = leaf_id if kind == "g" else scope_id
        self_deaf = bool(metadata.get("self_deaf", False))
        await set_occupant(
            redis,
            settings.domain,
            Occupant(
                identity=identity,
                user_id=str(user_id),
                user_domain=user_domain,
                room=room,
                guild_id=str(scope_id) if kind == "g" else None,
                channel_id=str(resolved_channel_id),
                joined_at=int(getattr(participant, "joined_at", 0)) or int(time.time()),
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
            ),
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
    for occupant in await room_occupants(redis, settings.domain, room):
        if occupant.identity not in seen:
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


async def voice_coordinator(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Elect one API process for reconciliation and federation heartbeats."""

    owner = secrets.token_urlsafe(24)
    lease_key = "voice:coordinator"
    last_reconciliation = 0.0
    last_heartbeat = 0.0
    try:
        while True:
            leader = bool(
                await redis.set(
                    lease_key,
                    owner,
                    ex=COORDINATOR_LEASE_SECONDS,
                    nx=True,
                )
            )
            if not leader:
                await asyncio.sleep(5)
                continue
            try:
                await migrate_legacy_voice_room_registry(redis, sessionmaker, settings)
                while True:
                    renewed = await cast(Any, redis.eval)(
                        RENEW_LEASE_LUA,
                        1,
                        lease_key,
                        owner,
                        str(COORDINATOR_LEASE_SECONDS),
                    )
                    if int(renewed) != 1:
                        break
                    now = time.monotonic()
                    rooms_raw = await cast(
                        Any,
                        redis.smembers(voice_room_registry_key(settings.domain)),
                    )
                    rooms = sorted(
                        item.decode() if isinstance(item, bytes) else str(item)
                        for item in rooms_raw
                    )
                    async with sessionmaker() as session:
                        for room in rooms:
                            with suppress(ValueError):
                                kind, _, _ = parse_room_name(room)
                                if kind == "g":
                                    await enforce_room_permissions(session, redis, settings, room)
                    if now - last_reconciliation >= 60:
                        async with sessionmaker() as session:
                            for room in rooms:
                                with suppress(ValueError):
                                    kind, _, _ = parse_room_name(room)
                                    if kind == "g" or kind == "d":
                                        await _reconcile_room(redis, session, settings, room)
                        last_reconciliation = now
                    if now - last_heartbeat >= 30:
                        generated_at = int(time.time())
                        for room in rooms:
                            with suppress(ValueError):
                                kind, _, _ = parse_room_name(room)
                                occupants = await room_occupants(redis, settings.domain, room)
                                await redis.set(
                                    room_state_key("heartbeat", settings.domain, room),
                                    str(generated_at),
                                    ex=300,
                                )
                                if kind == "g":
                                    await _publish_local_room_snapshot(
                                        redis,
                                        settings,
                                        room,
                                        occupants,
                                        generated_at,
                                    )
                                await replicate_room(
                                    redis,
                                    sessionmaker,
                                    settings,
                                    room,
                                )
                        last_heartbeat = now
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("voice_coordinator_cycle_failed")
                await asyncio.sleep(2)
    finally:
        with suppress(Exception):
            await cast(Any, redis.eval)(RELEASE_LEASE_LUA, 1, lease_key, owner)
