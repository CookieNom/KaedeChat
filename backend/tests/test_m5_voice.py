import base64
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import jwt
import pytest
from fastapi import HTTPException, Request
from livekit import api
from pydantic import ValidationError

from app.api.voice import livekit_webhook
from app.core.settings import Settings
from app.voice.background import _publish_local_room_snapshot
from app.voice.livekit import mint_join_token, publication_sources, receive_webhook
from app.voice.rooms import (
    dm_room_name,
    guild_room_name,
    parse_participant_identity,
    parse_room_name,
    participant_identity,
)
from app.voice.schemas import CallAction, VoiceBrokerRequest
from app.voice.service import parse_minted_metadata
from app.voice.state import CALL_TRANSITION_LUA, Occupant, occupancy_snapshot

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
LIVEKIT_SECRET = "livekit-test-secret-000000000000000000000000000000000000000"


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "alpha.localhost",
        "environment": "test",
        "secret_key": VALID_KEY,
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "dragonfly_url": "redis://dragonfly:6379/0",
        "media_s3_access_key": "GK00000000000000000000000000000000",
        "media_s3_secret_key": "0" * 64,
        "voice_enabled": True,
        "voice_api_key": "LKtestkey",
        "voice_api_secret": LIVEKIT_SECRET,
        "voice_public_url": "wss://alpha.localhost/livekit",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_room_and_participant_identifiers_are_canonical_and_round_trip() -> None:
    assert guild_room_name(12, 34) == "g.12.34"
    assert dm_room_name(34, 56) == "d.34.56"
    assert parse_room_name("g.12.34") == ("g", 12, 34)
    assert participant_identity(78, "ALPHA.LOCALHOST.") == "78@alpha.localhost"
    assert parse_participant_identity("78@alpha.localhost") == (78, "alpha.localhost")


@pytest.mark.parametrize(
    "value",
    ["g.01.2", "g.1.-2", "x.1.2", "g.9223372036854775808.2", "../g.1.2"],
)
def test_invalid_room_identifiers_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        parse_room_name(value)


def test_livekit_token_is_short_lived_room_scoped_and_has_no_admin_grant() -> None:
    configured = settings()
    token, expires_at = mint_join_token(
        configured,
        room="g.12.34",
        identity="78@alpha.localhost",
        display_name="Paper Lantern",
        metadata={"generation": 3, "user_domain": "alpha.localhost"},
        can_speak=True,
        can_stream=False,
    )
    claims = jwt.decode(token, LIVEKIT_SECRET, algorithms=["HS256"])
    assert claims["iss"] == "LKtestkey"
    assert claims["sub"] == "78@alpha.localhost"
    assert claims["video"] == {
        "roomJoin": True,
        "room": "g.12.34",
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": False,
        "canPublishSources": ["microphone"],
        "canUpdateOwnMetadata": False,
    }
    assert "roomAdmin" not in claims["video"]
    assert claims["exp"] - claims["nbf"] == configured.voice_token_ttl_seconds
    assert 850 <= (expires_at - datetime.now(UTC)).total_seconds() <= 900


def test_publication_sources_follow_speak_and_stream_independently() -> None:
    assert publication_sources(can_speak=False, can_stream=False) == []
    assert publication_sources(can_speak=True, can_stream=False) == ["microphone"]
    assert publication_sources(can_speak=False, can_stream=True) == [
        "camera",
        "screen_share",
        "screen_share_audio",
    ]
    protobuf_sources = [
        api.TrackSource.Value(item.upper())
        for item in publication_sources(can_speak=True, can_stream=True)
    ]
    permission = api.ParticipantPermission(can_publish_sources=protobuf_sources)
    assert list(permission.can_publish_sources) == [2, 1, 3, 4]


def test_signed_webhook_body_hash_is_verified() -> None:
    configured = settings()
    body = json.dumps({"event": "room_started", "room": {"name": "g.12.34"}})
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    authorization = api.AccessToken("LKtestkey", LIVEKIT_SECRET).with_sha256(digest).to_jwt()
    event = receive_webhook(configured, body, authorization)
    assert event.event == "room_started"
    with pytest.raises(Exception, match="invalid LiveKit webhook signature"):
        receive_webhook(configured, body + " ", authorization)


async def test_livekit_webhook_rejects_an_oversized_stream_before_buffering() -> None:
    chunks = iter(
        (
            {"type": "http.request", "body": b"x" * (128 * 1024), "more_body": True},
            {"type": "http.request", "body": b"x" * (128 * 1024 + 1), "more_body": False},
        )
    )

    async def receive() -> dict[str, object]:
        return next(chunks)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/livekit/webhook",
            "headers": [],
        },
        receive,
    )
    with pytest.raises(HTTPException) as raised:
        await livekit_webhook(
            request=request,
            authorization="",
            session=cast(Any, None),
            redis=cast(Any, None),
            settings=settings(),
        )
    assert raised.value.status_code == 413
    assert raised.value.detail == {"code": "VOICE_WEBHOOK_TOO_LARGE"}


def webhook_request() -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/livekit/webhook",
            "headers": [],
        },
        receive,
    )


def webhook_event(event_type: str, *, metadata: str = "") -> SimpleNamespace:
    event = SimpleNamespace(
        event=event_type,
        room=SimpleNamespace(name="g.12.34"),
        participant=SimpleNamespace(identity="78@alpha.localhost", metadata=metadata),
    )
    event.HasField = lambda field: field in {"room", "participant"}
    return event


async def test_track_webhook_cannot_evict_a_joined_participant(monkeypatch: Any) -> None:
    removed: list[tuple[str, str]] = []

    async def remove(_self: object, room: str, identity: str) -> None:
        removed.append((room, identity))

    monkeypatch.setattr(
        "app.api.voice.receive_webhook",
        lambda *_args: webhook_event("track_published"),
    )
    monkeypatch.setattr("app.api.voice.LiveKitControl.remove_participant", remove)

    response = await livekit_webhook(
        request=webhook_request(),
        authorization="signed",
        session=cast(Any, None),
        redis=cast(Any, None),
        settings=settings(),
    )

    assert response.status_code == 204
    assert removed == []


async def test_participant_left_does_not_require_join_metadata(monkeypatch: Any) -> None:
    removed: list[tuple[str, str, str]] = []
    published: list[tuple[str, str, dict[str, object]]] = []
    queued: list[str] = []

    async def remove(_redis: object, authority: str, room: str, identity: str) -> None:
        removed.append((authority, room, identity))

    async def publish(_redis: object, topic: str, event: str, payload: dict[str, object]) -> None:
        published.append((topic, event, payload))

    async def enqueue(_task: object, room: str) -> bool:
        queued.append(room)
        return True

    monkeypatch.setattr(
        "app.api.voice.receive_webhook",
        lambda *_args: webhook_event("participant_left"),
    )
    monkeypatch.setattr("app.api.voice.remove_occupant", remove)
    monkeypatch.setattr("app.api.voice.publish_ephemeral", publish)
    monkeypatch.setattr("app.api.voice.enqueue_best_effort", enqueue)

    response = await livekit_webhook(
        request=webhook_request(),
        authorization="signed",
        session=cast(Any, None),
        redis=cast(Any, None),
        settings=settings(),
    )

    assert response.status_code == 204
    assert removed == [("alpha.localhost", "g.12.34", "78@alpha.localhost")]
    assert published[0][1] == "VOICE_STATE_UPDATE"
    assert published[0][2]["connected"] is False
    assert queued == ["g.12.34"]


async def test_voice_coordinator_publishes_scoped_local_snapshots(monkeypatch: Any) -> None:
    published: list[tuple[str, str, dict[str, object]]] = []

    async def publish(_redis: object, topic: str, event: str, payload: dict[str, object]) -> None:
        published.append((topic, event, payload))

    monkeypatch.setattr("app.voice.background.publish_ephemeral", publish)
    participant = Occupant(
        identity="78@alpha.localhost",
        user_id="78",
        user_domain="alpha.localhost",
        room="g.12.34",
        guild_id="12",
        channel_id="34",
        joined_at=1,
        server_mute=False,
        server_deaf=False,
        can_speak=True,
        can_stream=True,
    )

    await _publish_local_room_snapshot(cast(Any, None), settings(), "g.12.34", [participant], 123)

    assert published == [
        (
            "guild:alpha.localhost:12",
            "VOICE_STATE_UPDATE",
            {
                "room": "g.12.34",
                "guild_id": "12",
                "channel_id": "34",
                "channel_domain": "alpha.localhost",
                "participants": [asdict(participant)],
                "generated_at": 123,
                "heartbeat": True,
            },
        )
    ]


def test_voice_metadata_is_bound_to_identity_and_room() -> None:
    metadata = {
        "generation": 4,
        "user_id": "78",
        "user_domain": "alpha.localhost",
        "channel_id": "34",
        "can_speak": True,
        "can_stream": False,
        "server_mute": False,
        "server_deaf": False,
    }
    encoded = json.dumps(metadata)
    assert parse_minted_metadata(encoded, room="g.12.34", identity="78@alpha.localhost") == metadata
    with pytest.raises(ValueError, match="identity mismatch"):
        parse_minted_metadata(encoded, room="g.12.34", identity="79@alpha.localhost")
    with pytest.raises(ValueError, match="room mismatch"):
        parse_minted_metadata(encoded, room="g.12.35", identity="78@alpha.localhost")
    dm_metadata = {**metadata, "channel_id": "34", "call_id": "56"}
    assert (
        parse_minted_metadata(
            json.dumps(dm_metadata), room="d.34.56", identity="78@alpha.localhost"
        )
        == dm_metadata
    )


def test_voice_and_call_federation_schemas_forbid_malleable_fields() -> None:
    request = VoiceBrokerRequest.model_validate(
        {
            "guild_id": "12",
            "channel_id": "34",
            "actor_id": "78",
            "actor_domain": "beta.localhost",
        }
    )
    assert request.actor_domain == "beta.localhost"
    with pytest.raises(ValidationError):
        VoiceBrokerRequest.model_validate({**request.model_dump(), "admin": True})
    with pytest.raises(ValidationError):
        CallAction(action="ring")  # type: ignore[arg-type]


async def test_occupancy_staleness_is_explicit_not_silently_empty() -> None:
    occupant = Occupant(
        identity="78@alpha.localhost",
        user_id="78",
        user_domain="alpha.localhost",
        room="g.12.34",
        guild_id="12",
        channel_id="34",
        joined_at=100,
    )
    redis = cast(
        Any,
        SimpleNamespace(
            get=lambda _key: _async_value("100"),
            hvals=lambda _key: _async_value([json.dumps(asdict(occupant))]),
        ),
    )
    snapshot = await occupancy_snapshot(redis, "alpha.localhost", "g.12.34", settings(), now=176)
    assert snapshot["stale"] is True
    assert (
        cast(list[dict[str, object]], snapshot["participants"])[0]["identity"] == occupant.identity
    )


async def _async_value(value: object) -> object:
    return value


def test_call_transitions_are_atomic_and_decline_completion_is_bounded() -> None:
    assert "SISMEMBER" in CALL_TRANSITION_LUA
    assert "SCARD" in CALL_TRANSITION_LUA
    assert "call['ended_at']" in CALL_TRANSITION_LUA
