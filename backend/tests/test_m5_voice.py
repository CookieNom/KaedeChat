import base64
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import HTTPException, Request
from livekit import api
from pydantic import ValidationError

from app.api.bot_voice import bot_disconnect_voice
from app.api.calls import call_voice_token
from app.api.voice import (
    channel_voice_token,
    federation_voice_move,
    livekit_webhook,
    move_member_voice,
)
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation, BotWorker
from app.db.models import User
from app.federation.security import FederationPrincipal
from app.voice.background import _publish_local_room_snapshot
from app.voice.e2ee import MediaSessionRotationError, evict_channel_media_sessions
from app.voice.livekit import (
    LiveKitError,
    mint_join_token,
    publication_sources,
    receive_webhook,
    screen_share_is_active,
)
from app.voice.rooms import (
    dm_room_name,
    guild_room_name,
    parse_participant_identity,
    parse_room_name,
    participant_identity,
)
from app.voice.schemas import (
    BotVoiceDisconnectRequest,
    CallAction,
    VoiceBrokerRequest,
    VoiceMoveFederationRequest,
    VoiceMoveRequest,
    VoiceTokenResponse,
)
from app.voice.service import (
    MEDIA_E2EE_PROTOCOL,
    MEDIA_E2EE_SUITE,
    authoritative_guild_token,
    effective_voice_user_limit,
    federated_voice_grant_matches,
    media_session_id,
    parse_minted_metadata,
    require_e2ee_voice_device,
    update_authoritative_occupant_grant,
    valid_federated_voice_url,
    voice_metadata_matches_policy,
)
from app.voice.state import (
    ACTIVATE_FEDERATED_VOICE_SESSION_LUA,
    ADMIT_OCCUPANT_LUA,
    ADVANCE_FEDERATED_VOICE_SESSION_LUA,
    CALL_TRANSITION_LUA,
    FederatedVoiceSession,
    Occupant,
    activate_federated_voice_home_session,
    admit_occupant,
    advance_federated_voice_home_session,
    begin_federated_voice_home_session,
    discard_all_federated_voice_home_sessions,
    federated_voice_pending_key,
    federated_voice_session_key,
    occupancy_snapshot,
    public_occupant_state,
    voice_room_registry_key,
    voice_user_room_key,
)

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


@pytest.mark.asyncio
async def test_screen_share_lookup_binds_exact_participant_and_track_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participants = [
        SimpleNamespace(
            identity="42@people.example",
            tracks=[SimpleNamespace(source=api.TrackSource.Value("SCREEN_SHARE"))],
        ),
        SimpleNamespace(
            identity="43@people.example",
            tracks=[SimpleNamespace(source=api.TrackSource.Value("CAMERA"))],
        ),
    ]
    monkeypatch.setattr(
        "app.voice.livekit.LiveKitControl.list_participants",
        AsyncMock(return_value=participants),
    )

    assert await screen_share_is_active(
        cast(Any, SimpleNamespace(voice_api_key=object(), voice_api_secret=object())),
        "g.10.20",
        "42@people.example",
    )
    assert not await screen_share_is_active(
        cast(Any, SimpleNamespace(voice_api_key=object(), voice_api_secret=object())),
        "g.10.20",
        "43@people.example",
    )


LIVEKIT_SECRET = "livekit-test-secret-000000000000000000000000000000000000000"


@pytest.mark.asyncio
async def test_active_voice_scheduled_event_caps_room_at_99() -> None:
    channel = SimpleNamespace(
        id=34,
        origin_domain="alpha.localhost",
        type=2,
        user_limit=0,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=91))

    assert await effective_voice_user_limit(session, channel) == 99
    statement = session.scalar.await_args.args[0]
    rendered = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "guild_scheduled_events.status = 2" in rendered
    assert "guild_scheduled_events.entity_type = 2" in rendered
    assert "guild_scheduled_events.channel_id = 34" in rendered

    channel.user_limit = 40
    assert await effective_voice_user_limit(session, channel) == 40
    session.scalar.return_value = None
    channel.user_limit = 0
    assert await effective_voice_user_limit(session, channel) == 0

    channel.type = 13
    channel.user_limit = 10_000
    session.scalar.reset_mock()
    assert await effective_voice_user_limit(session, channel) == 10_000
    session.scalar.assert_not_awaited()


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


def test_voice_redis_indexes_isolate_identical_snowflakes_by_authority() -> None:
    identity = "78@users.localhost"

    assert voice_room_registry_key("ALPHA.LOCALHOST.") == "voice:v2:rooms:alpha.localhost"
    assert voice_room_registry_key("alpha.localhost") != voice_room_registry_key("beta.localhost")
    assert (
        voice_user_room_key("ALPHA.LOCALHOST.", identity)
        == "voice:v2:user-room:alpha.localhost:78@users.localhost"
    )
    assert voice_user_room_key("alpha.localhost", identity) != voice_user_room_key(
        "beta.localhost", identity
    )


@pytest.mark.asyncio
async def test_bot_disconnect_releases_its_claim_when_livekit_is_unavailable(
    monkeypatch: Any,
) -> None:
    channel = SimpleNamespace(
        id=34,
        type=2,
        guild_id=12,
        origin_domain="alpha.localhost",
        guild_domain="alpha.localhost",
    )
    connection_id = "c" * 43
    events: list[str] = []
    installation = BotInstallation(
        id=1,
        application_id=70,
        application_domain="alpha.localhost",
        grant_revision=1,
    )
    worker = SimpleNamespace(id=2)

    async def record(name: str, *_args: object, **_kwargs: object) -> None:
        events.append(name)

    monkeypatch.setattr(
        "app.api.bot_voice.installation_for_channel",
        AsyncMock(return_value=(channel, installation)),
    )
    monkeypatch.setattr("app.api.bot_voice.voice_connection_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.api.bot_voice.occupant_in_room",
        AsyncMock(
            return_value=Occupant(
                identity="78@alpha.localhost",
                user_id="78",
                user_domain="alpha.localhost",
                room="g.12.34",
                guild_id="12",
                channel_id="34",
                joined_at=1,
                connection_id=connection_id,
                client_kind="bot",
                participant_metadata={
                    "bot_application_id": "70",
                    "bot_application_domain": "alpha.localhost",
                    "bot_worker_id": 2,
                    "bot_installation_id": 1,
                    "bot_installation_revision": 1,
                },
            )
        ),
    )
    monkeypatch.setattr(
        "app.api.bot_voice.bump_generation",
        lambda *args, **kwargs: record("fenced", *args, **kwargs),
    )

    async def failed_control(*_args: object, **_kwargs: object) -> None:
        events.append("control")
        raise LiveKitError("unavailable")

    monkeypatch.setattr("app.api.bot_voice.LiveKitControl.remove_participant", failed_control)
    monkeypatch.setattr(
        "app.api.bot_voice.remove_occupant_connection",
        lambda *args, **kwargs: record("occupant", *args, **kwargs),
    )
    monkeypatch.setattr(
        "app.api.bot_voice.release_voice_connection",
        lambda *args, **kwargs: record("claim", *args, **kwargs),
    )
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        id=12,
        origin_domain="alpha.localhost",
        unavailable=False,
    )

    with pytest.raises(HTTPException) as caught:
        await bot_disconnect_voice(
            EntityRef("34@alpha.localhost"),
            BotVoiceDisconnectRequest(connection_id=connection_id, generation=4),
            cast(
                Any,
                SimpleNamespace(
                    user=SimpleNamespace(id=78, origin_domain="alpha.localhost"),
                    worker=worker,
                ),
            ),
            session,
            cast(Any, SimpleNamespace()),
            settings(),
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "code": "VOICE_HOME_UNREACHABLE",
        "retry_after_ms": 2000,
    }
    assert events == ["fenced", "control", "occupant", "claim"]


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


@pytest.mark.asyncio
async def test_failed_voice_token_mint_releases_connection_claim(monkeypatch: Any) -> None:
    actor = SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        username="bot",
        display_name="Bot",
        profile_resolved=True,
        account_type="human",
    )
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    channel = SimpleNamespace(
        id=34,
        origin_domain="alpha.localhost",
        encryption_mode="plaintext",
        encryption_state="disabled",
        encryption_policy_generation=0,
        encryption_epoch=None,
        bitrate=96_000,
        user_limit=0,
        rtc_region=None,
        video_quality_mode=2,
    )
    member = SimpleNamespace(voice_flags=0)
    session = AsyncMock()
    session.get.return_value = member
    redis = AsyncMock()
    control = SimpleNamespace(
        ensure_room=AsyncMock(),
        remove_participant=AsyncMock(),
    )
    release = AsyncMock(return_value=True)
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.require_permissions",
        AsyncMock(
            return_value=(
                Permission.CONNECT | Permission.SPEAK | Permission.STREAM | Permission.USE_VAD
            )
        ),
    )
    monkeypatch.setattr("app.voice.service.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr(
        "app.voice.service.claim_voice_connection",
        AsyncMock(return_value=(True, 7, "", "")),
    )
    monkeypatch.setattr("app.voice.service.release_voice_connection", release)
    monkeypatch.setattr(
        "app.voice.service.mint_join_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LiveKitError("mint failed")),
    )

    with pytest.raises(HTTPException) as caught:
        await authoritative_guild_token(
            session,
            redis,
            settings(),
            channel=channel,
            guild=guild,
            actor=actor,
            connection_id="c" * 43,
            client_kind="web",
        )

    assert caught.value.status_code == 503
    release.assert_awaited_once_with(
        redis,
        "alpha.localhost",
        "78@alpha.localhost",
        "c" * 43,
        room="g.12.34",
        client_kind="web",
    )


@pytest.mark.asyncio
async def test_voice_channel_full_is_rejected_before_claiming_connection(
    monkeypatch: Any,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost", account_type="human")
    channel = SimpleNamespace(
        id=34,
        origin_domain="alpha.localhost",
        encryption_mode="plaintext",
        encryption_state="disabled",
        encryption_policy_generation=0,
        encryption_epoch=None,
        bitrate=64_000,
        user_limit=1,
        rtc_region=None,
        video_quality_mode=1,
    )
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(voice_flags=0)
    claim = AsyncMock()
    monkeypatch.setattr(
        "app.voice.service.require_permissions",
        AsyncMock(return_value=Permission.CONNECT),
    )
    monkeypatch.setattr("app.voice.service.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr(
        "app.voice.service.room_occupants",
        AsyncMock(
            return_value=[
                Occupant(
                    identity="79@alpha.localhost",
                    user_id="79",
                    user_domain="alpha.localhost",
                    room="g.12.34",
                    guild_id="12",
                    channel_id="34",
                    joined_at=1,
                )
            ]
        ),
    )
    monkeypatch.setattr("app.voice.service.claim_voice_connection", claim)

    with pytest.raises(HTTPException) as caught:
        await authoritative_guild_token(
            session,
            AsyncMock(),
            settings(),
            channel=channel,
            guild=guild,
            actor=actor,
            connection_id="c" * 43,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "VOICE_CHANNEL_FULL",
        "message": "This voice channel has reached its user limit.",
        "user_limit": 1,
    }
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_admission_enforces_limit_atomically() -> None:
    occupant = Occupant(
        identity="78@alpha.localhost",
        user_id="78",
        user_domain="alpha.localhost",
        room="g.12.34",
        guild_id="12",
        channel_id="34",
        joined_at=1,
    )
    redis = AsyncMock()
    redis.eval.return_value = 0

    assert not await admit_occupant(
        redis,
        "alpha.localhost",
        occupant,
        user_limit=1,
        bypass_limit=False,
    )
    assert redis.eval.await_args.args[0] == ADMIT_OCCUPANT_LUA
    assert redis.eval.await_args.args[3:5] == (
        "voice:v2:rooms:alpha.localhost",
        "voice:v2:user-room:alpha.localhost:78@alpha.localhost",
    )
    assert redis.eval.await_args.args[-2:] == ("1", "0")
    redis.eval.return_value = 1
    assert await admit_occupant(
        redis,
        "alpha.localhost",
        occupant,
        user_limit=1,
        bypass_limit=True,
    )
    assert redis.eval.await_args.args[-2:] == ("1", "1")


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


async def test_participant_left_without_connection_metadata_cannot_remove_new_lease(
    monkeypatch: Any,
) -> None:
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
    assert removed == []
    assert published == []
    assert queued == []


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
                "participants": [public_occupant_state(participant)],
                "generated_at": 123,
                "heartbeat": True,
            },
        )
    ]


def test_voice_metadata_is_bound_to_identity_and_room() -> None:
    metadata = {
        "generation": 4,
        "connection_id": "c" * 43,
        "client_kind": "web",
        "user_id": "78",
        "user_domain": "alpha.localhost",
        "channel_id": "34",
        "channel_domain": "alpha.localhost",
        "e2ee": False,
        "can_speak": True,
        "can_stream": False,
        "can_use_vad": True,
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


def test_encrypted_voice_context_is_complete_and_policy_bound() -> None:
    channel = SimpleNamespace(
        id=34,
        origin_domain="alpha.localhost",
        encryption_mode="e2ee",
        encryption_state="active",
        encryption_group_id="group-a",
        encryption_policy_generation=5,
        encryption_epoch=7,
    )
    first = media_session_id(channel, "g.12.34")
    assert len(first) == 43
    channel.encryption_epoch = 8
    assert media_session_id(channel, "g.12.34") != first
    channel.encryption_epoch = 7
    assert media_session_id(channel, "d.34.56") != first

    complete = {
        "token": "x" * 32,
        "url": "wss://alpha.localhost/livekit",
        "room": "g.12.34",
        "generation": 1,
        "connection_id": "c" * 43,
        "expires_at": "2026-08-11T12:00:00+00:00",
        "can_speak": True,
        "can_stream": True,
        "e2ee": True,
        "channel_id": "34",
        "channel_domain": "alpha.localhost",
        "encryption_policy_generation": "5",
        "encryption_epoch": "7",
        "media_protocol": MEDIA_E2EE_PROTOCOL,
        "media_suite": MEDIA_E2EE_SUITE,
        "media_session_id": first,
        "media_epoch": "7",
    }
    assert VoiceTokenResponse.model_validate(complete).media_session_id == first
    with pytest.raises(ValidationError):
        VoiceTokenResponse.model_validate(
            {key: value for key, value in complete.items() if key != "e2ee"}
        )
    with pytest.raises(ValidationError, match="channel reference"):
        VoiceTokenResponse.model_validate({**complete, "channel_id": None})
    with pytest.raises(ValidationError, match="complete room context"):
        VoiceTokenResponse.model_validate({**complete, "media_session_id": None})
    with pytest.raises(ValidationError, match="media epoch"):
        VoiceTokenResponse.model_validate({**complete, "media_epoch": "6"})

    metadata = {
        "e2ee": True,
        "encryption_policy_generation": "5",
        "encryption_epoch": "7",
        "media_protocol": MEDIA_E2EE_PROTOCOL,
        "media_suite": MEDIA_E2EE_SUITE,
        "media_session_id": first,
        "media_epoch": "7",
    }
    assert voice_metadata_matches_policy(channel, "g.12.34", metadata)
    assert not voice_metadata_matches_policy(channel, "g.12.34", {**metadata, "media_epoch": "6"})
    channel.encryption_state = "rekeying"
    assert not voice_metadata_matches_policy(channel, "g.12.34", metadata)


@pytest.mark.asyncio
async def test_encrypted_voice_requires_an_active_owned_device() -> None:
    actor = SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        account_type="human",
    )
    channel = SimpleNamespace(encryption_mode="e2ee", encryption_state="active")
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as missing:
        await require_e2ee_voice_device(
            session,
            settings(),
            channel,
            actor,
            None,  # type: ignore[arg-type]
        )
    assert missing.value.detail == {"code": "E2EE_SENDER_DEVICE_INVALID"}
    with pytest.raises(HTTPException) as unowned:
        await require_e2ee_voice_device(
            session,
            settings(),
            channel,
            actor,
            "ked_" + "a" * 43,  # type: ignore[arg-type]
        )
    assert unowned.value.detail == {"code": "E2EE_SENDER_DEVICE_INVALID"}

    channel.encryption_state = "rekeying"
    with pytest.raises(HTTPException) as paused:
        await require_e2ee_voice_device(
            session,
            settings(),
            channel,
            actor,
            "ked_" + "a" * 43,  # type: ignore[arg-type]
        )
    assert paused.value.detail == {"code": "E2EE_REKEY_REQUIRED"}


@pytest.mark.asyncio
async def test_media_rotation_fences_grants_before_deleting_the_old_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupant = Occupant(
        identity="78@alpha.localhost",
        user_id="78",
        user_domain="alpha.localhost",
        room="g.12.34",
        guild_id="12",
        channel_id="34",
        joined_at=1,
    )
    list_rooms = AsyncMock(return_value=[SimpleNamespace(name="g.12.34")])
    delete_room = AsyncMock()
    bump = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr("app.voice.e2ee.LiveKitControl.list_rooms", list_rooms)
    monkeypatch.setattr("app.voice.e2ee.LiveKitControl.delete_room", delete_room)
    monkeypatch.setattr("app.voice.e2ee.room_occupants", AsyncMock(return_value=[occupant]))
    monkeypatch.setattr("app.voice.e2ee.bump_generation", bump)
    monkeypatch.setattr("app.voice.e2ee.remove_occupant", remove)

    channel = SimpleNamespace(id=34, type=2, guild_id=12)
    redis = AsyncMock()
    await evict_channel_media_sessions(redis, settings(), channel)  # type: ignore[arg-type]

    bump.assert_awaited_once_with(redis, "alpha.localhost", "g.12.34", occupant.identity)
    delete_room.assert_awaited_once_with("g.12.34")
    remove.assert_awaited_once_with(redis, "alpha.localhost", "g.12.34", occupant.identity)


@pytest.mark.asyncio
async def test_media_rotation_fails_closed_when_livekit_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.voice.e2ee.LiveKitControl.list_rooms",
        AsyncMock(side_effect=LiveKitError("unavailable")),
    )
    channel = SimpleNamespace(id=34, type=2, guild_id=12)
    with pytest.raises(MediaSessionRotationError):
        await evict_channel_media_sessions(AsyncMock(), settings(), channel)  # type: ignore[arg-type]


def test_voice_and_call_federation_schemas_forbid_malleable_fields() -> None:
    move_session_id = "abcdefghijklmnopqrstuvwxyz0123456789_AB"
    request = VoiceBrokerRequest.model_validate(
        {
            "guild_id": "12",
            "channel_id": "34",
            "actor_id": "78",
            "actor_domain": "beta.localhost",
            "move_session_id": move_session_id,
            "connection_id": "c" * 43,
        }
    )
    assert request.actor_domain == "beta.localhost"
    with pytest.raises(ValidationError):
        VoiceBrokerRequest.model_validate({**request.model_dump(), "admin": True})
    with pytest.raises(ValidationError):
        CallAction(action="ring")  # type: ignore[arg-type]

    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://alpha.localhost/livekit",
        room="g.12.35",
        generation=5,
        connection_id="c" * 43,
        expires_at="2026-08-11T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=False,
        channel_id="35",
        channel_domain="alpha.localhost",
        move_session_id=move_session_id,
    )
    VoiceMoveFederationRequest(
        guild_id="12",
        channel_id="35",
        target_id="78",
        target_domain="beta.localhost",
        move_session_id=move_session_id,
        source_room="g.12.34",
        source_generation=4,
        grant=grant,
    )
    with pytest.raises(ValidationError, match="correlation"):
        VoiceMoveFederationRequest(
            guild_id="12",
            channel_id="35",
            target_id="78",
            target_domain="beta.localhost",
            move_session_id="z" * 32,
            source_room="g.12.34",
            source_generation=4,
            grant=grant,
        )


@pytest.mark.parametrize(
    "url",
    [
        "ws://beta.localhost/livekit",
        "http://beta.localhost/livekit",
        "custom://beta.localhost/livekit",
        "wss://user@beta.localhost/livekit",
        "wss://beta.localhost:8443/livekit",
        "wss://beta.localhost/livekit?token=peer-controlled",
        "wss://beta.localhost/livekit?",
        "wss://beta.localhost/livekit#fragment",
        "wss://beta.localhost/livekit#",
        "wss://beta.localhost/not-livekit",
        "wss://beta.localhost//livekit",
        "wss://other.localhost/livekit",
    ],
)
def test_federated_voice_urls_reject_downgrades_and_ambiguous_authorities(url: str) -> None:
    assert not valid_federated_voice_url(url, "beta.localhost")


@pytest.mark.parametrize(
    "url",
    [
        "wss://beta.localhost",
        "wss://beta.localhost/",
        "wss://beta.localhost/livekit",
        "wss://beta.localhost:443/livekit",
    ],
)
def test_federated_voice_urls_accept_only_secure_authority_endpoints(url: str) -> None:
    assert valid_federated_voice_url(url, "beta.localhost")


def test_plaintext_federated_voice_grant_is_bound_to_room_channel_and_authority() -> None:
    channel = SimpleNamespace(
        id=34,
        origin_domain="beta.localhost",
        encryption_mode="plaintext",
    )
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://beta.localhost/livekit",
        room="d.34.56",
        generation=1,
        connection_id="c" * 43,
        expires_at="2026-08-18T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=False,
        channel_id="34",
        channel_domain="beta.localhost",
    )
    assert federated_voice_grant_matches(
        grant,
        channel,  # type: ignore[arg-type]
        expected_room="d.34.56",
        authority_domain="beta.localhost",
    )
    for substituted in (
        grant.model_copy(update={"room": "d.34.57"}),
        grant.model_copy(update={"url": "wss://media.attacker.example/livekit"}),
        grant.model_copy(update={"channel_id": "35"}),
        grant.model_copy(update={"channel_domain": "gamma.localhost"}),
    ):
        assert not federated_voice_grant_matches(
            substituted,
            channel,  # type: ignore[arg-type]
            expected_room="d.34.56",
            authority_domain="beta.localhost",
        )


def test_encrypted_federated_voice_grant_is_bound_to_room_channel_and_authority() -> None:
    channel = SimpleNamespace(
        id=34,
        origin_domain="beta.localhost",
        encryption_mode="e2ee",
        encryption_state="active",
        encryption_group_id="group-a",
        encryption_policy_generation=5,
        encryption_epoch=7,
    )
    room = "d.34.56"
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://beta.localhost/livekit",
        room=room,
        generation=1,
        connection_id="c" * 43,
        expires_at="2026-08-18T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=True,
        channel_id="34",
        channel_domain="beta.localhost",
        encryption_policy_generation="5",
        encryption_epoch="7",
        media_protocol=MEDIA_E2EE_PROTOCOL,
        media_suite=MEDIA_E2EE_SUITE,
        media_session_id=media_session_id(channel, room),
        media_epoch="7",
    )
    assert federated_voice_grant_matches(
        grant,
        channel,  # type: ignore[arg-type]
        expected_room=room,
        authority_domain="beta.localhost",
    )
    for substituted in (
        grant.model_copy(update={"room": "d.34.57"}),
        grant.model_copy(update={"url": "wss://beta.localhost/not-livekit"}),
        grant.model_copy(update={"channel_id": "35"}),
        grant.model_copy(update={"channel_domain": "gamma.localhost"}),
    ):
        assert not federated_voice_grant_matches(
            substituted,
            channel,  # type: ignore[arg-type]
            expected_room=room,
            authority_domain="beta.localhost",
        )


@pytest.mark.asyncio
async def test_remote_dm_voice_grant_rejects_a_substituted_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost")
    record = {
        "id": "56",
        "authority_domain": "beta.localhost",
        "channel_id": "34",
        "channel_domain": "beta.localhost",
        "room": "d.34.56",
    }
    channel = SimpleNamespace(
        id=34,
        origin_domain="beta.localhost",
        encryption_mode="plaintext",
    )
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://beta.localhost/livekit",
        room="d.34.57",
        generation=1,
        connection_id="c" * 43,
        expires_at="2026-08-18T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=False,
        channel_id="34",
        channel_domain="beta.localhost",
    )
    session = AsyncMock()
    session.get.return_value = channel
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=record))
    monkeypatch.setattr("app.api.calls.require_call_policy", AsyncMock())
    monkeypatch.setattr("app.api.calls.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr(
        "app.api.calls.signed_request",
        AsyncMock(return_value=httpx.Response(200, json=grant.model_dump(mode="json"))),
    )
    discard = AsyncMock()
    monkeypatch.setattr("app.api.calls.discard_all_federated_voice_home_sessions", discard)

    with pytest.raises(HTTPException) as caught:
        await call_voice_token(
            EntityRef("56@beta.localhost"),
            SimpleNamespace(user=actor),  # type: ignore[arg-type]
            session,
            AsyncMock(),
            settings(),
        )

    assert caught.value.status_code == 502
    assert caught.value.detail == {"code": "VOICE_HOME_INVALID_RESPONSE"}
    discard.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_local_voice_replacement_revokes_federated_move_session(
    monkeypatch: Any,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost")
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://alpha.localhost/livekit",
        room="g.12.34",
        generation=1,
        connection_id="c" * 43,
        expires_at="2026-08-11T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=False,
        channel_id="34",
        channel_domain="alpha.localhost",
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
    monkeypatch.setattr("app.api.voice.get_permissions", AsyncMock(return_value=Permission.CONNECT))
    monkeypatch.setattr("app.api.voice.authoritative_guild_token", AsyncMock(return_value=grant))
    discard = AsyncMock()
    monkeypatch.setattr("app.api.voice.discard_all_federated_voice_home_sessions", discard)
    redis = AsyncMock()

    assert (
        await channel_voice_token(
            EntityRef("34@alpha.localhost"),
            SimpleNamespace(user=actor),  # type: ignore[arg-type]
            AsyncMock(),
            redis,
            settings(),
        )
        == grant
    )
    discard.assert_awaited_once_with(redis, "78@alpha.localhost")


@pytest.mark.asyncio
async def test_successful_dm_voice_replacement_revokes_federated_move_session(
    monkeypatch: Any,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost")
    record = {"authority_domain": "alpha.localhost"}
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://alpha.localhost/livekit",
        room="d.34.56",
        generation=1,
        connection_id="c" * 43,
        expires_at="2026-08-11T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=False,
        channel_id="34",
        channel_domain="alpha.localhost",
    )
    monkeypatch.setattr("app.api.calls.get_call", AsyncMock(return_value=record))
    monkeypatch.setattr("app.api.calls.mint_dm_call_token", AsyncMock(return_value=grant))
    discard = AsyncMock()
    monkeypatch.setattr("app.api.calls.discard_all_federated_voice_home_sessions", discard)
    redis = AsyncMock()

    assert (
        await call_voice_token(
            EntityRef("56@alpha.localhost"),
            SimpleNamespace(user=actor),  # type: ignore[arg-type]
            AsyncMock(),
            redis,
            settings(),
        )
        == grant
    )
    discard.assert_awaited_once_with(redis, "78@alpha.localhost")


class FederatedVoiceRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: object, **_kwargs: object) -> bool:
        self.values[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += self.values.pop(key, None) is not None
        return removed

    async def eval(self, script: str, key_count: int, *values: object) -> object:
        keys = [str(value) for value in values[:key_count]]
        args = [str(value) for value in values[key_count:]]
        if script == ACTIVATE_FEDERATED_VOICE_SESSION_LUA:
            if self.values.get(keys[0]) != args[0]:
                return 0
            self.values[keys[1]] = json.dumps(
                {
                    "authority_domain": args[1],
                    "guild_id": args[2],
                    "room": args[3],
                    "generation": int(args[4]),
                    "move_session_id": args[0],
                    "connection_id": args[6],
                    "client_kind": args[7],
                    "ready": True,
                    "active": False,
                }
            )
            self.values.pop(keys[0], None)
            return 1
        if script == ADVANCE_FEDERATED_VOICE_SESSION_LUA:
            raw = self.values.get(keys[0])
            if raw is None:
                return [0, "missing"]
            current = json.loads(raw)
            if not current["ready"]:
                return [0, "pending"]
            if (
                current["move_session_id"] == args[0]
                and current["authority_domain"] == args[1]
                and str(current["guild_id"]) == args[2]
                and current["room"] == args[5]
                and int(current["generation"]) == int(args[6])
                and not current["active"]
            ):
                return [1, "replay"]
            expected = (
                current["move_session_id"],
                current["authority_domain"],
                str(current["guild_id"]),
                current["room"],
                int(current["generation"]),
            )
            received = (args[0], args[1], args[2], args[3], int(args[4]))
            if expected != received:
                return [0, "mismatch"]
            if not current["active"]:
                return [0, "inactive"]
            current["room"] = args[5]
            current["generation"] = int(args[6])
            current["active"] = False
            encoded = json.dumps(current)
            self.values[keys[0]] = encoded
            return [1, encoded]
        raise AssertionError("unexpected Redis script")


@pytest.mark.asyncio
async def test_local_voice_replacement_fences_an_inflight_federated_broker() -> None:
    redis = cast(Any, FederatedVoiceRedis())
    identity = "78@beta.localhost"
    redis.values[federated_voice_pending_key(identity)] = "a" * 32
    redis.values[federated_voice_session_key("home", identity)] = "active"

    assert await discard_all_federated_voice_home_sessions(redis, identity)
    assert federated_voice_pending_key(identity) not in redis.values
    assert federated_voice_session_key("home", identity) not in redis.values


@pytest.mark.asyncio
async def test_federated_voice_move_requires_current_source_and_rejects_replay() -> None:
    redis = cast(Any, FederatedVoiceRedis())
    identity = "78@beta.localhost"
    move_session_id = "abcdefghijklmnopqrstuvwxyz0123456789_AB"

    assert (
        await advance_federated_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain="alpha.localhost",
            guild_id="12",
            source_room="g.12.34",
            source_generation=4,
            target_room="g.12.35",
            target_generation=5,
        )
        == "missing"
    )
    await begin_federated_voice_home_session(
        redis,
        identity,
        FederatedVoiceSession(
            authority_domain="alpha.localhost",
            guild_id="12",
            room="g.12.34",
            generation=0,
            move_session_id=move_session_id,
        ),
    )
    assert redis.values[federated_voice_pending_key(identity)] == move_session_id
    assert await activate_federated_voice_home_session(
        redis,
        identity,
        move_session_id=move_session_id,
        authority_domain="alpha.localhost",
        guild_id="12",
        room="g.12.34",
        generation=4,
        connection_id="c" * 43,
        client_kind="desktop",
    )
    assert (
        await advance_federated_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain="alpha.localhost",
            guild_id="12",
            source_room="g.12.34",
            source_generation=4,
            target_room="g.12.35",
            target_generation=5,
        )
        == "inactive"
    )
    current = json.loads(redis.values[federated_voice_session_key("home", identity)])
    assert current["connection_id"] == "c" * 43
    assert current["client_kind"] == "desktop"
    current["active"] = True
    redis.values[federated_voice_session_key("home", identity)] = json.dumps(current)
    assert (
        await advance_federated_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain="alpha.localhost",
            guild_id="12",
            source_room="g.12.99",
            source_generation=4,
            target_room="g.12.35",
            target_generation=5,
        )
        == "mismatch"
    )
    assert (
        await advance_federated_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain="alpha.localhost",
            guild_id="12",
            source_room="g.12.34",
            source_generation=3,
            target_room="g.12.35",
            target_generation=5,
        )
        == "mismatch"
    )
    assert (
        await advance_federated_voice_home_session(
            redis,
            identity,
            move_session_id="z" * 32,
            authority_domain="alpha.localhost",
            guild_id="12",
            source_room="g.12.34",
            source_generation=4,
            target_room="g.12.35",
            target_generation=5,
        )
        == "mismatch"
    )
    assert (
        await advance_federated_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain="alpha.localhost",
            guild_id="12",
            source_room="g.12.34",
            source_generation=4,
            target_room="g.12.35",
            target_generation=5,
        )
        is None
    )
    moved = json.loads(redis.values[federated_voice_session_key("home", identity)])
    assert (moved["room"], moved["generation"]) == ("g.12.35", 5)
    # A lost HTTP response or dispatch publish can safely retry the exact move;
    # the endpoint will replay its idempotent client notifications.
    assert (
        await advance_federated_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain="alpha.localhost",
            guild_id="12",
            source_room="g.12.34",
            source_generation=4,
            target_room="g.12.35",
            target_generation=5,
        )
        is None
    )


@pytest.mark.asyncio
async def test_federation_move_endpoint_rejects_unsolicited_before_dispatch(
    monkeypatch: Any,
) -> None:
    move_session_id = "abcdefghijklmnopqrstuvwxyz0123456789_AB"
    payload = VoiceMoveFederationRequest(
        guild_id="12",
        channel_id="35",
        target_id="78",
        target_domain="alpha.localhost",
        move_session_id=move_session_id,
        source_room="g.12.34",
        source_generation=4,
        grant=VoiceTokenResponse(
            token="x" * 32,
            url="wss://beta.localhost/livekit",
            room="g.12.35",
            generation=5,
            connection_id="c" * 43,
            expires_at="2026-08-11T12:00:00+00:00",
            can_speak=True,
            can_stream=True,
            e2ee=False,
            channel_id="35",
            channel_domain="beta.localhost",
            guild_id="12",
            guild_domain="beta.localhost",
            move_session_id=move_session_id,
        ),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=78)))
    redis = cast(Any, SimpleNamespace())
    monkeypatch.setattr("app.api.voice.enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(
        "app.api.voice.load_voice_channel",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    id=35,
                    origin_domain="beta.localhost",
                    encryption_mode="plaintext",
                    type=2,
                ),
                SimpleNamespace(id=12, origin_domain="beta.localhost"),
            )
        ),
    )
    monkeypatch.setattr("app.api.voice.get_permissions", AsyncMock(return_value=Permission.CONNECT))
    active_session = FederatedVoiceSession(
        authority_domain="beta.localhost",
        guild_id="12",
        room="g.12.34",
        generation=4,
        move_session_id=move_session_id,
        ready=True,
        active=True,
        connection_id="c" * 43,
        client_kind="desktop",
    )
    active = AsyncMock(return_value=active_session)
    monkeypatch.setattr("app.api.voice.get_federated_voice_session", active)
    advance = AsyncMock(return_value="missing")
    publish = AsyncMock()
    monkeypatch.setattr("app.api.voice.advance_federated_voice_home_session", advance)
    monkeypatch.setattr("app.api.voice.publish_dispatch", publish)

    with pytest.raises(HTTPException) as caught:
        await federation_voice_move(
            payload,
            FederationPrincipal(origin="beta.localhost", key_id="test"),
            cast(Any, session),
            redis,
            settings(),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "KAED_VOICE_MOVE_NOT_EXPECTED"}
    publish.assert_not_awaited()

    advance.return_value = None
    response = await federation_voice_move(
        payload,
        FederationPrincipal(origin="beta.localhost", key_id="test"),
        cast(Any, session),
        redis,
        settings(),
    )
    assert response.status_code == 204
    assert [call.args[2] for call in publish.await_args_list] == [
        "VOICE_CHANNEL_MOVE",
        "VOICE_TOKEN",
    ]

    priority_grant = VoiceTokenResponse.model_validate(
        payload.grant.model_dump() | {"can_priority_speak": True}
    )
    priority_payload = payload.model_copy(update={"grant": priority_grant})
    for client_kind in ("web", "mobile"):
        active.return_value = replace(active_session, client_kind=client_kind)
        advance.reset_mock()
        publish.reset_mock()
        with pytest.raises(HTTPException) as priority_error:
            await federation_voice_move(
                priority_payload,
                FederationPrincipal(origin="beta.localhost", key_id="test"),
                cast(Any, session),
                redis,
                settings(),
            )
        assert priority_error.value.status_code == 400
        assert priority_error.value.detail == {"code": "KAED_VOICE_INVALID_STATE"}
        advance.assert_not_awaited()
        publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_move_rejection_preserves_the_source_voice_session(
    monkeypatch: Any,
) -> None:
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    source_channel = SimpleNamespace(id=34, origin_domain="alpha.localhost")
    target_channel = SimpleNamespace(
        id=35,
        origin_domain="alpha.localhost",
        guild_id=12,
        guild_domain="alpha.localhost",
        unavailable=False,
        parent_id=None,
        parent_domain=None,
        type=2,
    )
    target_user = SimpleNamespace(
        id=78,
        origin_domain="beta.localhost",
        account_type="human",
    )
    moderator = SimpleNamespace(id=1, origin_domain="alpha.localhost")
    move_session_id = "abcdefghijklmnopqrstuvwxyz0123456789_AB"
    move_session = FederatedVoiceSession(
        move_session_id=move_session_id,
        authority_domain="alpha.localhost",
        guild_id="12",
        room="g.12.34",
        generation=4,
        ready=True,
        active=True,
        connection_id="c" * 43,
    )
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://alpha.localhost/livekit",
        room="g.12.35",
        generation=5,
        connection_id="c" * 43,
        expires_at="2026-08-11T12:00:00+00:00",
        can_speak=True,
        can_stream=True,
        e2ee=False,
        channel_id="35",
        channel_domain="alpha.localhost",
        move_session_id=move_session_id,
    )
    session = AsyncMock()
    session.get.return_value = target_user
    redis = AsyncMock()
    redis.get.return_value = b"g.12.34"
    monkeypatch.setattr(
        "app.api.voice.voice_user_room",
        AsyncMock(return_value="g.12.34"),
    )
    monkeypatch.setattr("app.api.guilds.local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr("app.api.voice.require_can_manage_member", AsyncMock())
    monkeypatch.setattr(
        "app.api.voice.load_voice_channel",
        AsyncMock(side_effect=[(target_channel, guild), (source_channel, guild)]),
    )
    monkeypatch.setattr("app.api.voice.get_permissions", AsyncMock(return_value=Permission.CONNECT))
    monkeypatch.setattr("app.api.voice.require_permissions", AsyncMock())
    monkeypatch.setattr("app.api.voice.current_generation", AsyncMock(return_value=4))
    monkeypatch.setattr(
        "app.api.voice.room_occupants",
        AsyncMock(
            return_value=[
                Occupant(
                    identity="78@beta.localhost",
                    user_id="78",
                    user_domain="beta.localhost",
                    room="g.12.34",
                    guild_id="12",
                    channel_id="34",
                    joined_at=1,
                    connection_id="c" * 43,
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "app.api.voice.get_federated_voice_session",
        AsyncMock(return_value=move_session),
    )
    monkeypatch.setattr("app.api.voice.add_audit_entry", AsyncMock())
    monkeypatch.setattr("app.api.voice.authoritative_guild_token", AsyncMock(return_value=grant))
    monkeypatch.setattr(
        "app.api.voice.signed_request",
        AsyncMock(return_value=SimpleNamespace(status_code=409)),
    )
    bump = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr("app.api.voice.bump_generation", bump)
    monkeypatch.setattr("app.api.voice.remove_occupant", remove)

    with pytest.raises(HTTPException) as caught:
        await move_member_voice(
            EntityRef("12@alpha.localhost"),
            EntityRef("78@beta.localhost"),
            VoiceMoveRequest(channel_id=EntityRef("35@alpha.localhost")),
            SimpleNamespace(user=moderator),  # type: ignore[arg-type]
            session,
            redis,
            AsyncMock(),
            settings(),
            None,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "VOICE_MOVE_REJECTED"}
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    bump.assert_not_awaited()
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_bot_move_stays_at_guild_authority_and_preserves_lineage(
    monkeypatch: Any,
) -> None:
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    source_channel = SimpleNamespace(id=34, origin_domain="alpha.localhost")
    target_channel = SimpleNamespace(
        id=35,
        origin_domain="alpha.localhost",
        guild_id=12,
        guild_domain="alpha.localhost",
        unavailable=False,
        parent_id=None,
        parent_domain=None,
        type=2,
    )
    target_user = SimpleNamespace(
        id=78,
        origin_domain="apps.example",
        account_type="bot",
    )
    moderator = SimpleNamespace(id=1, origin_domain="alpha.localhost")
    installation = BotInstallation(
        id=90,
        application_id=70,
        application_domain="apps.example",
        bot_user_id=78,
        bot_user_domain="apps.example",
        guild_id=12,
        guild_domain="alpha.localhost",
        status="active",
        grant_revision=6,
        granted_scopes=["voice.connect", "voice.listen"],
        channel_restrictions=[],
    )
    worker = BotWorker(
        id=80,
        application_id=70,
        application_domain="apps.example",
        name="voice worker",
        public_key=b"w" * 32,
        scopes=["voice.connect", "voice.listen"],
        intents=["guild_voice_states"],
        target_domains=["alpha.localhost"],
    )
    occupant = Occupant(
        identity="78@apps.example",
        user_id="78",
        user_domain="apps.example",
        room="g.12.34",
        guild_id="12",
        channel_id="34",
        joined_at=1,
        connection_id="c" * 43,
        client_kind="bot",
        self_mute=True,
        allow_listen=True,
        allow_speak=False,
        allow_stream=False,
        participant_metadata={
            "generation": 4,
            "bot_application_id": "70",
            "bot_application_domain": "apps.example",
            "bot_worker_id": 80,
            "bot_installation_id": 90,
            "bot_installation_revision": 6,
            "bot_e2ee_device_id": "kbe_" + "d" * 43,
        },
    )
    grant = VoiceTokenResponse(
        token="x" * 32,
        url="wss://alpha.localhost/livekit",
        room="g.12.35",
        generation=1,
        connection_id="c" * 43,
        expires_at="2026-08-11T12:00:00+00:00",
        can_listen=True,
        can_speak=False,
        can_stream=False,
        e2ee=False,
        channel_id="35",
        channel_domain="alpha.localhost",
    )

    async def get(model: object, _key: object) -> object | None:
        if model is User:
            return target_user
        if model is BotWorker:
            return worker
        return None

    session = AsyncMock()
    session.get = AsyncMock(side_effect=get)
    redis = AsyncMock()
    redis.get.return_value = b"g.12.34"
    monkeypatch.setattr(
        "app.api.voice.voice_user_room",
        AsyncMock(return_value="g.12.34"),
    )
    monkeypatch.setattr("app.api.voice.proxy_remote_guild_management", AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.guilds.local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr("app.api.voice.require_can_manage_member", AsyncMock())
    monkeypatch.setattr(
        "app.api.voice.load_voice_channel",
        AsyncMock(side_effect=[(target_channel, guild), (source_channel, guild)]),
    )
    monkeypatch.setattr("app.api.voice.get_permissions", AsyncMock(return_value=Permission.CONNECT))
    monkeypatch.setattr("app.api.voice.require_permissions", AsyncMock())
    monkeypatch.setattr("app.api.voice.current_generation", AsyncMock(return_value=4))
    monkeypatch.setattr("app.api.voice.room_occupants", AsyncMock(return_value=[occupant]))
    monkeypatch.setattr("app.api.voice.voice_connection_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.api.voice.active_bot_guild_voice_installation",
        AsyncMock(return_value=installation),
    )
    monkeypatch.setattr("app.api.voice.add_audit_entry", AsyncMock())
    mint = AsyncMock(return_value=grant)
    monkeypatch.setattr("app.api.voice.authoritative_guild_token", mint)
    publish = AsyncMock()
    monkeypatch.setattr("app.api.voice.publish_dispatch", publish)
    federation = AsyncMock()
    monkeypatch.setattr("app.api.voice.signed_request", federation)
    bump = AsyncMock()
    monkeypatch.setattr("app.api.voice.bump_generation", bump)
    monkeypatch.setattr("app.api.voice.remove_occupant", AsyncMock())

    response = await move_member_voice(
        EntityRef("12@alpha.localhost"),
        EntityRef("78@apps.example"),
        VoiceMoveRequest(channel_id=EntityRef("35@alpha.localhost")),
        SimpleNamespace(user=moderator),  # type: ignore[arg-type]
        session,
        redis,
        AsyncMock(),
        settings(),
        None,
    )

    assert response.status_code == 204
    federation.assert_not_awaited()
    bump.assert_not_awaited()
    assert [call.args[2] for call in publish.await_args_list] == [
        "VOICE_CHANNEL_MOVE",
        "VOICE_TOKEN",
    ]
    assert all(call.args[1] == "user:apps.example:78" for call in publish.await_args_list)
    assert mint.await_args.kwargs == {
        "channel": target_channel,
        "guild": guild,
        "actor": target_user,
        "move_session_id": None,
        "disconnect_previous": True,
        "sender_device_id": "kbe_" + "d" * 43,
        "bot_installation": installation,
        "bot_worker": worker,
        "connection_id": "c" * 43,
        "takeover": True,
        "client_kind": "bot",
        "allow_listen": True,
        "allow_speak": False,
        "allow_stream": False,
        "self_mute": True,
        "self_deaf": False,
    }


@pytest.mark.asyncio
async def test_bot_moderation_rotation_dispatches_private_generation_fence(
    monkeypatch: Any,
) -> None:
    occupant = Occupant(
        identity="78@apps.example",
        user_id="78",
        user_domain="apps.example",
        room="g.12.34",
        guild_id="12",
        channel_id="34",
        joined_at=1,
        connection_id="c" * 43,
        client_kind="bot",
        participant_metadata={
            "generation": 4,
            "channel_domain": "alpha.localhost",
        },
    )
    monkeypatch.setattr(
        "app.voice.service.claim_voice_grant_transition",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("app.voice.service.release_voice_grant_transition", AsyncMock())
    monkeypatch.setattr("app.voice.service.current_generation", AsyncMock(return_value=4))
    monkeypatch.setattr("app.voice.service.LiveKitControl.update_participant", AsyncMock())
    monkeypatch.setattr("app.voice.service.rotate_occupant_grant", AsyncMock(return_value=5))
    monkeypatch.setattr(
        "app.voice.service.get_federated_voice_session",
        AsyncMock(return_value=None),
    )
    publish = AsyncMock()
    monkeypatch.setattr("app.voice.service.publish_dispatch", publish)

    updated = await update_authoritative_occupant_grant(
        AsyncMock(),
        settings(),
        occupant,
        can_speak=False,
        can_stream=False,
        server_mute=True,
        server_deaf=True,
    )

    assert updated.participant_metadata["generation"] == 5
    assert updated.server_mute and updated.server_deaf
    assert publish.await_args.args[1:3] == (
        "user:apps.example:78",
        "VOICE_STATE_UPDATE",
    )
    private_state = publish.await_args.args[3]
    assert private_state["connection_id"] == "c" * 43
    assert private_state["generation"] == 5
    assert private_state["server_mute"] is True
    assert private_state["server_deaf"] is True


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
