"""Execute production Lua against TEST_REDIS_URL; each case owns a unique namespace."""

from __future__ import annotations

import asyncio
import json

import pytest

import app.gateway as gateway
from app.auth.tokens import LoginLimiter
from app.chat.events import publish_presence
from app.core.settings import get_settings
from app.db.models import User
from app.voice.state import call_key, create_call, get_call, transition_call


@pytest.mark.asyncio
async def test_login_failure_lua_locks_at_threshold_and_sets_expiry(redis_scope) -> None:
    redis, domain = redis_scope
    limiter = LoginLimiter(redis)
    account, ip = domain, f"ip-{domain}"
    assert await limiter.admit(account, ip)
    for _ in range(4):
        await limiter.failure(account, ip)
        assert not await limiter.is_locked(account, ip)
    await limiter.failure(account, ip)
    assert await limiter.is_locked(account, ip)
    assert 0 < await redis.pttl(f"auth:lock:account:{account}") <= 1000
    assert 0 < await redis.ttl(f"auth:fail:account:{account}") <= 3600
    assert not await limiter.is_locked(f"other-{domain}", ip)
    await limiter.failure(account, ip)
    assert 1000 < await redis.pttl(f"auth:lock:account:{account}") <= 2000
    await limiter.success(account)
    assert not await limiter.is_locked(account, ip)
    assert await redis.get(f"auth:fail:account:{account}") is None
    assert await redis.get(f"auth:fail:ip:{ip}") == "6"


@pytest.mark.asyncio
async def test_session_claim_reserves_each_visible_slot_and_rejects_overflow(redis_scope) -> None:
    redis, domain = redis_scope
    user = User(id=7, origin_domain=domain, is_local=True, username="maple")
    sessions = [f"{domain}-{index}" for index in range(gateway.USER_SESSION_LIMIT + 1)]
    results = await asyncio.gather(
        *(gateway.claim_user_gateway_session(redis, user, session) for session in sessions)
    )
    assert sum(results) == gateway.USER_SESSION_LIMIT
    visible = await redis.zrange(f"gateway:user-sessions:{domain}:7", 0, -1)
    assert set(visible) == {
        session for session, accepted in zip(sessions, results, strict=True) if accepted
    }
    for session in visible:
        assert await redis.hget(gateway.session_key(session), "reserved") == "1"
        assert await redis.ttl(gateway.session_key(session)) > 0
    rejected = sessions[results.index(False)]
    assert not await redis.exists(gateway.session_key(rejected))
    await gateway.discard_gateway_session(redis, user, visible[0])
    assert not await redis.exists(gateway.session_key(visible[0]))
    assert await gateway.claim_user_gateway_session(redis, user, rejected)


@pytest.mark.asyncio
async def test_presence_renewal_fences_old_expiry_and_preserves_empty_arrays(
    redis_scope, monkeypatch
) -> None:
    redis, domain = redis_scope
    user = User(id=7, origin_domain=domain, is_local=True, username="maple")
    handle = f"{domain}:7"
    monkeypatch.setattr(gateway.time, "time", lambda: 1000.0)
    assert await gateway.set_presence_state(redis, user, "online", activities=[]) == 1
    monkeypatch.setattr(gateway.time, "time", lambda: 1050.0)
    assert await gateway.renew_presence_state(redis, user) == 2
    renewed = gateway.decode_presence_state(await redis.get(f"presence:{handle}"))
    assert renewed is not None and renewed[1:3] == (2, [])
    assert await gateway.claim_expired_presence(redis, handle, 1090) == 0
    assert await gateway.claim_expired_presence(redis, handle, 1140) == 3
    claimed = gateway.decode_presence_state(await redis.get(f"presence:{handle}"))
    assert claimed is not None and claimed[1:3] == (3, [])
    assert await gateway.finalize_expired_presence(redis, handle, 3, 1140)


@pytest.mark.asyncio
async def test_presence_publication_rejects_delayed_offline_generation(redis_scope) -> None:
    redis, domain = redis_scope
    topic = f"guild:{domain}:42"
    await redis.set(f"presence:generation:{domain}:7", "2")
    async with redis.pubsub() as subscription:
        await subscription.subscribe(f"dispatch:{topic}")
        # Consume subscription acknowledgement before publishing.
        acknowledgement = await subscription.get_message(timeout=2)
        assert acknowledgement is not None and acknowledgement["type"] == "subscribe"
        data = {"user_id": "7", "user_domain": domain, "status": "online"}
        assert await publish_presence(
            redis, topic, data, user_domain=domain, user_id=7, generation=2
        )
        assert not await publish_presence(
            redis, topic, {**data, "status": "offline"}, user_domain=domain, user_id=7, generation=1
        )
        event = await subscription.get_message(ignore_subscribe_messages=True, timeout=2)
        assert event is not None
        assert json.loads(event["data"])["d"] == {**data, "generation": 2}
        assert await redis.get(f"presence:published:{topic}:{domain}:7") == "2"


@pytest.mark.asyncio
async def test_call_lua_rejects_caller_reaccept_and_completes_declines_atomically(
    redis_scope,
) -> None:
    redis, domain = redis_scope
    settings = get_settings()
    caller, first, second = (f"{user}@{domain}" for user in (7, 8, 9))
    record = {
        "id": "10",
        "channel_id": "20",
        "channel_domain": domain,
        "authority_domain": domain,
        "room": "d.20.10",
        "state": "ringing",
        "created_at": 1000,
        "ended_at": None,
        "caller": caller,
        "participants": [caller, first, second],
    }
    assert await create_call(redis, record, {caller, first, second}, settings, accepted={caller})
    assert await transition_call(redis, domain, 10, caller, "accept", settings, now=1001) == (
        False,
        False,
        "accepted",
    )
    assert (await get_call(redis, domain, 10))["state"] == "ringing"
    results = await asyncio.gather(
        *(
            transition_call(redis, domain, 10, participant, "decline", settings, now=1002)
            for participant in (first, second)
        )
    )
    assert all(accepted and changed for accepted, changed, _ in results)
    terminal = await get_call(redis, domain, 10)
    assert terminal["state"] == "ended" and terminal["ended_at"] == 1002
    assert await redis.scard(f"{call_key(domain, 10)}:declined") == 2
    replay = await transition_call(redis, domain, 10, second, "decline", settings, now=1003)
    assert replay == (True, False, terminal)
    assert 0 < await redis.ttl(call_key(domain, 10)) <= settings.voice_call_ttl_seconds


@pytest.mark.asyncio
async def test_dispatch_lua_preserves_empty_arrays_in_live_and_retained_events(redis_scope) -> None:
    from app.chat.events import publish_dispatch

    redis, domain = redis_scope
    topic = f"guild:{domain}:42"
    data = {
        "id": "9007199254740993",
        "attachments": [],
        "mention_user_refs": [],
        "nested": {"items": []},
    }
    async with redis.pubsub() as subscription:
        await subscription.subscribe(f"dispatch:{topic}")
        assert (await subscription.get_message(timeout=2))["type"] == "subscribe"
        for sequence in (1, 2):
            published = await publish_dispatch(redis, topic, "MESSAGE_CREATE", data)
            assert published["d"] == data
            assert published["topic_seq"] == sequence
            live = await subscription.get_message(ignore_subscribe_messages=True, timeout=2)
            assert json.loads(live["data"]) == published
            retained = await redis.xrevrange(f"dispatch:stream:{topic}", count=1)
            assert json.loads(retained[0][1]["event"]) == published


@pytest.mark.asyncio
async def test_remote_presence_lua_preserves_payload_and_rejects_older_generation(
    redis_scope,
) -> None:
    from app.chat.presence import encode_presence_state
    from app.federation.presence import SET_REMOTE_PRESENCE_SCRIPT

    redis, domain = redis_scope
    handle = f"{domain}:7"
    keys = (f"presence:generation:{handle}", f"presence:{handle}", "presence:expirations")
    for generation, status, activities in (
        (2, "online", [{"name": "Game", "type": 0}]),
        (3, "idle", []),
    ):
        encoded = encode_presence_state(
            status, activities, None, False, generation=generation, expires_at=1140
        )
        assert (
            await redis.eval(
                SET_REMOTE_PRESENCE_SCRIPT, 3, *keys, generation, encoded, 1140, handle
            )
            == 1
        )
        assert await redis.get(keys[1]) == encoded
        assert json.loads(await redis.get(keys[1]))["activities"] == activities
    assert await redis.eval(SET_REMOTE_PRESENCE_SCRIPT, 3, *keys, 1, "{}", 1000, handle) == 0
    assert await redis.get(keys[0]) == "3"
    assert await redis.get(keys[1]) == encoded
    assert await redis.zscore(keys[2], handle) == 1140
