from __future__ import annotations

import json
import time
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from typing import Any, cast

from redis.asyncio import Redis

from app.core.settings import Settings
from app.federation.network import FederationNetworkError, normalize_domain

CALL_TRANSITION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {0, 'missing'} end
local call = cjson.decode(raw)
local action = ARGV[1]
local actor = ARGV[2]
local participant_key = KEYS[2]
if redis.call('SISMEMBER', participant_key, actor) ~= 1 then return {0, 'forbidden'} end
if call['state'] == 'ended' then
  if action == 'end' then return {2, raw} end
  if action == 'decline' and redis.call('SISMEMBER', KEYS[4], actor) == 1 then
    return {2, raw}
  end
  return {0, 'ended'}
end
if action == 'accept' then
  if redis.call('SISMEMBER', KEYS[3], actor) == 1 then
    if call['state'] == 'active' then return {2, raw} end
    return {0, 'accepted'}
  end
  call['state'] = 'active'
  redis.call('SADD', KEYS[3], actor)
elseif action == 'decline' then
  if redis.call('SISMEMBER', KEYS[4], actor) == 1 then return {2, raw} end
  redis.call('SADD', KEYS[4], actor)
  if redis.call('SCARD', KEYS[4]) >= redis.call('SCARD', participant_key) - 1 then
    call['state'] = 'ended'
    call['ended_at'] = math.max(tonumber(call['created_at']), tonumber(ARGV[3]))
  end
elseif action == 'end' then
  call['state'] = 'ended'
  call['ended_at'] = math.max(tonumber(call['created_at']), tonumber(ARGV[3]))
else
  return {0, 'action'}
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = tonumber(ARGV[4]) end
redis.call('SET', KEYS[1], cjson.encode(call), 'EX', ttl)
redis.call('EXPIRE', KEYS[2], ttl)
redis.call('EXPIRE', KEYS[3], ttl)
redis.call('EXPIRE', KEYS[4], ttl)
if call['state'] == 'ended' and redis.call('GET', KEYS[5]) == ARGV[5] then
  redis.call('DEL', KEYS[5])
end
return {1, cjson.encode(call)}
"""
APPLY_AUTHORITATIVE_CALL_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {0, 'missing'} end
local current = cjson.decode(raw)
local incoming = cjson.decode(ARGV[1])
local immutable = {
  'id', 'channel_id', 'channel_domain', 'authority_domain',
  'room', 'created_at', 'caller'
}
for _, field in ipairs(immutable) do
  if tostring(current[field]) ~= tostring(incoming[field]) then return {0, 'mismatch'} end
end
if #current['participants'] ~= #incoming['participants'] then return {0, 'mismatch'} end
for index = 1, #current['participants'] do
  if current['participants'][index] ~= incoming['participants'][index] then
    return {0, 'mismatch'}
  end
end
local action = ARGV[2]
local actor = ARGV[3]
local function is_null(value)
  return value == nil or value == cjson.null
end
if actor ~= '' and redis.call('SISMEMBER', KEYS[2], actor) ~= 1 then
  return {0, 'forbidden'}
end
if action == 'accept' then
  if incoming['state'] ~= 'active' or not is_null(incoming['ended_at']) then
    return {0, 'transition'}
  end
elseif action == 'decline' or action == 'end' or action == 'sync' then
  if incoming['state'] ~= 'ended' or is_null(incoming['ended_at']) then
    return {0, 'transition'}
  end
else
  return {0, 'action'}
end
if current['state'] == 'ended' then
  if incoming['state'] ~= 'ended'
      or tonumber(current['ended_at']) ~= tonumber(incoming['ended_at']) then
    return {0, 'transition'}
  end
  if action == 'accept' then redis.call('SADD', KEYS[3], actor) end
  if action == 'decline' then redis.call('SADD', KEYS[4], actor) end
  return {2, raw}
end
if current['state'] == 'active' and incoming['state'] == 'ringing' then
  return {0, 'transition'}
end
if action == 'accept' then redis.call('SADD', KEYS[3], actor) end
if action == 'decline' then redis.call('SADD', KEYS[4], actor) end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = tonumber(ARGV[4]) end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
redis.call('EXPIRE', KEYS[2], ttl)
redis.call('EXPIRE', KEYS[3], ttl)
redis.call('EXPIRE', KEYS[4], ttl)
if incoming['state'] == 'ended' and redis.call('GET', KEYS[5]) == ARGV[5] then
  redis.call('DEL', KEYS[5])
end
return {1, ARGV[1]}
"""
CREATE_CALL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
if not redis.call('SET', KEYS[5], ARGV[3], 'EX', ARGV[2], 'NX') then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
local participant_count = tonumber(ARGV[4])
local cursor = 5
for index = 1, participant_count do
  redis.call('SADD', KEYS[2], ARGV[cursor])
  cursor = cursor + 1
end
local accepted_count = tonumber(ARGV[cursor])
cursor = cursor + 1
for index = 1, accepted_count do
  redis.call('SADD', KEYS[3], ARGV[cursor])
  cursor = cursor + 1
end
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[2])
redis.call('EXPIRE', KEYS[4], ARGV[2])
return 1
"""
REPLACE_OCCUPANCY_LUA = """
local current = tonumber(redis.call('GET', KEYS[2]) or '0')
local incoming = tonumber(ARGV[1])
if current > incoming then return 0 end
redis.call('DEL', KEYS[1])
for index = 2, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[index], ARGV[index + 1])
end
redis.call('SET', KEYS[2], ARGV[1], 'EX', 300)
redis.call('EXPIRE', KEYS[1], 300)
return 1
"""
BUMP_GENERATION_LUA = """
local generation = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
return generation
"""
REMOVE_OCCUPANT_LUA = """
redis.call('HDEL', KEYS[1], ARGV[1])
if redis.call('GET', KEYS[2]) == ARGV[2] then
  redis.call('DEL', KEYS[2])
end
return 1
"""
ACTIVATE_FEDERATED_VOICE_SESSION_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local current = {
  authority_domain = ARGV[2],
  guild_id = ARGV[3],
  room = ARGV[4],
  generation = tonumber(ARGV[5]),
  move_session_id = ARGV[1],
  ready = true,
  active = false
}
redis.call('SET', KEYS[2], cjson.encode(current), 'EX', tonumber(ARGV[6]))
redis.call('DEL', KEYS[1])
return 1
"""
ADVANCE_FEDERATED_VOICE_SESSION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {0, 'missing'} end
local current = cjson.decode(raw)
if current['ready'] ~= true then return {0, 'pending'} end
-- The first request may have advanced state and then lost a Redis publish or
-- its HTTP response. Treat an exact retry as accepted so the endpoint can
-- replay both idempotent client dispatches instead of permanently wedging the
-- authority and member home on opposite rooms.
if current['move_session_id'] == ARGV[1]
    and current['authority_domain'] == ARGV[2]
    and tostring(current['guild_id']) == ARGV[3]
    and current['room'] == ARGV[6]
    and tonumber(current['generation']) == tonumber(ARGV[7])
    and current['active'] ~= true then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[8]))
  return {1, 'replay'}
end
if current['move_session_id'] ~= ARGV[1]
    or current['authority_domain'] ~= ARGV[2]
    or tostring(current['guild_id']) ~= ARGV[3]
    or current['room'] ~= ARGV[4]
    or tonumber(current['generation']) ~= tonumber(ARGV[5]) then
  return {0, 'mismatch'}
end
if current['active'] ~= true then return {0, 'inactive'} end
current['room'] = ARGV[6]
current['generation'] = tonumber(ARGV[7])
current['active'] = false
redis.call('SET', KEYS[1], cjson.encode(current), 'EX', tonumber(ARGV[8]))
return {1, cjson.encode(current)}
"""
CONFIRM_FEDERATED_VOICE_SESSION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local current = cjson.decode(raw)
if current['ready'] ~= true
    or current['authority_domain'] ~= ARGV[1]
    or current['room'] ~= ARGV[2] then
  return 0
end
current['active'] = true
redis.call('SET', KEYS[1], cjson.encode(current), 'EX', tonumber(ARGV[3]))
return 1
"""
DELETE_FEDERATED_VOICE_SESSION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local current = cjson.decode(raw)
if ARGV[1] ~= '' and current['move_session_id'] ~= ARGV[1] then return 0 end
if ARGV[2] ~= '' and current['room'] ~= ARGV[2] then return 0 end
if ARGV[3] == '1' and current['active'] ~= true then return 0 end
if ARGV[4] ~= '' and current['authority_domain'] ~= ARGV[4] then return 0 end
return redis.call('DEL', KEYS[1])
"""
DELETE_VALUE_IF_EQUAL_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

FEDERATED_VOICE_PENDING_TTL_SECONDS = 15 * 60
FEDERATED_VOICE_ACTIVE_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class Occupant:
    identity: str
    user_id: str
    user_domain: str
    room: str
    guild_id: str | None
    channel_id: str
    joined_at: int
    self_mute: bool = False
    self_deaf: bool = False
    server_mute: bool = False
    server_deaf: bool = False
    can_speak: bool = False
    can_stream: bool = False


@dataclass(frozen=True, slots=True)
class FederatedVoiceSession:
    """Move correlation retained independently by a member home and guild home."""

    authority_domain: str
    guild_id: str
    room: str
    generation: int
    move_session_id: str
    ready: bool = False
    active: bool = False


def _authority(value: str) -> str:
    return normalize_domain(value)


def room_state_key(kind: str, authority_domain: str, room: str) -> str:
    return f"voice:v2:{kind}:{_authority(authority_domain)}:{room}"


def generation_key(authority_domain: str, room: str, identity: str) -> str:
    return f"{room_state_key('generation', authority_domain, room)}:{identity}"


def federated_voice_session_key(role: str, identity: str) -> str:
    if role not in {"home", "authority"}:
        raise ValueError("invalid federated voice session role")
    return f"voice:v2:federated-session:{role}:{identity}"


def federated_voice_pending_key(identity: str) -> str:
    return f"voice:v2:federated-session:home-pending:{identity}"


def _decode_federated_voice_session(raw: object) -> FederatedVoiceSession | None:
    try:
        parsed = json.loads(cast(str | bytes | bytearray, raw))
        if not isinstance(parsed, dict):
            return None
        session = FederatedVoiceSession(**parsed)
        authority_domain = _authority(session.authority_domain)
    except (FederationNetworkError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        session.authority_domain != authority_domain
        or not session.guild_id.isascii()
        or not session.guild_id.isdecimal()
        or not session.move_session_id
        or session.generation < 0
    ):
        return None
    return session


async def get_federated_voice_session(
    redis: Redis, role: str, identity: str
) -> FederatedVoiceSession | None:
    raw = await redis.get(federated_voice_session_key(role, identity))
    return _decode_federated_voice_session(raw) if raw is not None else None


async def begin_federated_voice_home_session(
    redis: Redis,
    identity: str,
    session: FederatedVoiceSession,
) -> None:
    await redis.set(
        federated_voice_pending_key(identity),
        session.move_session_id,
        ex=FEDERATED_VOICE_PENDING_TTL_SECONDS,
    )


async def activate_federated_voice_home_session(
    redis: Redis,
    identity: str,
    *,
    move_session_id: str,
    authority_domain: str,
    guild_id: str,
    room: str,
    generation: int,
) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            ACTIVATE_FEDERATED_VOICE_SESSION_LUA,
            2,
            federated_voice_pending_key(identity),
            federated_voice_session_key("home", identity),
            move_session_id,
            _authority(authority_domain),
            guild_id,
            room,
            str(generation),
            str(FEDERATED_VOICE_PENDING_TTL_SECONDS),
        ),
    )
    return bool(result)


async def discard_pending_federated_voice_home_session(
    redis: Redis, identity: str, move_session_id: str
) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            DELETE_VALUE_IF_EQUAL_LUA,
            1,
            federated_voice_pending_key(identity),
            move_session_id,
        ),
    )
    return bool(result)


async def discard_all_federated_voice_home_sessions(redis: Redis, identity: str) -> bool:
    """Atomically revoke both an active capability and any in-flight broker."""

    removed = await redis.delete(
        federated_voice_session_key("home", identity),
        federated_voice_pending_key(identity),
    )
    return bool(removed)


async def set_federated_voice_authority_session(
    redis: Redis,
    identity: str,
    session: FederatedVoiceSession,
) -> None:
    encoded = json.dumps(asdict(session), separators=(",", ":"), sort_keys=True)
    await redis.set(
        federated_voice_session_key("authority", identity),
        encoded,
        ex=FEDERATED_VOICE_ACTIVE_TTL_SECONDS,
    )


async def advance_federated_voice_home_session(
    redis: Redis,
    identity: str,
    *,
    move_session_id: str,
    authority_domain: str,
    guild_id: str,
    source_room: str,
    source_generation: int,
    target_room: str,
    target_generation: int,
) -> str | None:
    """Atomically validate and advance a remote move, returning a rejection reason."""

    result = await cast(
        Awaitable[object],
        redis.eval(
            ADVANCE_FEDERATED_VOICE_SESSION_LUA,
            1,
            federated_voice_session_key("home", identity),
            move_session_id,
            _authority(authority_domain),
            guild_id,
            source_room,
            str(source_generation),
            target_room,
            str(target_generation),
            str(FEDERATED_VOICE_PENDING_TTL_SECONDS),
        ),
    )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise RuntimeError("Dragonfly returned an invalid federated voice move result")
    accepted = int(cast(int | bytes | str, result[0])) == 1
    reason_raw = result[1]
    reason = reason_raw.decode() if isinstance(reason_raw, bytes) else str(reason_raw)
    return None if accepted else reason


async def confirm_federated_voice_home_session(
    redis: Redis,
    identity: str,
    *,
    authority_domain: str,
    room: str,
) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            CONFIRM_FEDERATED_VOICE_SESSION_LUA,
            1,
            federated_voice_session_key("home", identity),
            _authority(authority_domain),
            room,
            str(FEDERATED_VOICE_ACTIVE_TTL_SECONDS),
        ),
    )
    return bool(result)


async def discard_federated_voice_session(
    redis: Redis,
    role: str,
    identity: str,
    *,
    move_session_id: str | None = None,
    room: str | None = None,
    active_only: bool = False,
    authority_domain: str | None = None,
) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            DELETE_FEDERATED_VOICE_SESSION_LUA,
            1,
            federated_voice_session_key(role, identity),
            move_session_id or "",
            room or "",
            "1" if active_only else "0",
            _authority(authority_domain) if authority_domain is not None else "",
        ),
    )
    return bool(result)


async def current_generation(redis: Redis, authority_domain: str, room: str, identity: str) -> int:
    value = await redis.get(generation_key(authority_domain, room, identity))
    if value is None:
        return 0
    return int(cast(bytes | str, value))


async def bump_generation(redis: Redis, authority_domain: str, room: str, identity: str) -> int:
    result = await cast(
        Awaitable[object],
        redis.eval(
            BUMP_GENERATION_LUA,
            1,
            generation_key(authority_domain, room, identity),
            str(24 * 60 * 60),
        ),
    )
    return int(cast(int | bytes | str, result))


async def set_occupant(redis: Redis, authority_domain: str, occupant: Occupant) -> None:
    encoded = json.dumps(asdict(occupant), separators=(",", ":"), sort_keys=True)
    async with redis.pipeline(transaction=True) as pipeline:
        pipeline.hset(
            room_state_key("occupancy", authority_domain, occupant.room), occupant.identity, encoded
        )
        pipeline.sadd("voice:rooms", occupant.room)
        pipeline.set(f"voice:user-room:{occupant.identity}", occupant.room, ex=24 * 60 * 60)
        pipeline.set(
            room_state_key("heartbeat", authority_domain, occupant.room),
            str(int(time.time())),
            ex=300,
        )
        await pipeline.execute()


async def remove_occupant(redis: Redis, authority_domain: str, room: str, identity: str) -> None:
    await cast(
        Awaitable[object],
        redis.eval(
            REMOVE_OCCUPANT_LUA,
            2,
            room_state_key("occupancy", authority_domain, room),
            f"voice:user-room:{identity}",
            identity,
            room,
        ),
    )


async def update_self_flags(
    redis: Redis,
    authority_domain: str,
    identity: str,
    *,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant | None:
    room_raw = await redis.get(f"voice:user-room:{identity}")
    if room_raw is None:
        return None
    room = room_raw.decode() if isinstance(room_raw, bytes) else str(room_raw)
    occupancy_key = room_state_key("occupancy", authority_domain, room)
    raw = await cast(Awaitable[Any], redis.hget(occupancy_key, identity))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        data["self_mute"] = self_mute
        data["self_deaf"] = self_deaf
        occupant = Occupant(**data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    await cast(
        Awaitable[Any],
        redis.hset(
            occupancy_key,
            identity,
            json.dumps(asdict(occupant), separators=(",", ":"), sort_keys=True),
        ),
    )
    return occupant


async def room_occupants(redis: Redis, authority_domain: str, room: str) -> list[Occupant]:
    values = await cast(
        Awaitable[list[Any]], redis.hvals(room_state_key("occupancy", authority_domain, room))
    )
    occupants: list[Occupant] = []
    for raw in values:
        try:
            data = json.loads(raw)
            occupants.append(Occupant(**data))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(occupants, key=lambda item: (item.joined_at, item.identity))


async def occupancy_snapshot(
    redis: Redis,
    authority_domain: str,
    room: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> dict[str, object]:
    heartbeat = await redis.get(room_state_key("heartbeat", authority_domain, room))
    current = int(time.time()) if now is None else now
    generated_at = int(cast(bytes | str, heartbeat)) if heartbeat is not None else 0
    return {
        "room": room,
        "participants": [
            asdict(item) for item in await room_occupants(redis, authority_domain, room)
        ],
        "generated_at": generated_at,
        "stale": current - generated_at > settings.voice_occupancy_stale_seconds,
    }


async def replace_occupancy(
    redis: Redis,
    authority_domain: str,
    room: str,
    occupants: list[Occupant],
    *,
    generated_at: int,
) -> bool:
    encoded = {
        item.identity: json.dumps(asdict(item), separators=(",", ":"), sort_keys=True)
        for item in occupants
    }
    arguments: list[str] = [str(generated_at)]
    for identity, value in sorted(encoded.items()):
        arguments.extend((identity, value))
    result = await cast(
        Awaitable[object],
        redis.eval(
            REPLACE_OCCUPANCY_LUA,
            2,
            room_state_key("occupancy", authority_domain, room),
            room_state_key("heartbeat", authority_domain, room),
            *arguments,
        ),
    )
    return bool(result)


def call_key(authority_domain: str, call_id: int) -> str:
    return f"voice:v2:call:{_authority(authority_domain)}:{call_id}"


def call_ref(authority_domain: str, call_id: int) -> str:
    return f"{call_id}@{_authority(authority_domain)}"


def accepted_call_key(authority_domain: str, call_id: int) -> str:
    return f"{call_key(authority_domain, call_id)}:accepted"


def channel_call_key(channel_domain: str, channel_id: int | str) -> str:
    return f"voice:channel-call:{_authority(channel_domain)}:{channel_id}"


async def create_call(
    redis: Redis,
    record: dict[str, object],
    participants: set[str],
    settings: Settings,
    *,
    accepted: set[str] | None = None,
) -> bool:
    call_id = int(cast(str, record["id"]))
    authority_domain = str(record["authority_domain"])
    key = call_key(authority_domain, call_id)
    encoded = json.dumps(record, separators=(",", ":"), sort_keys=True)
    channel_domain = str(record["channel_domain"])
    channel_id = str(record["channel_id"])
    participant_values = sorted(participants)
    accepted_values = sorted(accepted or set())
    arguments = [
        encoded,
        str(settings.voice_call_ttl_seconds),
        call_ref(authority_domain, call_id),
        str(len(participant_values)),
        *participant_values,
        str(len(accepted_values)),
        *accepted_values,
    ]
    result = await cast(
        Awaitable[object],
        redis.eval(
            CREATE_CALL_LUA,
            5,
            key,
            f"{key}:participants",
            f"{key}:accepted",
            f"{key}:declined",
            channel_call_key(channel_domain, channel_id),
            *arguments,
        ),
    )
    return bool(result)


async def get_call(redis: Redis, authority_domain: str, call_id: int) -> dict[str, Any] | None:
    raw = await redis.get(call_key(authority_domain, call_id))
    if raw is None:
        return None
    parsed = json.loads(raw)
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None


async def get_active_call(
    redis: Redis, channel_domain: str, channel_id: int
) -> dict[str, Any] | None:
    raw = await redis.get(channel_call_key(channel_domain, channel_id))
    if raw is None:
        return None
    rendered = raw.decode() if isinstance(raw, bytes) else str(raw)
    call_id_raw, separator, authority_domain = rendered.partition("@")
    if not separator or not call_id_raw.isdecimal():
        await redis.delete(channel_call_key(channel_domain, channel_id))
        return None
    record = await get_call(redis, authority_domain, int(call_id_raw))
    if record is None or (str(record.get("channel_id")), str(record.get("channel_domain"))) != (
        str(channel_id),
        _authority(channel_domain),
    ):
        await redis.delete(channel_call_key(channel_domain, channel_id))
        return None
    return record


async def is_call_accepted(
    redis: Redis, authority_domain: str, call_id: int, identity: str
) -> bool:
    result = await cast(
        Awaitable[int],
        redis.sismember(accepted_call_key(authority_domain, call_id), identity),
    )
    return bool(result)


async def transition_call(
    redis: Redis,
    authority_domain: str,
    call_id: int,
    identity: str,
    action: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> tuple[bool, bool, str | dict[str, Any]]:
    key = call_key(authority_domain, call_id)
    existing = await get_call(redis, authority_domain, call_id)
    active_key = (
        channel_call_key(str(existing["channel_domain"]), str(existing["channel_id"]))
        if existing is not None
        else f"voice:channel-call:missing:{_authority(authority_domain)}:{call_id}"
    )
    result = await cast(
        Awaitable[object],
        redis.eval(
            CALL_TRANSITION_LUA,
            5,
            key,
            f"{key}:participants",
            f"{key}:accepted",
            f"{key}:declined",
            active_key,
            action,
            identity,
            str(int(time.time()) if now is None else now),
            str(settings.voice_call_ttl_seconds),
            call_ref(authority_domain, call_id),
        ),
    )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise RuntimeError("Dragonfly returned an invalid call transition")
    status = int(cast(int | bytes | str, result[0]))
    accepted = status != 0
    raw = result[1].decode() if isinstance(result[1], bytes) else str(result[1])
    if not accepted:
        return False, False, raw
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Dragonfly returned an invalid call record")
    return True, status == 1, cast(dict[str, Any], parsed)


async def apply_authoritative_call(
    redis: Redis,
    record: dict[str, Any],
    settings: Settings,
    *,
    action: str,
    identity: str | None = None,
) -> tuple[bool, bool, str | dict[str, Any]]:
    """Apply an exact state returned by the call authority to a replica."""

    authority_domain = str(record["authority_domain"])
    call_id = int(str(record["id"]))
    key = call_key(authority_domain, call_id)
    encoded = json.dumps(record, separators=(",", ":"), sort_keys=True)
    result = await cast(
        Awaitable[object],
        redis.eval(
            APPLY_AUTHORITATIVE_CALL_LUA,
            5,
            key,
            f"{key}:participants",
            f"{key}:accepted",
            f"{key}:declined",
            channel_call_key(str(record["channel_domain"]), str(record["channel_id"])),
            encoded,
            action,
            identity or "",
            str(settings.voice_call_ttl_seconds),
            call_ref(authority_domain, call_id),
        ),
    )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise RuntimeError("Dragonfly returned an invalid authoritative call transition")
    status = int(cast(int | bytes | str, result[0]))
    raw = result[1].decode() if isinstance(result[1], bytes) else str(result[1])
    if status == 0:
        return False, False, raw
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Dragonfly returned an invalid authoritative call record")
    return True, status == 1, cast(dict[str, Any], parsed)
