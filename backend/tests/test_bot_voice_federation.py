from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.bot_voice import bot_channel_voice_token, bot_disconnect_voice
from app.api.calls import bot_call_response
from app.core.settings import Settings
from app.core.types import EntityRef
from app.voice.broker import request_remote_guild_voice_token
from app.voice.schemas import (
    BotCallResponse,
    BotVoiceTokenRequest,
    VoiceBrokerRequest,
    VoiceTokenResponse,
)
from app.voice.state import BOT_CAPABILITY_BINDINGS_FIELD, FederatedVoiceSession


def settings() -> Settings:
    return Settings(
        domain="alpha.localhost",
        environment="test",
        secret_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
        voice_enabled=True,
        voice_api_key="LKtestkey",
        voice_api_secret="livekit-test-secret-000000000000000000000000000000000000000",
        voice_public_url="wss://alpha.localhost/livekit",
    )


def remote_channel() -> SimpleNamespace:
    return SimpleNamespace(
        id=34,
        type=2,
        guild_id=12,
        guild_domain="beta.localhost",
        origin_domain="beta.localhost",
        parent_id=None,
        parent_domain=None,
        encryption_mode="plaintext",
        e2ee_required=False,
        unavailable=False,
    )


def remote_guild() -> SimpleNamespace:
    return SimpleNamespace(id=12, origin_domain="beta.localhost", unavailable=False)


def bot_actor() -> SimpleNamespace:
    return SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        account_type="bot",
        disabled_at=None,
    )


def human_actor() -> SimpleNamespace:
    return SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        account_type="human",
        disabled_at=None,
    )


def voice_grant(*, move_session_id: str = "m" * 43) -> VoiceTokenResponse:
    return VoiceTokenResponse(
        token="x" * 32,
        url="wss://beta.localhost/livekit",
        room="g.12.34",
        generation=7,
        connection_id="c" * 43,
        expires_at="2026-08-28T12:00:00+00:00",
        can_speak=True,
        can_stream=False,
        can_listen=False,
        e2ee=False,
        channel_id="34",
        channel_domain="beta.localhost",
        move_session_id=move_session_id,
    )


def test_bot_call_projection_retains_only_exact_capability_lineage() -> None:
    grant_id = "kbdg_" + "a" * 43
    bot_identity = "78@apps.localhost"
    capability = SimpleNamespace(
        bot_user_id=78,
        bot_user_domain="apps.localhost",
        grant_id=grant_id,
        revision=4,
        source_installation_id=60,
        source_installation_domain="guilds.localhost",
        source_kind="guild",
    )
    record = {
        "id": "99",
        "channel_id": "55",
        "channel_domain": "alpha.localhost",
        "authority_domain": "alpha.localhost",
        "room": "d.55.99",
        "state": "active",
        "created_at": 1,
        "ended_at": None,
        "caller": bot_identity,
        "participants": [bot_identity, "8@users.localhost"],
        BOT_CAPABILITY_BINDINGS_FIELD: {bot_identity: {"grant_id": grant_id, "revision": 4}},
        "private_internal_field": "must-not-leak",
    }

    projection = bot_call_response(record, cast(Any, capability))

    assert isinstance(projection, BotCallResponse)
    assert projection.bot_dm_capability_id == grant_id
    assert projection.bot_dm_capability_revision == "4"
    assert projection.bot_installation_ref == "60@guilds.localhost"
    assert projection.bot_installation_type == "guild"
    assert "private_internal_field" not in projection.model_dump()

    capability.revision = 5
    with pytest.raises(HTTPException) as mismatch:
        bot_call_response(record, cast(Any, capability))
    assert mismatch.value.detail == {"code": "BOT_DM_CALL_GRANT_MISMATCH"}


def test_instance_voice_broker_rejects_bot_client_kind() -> None:
    with pytest.raises(ValidationError, match="web.*desktop.*mobile"):
        VoiceBrokerRequest.model_validate(
            {
                "guild_id": "12",
                "channel_id": "34",
                "actor_id": "78",
                "actor_domain": "alpha.localhost",
                "move_session_id": "m" * 43,
                "connection_id": "c" * 43,
                "client_kind": "bot",
            }
        )


@pytest.mark.asyncio
async def test_remote_bot_token_requires_direct_authority_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = HTTPException(
        status_code=409,
        detail={
            "code": "BOT_RESOURCE_AUTHORITY_REQUIRED",
            "authority_domain": "beta.localhost",
        },
    )
    authorize = AsyncMock(side_effect=rejected)
    mint = AsyncMock()
    monkeypatch.setattr("app.api.bot_voice.installation_for_channel", authorize)
    monkeypatch.setattr("app.api.bot_voice.authoritative_guild_token", mint)

    with pytest.raises(HTTPException) as failure:
        await bot_channel_voice_token(
            EntityRef("34@beta.localhost"),
            BotVoiceTokenRequest(connection_id="c" * 43, speak=True),
            cast(Any, SimpleNamespace(user=bot_actor())),
            AsyncMock(),
            AsyncMock(),
            settings(),
        )

    assert failure.value is rejected
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_remote_voice_broker_is_human_only_and_binds_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = remote_channel()
    guild = remote_guild()
    actor = human_actor()
    grant = voice_grant()
    signed = AsyncMock(return_value=httpx.Response(200, json=grant.model_dump(mode="json")))
    begin = AsyncMock()
    activate = AsyncMock(return_value=True)
    monkeypatch.setattr("app.voice.broker.secrets.token_urlsafe", lambda _size: "m" * 43)
    monkeypatch.setattr("app.voice.broker.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr("app.voice.broker.begin_federated_voice_home_session", begin)
    monkeypatch.setattr("app.voice.broker.activate_federated_voice_home_session", activate)
    monkeypatch.setattr("app.voice.broker.signed_request", signed)

    result = await request_remote_guild_voice_token(
        AsyncMock(),
        AsyncMock(),
        settings(),
        channel=cast(Any, channel),
        guild=cast(Any, guild),
        actor=cast(Any, actor),
        sender_device_id=None,
        connection_id="c" * 43,
        takeover=False,
        client_kind="web",
    )

    assert result == grant
    sent = signed.await_args.kwargs["payload"]
    assert sent["client_kind"] == "web"
    assert sent["allow_listen"] is True
    assert sent["allow_speak"] is True
    assert sent["allow_stream"] is True
    pending = begin.await_args.args[2]
    assert pending == FederatedVoiceSession(
        authority_domain="beta.localhost",
        guild_id="12",
        room="g.12.34",
        generation=0,
        move_session_id="m" * 43,
    )
    assert activate.await_args.kwargs["generation"] == 7


@pytest.mark.asyncio
async def test_remote_bot_disconnect_requires_direct_authority_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = HTTPException(
        status_code=409,
        detail={"code": "BOT_RESOURCE_AUTHORITY_REQUIRED"},
    )
    authorize = AsyncMock(side_effect=rejected)
    monkeypatch.setattr("app.api.bot_voice.installation_for_channel", authorize)

    with pytest.raises(HTTPException) as failure:
        await bot_disconnect_voice(
            EntityRef("34@beta.localhost"),
            cast(Any, SimpleNamespace(connection_id="c" * 43, generation=7)),
            cast(Any, SimpleNamespace(user=bot_actor())),
            AsyncMock(),
            AsyncMock(),
            settings(),
        )

    assert failure.value is rejected
