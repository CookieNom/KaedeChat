from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.voice.background import migrate_legacy_voice_room_registry
from app.voice.state import (
    CLAIM_CONNECTION_LUA,
    DELETE_VALUE_IF_EQUAL_LUA,
    MIGRATE_BOT_GUILD_CONNECTION_LUA,
    RELEASE_CONNECTION_LUA,
    REMOVE_OCCUPANT_LUA,
    bot_guild_voice_connection_claims,
    bot_voice_connection_index_key,
    claim_voice_connection,
    legacy_voice_connection_key,
    release_voice_connection,
    remove_occupant,
    voice_connection_key,
    voice_connection_matches,
    voice_user_room,
)


class VoiceRedis:
    """Small Redis state machine for exact connection/index key regressions."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[tuple[str, str], str] = {}
        self.generations: dict[str, int] = {}
        self.eval_calls: list[tuple[object, ...]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get((key, field))

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def sadd(self, key: str, *members: str) -> int:
        values = self.sets.setdefault(key, set())
        before = len(values)
        values.update(members)
        return len(values) - before

    async def srem(self, key: str, *members: str) -> int:
        values = self.sets.setdefault(key, set())
        before = len(values)
        values.difference_update(members)
        return before - len(values)

    async def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(self.values.pop(key, None) is not None)
            removed += int(self.sets.pop(key, None) is not None)
        return removed

    async def eval(self, script: str, key_count: int, *items: object) -> object:
        self.eval_calls.append((script, key_count, *items))
        keys = [str(item) for item in items[:key_count]]
        args = [str(item) for item in items[key_count:]]
        handlers: dict[str, Callable[[list[str], list[str]], object]] = {
            MIGRATE_BOT_GUILD_CONNECTION_LUA: self._migrate,
            CLAIM_CONNECTION_LUA: self._claim,
            RELEASE_CONNECTION_LUA: self._release,
            REMOVE_OCCUPANT_LUA: self._remove_occupant,
            DELETE_VALUE_IF_EQUAL_LUA: self._delete_value,
        }
        return handlers[script](keys, args)

    def _migrate(self, keys: list[str], args: list[str]) -> int:
        primary, legacy, index = keys
        prefix, _fallback_ttl, member = args
        if primary in self.values:
            self.sets.setdefault(index, set()).add(member)
            return 1
        raw = self.values.get(legacy)
        if raw is None:
            return 0
        current = json.loads(raw)
        if current.get("client_kind") != "bot" or not str(current.get("room", "")).startswith(
            prefix
        ):
            return 0
        self.values[primary] = raw
        del self.values[legacy]
        self.sets.setdefault(index, set()).add(member)
        return 1

    def _claim(self, keys: list[str], args: list[str]) -> list[object]:
        connection_key, generation_key, index_key = keys
        (
            connection_id,
            room,
            client_kind,
            claimed_at,
            takeover,
            _claim_ttl,
            _generation_prefix,
            _identity,
            _generation_ttl,
            bot_lineage,
            index_member,
        ) = args
        raw = self.values.get(connection_key)
        current = json.loads(raw) if raw is not None else None
        if current is not None:
            if index_member:
                self.sets.setdefault(index_key, set()).add(index_member)
            if current["connection_id"] == connection_id and current["room"] == room:
                return [1, current["generation"], "", ""]
            if takeover != "1":
                return [
                    0,
                    current["generation"],
                    current["room"],
                    current["client_kind"],
                ]
        generation = self.generations.get(generation_key, 0) + 1
        self.generations[generation_key] = generation
        claimed: dict[str, object] = {
            "connection_id": connection_id,
            "room": room,
            "generation": generation,
            "client_kind": client_kind,
            "claimed_at": int(claimed_at),
        }
        if bot_lineage:
            claimed["bot_lineage"] = bot_lineage
        self.values[connection_key] = json.dumps(claimed)
        if index_member:
            self.sets.setdefault(index_key, set()).add(index_member)
        return [
            1,
            generation,
            current["room"] if current is not None else "",
            current["client_kind"] if current is not None else "",
        ]

    def _release(self, keys: list[str], args: list[str]) -> int:
        connection_key, index_key = keys
        connection_id, room, generation, index_member = args
        raw = self.values.get(connection_key)
        if raw is None:
            return 0
        current = json.loads(raw)
        if current["connection_id"] != connection_id:
            return 0
        if room and current["room"] != room:
            return 0
        if generation and current["generation"] != int(generation):
            return 0
        del self.values[connection_key]
        if index_member:
            self.sets.setdefault(index_key, set()).discard(index_member)
        return 1

    def _remove_occupant(self, keys: list[str], args: list[str]) -> int:
        _occupancy, *pointer_keys = keys
        _identity, room = args
        for key in pointer_keys:
            if self.values.get(key) == room:
                del self.values[key]
        return 1

    def _delete_value(self, keys: list[str], args: list[str]) -> int:
        if self.values.get(keys[0]) != args[0]:
            return 0
        del self.values[keys[0]]
        return 1


@pytest.mark.asyncio
async def test_bot_voice_claims_are_independent_per_authority_qualified_guild() -> None:
    redis = VoiceRedis()
    identity = "70@apps.example"

    first = await claim_voice_connection(
        cast(Any, redis),
        "chat.example",
        identity,
        connection_id="a" * 43,
        room="g.10.101",
        client_kind="bot",
        takeover=False,
        bot_lineage={"bot_installation_id": 1},
    )
    second = await claim_voice_connection(
        cast(Any, redis),
        "chat.example",
        identity,
        connection_id="b" * 43,
        room="g.20.201",
        client_kind="bot",
        takeover=False,
        bot_lineage={"bot_installation_id": 2},
    )

    first_key = voice_connection_key("chat.example", identity, room="g.10.101", client_kind="bot")
    second_key = voice_connection_key("chat.example", identity, room="g.20.201", client_kind="bot")
    assert first == (True, 1, "", "")
    assert second == (True, 1, "", "")
    assert first_key != second_key
    assert "guild:10@chat.example" in first_key
    assert "guild:20@chat.example" in second_key
    assert set(redis.values) >= {first_key, second_key}
    assert redis.sets[bot_voice_connection_index_key("chat.example", identity)] == {
        first_key,
        second_key,
    }


@pytest.mark.asyncio
async def test_bot_voice_same_guild_takeover_and_disconnect_do_not_touch_other_guild() -> None:
    redis = VoiceRedis()
    identity = "70@apps.example"
    common = (cast(Any, redis), "chat.example", identity)

    assert (
        await claim_voice_connection(
            *common,
            connection_id="a" * 43,
            room="g.10.101",
            client_kind="bot",
            takeover=False,
        )
    )[0]
    rejected = await claim_voice_connection(
        *common,
        connection_id="b" * 43,
        room="g.10.102",
        client_kind="bot",
        takeover=False,
    )
    assert rejected[0] is False
    assert rejected[2:] == ("g.10.101", "bot")
    taken_over = await claim_voice_connection(
        *common,
        connection_id="b" * 43,
        room="g.10.102",
        client_kind="bot",
        takeover=True,
    )
    assert taken_over[0] is True
    assert taken_over[2:] == ("g.10.101", "bot")
    assert (
        await claim_voice_connection(
            *common,
            connection_id="c" * 43,
            room="g.20.201",
            client_kind="bot",
            takeover=False,
        )
    )[0]

    assert await release_voice_connection(
        *common,
        "b" * 43,
        room="g.10.102",
        client_kind="bot",
    )
    assert await voice_connection_matches(
        *common,
        connection_id="c" * 43,
        room="g.20.201",
        generation=1,
        client_kind="bot",
    )
    claims = await bot_guild_voice_connection_claims(*common)
    assert [claim["room"] for claim in claims] == ["g.20.201"]


@pytest.mark.asyncio
async def test_legacy_bot_claim_migrates_only_to_its_exact_guild_scope() -> None:
    redis = VoiceRedis()
    identity = "70@apps.example"
    legacy_key = legacy_voice_connection_key("chat.example", identity)
    redis.values[legacy_key] = json.dumps(
        {
            "connection_id": "a" * 43,
            "room": "g.10.101",
            "generation": 4,
            "client_kind": "bot",
            "claimed_at": 1,
        }
    )

    assert await voice_connection_matches(
        cast(Any, redis),
        "chat.example",
        identity,
        connection_id="a" * 43,
        room="g.10.101",
        generation=4,
        client_kind="bot",
    )
    migrated_key = voice_connection_key(
        "chat.example", identity, room="g.10.101", client_kind="bot"
    )
    assert legacy_key not in redis.values
    assert migrated_key in redis.values
    assert redis.sets[bot_voice_connection_index_key("chat.example", identity)] == {migrated_key}


@pytest.mark.asyncio
async def test_ambiguous_bare_pointer_is_never_consumed_or_deleted_on_collision() -> None:
    redis = VoiceRedis()
    identity = "70@apps.example"
    room = "g.10.101"
    bare_key = f"voice:user-room:{identity}"
    redis.values[bare_key] = room

    assert (
        await voice_user_room(
            cast(Any, redis),
            "remote.example",
            identity,
            guild_id=10,
        )
        is None
    )
    await remove_occupant(cast(Any, redis), "remote.example", room, identity)

    assert redis.values[bare_key] == room
    remove_call = next(call for call in redis.eval_calls if call[0] == REMOVE_OCCUPANT_LUA)
    assert bare_key not in remove_call


@pytest.mark.asyncio
async def test_legacy_room_registry_is_rebuilt_only_from_verified_local_livekit_rooms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = VoiceRedis()
    redis.sets["voice:rooms"] = {"g.10.101", "g.999.999"}
    control = SimpleNamespace(
        list_rooms=AsyncMock(
            return_value=[SimpleNamespace(name="g.10.101"), SimpleNamespace(name="g.20.201")]
        )
    )
    monkeypatch.setattr("app.voice.background.LiveKitControl", lambda _settings: control)

    class Session:
        async def get(self, model: object, key: tuple[int, str]) -> object | None:
            if getattr(model, "__name__", "") == "Guild" and key[0] == 10:
                return SimpleNamespace(id=10, origin_domain="chat.example")
            if getattr(model, "__name__", "") == "Channel" and key[0] == 101:
                return SimpleNamespace(
                    id=101,
                    origin_domain="chat.example",
                    type=2,
                    guild_id=10,
                    guild_domain="chat.example",
                )
            return None

    class SessionContext:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_args: object) -> None:
            return None

    count = await migrate_legacy_voice_room_registry(
        cast(Any, redis),
        cast(Any, lambda: SessionContext()),
        cast(Any, SimpleNamespace(domain="chat.example")),
    )

    assert count == 1
    assert redis.sets["voice:v2:rooms:chat.example"] == {"g.10.101"}
    assert "voice:rooms" not in redis.sets
