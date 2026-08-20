from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.gateway as gateway
from app.chat.events import (
    PUBLISH_EPHEMERAL_SCRIPT,
    PUBLISH_PRESENCE_SCRIPT,
    publish_ephemeral,
    publish_presence,
)
from app.db.models import User


class LimiterRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def eval(self, script: str, numkeys: int, *args: str) -> list[int]:
        assert script == gateway.IDENTIFY_LIMIT_SCRIPT
        assert numkeys == 3
        (
            client_key,
            global_key,
            _ready_key,
            _client_rate,
            client_burst,
            _global_rate,
            global_burst,
        ) = args
        if self.counts.get(client_key, 0) >= int(client_burst):
            return [0, 200, max(0, int(global_burst) - self.counts.get(global_key, 0))]
        if self.counts.get(global_key, 0) >= int(global_burst):
            return [0, 10, 0]
        self.counts[client_key] = self.counts.get(client_key, 0) + 1
        self.counts[global_key] = self.counts.get(global_key, 0) + 1
        return [1, 0, int(global_burst) - self.counts[global_key]]


@pytest.mark.asyncio
async def test_ready_history_status_projection_is_user_scoped_and_safe() -> None:
    class StatusRedis:
        def __init__(self) -> None:
            self.removed_fields: tuple[object, ...] = ()

        async def hgetall(self, key: str) -> dict[bytes, bytes]:
            assert key == gateway.history_status_key("alpha.test", 7)
            # The accessible record deliberately follows more than the old
            # projection cap's worth of stale records. READY must still expose
            # its warning and clean the stale projection entries.
            records = {
                f"{100 + index}@left-{index}.test".encode(): (
                    b'{"status":"failed","code":"not accessible"}'
                )
                for index in range(40)
            }
            records[b"42@remote.test"] = json.dumps(
                {
                    "guild_id": "42",
                    "guild_domain": "remote.test",
                    "status": "retrying",
                    "code": "KAED_FED_HISTORY_CAPACITY",
                    "retry_after_ms": 60_000,
                }
            ).encode()
            records[b"broken"] = b"not-json"
            return records

        async def hdel(self, key: str, *fields: object) -> int:
            assert key == gateway.history_status_key("alpha.test", 7)
            self.removed_fields = fields
            return len(fields)

    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )
    redis = StatusRedis()
    statuses = await gateway.guild_history_sync_statuses(
        redis,  # type: ignore[arg-type]
        user,
        [SimpleNamespace(id=42, origin_domain="remote.test")],  # type: ignore[list-item]
    )

    assert statuses == {
        (42, "remote.test"): {
            "history_sync_status": "retrying",
            "history_sync_error_code": "KAED_FED_HISTORY_CAPACITY",
            "history_sync_retry_after_ms": 60_000,
        }
    }
    assert len(redis.removed_fields) == 41
    assert b"broken" in redis.removed_fields
    assert b"42@remote.test" not in redis.removed_fields


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_operation", ["read", "cleanup"])
async def test_ready_history_status_cache_failure_does_not_break_login(
    fail_operation: str,
) -> None:
    class UnavailableStatusRedis:
        async def hgetall(self, _key: str) -> dict[bytes, bytes]:
            if fail_operation == "read":
                raise ConnectionError("cache unavailable")
            return {
                b"42@remote.test": b'{"status":"failed","code":"safe"}',
                b"99@left.test": b'{"status":"failed","code":"stale"}',
            }

        async def hdel(self, _key: str, *_fields: object) -> int:
            if fail_operation == "cleanup":
                raise ConnectionError("cache unavailable")
            return 0

    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    statuses = await gateway.guild_history_sync_statuses(
        UnavailableStatusRedis(),  # type: ignore[arg-type]
        user,
        [SimpleNamespace(id=42, origin_domain="remote.test")],  # type: ignore[list-item]
    )

    if fail_operation == "read":
        assert statuses == {}
    else:
        assert statuses == {
            (42, "remote.test"): {
                "history_sync_status": "failed",
                "history_sync_error_code": "safe",
            }
        }


@pytest.mark.asyncio
async def test_fresh_ready_hydrates_history_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )
    guild = SimpleNamespace(id=42, origin_domain="remote.test")
    expected_statuses = {
        (42, "remote.test"): {
            "history_sync_status": "retrying",
            "history_sync_error_code": "KAED_FED_HISTORY_CAPACITY",
        }
    }
    captured: dict[str, object] = {}

    async def presence(*_args: object) -> str:
        return "idle"

    async def statuses(*_args: object) -> dict[tuple[int, str], dict[str, object]]:
        return expected_statuses

    expected_presences = [{"user_id": "9", "user_domain": "remote.test", "status": "idle"}]

    async def presences(*_args: object) -> list[dict[str, object]]:
        return expected_presences

    def payload(*args: object) -> dict[str, object]:
        captured["args"] = args
        return {"ready": True}

    monkeypatch.setattr(gateway, "current_presence_preference", presence)
    monkeypatch.setattr(gateway, "guild_history_sync_statuses", statuses)
    monkeypatch.setattr(gateway, "dm_presence_snapshot", presences)
    monkeypatch.setattr(gateway, "ready_payload", payload)

    result = await gateway.hydrated_ready_payload(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        user,
        [guild],  # type: ignore[list-item]
        [],
        [],
        "fresh-session",
    )

    assert result == {"ready": True}
    assert captured["args"] == (
        user,
        [guild],
        [],
        [],
        "fresh-session",
        "idle",
        expected_statuses,
        expected_presences,
    )


@pytest.mark.asyncio
async def test_dm_presence_snapshot_is_scoped_deduplicated_and_visible() -> None:
    class PresenceRedis:
        async def mget(self, *keys: str) -> list[bytes | None]:
            assert keys == (
                "presence:alpha.test:7",
                "presence:remote.test:8",
                "presence:remote.test:9",
            )
            return [
                b'{"status":"online"}',
                b'{"status":"invisible"}',
                b'{"status":"idle"}',
            ]

    channels: list[dict[str, object]] = [
        {
            "recipients": [
                {"id": "9", "origin_domain": "remote.test"},
                {"id": "8", "origin_domain": "remote.test"},
            ]
        },
        {
            "recipients": [
                {"id": "9", "origin_domain": "remote.test"},
                {"id": "7", "origin_domain": "alpha.test"},
            ]
        },
    ]

    assert await gateway.dm_presence_snapshot(  # type: ignore[arg-type]
        PresenceRedis(), channels
    ) == [
        {"user_id": "7", "user_domain": "alpha.test", "status": "online"},
        {"user_id": "8", "user_domain": "remote.test", "status": "offline"},
        {"user_id": "9", "user_domain": "remote.test", "status": "idle"},
    ]


@pytest.mark.asyncio
async def test_dm_presence_snapshot_cache_failure_defaults_offline() -> None:
    class UnavailablePresenceRedis:
        async def mget(self, *_keys: str) -> list[bytes | None]:
            raise ConnectionError("cache unavailable")

    assert await gateway.dm_presence_snapshot(  # type: ignore[arg-type]
        UnavailablePresenceRedis(),
        [{"recipients": [{"id": "8", "origin_domain": "remote.test"}]}],
    ) == [{"user_id": "8", "user_domain": "remote.test", "status": "offline"}]


@pytest.mark.asyncio
async def test_discard_gateway_session_releases_reserved_slot() -> None:
    operations: list[tuple[str, tuple[object, ...]]] = []

    class Pipeline:
        async def __aenter__(self) -> Pipeline:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def delete(self, *args: object) -> None:
            operations.append(("delete", args))

        def zrem(self, *args: object) -> None:
            operations.append(("zrem", args))

        async def execute(self) -> None:
            operations.append(("execute", ()))

    class Redis:
        def pipeline(self, *, transaction: bool) -> Pipeline:
            assert transaction
            return Pipeline()

    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    await gateway.discard_gateway_session(  # type: ignore[arg-type]
        Redis(), user, "fresh-session"
    )

    assert operations == [
        (
            "delete",
            (
                "gateway:session:fresh-session",
                "gateway:session:fresh-session:progress",
            ),
        ),
        ("zrem", ("gateway:user-sessions:alpha.test:7", "fresh-session")),
        ("execute", ()),
    ]


class HandshakeWebSocket:
    def __init__(self, app: SimpleNamespace, payload: object | None = None) -> None:
        self.app = app
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.client = SimpleNamespace(host="192.0.2.10")
        self.payload = payload
        self.accepted = False
        self.sent: list[object] = []
        self.close_codes: list[int] = []
        self.denial_statuses: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: object) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> object:
        return self.payload

    async def close(self, code: int, reason: str = "") -> None:
        self.close_codes.append(code)

    async def send_denial_response(self, response: object) -> None:
        self.denial_statuses.append(int(response.status_code))  # type: ignore[attr-defined]


class EphemeralRedis:
    def __init__(self) -> None:
        self.fences: dict[str, int] = {}
        self.generations: dict[str, int] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def eval(self, script: str, numkeys: int, *args: str) -> int:
        if script == PUBLISH_EPHEMERAL_SCRIPT:
            assert numkeys == 1
            channel, encoded = args
        else:
            assert script == PUBLISH_PRESENCE_SCRIPT
            assert numkeys == 3
            generation_key, fence, channel, raw_generation, encoded = args
            generation = int(raw_generation)
            if self.generations.get(generation_key, generation) > generation:
                return 0
            if self.fences.get(fence, 0) > generation:
                return 0
            if self.fences.get(fence) == generation:
                return 1
            self.fences[fence] = generation
        self.published.append((channel, json.loads(encoded)))
        return 1


class PresenceRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.scores: dict[str, int] = {}

    async def eval(self, script: str, numkeys: int, *args: str) -> int:
        if script == gateway.SET_PRESENCE_SCRIPT:
            assert numkeys == 3
            generation_key, state_key, _expiration_key, status, raw_expiry, handle = args
            generation = int(self.values.get(generation_key, "0")) + 1
            self.values[generation_key] = str(generation)
            self.values[state_key] = json.dumps(
                {"status": status, "generation": generation, "expires_at": int(raw_expiry)}
            )
            self.scores[handle] = int(raw_expiry)
            return generation
        if script == gateway.RENEW_PRESENCE_SCRIPT:
            assert numkeys == 3
            generation_key, state_key, _expiration_key, raw_expiry, handle = args
            raw_state = self.values.get(state_key)
            if raw_state is None:
                return 0
            state = json.loads(raw_state)
            generation = int(self.values[generation_key]) + 1
            self.values[generation_key] = str(generation)
            state["generation"] = generation
            state["expires_at"] = int(raw_expiry)
            self.values[state_key] = json.dumps(state)
            self.scores[handle] = int(raw_expiry)
            return generation
        if script == gateway.CLAIM_EXPIRED_PRESENCE_SCRIPT:
            assert numkeys == 3
            state_key, _expiration_key, generation_key, handle, raw_now, raw_lease = args
            now = int(raw_now)
            if self.scores.get(handle, now + 1) > now:
                return 0
            raw_state = self.values.get(state_key)
            if raw_state is None:
                self.scores.pop(handle, None)
                return 0
            state = json.loads(raw_state)
            if int(state["expires_at"]) > now:
                self.scores[handle] = int(state["expires_at"])
                return 0
            generation = int(self.values.get(generation_key, "0")) + 1
            self.values[generation_key] = str(generation)
            state["generation"] = generation
            state["claim_until"] = now + int(raw_lease)
            self.values[state_key] = json.dumps(state)
            self.scores[handle] = int(state["claim_until"])
            return generation
        assert script == gateway.FINALIZE_EXPIRED_PRESENCE_SCRIPT
        assert numkeys == 2
        state_key, _expiration_key, handle, raw_generation, raw_now = args
        raw_state = self.values.get(state_key)
        if raw_state is None:
            self.scores.pop(handle, None)
            return 1
        state = json.loads(raw_state)
        if int(state["generation"]) != int(raw_generation):
            return 0
        if int(state["expires_at"]) > int(raw_now):
            return 0
        self.values.pop(state_key)
        self.scores.pop(handle, None)
        return 1


def test_gateway_progress_snapshot_round_trips_redis_text_and_bytes() -> None:
    cursors = {
        "guild:alpha.test:42": 17,
        "user:alpha.test:7": 9,
    }
    topics = ["guild:alpha.test:42", "user:alpha.test:7"]
    encoded = gateway.encode_gateway_progress(cursors, topics)

    assert gateway.decode_gateway_progress(encoded) == (cursors, topics)
    assert gateway.decode_gateway_progress(encoded.encode()) == (cursors, topics)


@pytest.mark.parametrize(
    "encoded",
    [
        "[]",
        '{"cursors":{},"topics":"guild:alpha.test:42"}',
        '{"cursors":[],"topics":[]}',
        '{"cursors":{},"topics":[42]}',
    ],
)
def test_gateway_progress_snapshot_rejects_invalid_shapes(encoded: str) -> None:
    with pytest.raises(TypeError):
        gateway.decode_gateway_progress(encoded)


@pytest.mark.asyncio
async def test_identify_client_limit_does_not_consume_global_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = LimiterRedis()
    results = [
        await gateway.identify_admitted(redis, "192.0.2.1")  # type: ignore[arg-type]
        for _ in range(gateway.settings.gateway_identify_ip_burst + 1)
    ]

    assert results == [True] * gateway.settings.gateway_identify_ip_burst + [False]
    assert redis.counts["gateway:identify:global"] == gateway.settings.gateway_identify_ip_burst
    for index in range(
        gateway.settings.gateway_identify_burst - gateway.settings.gateway_identify_ip_burst
    ):
        assert await gateway.identify_admitted(  # type: ignore[arg-type]
            redis, f"198.51.100.{index}"
        )
    assert not await gateway.identify_admitted(redis, "203.0.113.1")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_gateway_rejects_excess_preauth_connection_before_accept() -> None:
    admission = gateway.PreAuthAdmission(limit=1)
    blocker = object()
    assert admission.try_acquire(blocker)
    app = SimpleNamespace(state=SimpleNamespace(preauth_admission=admission, connections=set()))
    websocket = HandshakeWebSocket(app)

    await gateway.gateway(websocket)  # type: ignore[arg-type]

    assert not websocket.accepted
    assert websocket.denial_statuses == [429]
    assert not websocket.close_codes
    assert not websocket.sent
    admission.release(blocker)


@pytest.mark.asyncio
async def test_gateway_releases_preauth_capacity_after_invalid_handshake() -> None:
    admission = gateway.PreAuthAdmission(limit=1)
    app = SimpleNamespace(state=SimpleNamespace(preauth_admission=admission, connections=set()))
    websocket = HandshakeWebSocket(app, payload=[])

    await gateway.gateway(websocket)  # type: ignore[arg-type]

    assert websocket.accepted
    assert websocket.close_codes == [gateway.GatewayCloseCode.DECODE_ERROR]
    probe = object()
    assert admission.try_acquire(probe)
    admission.release(probe)


@pytest.mark.asyncio
async def test_gateway_releases_preauth_capacity_after_redis_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = gateway.PreAuthAdmission(limit=1)
    app = SimpleNamespace(
        state=SimpleNamespace(
            preauth_admission=admission,
            connections=set(),
            sessionmaker=object(),
            redis=object(),
        )
    )
    websocket = HandshakeWebSocket(
        app,
        {"op": gateway.GatewayOp.IDENTIFY, "d": {"token": "kc1_at_invalid"}},
    )

    async def admitted(_redis: object, _client: str) -> tuple[bool, int]:
        return True, 0

    async def missing_identity(*_args: object) -> None:
        probe = object()
        assert admission.try_acquire(probe)
        admission.release(probe)
        return None

    monkeypatch.setattr(gateway, "identify_admission", admitted)
    monkeypatch.setattr(gateway, "identify", missing_identity)

    await gateway.gateway(websocket)  # type: ignore[arg-type]

    assert websocket.accepted
    assert websocket.close_codes == [gateway.GatewayCloseCode.AUTHENTICATION_FAILED]


def test_gateway_session_claim_reserves_before_exposing_the_slot() -> None:
    assert gateway.USER_SESSION_LIMIT == 8
    assert "ZREMRANGEBYSCORE" in gateway.CLAIM_USER_SESSION_SCRIPT
    assert "HSET', KEYS[2], 'reserved'" in gateway.CLAIM_USER_SESSION_SCRIPT
    reservation = gateway.CLAIM_USER_SESSION_SCRIPT.index("HSET")
    exposure = gateway.CLAIM_USER_SESSION_SCRIPT.index("ZADD")
    assert reservation < exposure


def test_connection_op_limiter_uses_a_sliding_window() -> None:
    limiter = gateway.ConnectionOpLimiter(limit=2, window=10)

    assert limiter.admit(100)
    assert limiter.admit(101)
    assert not limiter.admit(109.99)
    assert limiter.admit(110)


@pytest.mark.asyncio
async def test_presence_heartbeat_atomically_supersedes_old_expiration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = PresenceRedis()
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )
    monkeypatch.setattr(gateway.time, "time", lambda: 1000.0)
    assert await gateway.set_presence_state(redis, user, "online") == 1  # type: ignore[arg-type]

    monkeypatch.setattr(gateway.time, "time", lambda: 1050.0)
    assert await gateway.renew_presence_state(redis, user) == 2  # type: ignore[arg-type]
    assert (
        await gateway.claim_expired_presence(  # type: ignore[arg-type]
            redis, "alpha.test:7", 1090
        )
        == 0
    )
    assert (
        await gateway.claim_expired_presence(  # type: ignore[arg-type]
            redis, "alpha.test:7", 1140
        )
        == 3
    )
    assert await gateway.finalize_expired_presence(  # type: ignore[arg-type]
        redis, "alpha.test:7", 3, 1140
    )


@pytest.mark.asyncio
async def test_presence_fanout_is_queued_without_signing_in_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[tuple[object, ...]] = []

    async def enqueue(_task: object, *args: object, **_kwargs: object) -> bool:
        queued.append(args)
        return True

    monkeypatch.setattr(gateway, "enqueue_best_effort", enqueue)
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    gateway.schedule_presence_fanout(user, "online", 4)
    await asyncio.gather(*list(gateway.presence_fanout_tasks))

    assert queued == [(7, "alpha.test", "online", 4)]


@pytest.mark.asyncio
async def test_ephemeral_events_never_receive_a_durable_topic_sequence() -> None:
    redis = EphemeralRedis()

    assert await publish_ephemeral(  # type: ignore[arg-type]
        redis, "guild:alpha.test:42", "TYPING_START", {"user_id": "7"}
    )

    channel, event = redis.published[0]
    assert channel == "dispatch:guild:alpha.test:42"
    assert event["ephemeral"] is True
    assert "topic_seq" not in event


@pytest.mark.asyncio
async def test_presence_generation_fence_rejects_delayed_offline_state() -> None:
    redis = EphemeralRedis()
    topic = "guild:alpha.test:42"
    redis.generations["presence:generation:alpha.test:7"] = 2

    assert await publish_presence(  # type: ignore[arg-type]
        redis,
        topic,
        {"user_id": "7", "user_domain": "alpha.test", "status": "online"},
        user_domain="alpha.test",
        user_id=7,
        generation=2,
    )
    assert not await publish_presence(  # type: ignore[arg-type]
        redis,
        topic,
        {"user_id": "7", "user_domain": "alpha.test", "status": "offline"},
        user_domain="alpha.test",
        user_id=7,
        generation=1,
    )

    assert len(redis.published) == 1
    assert redis.published[0][1]["d"] == {
        "user_id": "7",
        "user_domain": "alpha.test",
        "status": "online",
        "generation": 2,
    }


@pytest.mark.asyncio
async def test_ephemeral_delivery_does_not_advance_durable_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Socket:
        sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

    class PubSub:
        async def unsubscribe(self, channel: str) -> None:
            raise AssertionError(f"unexpected unsubscribe: {channel}")

    async def visible(*args: object) -> tuple[bool, bool]:
        del args
        return True, True

    async def no_control(*args: object) -> None:
        del args

    monkeypatch.setattr(gateway, "event_visibility", visible)
    monkeypatch.setattr(gateway, "apply_user_topic_control", no_control)
    socket = Socket()
    cursors = {"guild:alpha.test:42": 9}
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    sequence = await gateway.deliver_ephemeral_topic_event(
        socket,  # type: ignore[arg-type]
        PubSub(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        user,
        gateway.VisibilitySummary({(42, "alpha.test")}, {}),
        "guild:alpha.test:42",
        {
            "t": "PRESENCE_UPDATE",
            "d": {"user_id": "7", "user_domain": "alpha.test", "status": "online"},
            "ephemeral": True,
        },
        ["guild:alpha.test:42"],
        cursors,
        3,
    )

    assert sequence == 4
    assert cursors == {"guild:alpha.test:42": 9}
    assert socket.sent[0]["t"] == "PRESENCE_UPDATE"


@pytest.mark.asyncio
async def test_guild_delivery_fails_closed_after_lost_membership_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed kick must fence later events even if its dispatch was lost."""

    async def membership_was_removed(*args: object) -> None:
        del args
        return None

    monkeypatch.setattr(gateway, "current_acl_fence", membership_was_removed)
    summary = gateway.VisibilitySummary(
        {(42, "alpha.test")},
        {(42, "alpha.test"): {(99, "alpha.test")}},
        {(42, "alpha.test"): (3, 7)},
    )
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    visible, member = await gateway.event_visibility(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        user,
        summary,
        "guild:alpha.test:42",
        {
            "t": "MESSAGE_CREATE",
            "d": {"channel_id": "99", "channel_domain": "alpha.test"},
        },
    )

    assert not visible
    assert not member
    assert (42, "alpha.test") not in summary.guilds
    assert (42, "alpha.test") not in summary.channels
    assert (42, "alpha.test") not in summary.acl_fences


@pytest.mark.asyncio
async def test_live_interaction_delivery_is_hidden_from_human_guild_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "current_acl_fence", AsyncMock(return_value=(3, 7)))

    class Socket:
        sent: list[dict[str, Any]] = []

        async def send_json(self, value: dict[str, Any]) -> None:
            self.sent.append(value)

    class PubSub:
        async def unsubscribe(self, *_topics: str) -> None:
            raise AssertionError("a targeted event must not revoke guild membership")

    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )
    visibility = gateway.VisibilitySummary(
        {(42, "alpha.test")},
        {(42, "alpha.test"): {(99, "alpha.test")}},
        {(42, "alpha.test"): (3, 7)},
    )
    cursors = {"guild:alpha.test:42": 0}
    socket = Socket()
    sequence = await gateway.deliver_topic_event(
        socket,  # type: ignore[arg-type]
        PubSub(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        user,
        visibility,
        "guild:alpha.test:42",
        {
            "t": "INTERACTION_CREATE",
            "topic_seq": 1,
            # Even a list that contains the current user must fail closed when
            # an interaction has more than its one canonical bot recipient.
            "audience_user_refs": ["10@apps.test", "7@alpha.test"],
            "d": {
                "bot_user_ref": "10@apps.test",
                "channel_id": "99",
                "channel_domain": "alpha.test",
                "options": {"secret": "value"},
            },
        },
        ["guild:alpha.test:42"],
        cursors,
        4,
    )

    assert sequence == 4
    assert socket.sent == []
    assert cursors == {"guild:alpha.test:42": 1}


def test_interactions_without_a_valid_explicit_audience_fail_closed() -> None:
    user = User(
        id=10,
        origin_domain="apps.test",
        is_local=False,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
    )
    base = {
        "t": "INTERACTION_CREATE",
        "d": {"bot_user_ref": "10@apps.test", "options": {"secret": "value"}},
    }
    assert not gateway.dispatch_audience_allows(user, base)
    assert not gateway.dispatch_audience_allows(user, {**base, "audience_user_refs": []})
    assert not gateway.dispatch_audience_allows(
        user, {**base, "audience_user_refs": ["11@apps.test"]}
    )
    assert not gateway.dispatch_audience_allows(
        user, {**base, "audience_user_refs": ["10@apps.test", "7@people.test"]}
    )
    assert not gateway.dispatch_audience_allows(
        user,
        {
            **base,
            "d": {**base["d"], "bot_user_ref": "010@apps.test"},
            "audience_user_refs": ["010@apps.test"],
        },
    )
    assert gateway.dispatch_audience_allows(user, {**base, "audience_user_refs": ["10@apps.test"]})


@pytest.mark.asyncio
async def test_attachment_update_is_filtered_by_private_channel_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unchanged_fence(*_args: object) -> tuple[int, int]:
        return 3, 7

    monkeypatch.setattr(gateway, "current_acl_fence", unchanged_fence)
    summary = gateway.VisibilitySummary(
        {(42, "alpha.test")},
        {(42, "alpha.test"): {(99, "alpha.test")}},
        {(42, "alpha.test"): (3, 7)},
    )
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    visible, member = await gateway.event_visibility(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        user,
        summary,
        "guild:alpha.test:42",
        {
            "t": "ATTACHMENT_UPDATE",
            "d": {
                "message_id": "11",
                "message_domain": "alpha.test",
                "channel_id": "100",
                "channel_domain": "alpha.test",
                "attachment": {"id": "13", "scan_status": "rejected"},
            },
        },
    )

    assert not visible
    assert member


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel_fields",
    [
        {},
        {"channel_id": "99"},
        {"channel_domain": "alpha.test"},
        {"channel_id": "not-a-snowflake", "channel_domain": "alpha.test"},
    ],
)
async def test_attachment_update_without_a_canonical_channel_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    channel_fields: dict[str, str],
) -> None:
    async def unchanged_fence(*_args: object) -> tuple[int, int]:
        return 3, 7

    monkeypatch.setattr(gateway, "current_acl_fence", unchanged_fence)
    summary = gateway.VisibilitySummary(
        {(42, "alpha.test")},
        {(42, "alpha.test"): {(99, "alpha.test")}},
        {(42, "alpha.test"): (3, 7)},
    )
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    visible, member = await gateway.event_visibility(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        user,
        summary,
        "guild:alpha.test:42",
        {
            "t": "ATTACHMENT_UPDATE",
            "d": {
                "message_id": "11",
                "message_domain": "alpha.test",
                "attachment": {"id": "13", "scan_status": "rejected"},
                **channel_fields,
            },
        },
    )

    assert not visible
    assert member


@pytest.mark.asyncio
async def test_gateway_subscription_hub_reference_counts_channels() -> None:
    class PubSub:
        subscribed: list[tuple[str, ...]] = []
        unsubscribed: list[tuple[str, ...]] = []

        async def subscribe(self, *channels: str) -> None:
            self.subscribed.append(channels)

        async def unsubscribe(self, *channels: str) -> None:
            self.unsubscribed.append(channels)

        async def aclose(self) -> None:
            return None

    pubsub = PubSub()
    redis = SimpleNamespace(pubsub=lambda: pubsub)
    hub = gateway.GatewaySubscriptionHub(redis)  # type: ignore[arg-type]
    first = await hub.open()
    second = await hub.open()

    await first.subscribe("dispatch:guild:alpha.test:42")
    await second.subscribe("dispatch:guild:alpha.test:42")
    assert pubsub.subscribed == [("dispatch:guild:alpha.test:42",)]

    await first.aclose()
    assert pubsub.unsubscribed == []
    await second.aclose()
    assert pubsub.unsubscribed == [("dispatch:guild:alpha.test:42",)]
    await hub.aclose()


@pytest.mark.asyncio
async def test_gateway_subscription_overflow_is_explicit() -> None:
    hub = SimpleNamespace(close_subscription=lambda subscription: None)
    subscription = gateway.GatewaySubscription(hub, 1)  # type: ignore[arg-type]
    subscription.deliver({"type": "message", "data": "first"})
    subscription.deliver({"type": "message", "data": "second"})

    assert await subscription.get_message(timeout=0.1) == {"type": "overflow"}
