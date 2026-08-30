from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.settings import Settings
from app.core.voice_protocol import (
    PRIORITY_SPEAKER_ACTIVE_PAYLOAD,
    PRIORITY_SPEAKER_INACTIVE_PAYLOAD,
    PRIORITY_SPEAKER_TOPIC,
)
from app.db.bot_models import BotInstallation, BotWorker
from app.voice.livekit import mint_join_token
from app.voice.schemas import VoiceFederationOccupantState, VoiceTokenResponse
from app.voice.service import (
    authoritative_guild_token,
    parse_minted_metadata,
    priority_speaking_allowed,
    priority_speaking_granted,
    update_authoritative_occupant_grant,
)
from app.voice.state import (
    Occupant,
    federation_occupant_state,
    occupant_from_federation_state,
    public_occupant_state,
)

LIVEKIT_SECRET = "livekit-test-secret-000000000000000000000000000000000000000"


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
        voice_api_secret=LIVEKIT_SECRET,
        voice_public_url="wss://alpha.localhost/livekit",
    )


def grant_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "token": "x" * 32,
        "url": "wss://alpha.localhost/livekit",
        "room": "g.12.34",
        "generation": 3,
        "connection_id": "c" * 43,
        "expires_at": "2026-08-30T00:00:00+00:00",
        "can_speak": True,
        "can_stream": False,
        "e2ee": False,
        "channel_id": "34",
        "channel_domain": "alpha.localhost",
        "guild_id": "12",
        "guild_domain": "alpha.localhost",
    }
    payload.update(overrides)
    return payload


def metadata_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "generation": 3,
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
    payload.update(overrides)
    return payload


def test_priority_speaker_wire_contract_is_bounded() -> None:
    assert PRIORITY_SPEAKER_TOPIC == "kaede.priority-speaker.v1"
    assert PRIORITY_SPEAKER_INACTIVE_PAYLOAD == b"\x00"
    assert PRIORITY_SPEAKER_ACTIVE_PAYLOAD == b"\x01"


def test_priority_speaking_requires_ordinary_voice_and_effective_bit() -> None:
    assert priority_speaking_allowed(2, Permission.PRIORITY_SPEAKER)
    assert priority_speaking_allowed(2, Permission(ALL_PERMISSIONS))
    assert not priority_speaking_allowed(2, Permission.SPEAK)
    assert not priority_speaking_allowed(13, Permission(ALL_PERMISSIONS))
    assert priority_speaking_granted(
        channel_type=2,
        permissions=Permission(ALL_PERMISSIONS),
        client_kind="bot",
        can_speak=True,
    )


def test_voice_grants_and_metadata_roll_missing_priority_access_to_false() -> None:
    assert VoiceTokenResponse.model_validate(grant_payload()).can_priority_speak is False
    parsed = parse_minted_metadata(
        json.dumps(metadata_payload()),
        room="g.12.34",
        identity="78@alpha.localhost",
    )
    assert parsed.get("can_priority_speak", False) is False
    with pytest.raises(ValueError, match="metadata"):
        parse_minted_metadata(
            json.dumps(metadata_payload(can_priority_speak=1)),
            room="g.12.34",
            identity="78@alpha.localhost",
        )


def test_priority_voice_grant_requires_guild_speaking_access() -> None:
    assert VoiceTokenResponse.model_validate(
        grant_payload(can_priority_speak=True)
    ).can_priority_speak
    with pytest.raises(ValidationError, match="priority speaking"):
        VoiceTokenResponse.model_validate(grant_payload(can_priority_speak=True, can_speak=False))
    with pytest.raises(ValidationError, match="priority speaking"):
        VoiceTokenResponse.model_validate(
            grant_payload(
                room="d.34.56",
                guild_id=None,
                guild_domain=None,
                can_priority_speak=True,
            )
        )


def test_federation_projection_retains_priority_and_defaults_old_peers_false() -> None:
    occupant = Occupant(
        identity="78@alpha.localhost",
        user_id="78",
        user_domain="alpha.localhost",
        room="g.12.34",
        guild_id="12",
        channel_id="34",
        joined_at=1,
        connection_id="c" * 43,
        can_speak=True,
        can_priority_speak=True,
        participant_metadata={"generation": 3},
    )
    state = federation_occupant_state(occupant)
    assert public_occupant_state(occupant)["can_priority_speak"] is True
    assert occupant_from_federation_state(state).can_priority_speak is True
    state.pop("can_priority_speak")
    parsed = VoiceFederationOccupantState.model_validate(state)
    assert parsed.can_priority_speak is False
    assert occupant_from_federation_state(parsed.model_dump()).can_priority_speak is False
    with pytest.raises(ValidationError, match="priority speaking"):
        VoiceFederationOccupantState.model_validate(
            state | {"can_priority_speak": True, "can_speak": False}
        )


def test_livekit_data_publication_is_granted_only_for_priority_speakers() -> None:
    for allowed in (False, True):
        token, _ = mint_join_token(
            settings(),
            room="g.12.34",
            identity="78@alpha.localhost",
            display_name="Paper Lantern",
            metadata={"generation": 3, "user_domain": "alpha.localhost"},
            can_speak=True,
            can_stream=False,
            can_publish_data=allowed,
        )
        claims = jwt.decode(token, LIVEKIT_SECRET, algorithms=["HS256"])
        assert claims["video"]["canPublishData"] is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_type", "permissions", "voice_flags", "client_kind", "expected"),
    [
        (
            2,
            Permission.CONNECT | Permission.SPEAK | Permission.PRIORITY_SPEAKER,
            0,
            "desktop",
            True,
        ),
        (2, Permission.CONNECT | Permission.SPEAK, 0, "desktop", False),
        (
            2,
            Permission.CONNECT | Permission.SPEAK | Permission.PRIORITY_SPEAKER,
            1,
            "desktop",
            False,
        ),
        (13, Permission(ALL_PERMISSIONS), 0, "desktop", False),
        (2, Permission(ALL_PERMISSIONS), 0, "bot", True),
        (2, Permission(ALL_PERMISSIONS), 0, "web", False),
        (2, Permission(ALL_PERMISSIONS), 0, "mobile", False),
    ],
)
async def test_authoritative_grant_computes_priority_speaking_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    channel_type: int,
    permissions: Permission,
    voice_flags: int,
    client_kind: str,
    expected: bool,
) -> None:
    bot_client = client_kind == "bot"
    actor = SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        account_type="bot" if bot_client else "human",
        disabled_at=None,
        profile_resolved=True,
        display_name=None,
        username="speaker",
    )
    channel = SimpleNamespace(
        id=34,
        origin_domain="alpha.localhost",
        type=channel_type,
        encryption_mode="plaintext",
        encryption_state="disabled",
        encryption_policy_generation=0,
        encryption_epoch=None,
        bitrate=64_000,
        user_limit=0,
        rtc_region=None,
        video_quality_mode=1,
    )
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(voice_flags=voice_flags)
    control = SimpleNamespace(ensure_room=AsyncMock(), remove_participant=AsyncMock())
    captured: dict[str, object] = {}

    def mint(*_args: object, **kwargs: object) -> tuple[str, datetime]:
        captured.update(kwargs)
        return "x" * 32, datetime.now(UTC)

    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.require_permissions", AsyncMock(return_value=permissions)
    )
    monkeypatch.setattr("app.voice.service.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr(
        "app.voice.service.claim_voice_connection",
        AsyncMock(return_value=(True, 3, "", "")),
    )
    monkeypatch.setattr("app.voice.service.mint_join_token", mint)

    bot_installation = (
        BotInstallation(
            id=90,
            application_id=70,
            application_domain="alpha.localhost",
            guild_id=12,
            guild_domain="alpha.localhost",
            bot_user_id=78,
            bot_user_domain="alpha.localhost",
            installer_id=1,
            installer_domain="alpha.localhost",
            grant_revision=6,
        )
        if bot_client
        else None
    )
    bot_worker = (
        BotWorker(
            id=80,
            application_id=70,
            application_domain="alpha.localhost",
            name="priority speaker",
            public_key=b"w" * 32,
            scopes=["voice.connect", "voice.speak"],
            intents=["guild_voice_states"],
            target_domains=["alpha.localhost"],
        )
        if bot_client
        else None
    )

    grant = await authoritative_guild_token(
        session,
        AsyncMock(),
        settings(),
        channel=channel,
        guild=guild,
        actor=actor,
        bot_installation=bot_installation,
        bot_worker=bot_worker,
        connection_id="c" * 43,
        client_kind=client_kind,
    )

    assert grant.can_priority_speak is expected
    assert captured["can_publish_data"] is expected
    assert cast_dict(captured["metadata"])["can_priority_speak"] is expected


@pytest.mark.asyncio
async def test_suspended_actor_cannot_mint_priority_voice_grant() -> None:
    actor = SimpleNamespace(disabled_at=datetime.now(UTC))
    with pytest.raises(HTTPException) as raised:
        await authoritative_guild_token(
            AsyncMock(),
            AsyncMock(),
            settings(),
            channel=SimpleNamespace(origin_domain="alpha.localhost"),
            guild=SimpleNamespace(origin_domain="alpha.localhost"),
            actor=actor,
            connection_id="c" * 43,
        )
    assert raised.value.detail == {"code": "VOICE_DENIED"}


@pytest.mark.asyncio
async def test_permission_rotation_revokes_livekit_data_publication(
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
        connection_id="c" * 43,
        can_speak=True,
        can_priority_speak=True,
        participant_metadata={"generation": 3, "user_domain": "alpha.localhost"},
    )
    control = SimpleNamespace(update_participant=AsyncMock())
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.claim_voice_grant_transition", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("app.voice.service.current_generation", AsyncMock(return_value=3))
    monkeypatch.setattr("app.voice.service.rotate_occupant_grant", AsyncMock(return_value=4))
    monkeypatch.setattr("app.voice.service.release_voice_grant_transition", AsyncMock())
    monkeypatch.setattr(
        "app.voice.service.get_federated_voice_session", AsyncMock(return_value=None)
    )

    updated = await update_authoritative_occupant_grant(
        AsyncMock(),
        settings(),
        occupant,
        can_speak=True,
        can_stream=False,
        can_priority_speak=False,
    )

    assert updated.can_priority_speak is False
    assert updated.participant_metadata["can_priority_speak"] is False
    assert control.update_participant.await_args.kwargs["can_publish_data"] is False


def cast_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value
