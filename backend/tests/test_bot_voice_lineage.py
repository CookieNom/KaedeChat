from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.api.bot_voice as bot_voice_api
from app.bots.auth import BotPrincipal
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.bot_models import (
    BotApplication,
    BotDMCapability,
    BotInstallation,
    BotWorker,
)
from app.voice.e2ee import bot_voice_lineage_metadata, evict_bot_voice_runtime_sessions
from app.voice.schemas import BotVoiceDisconnectRequest, BotVoiceSelfStateRequest
from app.voice.service import parse_minted_metadata
from app.voice.state import Occupant


def settings() -> Settings:
    return Settings(
        domain="guilds.example",
        environment="test",
        secret_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
        voice_enabled=True,
        voice_api_key="LKtestkey",
        voice_api_secret="livekit-test-secret-000000000000000000000000000000000000000",
        voice_public_url="wss://guilds.example/livekit",
    )


def application() -> BotApplication:
    return BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=1,
        name="weather",
        description="weather bot",
        bot_user_id=10,
        bot_user_domain="apps.example",
        status="active",
        manifest_generation=1,
        revocation_generation=1,
    )


def worker(*, worker_id: int = 900) -> BotWorker:
    return BotWorker(
        id=worker_id,
        source_id=40,
        source_domain="apps.example",
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        generation=3,
        scopes=[],
        intents=[],
        target_domains=["guilds.example"],
    )


def guild_installation() -> BotInstallation:
    return BotInstallation(
        id=30,
        application_id=20,
        application_domain="apps.example",
        guild_id=50,
        guild_domain="guilds.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        installer_id=60,
        installer_domain="guilds.example",
        grant_revision=7,
    )


def dm_capability() -> BotDMCapability:
    return BotDMCapability(
        id=31,
        grant_id="kbdg_" + "g" * 43,
        source_kind="user",
        source_installation_id=80,
        source_installation_domain="users.example",
        application_id=20,
        application_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        target_user_id=60,
        target_user_domain="users.example",
        authority_domain="guilds.example",
        conversation_id=70,
        conversation_domain="guilds.example",
        revision=9,
        status="active",
    )


def guild_lineage(*, worker_id: int = 900) -> dict[str, object]:
    return bot_voice_lineage_metadata(
        worker(worker_id=worker_id),
        guild_installation(),
    )


def principal() -> BotPrincipal:
    return cast(
        BotPrincipal,
        SimpleNamespace(
            user=SimpleNamespace(id=10, origin_domain="apps.example"),
            worker=worker(),
        ),
    )


def occupant(
    lineage: dict[str, object],
    *,
    room: str = "g.50.70",
    guild_id: str | None = "50",
) -> Occupant:
    return Occupant(
        identity="10@apps.example",
        user_id="10",
        user_domain="apps.example",
        room=room,
        guild_id=guild_id,
        channel_id="70",
        joined_at=1,
        connection_id="c" * 43,
        client_kind="bot",
        participant_metadata={"generation": 4, **lineage},
    )


def test_voice_metadata_uses_local_worker_surrogate_not_colliding_source_id() -> None:
    lineage = guild_lineage()

    assert lineage["bot_worker_id"] == 900
    assert lineage["bot_worker_id"] != 40
    parsed = parse_minted_metadata(
        json.dumps(
            {
                "generation": 4,
                "connection_id": "c" * 43,
                "client_kind": "bot",
                "user_id": "10",
                "user_domain": "apps.example",
                "guild_id": "50",
                "channel_id": "70",
                "channel_domain": "guilds.example",
                "e2ee": False,
                "can_speak": True,
                "can_stream": False,
                "can_use_vad": True,
                "server_mute": False,
                "server_deaf": False,
                **lineage,
            }
        ),
        room="g.50.70",
        identity="10@apps.example",
    )
    assert parsed["bot_worker_id"] == 900


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel_type", "accepted"), [(13, True), (2, False)])
async def test_bot_stage_speaking_uses_stage_state_not_voice_only_speak_permission(
    monkeypatch: pytest.MonkeyPatch,
    channel_type: int,
    accepted: bool,
) -> None:
    installation = guild_installation()
    installation.granted_scopes = ["voice.connect", "voice.speak"]
    channel = SimpleNamespace(
        id=70,
        origin_domain="guilds.example",
        type=channel_type,
        guild_id=50,
        guild_domain="guilds.example",
    )
    current_guild = SimpleNamespace(id=50, origin_domain="guilds.example", unavailable=False)
    permissions = (
        Permission.VIEW_CHANNEL
        | Permission.CONNECT
        | Permission.MANAGE_CHANNELS
        | Permission.MUTE_MEMBERS
        | Permission.MOVE_MEMBERS
    )
    mint = AsyncMock(return_value=SimpleNamespace(can_speak=True))
    monkeypatch.setattr(
        bot_voice_api,
        "installation_for_channel",
        AsyncMock(return_value=(channel, installation)),
    )
    monkeypatch.setattr(bot_voice_api, "get_permissions", AsyncMock(return_value=permissions))
    monkeypatch.setattr(bot_voice_api, "authoritative_guild_token", mint)
    bot_principal = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=10, origin_domain="apps.example"),
            worker=worker(),
            require_scope=lambda _scope: None,
        ),
    )
    session = cast(Any, SimpleNamespace(get=AsyncMock(return_value=current_guild)))

    if accepted:
        await bot_voice_api.bot_channel_voice_token(
            EntityRef("70@guilds.example"),
            bot_voice_api.BotVoiceTokenRequest(connection_id="c" * 43, speak=True),
            bot_principal,
            session,
            cast(Any, SimpleNamespace()),
            settings(),
        )
        mint.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as denied:
            await bot_voice_api.bot_channel_voice_token(
                EntityRef("70@guilds.example"),
                bot_voice_api.BotVoiceTokenRequest(connection_id="c" * 43, speak=True),
                bot_principal,
                session,
                cast(Any, SimpleNamespace()),
                settings(),
            )
        assert denied.value.detail["code"] == "MISSING_PERMISSIONS"
        mint.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("joined", [False, True])
async def test_runtime_revoke_fences_prejoin_and_joined_exact_worker(
    monkeypatch: pytest.MonkeyPatch,
    joined: bool,
) -> None:
    connection_id = "c" * 43
    lineage = guild_lineage()
    session = SimpleNamespace(get=AsyncMock(return_value=application()))
    claim = {
        "connection_id": connection_id,
        "room": "g.50.70",
        "generation": 4,
        "client_kind": "bot",
        "bot_lineage": json.dumps(lineage, sort_keys=True, separators=(",", ":")),
    }
    occupant = (
        Occupant(
            identity="10@apps.example",
            user_id="10",
            user_domain="apps.example",
            room="g.50.70",
            guild_id="50",
            channel_id="70",
            joined_at=1,
            connection_id=connection_id,
        )
        if joined
        else None
    )
    control = SimpleNamespace(remove_participant=AsyncMock())
    redis = cast(Any, SimpleNamespace())
    bump = AsyncMock(return_value=5)
    release = AsyncMock(return_value=True)
    remove = AsyncMock(return_value=True)
    wake = AsyncMock()
    monkeypatch.setattr(
        "app.voice.e2ee.bot_guild_voice_connection_claims",
        AsyncMock(return_value=[claim]),
    )
    monkeypatch.setattr("app.voice.e2ee.voice_connection_claim", AsyncMock(return_value=None))
    monkeypatch.setattr("app.voice.e2ee.occupant_in_room", AsyncMock(return_value=occupant))
    monkeypatch.setattr("app.voice.e2ee.bump_generation", bump)
    monkeypatch.setattr("app.voice.e2ee.release_voice_connection", release)
    monkeypatch.setattr("app.voice.e2ee.remove_occupant_connection", remove)
    monkeypatch.setattr("app.voice.e2ee.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr("app.core.task_wake.enqueue_best_effort", wake)

    changed = await evict_bot_voice_runtime_sessions(
        cast(Any, session),
        redis,
        settings(),
        application_ref=(20, "apps.example"),
        worker_ids=(900,),
    )

    assert changed == {"g.50.70"}
    bump.assert_awaited_once_with(
        redis,
        "guilds.example",
        "g.50.70",
        "10@apps.example",
    )
    release.assert_awaited_once()
    remove.assert_awaited_once()
    if joined:
        control.remove_participant.assert_awaited_once_with("g.50.70", "10@apps.example")
    else:
        control.remove_participant.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_revoke_preserves_another_worker_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = {
        "connection_id": "c" * 43,
        "room": "g.50.70",
        "generation": 4,
        "client_kind": "bot",
        "bot_lineage": json.dumps(
            guild_lineage(worker_id=901),
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    bump = AsyncMock()
    monkeypatch.setattr(
        "app.voice.e2ee.bot_guild_voice_connection_claims",
        AsyncMock(return_value=[claim]),
    )
    monkeypatch.setattr("app.voice.e2ee.voice_connection_claim", AsyncMock(return_value=None))
    monkeypatch.setattr("app.voice.e2ee.bump_generation", bump)

    changed = await evict_bot_voice_runtime_sessions(
        cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(return_value=application()),
                execute=AsyncMock(return_value=[]),
            ),
        ),
        cast(Any, SimpleNamespace()),
        settings(),
        application_ref=(20, "apps.example"),
        worker_ids=(900,),
    )

    assert changed == set()
    bump.assert_not_awaited()


def simultaneous_guild_claims() -> list[dict[str, object]]:
    first_lineage = guild_lineage()
    second_lineage = {**first_lineage, "bot_installation_id": 31}
    return [
        {
            "connection_id": "a" * 43,
            "room": "g.50.70",
            "generation": 4,
            "client_kind": "bot",
            "bot_lineage": json.dumps(first_lineage, sort_keys=True, separators=(",", ":")),
        },
        {
            "connection_id": "b" * 43,
            "room": "g.51.71",
            "generation": 6,
            "client_kind": "bot",
            "bot_lineage": json.dumps(second_lineage, sort_keys=True, separators=(",", ":")),
        },
    ]


@pytest.mark.asyncio
async def test_installation_revoke_evicts_only_its_simultaneous_guild_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = simultaneous_guild_claims()
    redis = cast(Any, SimpleNamespace())
    bump = AsyncMock(return_value=7)
    release = AsyncMock(return_value=True)
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.voice.e2ee.bot_guild_voice_connection_claims",
        AsyncMock(return_value=claims),
    )
    monkeypatch.setattr("app.voice.e2ee.voice_connection_claim", AsyncMock(return_value=None))
    monkeypatch.setattr("app.voice.e2ee.occupant_in_room", AsyncMock(return_value=None))
    monkeypatch.setattr("app.voice.e2ee.bump_generation", bump)
    monkeypatch.setattr("app.voice.e2ee.release_voice_connection", release)
    monkeypatch.setattr("app.voice.e2ee.remove_occupant_connection", remove)
    monkeypatch.setattr(
        "app.voice.e2ee.LiveKitControl",
        lambda _settings: SimpleNamespace(remove_participant=AsyncMock()),
    )
    monkeypatch.setattr("app.core.task_wake.enqueue_best_effort", AsyncMock())

    changed = await evict_bot_voice_runtime_sessions(
        cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(return_value=application()),
                execute=AsyncMock(return_value=[]),
            ),
        ),
        redis,
        settings(),
        application_ref=(20, "apps.example"),
        installation_ids=(30,),
    )

    assert changed == {"g.50.70"}
    bump.assert_awaited_once_with(redis, "guilds.example", "g.50.70", "10@apps.example")
    release.assert_awaited_once_with(
        redis,
        "guilds.example",
        "10@apps.example",
        "a" * 43,
        room="g.50.70",
        generation=4,
        client_kind="bot",
    )


@pytest.mark.asyncio
async def test_application_revoke_evicts_all_simultaneous_guild_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = simultaneous_guild_claims()
    redis = cast(Any, SimpleNamespace())
    bump = AsyncMock(return_value=7)
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.voice.e2ee.bot_guild_voice_connection_claims",
        AsyncMock(return_value=claims),
    )
    monkeypatch.setattr("app.voice.e2ee.voice_connection_claim", AsyncMock(return_value=None))
    monkeypatch.setattr("app.voice.e2ee.occupant_in_room", AsyncMock(return_value=None))
    monkeypatch.setattr("app.voice.e2ee.bump_generation", bump)
    monkeypatch.setattr("app.voice.e2ee.release_voice_connection", release)
    monkeypatch.setattr("app.voice.e2ee.remove_occupant_connection", AsyncMock())
    monkeypatch.setattr(
        "app.voice.e2ee.LiveKitControl",
        lambda _settings: SimpleNamespace(remove_participant=AsyncMock()),
    )
    monkeypatch.setattr("app.core.task_wake.enqueue_best_effort", AsyncMock())

    changed = await evict_bot_voice_runtime_sessions(
        cast(Any, SimpleNamespace(get=AsyncMock(return_value=application()))),
        redis,
        settings(),
        application_ref=(20, "apps.example"),
    )

    assert changed == {"g.50.70", "g.51.71"}
    assert bump.await_count == 2
    assert release.await_count == 2
    assert {call.kwargs["room"] for call in release.await_args_list} == {
        "g.50.70",
        "g.51.71",
    }


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("bot_worker_id", 901),
        ("bot_installation_id", 31),
        ("bot_installation_revision", 8),
    ],
)
def test_guild_voice_control_requires_exact_occupant_lineage(
    field: str,
    wrong_value: object,
) -> None:
    installation = guild_installation()
    exact = occupant(bot_voice_lineage_metadata(worker(), installation))
    assert bot_voice_api._occupant_has_exact_bot_lineage(
        exact,
        principal(),
        installation,
    )

    exact.participant_metadata[field] = wrong_value
    assert not bot_voice_api._occupant_has_exact_bot_lineage(
        exact,
        principal(),
        installation,
    )


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("bot_worker_id", 901),
        ("bot_dm_capability_grant_id", "kbdg_" + "x" * 43),
        ("bot_dm_capability_revision", 8),
    ],
)
def test_dm_voice_control_requires_exact_occupant_lineage(
    field: str,
    wrong_value: object,
) -> None:
    capability = dm_capability()
    exact = occupant(bot_voice_lineage_metadata(worker(), capability))
    assert bot_voice_api._occupant_has_exact_bot_lineage(
        exact,
        principal(),
        capability,
    )

    exact.participant_metadata[field] = wrong_value
    assert not bot_voice_api._occupant_has_exact_bot_lineage(
        exact,
        principal(),
        capability,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["self_state", "disconnect"])
@pytest.mark.parametrize("grant_kind", ["guild", "dm"])
async def test_bot_voice_controls_reject_another_workers_session(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    grant_kind: str,
) -> None:
    installation: BotInstallation | BotDMCapability
    if grant_kind == "guild":
        installation = guild_installation()
        channel = SimpleNamespace(
            id=70,
            origin_domain="guilds.example",
            type=2,
            guild_id=50,
            guild_domain="guilds.example",
        )
        room = "g.50.70"
    else:
        installation = dm_capability()
        channel = SimpleNamespace(
            id=70,
            origin_domain="guilds.example",
            type=1,
            guild_id=None,
            guild_domain=None,
        )
        room = "d.70.80"
    foreign = occupant(
        bot_voice_lineage_metadata(worker(worker_id=901), installation),
        room=room,
        guild_id="50" if grant_kind == "guild" else None,
    )
    connection_matches = AsyncMock(return_value=True)
    monkeypatch.setattr(
        bot_voice_api,
        "installation_for_channel",
        AsyncMock(return_value=(channel, installation)),
    )
    monkeypatch.setattr(bot_voice_api, "occupant_in_room", AsyncMock(return_value=foreign))
    monkeypatch.setattr(bot_voice_api, "voice_connection_matches", connection_matches)
    monkeypatch.setattr(
        bot_voice_api,
        "get_active_call",
        AsyncMock(return_value={"state": "active", "room": room}),
    )
    monkeypatch.setattr(bot_voice_api, "require_call_bot_capability", lambda *_args: None)
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(unavailable=False)))
    payload: BotVoiceDisconnectRequest
    handler: Any
    if operation == "self_state":
        payload = BotVoiceSelfStateRequest(
            connection_id="c" * 43,
            generation=4,
            self_mute=False,
            self_deaf=False,
        )
        handler = bot_voice_api.bot_update_voice_self_state
    else:
        payload = BotVoiceDisconnectRequest(connection_id="c" * 43, generation=4)
        handler = bot_voice_api.bot_disconnect_voice

    with pytest.raises(HTTPException) as exc:
        await handler(
            EntityRef("70@guilds.example"),
            payload,
            principal(),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            settings(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "VOICE_SESSION_SUPERSEDED"}
    connection_matches.assert_not_awaited()
