from __future__ import annotations

import asyncio
import os
import time

from redis.asyncio import Redis

from app.core.settings import get_settings
from app.voice.cleanup import cleanup_orphaned_dm_rooms
from app.voice.livekit import LiveKitControl, mint_join_token
from app.voice.rooms import guild_room_name, participant_identity
from app.voice.state import (
    Occupant,
    apply_authoritative_call,
    call_key,
    create_call,
    get_call,
    occupancy_snapshot,
    replace_occupancy,
    room_state_key,
    transition_call,
    voice_room_registry_key,
)
from scripts.verification import VerificationFailure, failure_message, require


async def main() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    prefix = f"voice-validation:{os.getpid()}:{time.time_ns()}"
    room = guild_room_name(101, 202)
    identity = participant_identity(303, settings.domain)
    occupant = Occupant(
        identity=identity,
        user_id="303",
        user_domain=settings.domain,
        room=room,
        guild_id="101",
        channel_id="202",
        joined_at=int(time.time()),
        can_speak=True,
        can_stream=True,
    )
    call_id = int(str(time.time_ns())[-15:])
    stored_call_key = call_key(settings.domain, call_id)
    replica_call_id = call_id + 1
    replica_call_key = call_key(settings.domain, replica_call_id)
    try:
        require(
            await replace_occupancy(redis, settings.domain, room, [occupant], generated_at=200),
            "current occupancy was rejected",
        )
        require(
            not await replace_occupancy(redis, settings.domain, room, [], generated_at=199),
            "older occupancy replaced current state",
        )
        snapshot = await occupancy_snapshot(redis, settings.domain, room, settings, now=201)
        require(not snapshot["stale"], "fresh occupancy was marked stale")
        participants = snapshot["participants"]
        require(
            isinstance(participants, list) and len(participants) == 1,
            "occupancy participant was lost",
        )

        record: dict[str, object] = {
            "id": str(call_id),
            "channel_id": "404",
            "channel_domain": settings.domain,
            "authority_domain": settings.domain,
            "room": f"d.404.{call_id}",
            "state": "ringing",
            "created_at": int(time.time()),
            "ended_at": None,
            "caller": identity,
            "participants": [identity, f"505@{settings.domain}"],
            "validation_prefix": prefix,
        }
        require(
            await create_call(
                redis,
                record,
                {identity, f"505@{settings.domain}"},
                settings,
                accepted={identity},
            ),
            "call creation failed",
        )
        caller_reaccepted, changed, rejected = await transition_call(
            redis,
            settings.domain,
            call_id,
            identity,
            "accept",
            settings,
        )
        if caller_reaccepted or changed or rejected != "accepted":
            raise VerificationFailure(
                "call initiator acceptance should be idempotent with result "
                f"(accepted=True, changed=False, reason='accepted'); received "
                f"({caller_reaccepted=}, {changed=}, reason={rejected!r})"
            )
        accepted, changed, active = await transition_call(
            redis,
            settings.domain,
            call_id,
            f"505@{settings.domain}",
            "accept",
            settings,
        )
        require(
            accepted and changed and isinstance(active, dict) and active["state"] == "active",
            "call acceptance failed",
        )
        ended, changed, terminal = await transition_call(
            redis, settings.domain, call_id, identity, "end", settings
        )
        require(
            ended and changed and isinstance(terminal, dict) and terminal["state"] == "ended",
            "call termination failed",
        )
        if not isinstance(terminal, dict):
            raise VerificationFailure(
                f"call termination expected a record dictionary; received {terminal!r}"
            )
        replayed, changed, replay = await transition_call(
            redis, settings.domain, call_id, identity, "end", settings
        )
        require(
            replayed
            and not changed
            and isinstance(replay, dict)
            and replay["ended_at"] == terminal["ended_at"],
            "call termination was not replay-safe",
        )
        require(
            (await get_call(redis, settings.domain, call_id) or {}).get("state") == "ended",
            "terminal call state was not retained",
        )

        replica_record = {
            **record,
            "id": str(replica_call_id),
            "room": f"d.404.{replica_call_id}",
            "state": "ringing",
            "ended_at": None,
        }
        require(
            await create_call(
                redis,
                replica_record,
                {identity, f"505@{settings.domain}"},
                settings,
                accepted={identity},
            ),
            "replica call creation failed",
        )
        authority_state = {
            **replica_record,
            "state": "ended",
            "ended_at": int(time.time()),
        }
        applied, changed, applied_record = await apply_authoritative_call(
            redis,
            authority_state,
            settings,
            action="sync",
        )
        require(
            applied
            and changed
            and isinstance(applied_record, dict)
            and applied_record["state"] == "ended",
            "authoritative terminal call state was not applied",
        )
        replayed, changed, _replayed_record = await apply_authoritative_call(
            redis,
            authority_state,
            settings,
            action="sync",
        )
        require(replayed and not changed, "authoritative call replay was not idempotent")

        control = LiveKitControl(settings)
        await control.ensure_room(room)
        require(await control.list_participants(room) == [], "new room was not empty")
        token, _ = mint_join_token(
            settings,
            room=room,
            identity=identity,
            display_name="Voice validation",
            metadata={"generation": 0, "user_domain": settings.domain},
            can_speak=True,
            can_stream=True,
        )
        require(token.count(".") == 2, "LiveKit token was malformed")
        orphan_room = f"d.404.{replica_call_id + 1}"
        await control.ensure_room(orphan_room)
        require(
            await cleanup_orphaned_dm_rooms(redis, settings) == 1,
            "orphaned DM room was not deleted",
        )
        require(
            orphan_room not in {str(item.name) for item in await control.list_rooms()},
            "deleted orphaned DM room remained visible",
        )
        await control.delete_room(room)
        print("voice verification passed")
    finally:
        await redis.delete(
            room_state_key("occupancy", settings.domain, room),
            room_state_key("heartbeat", settings.domain, room),
            stored_call_key,
            f"{stored_call_key}:participants",
            f"{stored_call_key}:accepted",
            f"{stored_call_key}:declined",
            replica_call_key,
            f"{replica_call_key}:participants",
            f"{replica_call_key}:accepted",
            f"{replica_call_key}:declined",
        )
        await redis.srem(voice_room_registry_key(settings.domain), room)
        await redis.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except VerificationFailure as error:
        raise SystemExit(failure_message("voice", error, "make voice-check")) from None
