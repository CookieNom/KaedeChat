from __future__ import annotations

import base64
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.voice import federation_voice_self_state
from app.core.settings import Settings
from app.federation.security import FederationPrincipal
from app.voice.broker import request_remote_guild_voice_self_state
from app.voice.schemas import VoiceSelfStateFederationRequest
from app.voice.service import update_authoritative_occupant_self_state
from app.voice.state import FederatedVoiceSession, Occupant, public_occupant_state


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
        "identity": "78@beta.localhost",
        "user_id": "78",
        "user_domain": "beta.localhost",
        "room": "g.12.34",
        "guild_id": "12",
        "channel_id": "34",
        "joined_at": 100,
        "connection_id": "c" * 43,
        "client_kind": "web",
        "can_speak": True,
        "can_stream": True,
        "allow_listen": True,
        "participant_metadata": {
            "generation": 4,
            "connection_id": "c" * 43,
            "client_kind": "web",
            "user_id": "78",
            "user_domain": "beta.localhost",
            "channel_id": "34",
            "channel_domain": "alpha.localhost",
            "e2ee": False,
            "can_speak": True,
            "can_stream": True,
            "can_use_vad": True,
            "server_mute": False,
            "server_deaf": False,
            "self_mute": False,
            "self_deaf": False,
        },
    }
    values.update(overrides)
    return Occupant(**values)  # type: ignore[arg-type]


def test_self_state_federation_schema_is_exact_and_guild_scoped() -> None:
    valid = {
        "guild_id": "12",
        "actor_id": "78",
        "room": "g.12.34",
        "move_session_id": "m" * 43,
        "generation": 4,
        "connection_id": "c" * 43,
        "self_mute": True,
        "self_deaf": False,
    }
    assert VoiceSelfStateFederationRequest.model_validate(valid).self_mute is True
    with pytest.raises(ValidationError):
        VoiceSelfStateFederationRequest.model_validate({**valid, "room": "d.12.34"})
    with pytest.raises(ValidationError):
        VoiceSelfStateFederationRequest.model_validate({**valid, "admin": True})


@pytest.mark.asyncio
async def test_authority_self_state_is_locked_persisted_and_self_deaf_stops_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant()
    control = AsyncMock()
    monkeypatch.setattr(
        "app.voice.service.claim_voice_grant_transition",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("app.voice.service.current_generation", AsyncMock(return_value=4))
    connection_matches = AsyncMock(side_effect=[True, True])
    monkeypatch.setattr("app.voice.service.voice_connection_matches", connection_matches)
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    rotate = AsyncMock(return_value=5)
    release = AsyncMock()
    monkeypatch.setattr("app.voice.service.rotate_occupant_grant", rotate)
    monkeypatch.setattr("app.voice.service.release_voice_grant_transition", release)

    updated = await update_authoritative_occupant_self_state(
        AsyncMock(),
        settings(),
        current,
        self_mute=False,
        self_deaf=True,
    )

    assert updated.self_mute is True
    assert updated.self_deaf is True
    assert updated.participant_metadata["self_mute"] is True
    assert updated.participant_metadata["self_deaf"] is True
    assert updated.participant_metadata["generation"] == 5
    assert control.update_participant.await_args.kwargs["can_subscribe"] is False
    rotate.assert_awaited_once()
    assert connection_matches.await_count == 1
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_authority_self_state_rejects_a_stale_connection_before_livekit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = AsyncMock()
    monkeypatch.setattr(
        "app.voice.service.claim_voice_grant_transition",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("app.voice.service.current_generation", AsyncMock(return_value=4))
    monkeypatch.setattr(
        "app.voice.service.voice_connection_matches",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.release_voice_grant_transition",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as failure:
        await update_authoritative_occupant_self_state(
            AsyncMock(),
            settings(),
            occupant(),
            self_mute=True,
            self_deaf=False,
        )

    assert failure.value.status_code == 409
    assert cast(Any, failure.value.detail) == {"code": "VOICE_SESSION_STALE"}
    control.update_participant.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotation_fails_closed_when_the_same_federated_move_fence_cannot_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant(
        participant_metadata={
            **occupant().participant_metadata,
            "move_session_id": "m" * 43,
        }
    )
    active = FederatedVoiceSession(
        authority_domain="alpha.localhost",
        guild_id="12",
        room=current.room,
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        connection_id=current.connection_id,
    )
    control = SimpleNamespace(update_participant=AsyncMock(), remove_participant=AsyncMock())
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.claim_voice_grant_transition",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("app.voice.service.current_generation", AsyncMock(return_value=4))
    monkeypatch.setattr("app.voice.service.voice_connection_matches", AsyncMock(return_value=True))
    monkeypatch.setattr("app.voice.service.rotate_occupant_grant", AsyncMock(return_value=5))
    monkeypatch.setattr(
        "app.voice.service.get_federated_voice_session",
        AsyncMock(side_effect=[active, active]),
    )
    monkeypatch.setattr(
        "app.voice.service.sync_federated_voice_session_generation",
        AsyncMock(return_value=False),
    )
    bump = AsyncMock(return_value=6)
    remove = AsyncMock(return_value=True)
    release_connection = AsyncMock(return_value=True)
    discard = AsyncMock(return_value=True)
    monkeypatch.setattr("app.voice.service.bump_generation", bump)
    monkeypatch.setattr("app.voice.service.remove_occupant_connection", remove)
    monkeypatch.setattr("app.voice.service.release_voice_connection", release_connection)
    monkeypatch.setattr("app.voice.service.discard_federated_voice_session", discard)
    monkeypatch.setattr("app.voice.service.release_voice_grant_transition", AsyncMock())

    with pytest.raises(HTTPException) as failure:
        await update_authoritative_occupant_self_state(
            AsyncMock(),
            settings(),
            current,
            self_mute=True,
            self_deaf=False,
        )

    assert failure.value.status_code == 409
    bump.assert_awaited_once()
    control.remove_participant.assert_awaited_once_with(current.room, current.identity)
    remove_call = remove.await_args
    release_call = release_connection.await_args
    assert remove_call is not None
    assert release_call is not None
    assert remove_call.kwargs["generation"] == 5
    assert release_call.kwargs["generation"] == 5
    discard.assert_awaited_once()


@pytest.mark.asyncio
async def test_rotation_does_not_disconnect_a_concurrently_moved_federated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant(
        participant_metadata={
            **occupant().participant_metadata,
            "move_session_id": "m" * 43,
        }
    )
    active = FederatedVoiceSession(
        authority_domain="alpha.localhost",
        guild_id="12",
        room=current.room,
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        connection_id=current.connection_id,
    )
    moved = replace(active, room="g.12.35", generation=5, active=False)
    control = SimpleNamespace(update_participant=AsyncMock(), remove_participant=AsyncMock())
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.claim_voice_grant_transition",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("app.voice.service.current_generation", AsyncMock(return_value=4))
    monkeypatch.setattr("app.voice.service.voice_connection_matches", AsyncMock(return_value=True))
    monkeypatch.setattr("app.voice.service.rotate_occupant_grant", AsyncMock(return_value=5))
    monkeypatch.setattr(
        "app.voice.service.get_federated_voice_session",
        AsyncMock(side_effect=[active, moved]),
    )
    monkeypatch.setattr(
        "app.voice.service.sync_federated_voice_session_generation",
        AsyncMock(return_value=False),
    )
    remove = AsyncMock()
    monkeypatch.setattr("app.voice.service.remove_occupant_connection", remove)
    monkeypatch.setattr("app.voice.service.release_voice_connection", AsyncMock())
    monkeypatch.setattr("app.voice.service.release_voice_grant_transition", AsyncMock())

    updated = await update_authoritative_occupant_self_state(
        AsyncMock(),
        settings(),
        current,
        self_mute=True,
        self_deaf=False,
    )

    assert updated.participant_metadata["generation"] == 5
    control.remove_participant.assert_not_awaited()
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_self_state_is_bound_to_active_session_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = cast(
        Any,
        SimpleNamespace(
            id=78,
            origin_domain="alpha.localhost",
            account_type="human",
        ),
    )
    active = FederatedVoiceSession(
        authority_domain="beta.localhost",
        guild_id="12",
        room="g.12.34",
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        connection_id="c" * 43,
    )
    projected = occupant(
        identity="78@alpha.localhost",
        user_domain="alpha.localhost",
        self_mute=True,
    )
    signed = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"state": public_occupant_state(projected), "generation": 5},
        )
    )
    update_projection = AsyncMock(return_value=projected)
    monkeypatch.setattr(
        "app.voice.broker.get_federated_voice_session",
        AsyncMock(return_value=active),
    )
    monkeypatch.setattr("app.voice.broker.signed_request", signed)
    monkeypatch.setattr(
        "app.voice.broker.sync_federated_voice_session_generation",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.voice.broker.update_occupant_self_flags",
        update_projection,
    )

    result = await request_remote_guild_voice_self_state(
        AsyncMock(),
        AsyncMock(),
        settings(),
        actor=actor,
        self_mute=True,
        self_deaf=False,
    )

    assert result == projected
    signed_call = signed.await_args
    assert signed_call is not None
    assert signed_call.args[3] == "beta.localhost"
    assert signed_call.args[4] == "/_kaede/v1/voice/self-state"
    assert signed_call.kwargs["payload"] == {
        "guild_id": "12",
        "actor_id": "78",
        "room": "g.12.34",
        "move_session_id": "m" * 43,
        "generation": 4,
        "connection_id": "c" * 43,
        "self_mute": True,
        "self_deaf": False,
    }
    update_projection.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_self_state_rejects_authority_response_for_another_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = cast(
        Any,
        SimpleNamespace(id=78, origin_domain="alpha.localhost", account_type="human"),
    )
    active = FederatedVoiceSession(
        authority_domain="beta.localhost",
        guild_id="12",
        room="g.12.34",
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        connection_id="c" * 43,
    )
    wrong = occupant(
        identity="78@alpha.localhost",
        user_domain="alpha.localhost",
        room="g.12.35",
        channel_id="35",
        self_mute=True,
    )
    monkeypatch.setattr(
        "app.voice.broker.get_federated_voice_session",
        AsyncMock(return_value=active),
    )
    monkeypatch.setattr(
        "app.voice.broker.signed_request",
        AsyncMock(
            return_value=httpx.Response(
                200,
                json={"state": public_occupant_state(wrong), "generation": 5},
            )
        ),
    )

    with pytest.raises(HTTPException) as failure:
        await request_remote_guild_voice_self_state(
            AsyncMock(),
            AsyncMock(),
            settings(),
            actor=actor,
            self_mute=True,
            self_deaf=False,
        )

    assert failure.value.status_code == 502
    assert cast(Any, failure.value.detail) == {"code": "VOICE_AUTHORITY_INVALID_RESPONSE"}


@pytest.mark.asyncio
async def test_authority_endpoint_fences_move_session_and_fans_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant()
    updated = replace(
        current,
        self_mute=True,
        self_deaf=True,
        participant_metadata={**current.participant_metadata, "generation": 5},
    )
    voice_session = FederatedVoiceSession(
        authority_domain="alpha.localhost",
        guild_id="12",
        room="g.12.34",
        generation=4,
        move_session_id="m" * 43,
        ready=True,
        active=True,
        connection_id="c" * 43,
    )
    actor = SimpleNamespace(id=78, origin_domain="beta.localhost", account_type="human")
    member = SimpleNamespace(user_id=78, user_domain="beta.localhost")
    session = AsyncMock()
    session.get.side_effect = [actor, member]
    redis = AsyncMock()
    publish = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr("app.api.voice.require_guild_federation_access", lambda _p: None)
    monkeypatch.setattr(
        "app.api.voice.enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.voice.require_remote_user_creation_allowed",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.voice.load_voice_channel",
        AsyncMock(
            return_value=(
                SimpleNamespace(id=34),
                SimpleNamespace(id=12, origin_domain="alpha.localhost"),
            )
        ),
    )
    monkeypatch.setattr(
        "app.api.voice.get_federated_voice_session",
        AsyncMock(return_value=voice_session),
    )
    monkeypatch.setattr("app.api.voice.occupant_in_room", AsyncMock(return_value=current))
    apply_state = AsyncMock(return_value=updated)
    monkeypatch.setattr(
        "app.api.voice.update_authoritative_occupant_self_state",
        apply_state,
    )
    monkeypatch.setattr("app.api.voice.publish_ephemeral", publish)
    monkeypatch.setattr("app.api.voice.enqueue_best_effort", enqueue)
    monkeypatch.setattr(
        "app.api.voice.sync_federated_voice_session_generation",
        AsyncMock(return_value=True),
    )

    result = await federation_voice_self_state(
        VoiceSelfStateFederationRequest(
            guild_id="12",
            actor_id="78",
            room="g.12.34",
            move_session_id="m" * 43,
            generation=4,
            connection_id="c" * 43,
            self_mute=True,
            self_deaf=True,
        ),
        FederationPrincipal(origin="beta.localhost", key_id="key"),
        session,
        redis,
        settings(),
    )

    assert result.state.self_mute is True
    assert result.state.self_deaf is True
    assert result.generation == 5
    apply_state.assert_awaited_once()
    publish.assert_awaited_once()
    enqueue.assert_awaited_once()
    enqueue_call = enqueue.await_args
    assert enqueue_call is not None
    assert enqueue_call.args[1] == "g.12.34"


@pytest.mark.asyncio
async def test_authority_endpoint_rejects_a_stale_move_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.get.side_effect = [
        SimpleNamespace(id=78, origin_domain="beta.localhost", account_type="human"),
        SimpleNamespace(user_id=78, user_domain="beta.localhost"),
    ]
    monkeypatch.setattr("app.api.voice.require_guild_federation_access", lambda _p: None)
    monkeypatch.setattr(
        "app.api.voice.enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.voice.require_remote_user_creation_allowed",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.voice.load_voice_channel",
        AsyncMock(
            return_value=(
                SimpleNamespace(id=34),
                SimpleNamespace(id=12, origin_domain="alpha.localhost"),
            )
        ),
    )
    monkeypatch.setattr(
        "app.api.voice.get_federated_voice_session",
        AsyncMock(
            return_value=FederatedVoiceSession(
                authority_domain="alpha.localhost",
                guild_id="12",
                room="g.12.34",
                generation=4,
                move_session_id="old" * 11,
                ready=True,
                active=True,
                connection_id="c" * 43,
            )
        ),
    )

    with pytest.raises(HTTPException) as failure:
        await federation_voice_self_state(
            VoiceSelfStateFederationRequest(
                guild_id="12",
                actor_id="78",
                room="g.12.34",
                move_session_id="m" * 43,
                generation=4,
                connection_id="c" * 43,
                self_mute=True,
                self_deaf=False,
            ),
            FederationPrincipal(origin="beta.localhost", key_id="key"),
            session,
            AsyncMock(),
            settings(),
        )

    assert failure.value.status_code == 409
    assert cast(Any, failure.value.detail) == {"code": "VOICE_SESSION_STALE"}
