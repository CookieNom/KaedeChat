from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import suppress
from dataclasses import asdict
from typing import Any, cast

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.events import guild_topic, publish_ephemeral
from app.core.settings import Settings
from app.db.models import GuildMember
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError
from app.voice.enforcement import enforce_room_permissions
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.rooms import parse_participant_identity, parse_room_name
from app.voice.service import parse_minted_metadata
from app.voice.state import (
    Occupant,
    current_generation,
    release_voice_connection,
    remove_occupant,
    room_occupants,
    room_state_key,
    set_occupant,
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


async def replicate_room(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    room: str,
    *,
    wait_seconds: float = 5,
) -> int:
    """Serialize and replicate the newest room state across API/worker processes."""

    kind, guild_id, _ = parse_room_name(room)
    if kind != "g":
        return 0
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
        await redis.set(
            room_state_key("heartbeat", settings.domain, room),
            str(generated_at),
            ex=300,
        )
        async with sessionmaker() as session:
            destinations = set(
                await session.scalars(
                    select(GuildMember.user_domain).where(
                        GuildMember.guild_id == guild_id,
                        GuildMember.guild_domain == settings.domain,
                        GuildMember.user_domain != settings.domain,
                    )
                )
            )
        payload = {
            "guild_id": str(guild_id),
            "room": room,
            "generated_at": generated_at,
            "participants": [asdict(item) for item in occupants],
        }

        async def deliver(destination: str) -> None:
            try:
                async with sessionmaker() as session:
                    response = await signed_request(
                        session,
                        settings,
                        "POST",
                        destination,
                        "/_kaede/v1/voice/state",
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
) -> None:
    """Publish an authoritative room snapshot to connected local clients.

    Webhooks keep the UI responsive, while these snapshots guarantee eventual
    convergence if an ephemeral join/leave event is lost during a gateway
    reconnect or process restart.
    """

    kind, guild_id, channel_id = parse_room_name(room)
    if kind != "g":
        return
    await publish_ephemeral(
        redis,
        guild_topic(settings.domain, guild_id),
        "VOICE_STATE_UPDATE",
        {
            "room": room,
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "channel_domain": settings.domain,
            "participants": [asdict(item) for item in occupants],
            "generated_at": generated_at,
            "heartbeat": True,
        },
    )


async def _reconcile_room(redis: Redis, settings: Settings, room: str) -> None:
    try:
        participants = await LiveKitControl(settings).list_participants(room)
    except LiveKitError:
        log.warning("voice_reconciliation_failed", room=room)
        return
    seen: set[str] = set()
    kind, scope_id, leaf_id = parse_room_name(room)
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
        if generation != await current_generation(redis, settings.domain, room, identity):
            with suppress(LiveKitError):
                await LiveKitControl(settings).remove_participant(room, identity)
            continue
        seen.add(identity)
        resolved_channel_id = leaf_id if kind == "g" else scope_id
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
                connection_id=str(metadata["connection_id"]),
                client_kind=str(metadata["client_kind"]),
                server_mute=bool(metadata["server_mute"]),
                server_deaf=bool(metadata["server_deaf"]),
                can_speak=bool(metadata["can_speak"]),
                can_stream=bool(metadata["can_stream"]),
            ),
        )
    for occupant in await room_occupants(redis, settings.domain, room):
        if occupant.identity not in seen:
            await remove_occupant(redis, settings.domain, room, occupant.identity)
            if occupant.connection_id:
                await release_voice_connection(
                    redis, settings.domain, occupant.identity, occupant.connection_id
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
                    rooms_raw = await cast(Any, redis.smembers("voice:rooms"))
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
                        for room in rooms:
                            with suppress(ValueError):
                                kind, _, _ = parse_room_name(room)
                                if kind == "g" or kind == "d":
                                    await _reconcile_room(redis, settings, room)
                        last_reconciliation = now
                    if now - last_heartbeat >= 30:
                        generated_at = int(time.time())
                        for room in rooms:
                            with suppress(ValueError):
                                occupants = await room_occupants(redis, settings.domain, room)
                                await redis.set(
                                    room_state_key("heartbeat", settings.domain, room),
                                    str(generated_at),
                                    ex=300,
                                )
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
