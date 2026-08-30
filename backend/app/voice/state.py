from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, cast

from redis.asyncio import Redis

from app.core.settings import Settings
from app.federation.network import FederationNetworkError, normalize_domain
from app.voice.rooms import parse_room_name

BOT_CAPABILITY_BINDINGS_FIELD = "bot_capability_bindings"


def call_bot_capability_bindings(record: Mapping[str, Any]) -> dict[str, dict[str, object]]:
    """Parse the authority-private install proof pinned to each bot caller."""

    raw = record.get(BOT_CAPABILITY_BINDINGS_FIELD, {})
    if not isinstance(raw, dict):
        return {}
    rendered: dict[str, dict[str, object]] = {}
    for identity, binding in raw.items():
        if (
            isinstance(identity, str)
            and isinstance(binding, dict)
            and isinstance(binding.get("grant_id"), str)
            and type(binding.get("revision")) is int
        ):
            rendered[identity] = binding
    return rendered


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
local current = tonumber(redis.call('GET', KEYS[3]) or '0')
local incoming = tonumber(ARGV[1])
if current > incoming then return 0 end
redis.call('DEL', KEYS[1])
for index = 3, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[index], ARGV[index + 1])
end
redis.call('SET', KEYS[2], ARGV[2], 'EX', 300)
redis.call('SET', KEYS[3], ARGV[1], 'EX', 300)
redis.call('EXPIRE', KEYS[1], 300)
return 1
"""
ADMIT_OCCUPANT_LUA = """
local existing = redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1
local limit = tonumber(ARGV[5])
local bypass = ARGV[6] == '1'
if not existing and not bypass and limit > 0 and redis.call('HLEN', KEYS[1]) >= limit then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[3])
redis.call('SET', KEYS[3], ARGV[3], 'EX', 86400)
redis.call('SET', KEYS[4], ARGV[4], 'EX', 300)
return 1
"""
BUMP_GENERATION_LUA = """
local generation = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
return generation
"""
ROTATE_OCCUPANT_GRANT_LUA = """
local expected = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current ~= expected then return 0 end
local connection_raw = redis.call('GET', KEYS[2])
local occupant_raw = redis.call('HGET', KEYS[3], ARGV[2])
if not connection_raw or not occupant_raw then return 0 end
local connection = cjson.decode(connection_raw)
local occupant = cjson.decode(occupant_raw)
if connection['connection_id'] ~= ARGV[3]
    or connection['room'] ~= ARGV[4]
    or tonumber(connection['generation']) ~= expected
    or occupant['connection_id'] ~= ARGV[3]
    or occupant['room'] ~= ARGV[4] then
  return 0
end
local incoming = cjson.decode(ARGV[5])
if incoming['identity'] ~= ARGV[2]
    or incoming['connection_id'] ~= ARGV[3]
    or incoming['room'] ~= ARGV[4]
    or type(incoming['participant_metadata']) ~= 'table' then
  return 0
end
local generation = expected + 1
incoming['participant_metadata']['generation'] = generation
connection['generation'] = generation
redis.call('SET', KEYS[1], tostring(generation), 'EX', tonumber(ARGV[6]))
redis.call('SET', KEYS[2], cjson.encode(connection), 'EX', tonumber(ARGV[6]))
redis.call('HSET', KEYS[3], ARGV[2], cjson.encode(incoming))
redis.call('SET', KEYS[4], ARGV[7], 'EX', 300)
return generation
"""
CLAIM_CONNECTION_LUA = """
local raw = redis.call('GET', KEYS[1])
local function retain_index()
  if ARGV[11] ~= '' then
    redis.call('SADD', KEYS[3], ARGV[11])
    redis.call('EXPIRE', KEYS[3], tonumber(ARGV[9]))
  end
end
if raw then
  local current = cjson.decode(raw)
  retain_index()
  if current['connection_id'] == ARGV[1] and current['room'] == ARGV[2] then
    if (current['bot_lineage'] or '') ~= ARGV[10] then
      return {0, tostring(current['generation']), current['room'], 'another worker'}
    end
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[6]))
    return {1, tostring(current['generation']), '', ''}
  end
  if ARGV[5] ~= '1' then
    return {
      0,
      tostring(current['generation']),
      current['room'],
      current['client_kind'] or 'another device'
    }
  end
  if current['room'] ~= ARGV[2] then
    local old_generation_key = ARGV[7] .. current['room'] .. ':' .. ARGV[8]
    redis.call('INCR', old_generation_key)
    redis.call('EXPIRE', old_generation_key, tonumber(ARGV[9]))
  end
end
local generation = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[9]))
local claimed = {
  connection_id = ARGV[1],
  room = ARGV[2],
  generation = generation,
  client_kind = ARGV[3],
  claimed_at = tonumber(ARGV[4])
}
if ARGV[10] ~= '' then claimed['bot_lineage'] = ARGV[10] end
redis.call('SET', KEYS[1], cjson.encode(claimed), 'EX', tonumber(ARGV[6]))
retain_index()
local previous = raw and cjson.decode(raw) or nil
return {
  1,
  tostring(generation),
  previous and previous['room'] or '',
  previous and (previous['client_kind'] or 'another device') or ''
}
"""
RELEASE_CONNECTION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local current = cjson.decode(raw)
if current['connection_id'] ~= ARGV[1] then return 0 end
if ARGV[2] ~= '' and current['room'] ~= ARGV[2] then return 0 end
if ARGV[3] ~= '' and tonumber(current['generation']) ~= tonumber(ARGV[3]) then return 0 end
local removed = redis.call('DEL', KEYS[1])
if removed == 1 and ARGV[4] ~= '' then
  redis.call('SREM', KEYS[2], ARGV[4])
  if redis.call('SCARD', KEYS[2]) == 0 then redis.call('DEL', KEYS[2]) end
end
return removed
"""
REFRESH_CONNECTION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local current = cjson.decode(raw)
if current['connection_id'] ~= ARGV[1] then return 0 end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
if ARGV[3] ~= '' then
  redis.call('SADD', KEYS[2], ARGV[3])
  redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
end
return 1
"""
MIGRATE_BOT_GUILD_CONNECTION_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('SADD', KEYS[3], ARGV[3])
  local current_ttl = redis.call('TTL', KEYS[1])
  if current_ttl < 1 then current_ttl = tonumber(ARGV[2]) end
  redis.call('EXPIRE', KEYS[3], current_ttl)
  return 1
end
local raw = redis.call('GET', KEYS[2])
if not raw then return 0 end
local current = cjson.decode(raw)
if current['client_kind'] ~= 'bot'
    or type(current['room']) ~= 'string'
    or string.sub(current['room'], 1, string.len(ARGV[1])) ~= ARGV[1] then
  return 0
end
local ttl = redis.call('PTTL', KEYS[2])
if ttl < 1 then ttl = tonumber(ARGV[2]) * 1000 end
redis.call('SET', KEYS[1], raw, 'PX', ttl)
redis.call('DEL', KEYS[2])
redis.call('SADD', KEYS[3], ARGV[3])
redis.call('PEXPIRE', KEYS[3], ttl)
return 1
"""
REMOVE_OCCUPANT_CONNECTION_LUA = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local current = cjson.decode(raw)
if current['connection_id'] ~= ARGV[2] then return 0 end
if ARGV[4] ~= '' and (
    type(current['participant_metadata']) ~= 'table'
    or tonumber(current['participant_metadata']['generation']) ~= tonumber(ARGV[4])
  ) then return 0 end
redis.call('HDEL', KEYS[1], ARGV[1])
for index = 2, #KEYS do
  if redis.call('GET', KEYS[index]) == ARGV[3] then redis.call('DEL', KEYS[index]) end
end
return 1
"""
REMOVE_OCCUPANT_LUA = """
redis.call('HDEL', KEYS[1], ARGV[1])
for index = 2, #KEYS do
  if redis.call('GET', KEYS[index]) == ARGV[2] then
    redis.call('DEL', KEYS[index])
  end
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
  connection_id = ARGV[7],
  client_kind = ARGV[8],
  ready = true,
  active = false
}
redis.call('SET', KEYS[2], cjson.encode(current), 'EX', tonumber(ARGV[6]))
redis.call('DEL', KEYS[1])
return 1
"""
ACTIVATE_FEDERATED_DM_VOICE_SESSION_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local current = {
  authority_domain = ARGV[2],
  guild_id = '',
  room = ARGV[3],
  generation = tonumber(ARGV[4]),
  move_session_id = ARGV[1],
  ready = true,
  active = false,
  call_id = ARGV[5],
  channel_id = ARGV[6],
  connection_id = ARGV[7],
  client_kind = ARGV[9]
}
redis.call('SET', KEYS[2], cjson.encode(current), 'EX', tonumber(ARGV[8]))
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
    and (ARGV[8] == '' or (current['connection_id'] or '') == ARGV[8])
    and current['active'] ~= true then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[9]))
  return {1, 'replay'}
end
if current['move_session_id'] ~= ARGV[1]
    or current['authority_domain'] ~= ARGV[2]
    or tostring(current['guild_id']) ~= ARGV[3]
    or current['room'] ~= ARGV[4]
    or tonumber(current['generation']) ~= tonumber(ARGV[5])
    or (ARGV[8] ~= '' and current['connection_id'] and current['connection_id'] ~= ''
        and current['connection_id'] ~= ARGV[8]) then
  return {0, 'mismatch'}
end
if current['active'] ~= true then return {0, 'inactive'} end
current['room'] = ARGV[6]
current['generation'] = tonumber(ARGV[7])
if ARGV[8] ~= '' then current['connection_id'] = ARGV[8] end
current['active'] = false
redis.call('SET', KEYS[1], cjson.encode(current), 'EX', tonumber(ARGV[9]))
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
if ARGV[4] ~= '' then
  local incoming_generation = tonumber(ARGV[4])
  if incoming_generation < tonumber(current['generation']) then return 0 end
  current['generation'] = incoming_generation
end
if ARGV[5] ~= '' then
  if current['connection_id'] and current['connection_id'] ~= ''
      and current['connection_id'] ~= ARGV[5] then return 0 end
  current['connection_id'] = ARGV[5]
end
redis.call('SET', KEYS[1], cjson.encode(current), 'EX', tonumber(ARGV[3]))
return 1
"""
SYNC_FEDERATED_VOICE_SESSION_GENERATION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local current = cjson.decode(raw)
if current['move_session_id'] ~= ARGV[1]
    or current['authority_domain'] ~= ARGV[2]
    or current['room'] ~= ARGV[3]
    or (current['connection_id'] or '') ~= ARGV[4] then
  return 0
end
local expected = tonumber(ARGV[5])
local incoming = tonumber(ARGV[6])
local generation = tonumber(current['generation'])
if generation == incoming then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[7]))
  return 1
end
if generation ~= expected or incoming ~= expected + 1 then return 0 end
current['generation'] = incoming
redis.call('SET', KEYS[1], cjson.encode(current), 'EX', tonumber(ARGV[7]))
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
if ARGV[5] ~= '' and (current['connection_id'] or '') ~= ARGV[5] then return 0 end
if ARGV[6] ~= '' and tonumber(current['generation']) ~= tonumber(ARGV[6]) then return 0 end
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
    connection_id: str = ""
    client_kind: str = "unknown"
    self_mute: bool = False
    self_deaf: bool = False
    server_mute: bool = False
    server_deaf: bool = False
    suppressed: bool = False
    request_to_speak_timestamp: str | None = None
    can_speak: bool = False
    can_stream: bool = False
    can_priority_speak: bool = False
    allow_listen: bool = True
    allow_speak: bool = True
    allow_stream: bool = True
    participant_metadata: dict[str, object] = field(default_factory=dict)


def public_occupant_state(occupant: Occupant) -> dict[str, object]:
    """Return the Discord-like voice state safe to expose outside the authority.

    Connection ids, requested capability ceilings, and signed-token metadata
    are control-plane state. They must never be copied to clients or peers.
    """

    return {
        "identity": occupant.identity,
        "user_id": occupant.user_id,
        "user_domain": occupant.user_domain,
        "room": occupant.room,
        "guild_id": occupant.guild_id,
        "channel_id": occupant.channel_id,
        "joined_at": occupant.joined_at,
        "self_mute": occupant.self_mute,
        "self_deaf": occupant.self_deaf,
        "server_mute": occupant.server_mute,
        "server_deaf": occupant.server_deaf,
        "suppressed": occupant.suppressed,
        "request_to_speak_timestamp": occupant.request_to_speak_timestamp,
        "can_speak": occupant.can_speak,
        "can_stream": occupant.can_stream,
        "can_priority_speak": occupant.can_priority_speak,
    }


def federation_occupant_state(occupant: Occupant) -> dict[str, object]:
    """Render the private fence required for a peer to reject stale snapshots."""

    generation = occupant.participant_metadata.get("generation")
    if type(generation) is not int or not occupant.connection_id:
        raise ValueError("authoritative occupant is missing its capability fence")
    move_session_id = occupant.participant_metadata.get("move_session_id")
    if move_session_id is not None and not isinstance(move_session_id, str):
        raise ValueError("authoritative occupant has an invalid federation correlation")
    return {
        **public_occupant_state(occupant),
        "generation": generation,
        "connection_id": occupant.connection_id,
        "move_session_id": move_session_id,
    }


def occupant_from_federation_state(state: Mapping[str, object]) -> Occupant:
    """Hydrate one private peer projection without exposing its fence publicly."""

    payload = dict(state)
    generation = payload.pop("generation", None)
    move_session_id = payload.pop("move_session_id", None)
    connection_id = payload.get("connection_id")
    if type(generation) is not int or not isinstance(connection_id, str):
        raise ValueError("federated occupant is missing its capability fence")
    if move_session_id is not None and not isinstance(move_session_id, str):
        raise ValueError("federated occupant has an invalid session correlation")
    payload["participant_metadata"] = {
        "generation": generation,
        **({"move_session_id": move_session_id} if move_session_id is not None else {}),
    }
    return Occupant(**payload)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FederatedVoiceSession:
    """Fenced remote-room correlation retained by member and authority homes."""

    authority_domain: str
    guild_id: str
    room: str
    generation: int
    move_session_id: str
    ready: bool = False
    active: bool = False
    call_id: str | None = None
    channel_id: str | None = None
    connection_id: str = ""
    # Missing stored values are treated as web, which cannot publish priority data.
    client_kind: str = "web"


def _authority(value: str) -> str:
    return normalize_domain(value)


def room_state_key(kind: str, authority_domain: str, room: str) -> str:
    return f"voice:v2:{kind}:{_authority(authority_domain)}:{room}"


def generation_key(authority_domain: str, room: str, identity: str) -> str:
    return f"{room_state_key('generation', authority_domain, room)}:{identity}"


def legacy_voice_connection_key(authority_domain: str, identity: str) -> str:
    return f"voice:v2:connection:{_authority(authority_domain)}:{identity}"


def _bot_guild_id(room: str | None, client_kind: str | None) -> int | None:
    if client_kind != "bot" or room is None:
        return None
    kind, scope_id, _leaf_id = parse_room_name(room)
    return scope_id if kind == "g" else None


def voice_connection_key(
    authority_domain: str,
    identity: str,
    *,
    room: str | None = None,
    client_kind: str | None = None,
) -> str:
    """Return one human/DM claim or one bot-per-guild claim.

    Discord bots may maintain one voice client per guild. Human sessions and
    bot DM calls retain the existing per-authority identity ceiling.
    """

    authority = _authority(authority_domain)
    guild_id = _bot_guild_id(room, client_kind)
    if guild_id is None:
        return legacy_voice_connection_key(authority, identity)
    return f"voice:v3:connection:{authority}:{identity}:guild:{guild_id}@{authority}"


def bot_voice_connection_index_key(authority_domain: str, identity: str) -> str:
    return f"voice:v3:connection-index:{_authority(authority_domain)}:{identity}"


def _bot_connection_index_member(
    authority_domain: str,
    identity: str,
    room: str | None,
    client_kind: str | None,
) -> str:
    if _bot_guild_id(room, client_kind) is None:
        return ""
    return voice_connection_key(
        authority_domain,
        identity,
        room=room,
        client_kind=client_kind,
    )


def voice_room_registry_key(authority_domain: str) -> str:
    """Return the authority-scoped registry of locally controlled rooms."""

    return f"voice:v2:rooms:{_authority(authority_domain)}"


def voice_user_room_key(
    authority_domain: str,
    identity: str,
    *,
    guild_id: int | str | None = None,
) -> str:
    """Return an exact authority/participant room-pointer key."""

    authority = _authority(authority_domain)
    if guild_id is None:
        return f"voice:v2:user-room:{authority}:{identity}"
    rendered_guild_id = str(guild_id)
    if not rendered_guild_id.isascii() or not rendered_guild_id.isdecimal():
        raise ValueError("voice guild scope must be a snowflake")
    return f"voice:v3:user-room:{authority}:{identity}:guild:{int(rendered_guild_id)}@{authority}"


def _occupant_user_room_key(authority_domain: str, occupant: Occupant) -> str:
    guild_id = (
        occupant.guild_id
        if occupant.client_kind == "bot" and occupant.guild_id is not None
        else None
    )
    return voice_user_room_key(
        authority_domain,
        occupant.identity,
        guild_id=guild_id,
    )


def _removable_user_room_keys(
    authority_domain: str,
    room: str,
    identity: str,
) -> tuple[str, ...]:
    kind, scope_id, _leaf_id = parse_room_name(room)
    candidates = [
        voice_user_room_key(authority_domain, identity),
    ]
    if kind == "g":
        candidates.insert(
            0,
            voice_user_room_key(authority_domain, identity, guild_id=scope_id),
        )
    return tuple(dict.fromkeys(candidates))


def voice_grant_transition_key(authority_domain: str, room: str, identity: str) -> str:
    return f"{room_state_key('grant-transition', authority_domain, room)}:{identity}"


async def _migrate_bot_guild_connection(
    redis: Redis,
    authority_domain: str,
    identity: str,
    room: str,
    client_kind: str,
) -> str:
    """Move an exact legacy bot-guild claim into its per-guild namespace."""

    primary = voice_connection_key(
        authority_domain,
        identity,
        room=room,
        client_kind=client_kind,
    )
    legacy = legacy_voice_connection_key(authority_domain, identity)
    guild_id = _bot_guild_id(room, client_kind)
    if guild_id is None or primary == legacy:
        return primary
    await cast(
        Awaitable[object],
        redis.eval(
            MIGRATE_BOT_GUILD_CONNECTION_LUA,
            3,
            primary,
            legacy,
            bot_voice_connection_index_key(authority_domain, identity),
            f"g.{guild_id}.",
            str(2 * 60),
            primary,
        ),
    )
    return primary


async def claim_voice_connection(
    redis: Redis,
    authority_domain: str,
    identity: str,
    *,
    connection_id: str,
    room: str,
    client_kind: str,
    takeover: bool,
    bot_lineage: Mapping[str, object] | None = None,
) -> tuple[bool, int, str, str]:
    authority = _authority(authority_domain)
    connection_key = await _migrate_bot_guild_connection(
        redis,
        authority,
        identity,
        room,
        client_kind,
    )
    index_member = _bot_connection_index_member(
        authority,
        identity,
        room,
        client_kind,
    )
    result = await cast(
        Awaitable[object],
        redis.eval(
            CLAIM_CONNECTION_LUA,
            3,
            connection_key,
            generation_key(authority, room, identity),
            bot_voice_connection_index_key(authority, identity),
            connection_id,
            room,
            client_kind,
            str(int(time.time())),
            "1" if takeover else "0",
            str(2 * 60),
            f"{room_state_key('generation', authority, '')}",
            identity,
            str(24 * 60 * 60),
            (
                json.dumps(dict(bot_lineage), sort_keys=True, separators=(",", ":"))
                if bot_lineage is not None
                else ""
            ),
            index_member,
        ),
    )
    if not isinstance(result, (list, tuple)) or len(result) != 4:
        raise RuntimeError("Dragonfly returned an invalid voice connection claim")
    decoded = [item.decode() if isinstance(item, bytes) else str(item) for item in result]
    return decoded[0] == "1", int(decoded[1]), decoded[2], decoded[3]


async def voice_connection_claim(
    redis: Redis,
    authority_domain: str,
    identity: str,
    *,
    room: str | None = None,
    client_kind: str | None = None,
) -> dict[str, object] | None:
    key = (
        await _migrate_bot_guild_connection(
            redis,
            authority_domain,
            identity,
            room,
            client_kind,
        )
        if room is not None and client_kind is not None
        else voice_connection_key(authority_domain, identity)
    )
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return cast(dict[str, object], decoded)


async def bot_guild_voice_connection_claims(
    redis: Redis,
    authority_domain: str,
    identity: str,
) -> list[dict[str, object]]:
    """Enumerate every exact per-guild claim for one bot identity."""

    legacy = await voice_connection_claim(redis, authority_domain, identity)
    if (
        legacy is not None
        and legacy.get("client_kind") == "bot"
        and isinstance(legacy.get("room"), str)
    ):
        try:
            if parse_room_name(cast(str, legacy["room"]))[0] == "g":
                await _migrate_bot_guild_connection(
                    redis,
                    authority_domain,
                    identity,
                    cast(str, legacy["room"]),
                    "bot",
                )
        except ValueError:
            pass

    index_key = bot_voice_connection_index_key(authority_domain, identity)
    raw_members = await cast(Awaitable[set[Any]], redis.smembers(index_key))
    members = sorted(
        item.decode() if isinstance(item, bytes) else str(item) for item in raw_members
    )
    claims: list[dict[str, object]] = []
    stale: list[str] = []
    for member in members:
        raw = await redis.get(member)
        if raw is None:
            stale.append(member)
            continue
        try:
            parsed = json.loads(raw)
            room = parsed.get("room") if isinstance(parsed, dict) else None
            if (
                not isinstance(parsed, dict)
                or parsed.get("client_kind") != "bot"
                or not isinstance(room, str)
                or parse_room_name(room)[0] != "g"
                or member
                != voice_connection_key(
                    authority_domain,
                    identity,
                    room=room,
                    client_kind="bot",
                )
            ):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            stale.append(member)
            continue
        claims.append(cast(dict[str, object], parsed))
    if stale:
        await cast(Awaitable[object], redis.srem(index_key, *stale))
        remaining = await cast(Awaitable[int], redis.scard(index_key))
        if not remaining:
            await redis.delete(index_key)
    return sorted(claims, key=lambda claim: str(claim["room"]))


async def release_voice_connection(
    redis: Redis,
    authority_domain: str,
    identity: str,
    connection_id: str,
    *,
    room: str | None = None,
    generation: int | None = None,
    client_kind: str | None = None,
) -> bool:
    key = (
        await _migrate_bot_guild_connection(
            redis,
            authority_domain,
            identity,
            room,
            client_kind,
        )
        if room is not None and client_kind is not None
        else voice_connection_key(authority_domain, identity)
    )
    result = await cast(
        Awaitable[object],
        redis.eval(
            RELEASE_CONNECTION_LUA,
            2,
            key,
            bot_voice_connection_index_key(authority_domain, identity),
            connection_id,
            room or "",
            str(generation) if generation is not None else "",
            _bot_connection_index_member(
                authority_domain,
                identity,
                room,
                client_kind,
            ),
        ),
    )
    return bool(result)


async def refresh_voice_connection(
    redis: Redis,
    authority_domain: str,
    identity: str,
    connection_id: str,
    *,
    room: str,
    client_kind: str,
) -> bool:
    key = await _migrate_bot_guild_connection(
        redis,
        authority_domain,
        identity,
        room,
        client_kind,
    )
    result = await cast(
        Awaitable[object],
        redis.eval(
            REFRESH_CONNECTION_LUA,
            2,
            key,
            bot_voice_connection_index_key(authority_domain, identity),
            connection_id,
            str(24 * 60 * 60),
            _bot_connection_index_member(
                authority_domain,
                identity,
                room,
                client_kind,
            ),
        ),
    )
    return bool(result)


async def voice_connection_matches(
    redis: Redis,
    authority_domain: str,
    identity: str,
    *,
    connection_id: str,
    room: str,
    generation: int,
    client_kind: str | None = None,
) -> bool:
    key = (
        await _migrate_bot_guild_connection(
            redis,
            authority_domain,
            identity,
            room,
            client_kind,
        )
        if client_kind is not None
        else voice_connection_key(authority_domain, identity)
    )
    raw = await redis.get(key)
    if raw is None:
        return False
    try:
        current = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(current, dict)
        and current.get("connection_id") == connection_id
        and current.get("room") == room
        and current.get("generation") == generation
    )


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
    guild_session = session.guild_id.isascii() and session.guild_id.isdecimal()
    dm_session = (
        session.guild_id == ""
        and session.call_id is not None
        and session.call_id.isascii()
        and session.call_id.isdecimal()
        and session.channel_id is not None
        and session.channel_id.isascii()
        and session.channel_id.isdecimal()
    )
    if (
        session.authority_domain != authority_domain
        or guild_session == dm_session
        or not session.move_session_id
        or session.generation < 0
        or (guild_session and (session.call_id is not None or session.channel_id is not None))
        or (session.connection_id and len(session.connection_id) != 43)
        or session.client_kind not in {"web", "desktop", "mobile", "bot"}
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
    connection_id: str = "",
    client_kind: str = "web",
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
            connection_id,
            client_kind,
        ),
    )
    return bool(result)


async def activate_federated_dm_voice_home_session(
    redis: Redis,
    identity: str,
    *,
    move_session_id: str,
    authority_domain: str,
    call_id: str,
    channel_id: str,
    room: str,
    generation: int,
    connection_id: str,
    client_kind: str = "web",
) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            ACTIVATE_FEDERATED_DM_VOICE_SESSION_LUA,
            2,
            federated_voice_pending_key(identity),
            federated_voice_session_key("home", identity),
            move_session_id,
            _authority(authority_domain),
            room,
            str(generation),
            call_id,
            channel_id,
            connection_id,
            str(FEDERATED_VOICE_PENDING_TTL_SECONDS),
            client_kind,
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
    connection_id: str | None = None,
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
            connection_id or "",
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
    generation: int | None = None,
    connection_id: str | None = None,
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
            str(generation) if generation is not None else "",
            connection_id or "",
        ),
    )
    return bool(result)


async def sync_federated_voice_session_generation(
    redis: Redis,
    role: str,
    identity: str,
    *,
    move_session_id: str,
    authority_domain: str,
    room: str,
    connection_id: str,
    expected_generation: int,
    generation: int,
) -> bool:
    """Advance exactly one authority rotation, accepting its exact replay."""

    result = await cast(
        Awaitable[object],
        redis.eval(
            SYNC_FEDERATED_VOICE_SESSION_GENERATION_LUA,
            1,
            federated_voice_session_key(role, identity),
            move_session_id,
            _authority(authority_domain),
            room,
            connection_id,
            str(expected_generation),
            str(generation),
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
    connection_id: str | None = None,
    generation: int | None = None,
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
            connection_id or "",
            str(generation) if generation is not None else "",
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


async def claim_voice_grant_transition(
    redis: Redis,
    authority_domain: str,
    room: str,
    identity: str,
    token: str,
) -> bool:
    return bool(
        await redis.set(
            voice_grant_transition_key(authority_domain, room, identity),
            token,
            nx=True,
            ex=10,
        )
    )


async def release_voice_grant_transition(
    redis: Redis,
    authority_domain: str,
    room: str,
    identity: str,
    token: str,
) -> bool:
    result = await cast(
        Awaitable[object],
        redis.eval(
            DELETE_VALUE_IF_EQUAL_LUA,
            1,
            voice_grant_transition_key(authority_domain, room, identity),
            token,
        ),
    )
    return bool(result)


async def voice_grant_transition_active(
    redis: Redis,
    authority_domain: str,
    room: str,
    identity: str,
) -> bool:
    return bool(await redis.exists(voice_grant_transition_key(authority_domain, room, identity)))


async def rotate_occupant_grant(
    redis: Redis,
    authority_domain: str,
    occupant: Occupant,
    *,
    expected_generation: int,
) -> int | None:
    """Atomically supersede the old token while retaining a live participant."""

    if not occupant.connection_id or not occupant.participant_metadata:
        return None
    encoded = json.dumps(asdict(occupant), separators=(",", ":"), sort_keys=True)
    connection_key = await _migrate_bot_guild_connection(
        redis,
        authority_domain,
        occupant.identity,
        occupant.room,
        occupant.client_kind,
    )
    result = await cast(
        Awaitable[object],
        redis.eval(
            ROTATE_OCCUPANT_GRANT_LUA,
            4,
            generation_key(authority_domain, occupant.room, occupant.identity),
            connection_key,
            room_state_key("occupancy", authority_domain, occupant.room),
            room_state_key("heartbeat", authority_domain, occupant.room),
            str(expected_generation),
            occupant.identity,
            occupant.connection_id,
            occupant.room,
            encoded,
            str(24 * 60 * 60),
            str(int(time.time())),
        ),
    )
    generation = int(cast(int | bytes | str, result))
    return generation if generation > 0 else None


async def set_occupant(redis: Redis, authority_domain: str, occupant: Occupant) -> None:
    encoded = json.dumps(asdict(occupant), separators=(",", ":"), sort_keys=True)
    async with redis.pipeline(transaction=True) as pipeline:
        pipeline.hset(
            room_state_key("occupancy", authority_domain, occupant.room), occupant.identity, encoded
        )
        pipeline.sadd(voice_room_registry_key(authority_domain), occupant.room)
        pipeline.set(_occupant_user_room_key(authority_domain, occupant), occupant.room, ex=86400)
        pipeline.set(
            room_state_key("heartbeat", authority_domain, occupant.room),
            str(int(time.time())),
            ex=300,
        )
        await pipeline.execute()
    if occupant.connection_id:
        await refresh_voice_connection(
            redis,
            authority_domain,
            occupant.identity,
            occupant.connection_id,
            room=occupant.room,
            client_kind=occupant.client_kind,
        )


async def admit_occupant(
    redis: Redis,
    authority_domain: str,
    occupant: Occupant,
    *,
    user_limit: int,
    bypass_limit: bool,
) -> bool:
    """Atomically enforce a voice-channel limit and record an admitted user."""

    if not 0 <= user_limit <= 10_000:
        raise ValueError("voice channel user limit must be between 0 and 10000")
    encoded = json.dumps(asdict(occupant), separators=(",", ":"), sort_keys=True)
    admitted = bool(
        await cast(
            Awaitable[object],
            redis.eval(
                ADMIT_OCCUPANT_LUA,
                4,
                room_state_key("occupancy", authority_domain, occupant.room),
                voice_room_registry_key(authority_domain),
                _occupant_user_room_key(authority_domain, occupant),
                room_state_key("heartbeat", authority_domain, occupant.room),
                occupant.identity,
                encoded,
                occupant.room,
                str(int(time.time())),
                str(user_limit),
                "1" if bypass_limit else "0",
            ),
        )
    )
    if admitted and occupant.connection_id:
        await refresh_voice_connection(
            redis,
            authority_domain,
            occupant.identity,
            occupant.connection_id,
            room=occupant.room,
            client_kind=occupant.client_kind,
        )
    return admitted


async def remove_occupant(redis: Redis, authority_domain: str, room: str, identity: str) -> None:
    pointer_keys = _removable_user_room_keys(authority_domain, room, identity)
    await cast(
        Awaitable[object],
        redis.eval(
            REMOVE_OCCUPANT_LUA,
            1 + len(pointer_keys),
            room_state_key("occupancy", authority_domain, room),
            *pointer_keys,
            identity,
            room,
        ),
    )


async def remove_occupant_connection(
    redis: Redis,
    authority_domain: str,
    room: str,
    identity: str,
    connection_id: str,
    *,
    generation: int | None = None,
) -> bool:
    pointer_keys = _removable_user_room_keys(authority_domain, room, identity)
    result = await cast(
        Awaitable[object],
        redis.eval(
            REMOVE_OCCUPANT_CONNECTION_LUA,
            1 + len(pointer_keys),
            room_state_key("occupancy", authority_domain, room),
            *pointer_keys,
            identity,
            connection_id,
            room,
            str(generation) if generation is not None else "",
        ),
    )
    return bool(result)


async def update_self_flags(
    redis: Redis,
    authority_domain: str,
    identity: str,
    *,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant | None:
    occupant = await occupant_for_identity(redis, authority_domain, identity)
    if occupant is None:
        return None
    return await update_occupant_self_flags(
        redis,
        authority_domain,
        occupant.room,
        identity,
        self_mute=self_mute,
        self_deaf=self_deaf,
    )


async def update_occupant_self_flags(
    redis: Redis,
    authority_domain: str,
    room: str,
    identity: str,
    *,
    self_mute: bool,
    self_deaf: bool,
) -> Occupant | None:
    """Update a known authority/room projection without trusting a global pointer."""

    occupant = await occupant_in_room(redis, authority_domain, room, identity)
    if occupant is None:
        return None
    updated = replace(occupant, self_mute=self_mute, self_deaf=self_deaf)
    await cast(
        Awaitable[Any],
        redis.hset(
            room_state_key("occupancy", authority_domain, updated.room),
            identity,
            json.dumps(asdict(updated), separators=(",", ":"), sort_keys=True),
        ),
    )
    return updated


async def occupant_in_room(
    redis: Redis,
    authority_domain: str,
    room: str,
    identity: str,
) -> Occupant | None:
    """Resolve an occupant from one exact authority/room namespace."""

    raw = await cast(
        Awaitable[Any],
        redis.hget(room_state_key("occupancy", authority_domain, room), identity),
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        occupant = Occupant(**data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if occupant.identity != identity or occupant.room != room:
        return None
    return occupant


async def voice_user_room(
    redis: Redis,
    authority_domain: str,
    identity: str,
    *,
    guild_id: int | str | None = None,
) -> str | None:
    """Resolve and lazily migrate one exact authority-scoped room pointer.

    The pre-federation ``voice:user-room:{identity}`` key is intentionally not
    consumed: its bare room value cannot prove which authority wrote it. It is
    bounded by its existing TTL and reconnecting writes an exact v2/v3 key.
    """

    primary = voice_user_room_key(
        authority_domain,
        identity,
        guild_id=guild_id,
    )
    keys = tuple(
        dict.fromkeys(
            (
                primary,
                voice_user_room_key(authority_domain, identity),
            )
        )
    )
    expected_guild_id = str(int(str(guild_id))) if guild_id is not None else None
    for key in keys:
        raw = await redis.get(key)
        if raw is None:
            continue
        room = raw.decode() if isinstance(raw, bytes) else str(raw)
        occupant = await occupant_in_room(redis, authority_domain, room, identity)
        if occupant is None or (
            expected_guild_id is not None and occupant.guild_id != expected_guild_id
        ):
            continue
        if key == primary:
            return room
        await redis.set(primary, room, ex=24 * 60 * 60, nx=True)
        migrated_raw = await redis.get(primary)
        migrated = (
            migrated_raw.decode()
            if isinstance(migrated_raw, bytes)
            else str(migrated_raw)
            if migrated_raw is not None
            else None
        )
        if migrated == room:
            await cast(
                Awaitable[object],
                redis.eval(DELETE_VALUE_IF_EQUAL_LUA, 1, key, room),
            )
            return room
        if migrated is not None:
            current = await occupant_in_room(redis, authority_domain, migrated, identity)
            if current is not None and (
                expected_guild_id is None or current.guild_id == expected_guild_id
            ):
                return migrated
    return None


async def occupant_for_identity(
    redis: Redis,
    authority_domain: str,
    identity: str,
    *,
    guild_id: int | str | None = None,
) -> Occupant | None:
    """Resolve the exact room occupant without crossing authority namespaces."""

    room = await voice_user_room(
        redis,
        authority_domain,
        identity,
        guild_id=guild_id,
    )
    if room is None:
        return None
    return await occupant_in_room(redis, authority_domain, room, identity)


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
            public_occupant_state(item)
            for item in await room_occupants(redis, authority_domain, room)
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
    snapshot_version: int | None = None,
) -> bool:
    encoded = {
        item.identity: json.dumps(asdict(item), separators=(",", ":"), sort_keys=True)
        for item in occupants
    }
    arguments: list[str] = [
        str(generated_at if snapshot_version is None else snapshot_version),
        str(generated_at),
    ]
    for identity, value in sorted(encoded.items()):
        arguments.extend((identity, value))
    result = await cast(
        Awaitable[object],
        redis.eval(
            REPLACE_OCCUPANCY_LUA,
            3,
            room_state_key("occupancy", authority_domain, room),
            room_state_key("heartbeat", authority_domain, room),
            room_state_key("snapshot-version", authority_domain, room),
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
