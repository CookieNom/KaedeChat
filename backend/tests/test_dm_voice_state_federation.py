from __future__ import annotations

import base64
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request

from app.api.calls import (
    call_voice_token,
    federation_dm_voice_self_state,
    federation_dm_voice_state,
)
from app.api.voice import livekit_webhook
from app.core.settings import Settings
from app.core.types import EntityRef
from app.federation.security import FederationPrincipal
from app.voice.background import replicate_room
from app.voice.schemas import (
    DMVoiceSelfStateFederationRequest,
    DMVoiceStateFederationRequest,
    VoiceTokenRequest,
    VoiceTokenResponse,
)
from app.voice.state import FederatedVoiceSession, Occupant, federation_occupant_state


def settings() -> Settings:
    return Settings(
        domain="alpha.localhost",
        environment="test",
        secret_key=base64.urlsafe_b64encode(bytes(range(32))).decode(),
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
        voice_enabled=True,
        voice_api_key="LKtestkey",
        voice_api_secret="livekit-test-secret-000000000000000000000000000000000000000",
        voice_public_url="wss://alpha.localhost/livekit",
    )


def occupant(**overrides: object) -> Occupant:
    values: dict[str, object] = {
        "identity": "78@alpha.localhost",
        "user_id": "78",
        "user_domain": "alpha.localhost",
        "room": "d.34.56",
        "guild_id": None,
        "channel_id": "34",
        "joined_at": 100,
        "connection_id": "c" * 43,
        "client_kind": "web",
        "can_speak": True,
        "can_stream": True,
        "participant_metadata": {
            "generation": 4,
            "move_session_id": "m" * 43,
        },
    }
    values.update(overrides)
    return Occupant(**values)  # type: ignore[arg-type]


def call_record(*, authority: str = "beta.localhost") -> dict[str, object]:
    return {
        "id": "56",
        "authority_domain": authority,
        "channel_id": "34",
        "channel_domain": "beta.localhost",
        "room": "d.34.56",
        "state": "active",
        "participants": ["78@alpha.localhost", "90@beta.localhost"],
    }


def dm_webhook_event(event_type: str, metadata: dict[str, object]) -> SimpleNamespace:
    event = SimpleNamespace(
        id=f"event-{event_type}",
        event=event_type,
        room=SimpleNamespace(name="d.34.56"),
        participant=SimpleNamespace(
            identity="90@beta.localhost",
            metadata=json.dumps(metadata),
        ),
    )
    event.HasField = lambda field: field in {"room", "participant"}
    return event


def dm_metadata() -> dict[str, object]:
    return {
        "generation": 4,
        "connection_id": "c" * 43,
        "client_kind": "web",
        "user_id": "90",
        "user_domain": "beta.localhost",
        "channel_id": "34",
        "channel_domain": "beta.localhost",
        "call_id": "56",
        "e2ee": False,
        "can_speak": True,
        "can_stream": True,
        "can_use_vad": True,
        "server_mute": False,
        "server_deaf": False,
        "move_session_id": "m" * 43,
    }


@pytest.mark.asyncio
async def test_remote_dm_grant_retains_a_correlated_home_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost")
    channel = SimpleNamespace(id=34, origin_domain="beta.localhost", encryption_mode="plaintext")
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://beta.localhost/livekit",
        room="d.34.56",
        generation=4,
        connection_id="c" * 43,
        expires_at="2026-08-28T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=False,
        channel_id="34",
        channel_domain="beta.localhost",
        move_session_id="m" * 43,
    )
    session = AsyncMock()
    session.get.return_value = channel
    begin = AsyncMock()
    activate = AsyncMock(return_value=True)
    discard_all = AsyncMock()
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=call_record()))
    monkeypatch.setattr("app.api.calls.require_call_policy", AsyncMock())
    monkeypatch.setattr("app.api.calls.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr("app.api.calls.secrets.token_urlsafe", lambda _size: "m" * 43)
    monkeypatch.setattr("app.api.calls.begin_federated_voice_home_session", begin)
    monkeypatch.setattr("app.api.calls.activate_federated_dm_voice_home_session", activate)
    monkeypatch.setattr("app.api.calls.discard_all_federated_voice_home_sessions", discard_all)
    monkeypatch.setattr(
        "app.api.calls.signed_request",
        AsyncMock(return_value=httpx.Response(200, json=grant.model_dump(mode="json"))),
    )

    result = await call_voice_token(
        EntityRef("56@beta.localhost"),
        cast(Any, SimpleNamespace(user=actor)),
        session,
        AsyncMock(),
        settings(),
        VoiceTokenRequest(connection_id="c" * 43),
    )

    assert result == grant
    begin_call = begin.await_args
    activate_call = activate.await_args
    assert begin_call is not None
    assert activate_call is not None
    pending = begin_call.args[2]
    assert pending == FederatedVoiceSession(
        authority_domain="beta.localhost",
        guild_id="",
        room="d.34.56",
        generation=0,
        move_session_id="m" * 43,
        call_id="56",
        channel_id="34",
        connection_id="c" * 43,
    )
    assert activate_call.kwargs == {
        "move_session_id": "m" * 43,
        "authority_domain": "beta.localhost",
        "call_id": "56",
        "channel_id": "34",
        "room": "d.34.56",
        "generation": 4,
        "connection_id": "c" * 43,
        "client_kind": "web",
    }
    discard_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_snapshot_confirms_exact_session_and_projects_only_public_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant()
    active = FederatedVoiceSession(
        authority_domain="beta.localhost",
        guild_id="",
        room=current.room,
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        call_id="56",
        channel_id="34",
        connection_id=current.connection_id,
    )
    replace_state = AsyncMock(return_value=True)
    confirm = AsyncMock(return_value=True)
    publish = AsyncMock()
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=call_record()))
    monkeypatch.setattr(
        "app.api.calls.enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.calls.get_federated_voice_session",
        AsyncMock(return_value=active),
    )
    monkeypatch.setattr("app.api.calls.replace_occupancy", replace_state)
    monkeypatch.setattr("app.api.calls.confirm_federated_voice_home_session", confirm)
    monkeypatch.setattr("app.api.calls.publish_ephemeral", publish)
    generated_at = int(time.time())

    response = await federation_dm_voice_state(
        DMVoiceStateFederationRequest(
            call_id="56",
            channel_id="34",
            room="d.34.56",
            generated_at=generated_at,
            snapshot_version=7,
            participants=[federation_occupant_state(current)],
        ),
        FederationPrincipal(origin="beta.localhost", key_id="key"),
        AsyncMock(),
        settings(),
    )

    assert response.status_code == 204
    replace_call = replace_state.await_args
    confirm_call = confirm.await_args
    publish_call = publish.await_args
    assert replace_call is not None
    assert confirm_call is not None
    assert publish_call is not None
    assert replace_call.kwargs == {
        "generated_at": generated_at,
        "snapshot_version": 7,
    }
    assert confirm_call.kwargs == {
        "authority_domain": "beta.localhost",
        "room": "d.34.56",
        "generation": 4,
        "connection_id": "c" * 43,
    }
    public_participant = publish_call.args[3]["participants"][0]
    assert "connection_id" not in public_participant
    assert "generation" not in public_participant
    assert "participant_metadata" not in public_participant


@pytest.mark.asyncio
async def test_dm_snapshot_rejects_a_stale_connection_before_replacing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = FederatedVoiceSession(
        authority_domain="beta.localhost",
        guild_id="",
        room="d.34.56",
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        call_id="56",
        channel_id="34",
        connection_id="n" * 43,
    )
    replace_state = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=call_record()))
    monkeypatch.setattr("app.api.calls.enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(
        "app.api.calls.get_federated_voice_session",
        AsyncMock(return_value=active),
    )
    monkeypatch.setattr("app.api.calls.replace_occupancy", replace_state)

    with pytest.raises(HTTPException) as failure:
        await federation_dm_voice_state(
            DMVoiceStateFederationRequest(
                call_id="56",
                channel_id="34",
                room="d.34.56",
                generated_at=int(time.time()),
                snapshot_version=7,
                participants=[federation_occupant_state(occupant())],
            ),
            FederationPrincipal(origin="beta.localhost", key_id="key"),
            AsyncMock(),
            settings(),
        )

    assert failure.value.status_code == 409
    replace_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_self_state_is_fenced_and_normalizes_deaf_to_muted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant(
        identity="90@beta.localhost",
        user_id="90",
        user_domain="beta.localhost",
    )
    active = FederatedVoiceSession(
        authority_domain="alpha.localhost",
        guild_id="",
        room=current.room,
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        call_id="56",
        channel_id="34",
        connection_id=current.connection_id,
    )
    updated = replace(
        current,
        self_mute=True,
        self_deaf=True,
        participant_metadata={**current.participant_metadata, "generation": 5},
    )
    apply_state = AsyncMock(return_value=updated)
    monkeypatch.setattr(
        "app.api.calls.get_call",
        AsyncMock(
            return_value={
                **call_record(authority="alpha.localhost"),
                "participants": ["90@beta.localhost"],
            }
        ),
    )
    monkeypatch.setattr("app.api.calls.enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(
        "app.api.calls.get_federated_voice_session",
        AsyncMock(return_value=active),
    )
    monkeypatch.setattr("app.api.calls.occupant_in_room", AsyncMock(return_value=current))
    monkeypatch.setattr(
        "app.api.calls.require_remote_user_creation_allowed",
        AsyncMock(),
    )
    monkeypatch.setattr("app.api.calls.update_authoritative_occupant_self_state", apply_state)
    monkeypatch.setattr(
        "app.api.calls.sync_federated_voice_session_generation",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("app.api.calls.enqueue_best_effort", AsyncMock())

    result = await federation_dm_voice_self_state(
        DMVoiceSelfStateFederationRequest(
            call_id="56",
            channel_id="34",
            actor_id="90",
            room="d.34.56",
            move_session_id="m" * 43,
            generation=4,
            connection_id="c" * 43,
            self_mute=False,
            self_deaf=True,
        ),
        FederationPrincipal(origin="beta.localhost", key_id="key"),
        SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    id=90,
                    origin_domain="beta.localhost",
                    account_type="human",
                )
            )
        ),
        AsyncMock(),
        settings(),
    )

    apply_call = apply_state.await_args
    assert apply_call is not None
    assert apply_call.kwargs["self_mute"] is True
    assert result.state.self_mute is True
    assert result.state.self_deaf is True
    assert result.generation == 5


@pytest.mark.asyncio
async def test_dm_room_replication_sends_private_fences_but_local_public_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant()
    record = call_record(authority="alpha.localhost")
    redis = AsyncMock()
    redis.set.return_value = True
    redis.incr.return_value = 8
    redis.eval.return_value = 1
    session = AsyncMock()

    @asynccontextmanager
    async def open_session() -> Any:
        yield session

    signed = AsyncMock(return_value=SimpleNamespace(status_code=204))
    publish = AsyncMock()
    monkeypatch.setattr("app.voice.background.room_occupants", AsyncMock(return_value=[current]))
    monkeypatch.setattr("app.voice.background.get_call", AsyncMock(return_value=record))
    monkeypatch.setattr("app.voice.background.signed_request", signed)
    monkeypatch.setattr("app.voice.background.publish_ephemeral", publish)

    count = await replicate_room(
        redis,
        cast(Any, open_session),
        settings(),
        "d.34.56",
    )

    assert count == 1
    signed_call = signed.await_args
    publish_call = publish.await_args
    assert signed_call is not None
    assert publish_call is not None
    private_participant = signed_call.kwargs["payload"]["participants"][0]
    assert private_participant["connection_id"] == "c" * 43
    assert private_participant["generation"] == 4
    public_participant = publish_call.args[3]["participants"][0]
    assert "connection_id" not in public_participant
    assert "generation" not in public_participant


@pytest.mark.asyncio
async def test_dm_livekit_join_records_the_remote_authority_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = dm_webhook_event("participant_joined", dm_metadata())
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.return_value = True
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        id=34,
        origin_domain="beta.localhost",
        encryption_mode="plaintext",
        user_limit=0,
        account_type="human",
    )
    set_session = AsyncMock()
    publish = AsyncMock()
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.voice.bounded_request_body", AsyncMock(return_value=b"{}"))
    monkeypatch.setattr("app.api.voice.receive_webhook", lambda *_args: event)
    monkeypatch.setattr("app.api.voice.current_generation", AsyncMock(return_value=4))
    monkeypatch.setattr("app.api.voice.voice_connection_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.api.voice.get_call",
        AsyncMock(
            return_value={
                **call_record(authority="alpha.localhost"),
                "participants": ["90@beta.localhost"],
            }
        ),
    )
    monkeypatch.setattr("app.api.voice.admit_occupant", AsyncMock(return_value=True))
    monkeypatch.setattr("app.api.voice.set_federated_voice_authority_session", set_session)
    monkeypatch.setattr("app.api.voice.publish_ephemeral", publish)
    monkeypatch.setattr("app.api.voice.enqueue_best_effort", enqueue)

    response = await livekit_webhook(
        Request({"type": "http", "method": "POST", "path": "/"}),
        "signed",
        session,
        redis,
        settings(),
    )

    assert response.status_code == 204
    set_session_call = set_session.await_args
    publish_call = publish.await_args
    assert set_session_call is not None
    assert publish_call is not None
    authority_session = set_session_call.args[2]
    assert authority_session == FederatedVoiceSession(
        authority_domain="alpha.localhost",
        guild_id="",
        room="d.34.56",
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        call_id="56",
        channel_id="34",
        connection_id="c" * 43,
    )
    assert "connection_id" not in publish_call.args[3]["state"]
    assert "generation" not in publish_call.args[3]["state"]
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_dm_livekit_leave_uses_connection_generation_and_session_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = dm_webhook_event("participant_left", dm_metadata())
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.return_value = True
    remove = AsyncMock(return_value=True)
    release = AsyncMock(return_value=True)
    discard = AsyncMock(return_value=True)
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.voice.bounded_request_body", AsyncMock(return_value=b"{}"))
    monkeypatch.setattr("app.api.voice.receive_webhook", lambda *_args: event)
    monkeypatch.setattr("app.api.voice.remove_occupant_connection", remove)
    monkeypatch.setattr("app.api.voice.release_voice_connection", release)
    monkeypatch.setattr("app.api.voice.discard_federated_voice_session", discard)
    monkeypatch.setattr("app.api.voice.publish_ephemeral", AsyncMock())
    monkeypatch.setattr("app.api.voice.enqueue_best_effort", enqueue)

    response = await livekit_webhook(
        Request({"type": "http", "method": "POST", "path": "/"}),
        "signed",
        AsyncMock(),
        redis,
        settings(),
    )

    assert response.status_code == 204
    remove_call = remove.await_args
    release_call = release.await_args
    discard_call = discard.await_args
    assert remove_call is not None
    assert release_call is not None
    assert discard_call is not None
    assert remove_call.kwargs == {"generation": 4}
    assert release_call.kwargs == {
        "room": "d.34.56",
        "generation": 4,
        "client_kind": "web",
    }
    assert discard_call.kwargs == {
        "move_session_id": "m" * 43,
        "room": "d.34.56",
        "authority_domain": "alpha.localhost",
        "connection_id": "c" * 43,
        "generation": 4,
    }
    enqueue.assert_awaited_once()
