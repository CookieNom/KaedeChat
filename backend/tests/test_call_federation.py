import base64
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api.calls import (
    act_on_call,
    exact_call_projection,
    federation_call_signal,
    federation_call_state,
)
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Channel, DMConversation, User
from app.federation.security import FederationPrincipal
from app.tasks import voice_call_room_gc
from app.voice.cleanup import cleanup_orphaned_dm_rooms
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.schemas import (
    CallAction,
    CallFederationRequest,
    CallResponse,
    CallStateFederationRequest,
)
from app.voice.state import CALL_TRANSITION_LUA, transition_call

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def allow_call_policy_for_transport_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport-state tests isolate their subject from SQL privacy policy."""
    monkeypatch.setattr("app.api.calls.require_call_policy", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.calls.clear_terminal_call_voice_projection",
        AsyncMock(),
    )


def settings(domain: str = "alpha.localhost") -> Settings:
    return Settings(
        domain=domain,
        environment="test",
        secret_key=VALID_KEY,
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
        voice_enabled=True,
        voice_api_key="LKtestkey",
        voice_api_secret="livekit-test-secret-000000000000000000000000000000000000000",
        voice_public_url=f"wss://{domain}/livekit",
    )


def call_record(
    *,
    authority: str = "alpha.localhost",
    state: str = "active",
    created_at: int = 10,
    ended_at: int | None = None,
) -> dict[str, Any]:
    return {
        "id": "56",
        "channel_id": "34",
        "channel_domain": "alpha.localhost",
        "authority_domain": authority,
        "room": "d.34.56",
        "state": state,
        "created_at": created_at,
        "ended_at": ended_at,
        "caller": "1@alpha.localhost",
        "participants": ["1@alpha.localhost", "2@beta.localhost"],
    }


def test_peer_call_acknowledgement_is_an_exact_projection() -> None:
    record = call_record()

    assert exact_call_projection(record, record) == record
    with pytest.raises(ValueError):
        exact_call_projection(record, record | {"authority_secret": "leak"})
    with pytest.raises(ValueError):
        exact_call_projection(record, record | {"state": "ringing"})


def user(user_id: int, domain: str) -> User:
    local = domain == "alpha.localhost"
    return User(
        id=user_id,
        origin_domain=domain,
        is_local=local,
        username=f"user{user_id}",
        password_hash="hash" if local else None,
        email=f"user{user_id}@alpha.localhost" if local else None,
    )


def dm_channel() -> Channel:
    return Channel(
        id=34,
        origin_domain="alpha.localhost",
        type=1,
        name=None,
        created_floor_id=34,
    )


@pytest.mark.asyncio
async def test_remote_group_call_is_committed_and_fanned_out_by_group_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = dm_channel()
    conversation = DMConversation(
        id=channel.id,
        origin_domain=channel.origin_domain,
        pair_key="group:alpha.localhost:34",
        type="group",
        authority_domain="alpha.localhost",
        owner_id=1,
        owner_domain="alpha.localhost",
        state_version=7,
    )
    owner = user(1, "alpha.localhost")
    actor = user(2, "beta.localhost")
    third = user(3, "gamma.localhost")

    async def get_model(model: object, _key: object) -> object | None:
        if model is Channel:
            return channel
        if model is DMConversation:
            return conversation
        if model is User:
            return actor
        return None

    monkeypatch.setattr("app.api.calls.time.time", lambda: 10)
    monkeypatch.setattr("app.api.calls.enforce_federation_route_rate_limit", AsyncMock())
    remote_user_allowed = AsyncMock()
    monkeypatch.setattr(
        "app.api.calls.require_remote_user_creation_allowed",
        remote_user_allowed,
    )
    monkeypatch.setattr(
        "app.api.calls.local_dm_participants",
        AsyncMock(return_value=[owner, actor, third]),
    )
    monkeypatch.setattr("app.api.calls.create_call", AsyncMock(return_value=True))
    monkeypatch.setattr("app.api.calls.notify_call", AsyncMock())
    propagate = AsyncMock()
    monkeypatch.setattr("app.api.calls.propagate_call_create", propagate)

    response = await federation_call_signal(
        CallFederationRequest(
            call_id="56",
            channel_id="34",
            channel_domain="alpha.localhost",
            authority_domain="alpha.localhost",
            actor_id="2",
            actor_domain="beta.localhost",
            action="create",
            created_at=10,
            state_version="7",
        ),
        FederationPrincipal(origin="beta.localhost", key_id="beta-key"),
        cast(Any, SimpleNamespace(get=get_model)),
        cast(Any, object()),
        settings(),
    )

    assert response.authority_domain == "alpha.localhost"
    assert remote_user_allowed.await_args.args[1] is actor
    assert response.caller == "2@beta.localhost"
    assert set(response.participants) == {
        "1@alpha.localhost",
        "2@beta.localhost",
        "3@gamma.localhost",
    }
    propagate.assert_awaited_once()
    assert propagate.await_args.kwargs["exclude_domains"] == {"beta.localhost"}


def test_orphaned_call_room_cleanup_is_scheduled() -> None:
    assert voice_call_room_gc.labels["schedule"] == [{"cron": "*/5 * * * *"}]


def test_already_accepted_caller_cannot_activate_ringing_call() -> None:
    assert "if redis.call('SISMEMBER', KEYS[3], actor) == 1 then" in CALL_TRANSITION_LUA
    assert "if call['state'] == 'active' then return {2, raw} end" in CALL_TRANSITION_LUA
    assert "return {0, 'accepted'}" in CALL_TRANSITION_LUA


@pytest.mark.asyncio
async def test_caller_end_propagates_terminal_state_and_survives_livekit_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = call_record()
    terminal = call_record(state="ended", ended_at=200)
    sent: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_signed_request(
        _session: object,
        _settings: Settings,
        _method: str,
        destination: str,
        path: str,
        *,
        payload: dict[str, Any],
        **_kwargs: object,
    ) -> httpx.Response:
        sent.append((destination, path, payload))
        return httpx.Response(200, json=terminal)

    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=current))
    monkeypatch.setattr(
        "app.api.calls.transition_call", AsyncMock(return_value=(True, True, terminal))
    )
    dispatch = AsyncMock(side_effect=RuntimeError("dispatch unavailable"))
    monkeypatch.setattr("app.api.calls.publish_dispatch", dispatch)
    monkeypatch.setattr("app.api.calls.signed_request", fake_signed_request)
    delete_room = AsyncMock(side_effect=LiveKitError("unavailable"))
    monkeypatch.setattr(LiveKitControl, "delete_room", delete_room)

    response = await act_on_call(
        EntityRef("56"),
        CallAction(action="end"),
        auth=cast(Any, SimpleNamespace(user=user(1, "alpha.localhost"))),
        session=cast(Any, object()),
        redis=cast(Any, object()),
        settings=settings(),
    )

    assert response.state == "ended"
    dispatch.assert_awaited_once()
    delete_room.assert_awaited_once_with("d.34.56")
    assert sent == [
        (
            "beta.localhost",
            "/_kaede/v1/calls/state",
            {"call": terminal},
        )
    ]


@pytest.mark.asyncio
async def test_long_lived_remote_action_uses_stored_call_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = call_record(created_at=1)
    terminal = call_record(state="ended", created_at=1, ended_at=2_000_000_000)
    channel = dm_channel()
    actor = user(2, "beta.localhost")

    async def get_model(model: object, _key: object) -> object:
        return channel if model is Channel else actor

    monkeypatch.setattr("app.api.calls.enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=current))
    monkeypatch.setattr(
        "app.api.calls.local_dm_participants",
        AsyncMock(return_value=[user(1, "alpha.localhost"), actor]),
    )
    transition = AsyncMock(return_value=(True, True, terminal))
    monkeypatch.setattr("app.api.calls.transition_call", transition)
    monkeypatch.setattr("app.api.calls.notify_call", AsyncMock())
    monkeypatch.setattr("app.api.calls.delete_terminal_call_room", AsyncMock())
    monkeypatch.setattr("app.api.calls.propagate_terminal_call", AsyncMock())
    request = CallFederationRequest(
        call_id="56",
        channel_id="34",
        channel_domain="alpha.localhost",
        authority_domain="alpha.localhost",
        actor_id="2",
        actor_domain="beta.localhost",
        action="end",
        created_at=1,
    )

    response = await federation_call_signal(
        request,
        FederationPrincipal(origin="beta.localhost", key_id="beta-key"),
        cast(Any, SimpleNamespace(get=get_model)),
        cast(Any, object()),
        settings(),
    )

    assert response.ended_at == 2_000_000_000
    transition.assert_awaited_once()

    mismatched = request.model_copy(update={"channel_id": "35"})
    with pytest.raises(HTTPException) as error:
        await federation_call_signal(
            mismatched,
            FederationPrincipal(origin="beta.localhost", key_id="beta-key"),
            cast(Any, SimpleNamespace(get=get_model)),
            cast(Any, object()),
            settings(),
        )
    assert error.value.status_code == 409
    assert error.value.detail == {"code": "CALL_CONTEXT_MISMATCH"}


@pytest.mark.asyncio
async def test_lost_terminal_response_retry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = call_record(created_at=1)
    terminal = call_record(state="ended", created_at=1, ended_at=20)
    channel = dm_channel()
    actor = user(2, "beta.localhost")

    async def get_model(model: object, _key: object) -> object:
        return channel if model is Channel else actor

    monkeypatch.setattr("app.api.calls.enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=current))
    monkeypatch.setattr(
        "app.api.calls.local_dm_participants",
        AsyncMock(return_value=[user(1, "alpha.localhost"), actor]),
    )
    transition = AsyncMock(side_effect=[(True, True, terminal), (True, False, terminal)])
    monkeypatch.setattr("app.api.calls.transition_call", transition)
    notify = AsyncMock(side_effect=RuntimeError("dispatch unavailable"))
    cleanup = AsyncMock()
    propagate = AsyncMock()
    monkeypatch.setattr("app.api.calls.notify_call", notify)
    monkeypatch.setattr("app.api.calls.delete_terminal_call_room", cleanup)
    monkeypatch.setattr("app.api.calls.propagate_terminal_call", propagate)
    request = CallFederationRequest(
        call_id="56",
        channel_id="34",
        channel_domain="alpha.localhost",
        authority_domain="alpha.localhost",
        actor_id="2",
        actor_domain="beta.localhost",
        action="end",
        created_at=1,
    )
    arguments = (
        request,
        FederationPrincipal(origin="beta.localhost", key_id="beta-key"),
        cast(Any, SimpleNamespace(get=get_model)),
        cast(Any, object()),
        settings(),
    )

    first = await federation_call_signal(*arguments)
    replay = await federation_call_signal(*arguments)

    assert first == replay
    assert transition.await_count == 2
    assert notify.await_count == 2
    assert cleanup.await_count == 2
    assert propagate.await_count == 2


@pytest.mark.asyncio
async def test_replica_applies_exact_authority_response(monkeypatch: pytest.MonkeyPatch) -> None:
    current = call_record(authority="alpha.localhost")
    terminal = call_record(authority="alpha.localhost", state="ended", ended_at=999)

    async def fake_signed_request(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=terminal)

    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=current))
    monkeypatch.setattr("app.api.calls.signed_request", fake_signed_request)
    apply_state = AsyncMock(return_value=(True, True, terminal))
    monkeypatch.setattr("app.api.calls.apply_authoritative_call", apply_state)
    monkeypatch.setattr(
        "app.api.calls.transition_call",
        AsyncMock(side_effect=AssertionError("replica independently transitioned call")),
    )
    monkeypatch.setattr("app.api.calls.notify_call", AsyncMock())
    monkeypatch.setattr("app.api.calls.delete_terminal_call_room", AsyncMock())
    monkeypatch.setattr("app.api.calls.propagate_terminal_call", AsyncMock())

    response = await act_on_call(
        EntityRef("56@alpha.localhost"),
        CallAction(action="end"),
        auth=cast(Any, SimpleNamespace(user=user(2, "beta.localhost"))),
        session=cast(Any, object()),
        redis=cast(Any, object()),
        settings=settings("beta.localhost"),
    )

    assert response.ended_at == 999
    assert apply_state.await_args.args[1] == terminal


@pytest.mark.asyncio
async def test_authenticated_terminal_push_is_replay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = call_record()
    terminal = call_record(state="ended", ended_at=999)
    payload = CallStateFederationRequest(call=CallResponse.model_validate(terminal))

    monkeypatch.setattr("app.api.calls.enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=current))
    apply_state = AsyncMock(side_effect=[(True, True, terminal), (True, False, terminal)])
    monkeypatch.setattr("app.api.calls.apply_authoritative_call", apply_state)
    notify = AsyncMock()
    monkeypatch.setattr("app.api.calls.notify_call", notify)
    arguments = (
        payload,
        FederationPrincipal(origin="alpha.localhost", key_id="alpha-key"),
        cast(Any, object()),
        settings("beta.localhost"),
    )

    first = await federation_call_state(*arguments)
    replay = await federation_call_state(*arguments)

    assert first == replay
    assert apply_state.await_count == 2
    assert notify.await_count == 2


@pytest.mark.asyncio
async def test_transition_status_two_is_a_successful_unchanged_replay() -> None:
    terminal = call_record(state="ended", ended_at=20)

    class FakeRedis:
        async def get(self, _key: str) -> str:
            return json.dumps(terminal)

        async def eval(self, *_args: object) -> list[object]:
            return [2, json.dumps(terminal)]

    accepted, changed, result = await transition_call(
        cast(Any, FakeRedis()),
        "alpha.localhost",
        56,
        "2@beta.localhost",
        "end",
        settings(),
    )

    assert accepted
    assert not changed
    assert result == terminal


@pytest.mark.asyncio
async def test_orphaned_dm_room_cleanup_preserves_active_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rooms = [SimpleNamespace(name="d.34.56"), SimpleNamespace(name="d.34.57")]
    monkeypatch.setattr(LiveKitControl, "list_rooms", AsyncMock(return_value=rooms))
    delete_room = AsyncMock()
    monkeypatch.setattr(LiveKitControl, "delete_room", delete_room)

    async def fake_get_call(_redis: object, _authority: str, call_id: int) -> object:
        return call_record() if call_id == 56 else None

    monkeypatch.setattr("app.voice.cleanup.get_call", fake_get_call)
    removed = await cleanup_orphaned_dm_rooms(cast(Any, object()), settings(), limit=1)

    assert removed == 1
    delete_room.assert_awaited_once_with("d.34.57")
