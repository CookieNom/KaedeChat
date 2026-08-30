from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.bot_voice import bot_guild_voice_regions, bot_voice_regions
from app.api.bot_voice import router as bot_voice_router
from app.api.bots import require_bot_resource_authority
from app.api.guilds import voice_message_capability
from app.api.management import update_channel
from app.api.soundboard import (
    human_router as human_soundboard_router,
)
from app.api.soundboard import (
    probe_sound_duration_ms,
)
from app.api.soundboard import (
    router as soundboard_router,
)
from app.api.voice import router as human_voice_router
from app.api.voice import voice_regions as human_voice_regions
from app.chat.schemas import ChannelCreate, ChannelUpdate
from app.core.channel_types import is_soundboard_channel_type
from app.core.settings import VoiceRegionConfiguration
from app.core.types import EntityRef
from app.federation.guild_management import GuildManagementResult
from app.voice.regions import require_configured_rtc_region
from app.voice.schemas import (
    BotVoiceDisconnectRequest,
    BotVoiceTokenRequest,
    SoundboardSoundCreate,
    SoundboardSoundUpdate,
)
from app.voice.service import parse_minted_metadata


def test_voice_channel_settings_are_type_scoped_and_normalized() -> None:
    channel = ChannelCreate.model_validate(
        {
            "name": "Voice",
            "type": 2,
            "bitrate": 96_000,
            "user_limit": 12,
            "rtc_region": "  provider-region-1  ",
            "video_quality_mode": 2,
        }
    )
    assert channel.rtc_region == "provider-region-1"
    assert channel.bitrate == 96_000
    with pytest.raises(ValidationError, match="must be an integer"):
        ChannelCreate.model_validate({"name": "voice", "type": 2, "video_quality_mode": True})
    with pytest.raises(ValidationError, match="must be an integer"):
        ChannelUpdate.model_validate({"video_quality_mode": True})
    with pytest.raises(ValidationError, match="only valid for voice and Stage channels"):
        ChannelCreate.model_validate({"name": "text", "type": 0, "bitrate": 64_000})
    with pytest.raises(ValidationError, match="cannot be null"):
        ChannelCreate.model_validate({"name": "voice", "type": 2, "bitrate": None})
    assert ChannelUpdate.model_validate({"rtc_region": None}).rtc_region is None
    with pytest.raises(ValidationError, match="cannot be blank"):
        ChannelUpdate.model_validate({"rtc_region": "   "})
    with pytest.raises(ValidationError, match="at least one channel field"):
        ChannelUpdate.model_validate({"voice_status": "Office hours"})


@pytest.mark.asyncio
async def test_voice_regions_are_configured_typed_and_selectable() -> None:
    principal = SimpleNamespace(require_scope=Mock())
    configured = SimpleNamespace(
        voice_regions=[
            VoiceRegionConfiguration(
                id="provider-region-1",
                name="Provider Region 1",
                optimal=True,
                deprecated=False,
                custom=True,
            )
        ]
    )
    regions = await bot_voice_regions(principal, configured)  # type: ignore[arg-type]
    assert [region.model_dump() for region in regions] == [
        {
            "id": "provider-region-1",
            "name": "Provider Region 1",
            "optimal": True,
            "deprecated": False,
            "custom": True,
        }
    ]
    principal.require_scope.assert_called_once_with("voice.connect")
    assert require_configured_rtc_region(configured, None) is None  # type: ignore[arg-type]
    assert (
        require_configured_rtc_region(configured, "provider-region-1")  # type: ignore[arg-type]
        == "provider-region-1"
    )
    with pytest.raises(HTTPException) as invalid:
        require_configured_rtc_region(configured, "client-invented")  # type: ignore[arg-type]
    assert invalid.value.detail == {
        "code": "VOICE_REGION_INVALID",
        "rtc_region": "client-invented",
    }


@pytest.mark.asyncio
async def test_remote_human_voice_region_catalog_comes_from_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = AsyncMock(
        return_value=GuildManagementResult(
            request_id="kagm_" + "a" * 32,
            operation="voice.regions",
            guild={"id": "1", "domain": "guild.example"},
            status_code=200,
            body=[
                {
                    "id": "guild-edge",
                    "name": "Guild Edge",
                    "optimal": True,
                    "deprecated": False,
                    "custom": True,
                }
            ],
        )
    )
    monkeypatch.setattr("app.api.voice.proxy_remote_guild_management", upstream)
    auth = SimpleNamespace(user=SimpleNamespace(id=2, origin_domain="user.example"))
    regions = await human_voice_regions(
        EntityRef("1@guild.example"),
        auth,  # type: ignore[arg-type]
        AsyncMock(),
        SimpleNamespace(domain="user.example", voice_regions=[]),  # type: ignore[arg-type]
    )
    assert [region.id for region in regions] == ["guild-edge"]
    assert upstream.await_args.args[4] == "voice.regions"


@pytest.mark.asyncio
async def test_remote_voice_message_capability_is_authority_computed_without_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = AsyncMock(
        return_value=GuildManagementResult(
            request_id="kagm_" + "a" * 32,
            operation="voice_message.capability",
            guild={"id": "1", "domain": "guild.example"},
            status_code=200,
            body={"available": False},
        )
    )
    monkeypatch.setattr("app.api.guilds.proxy_remote_guild_management", upstream)
    auth = SimpleNamespace(user=SimpleNamespace(id=2, origin_domain="user.example"))
    result = await voice_message_capability(
        EntityRef("1@guild.example"),
        auth,  # type: ignore[arg-type]
        AsyncMock(),
        SimpleNamespace(domain="user.example"),  # type: ignore[arg-type]
    )
    assert result.model_dump() == {"available": False}
    assert upstream.await_args.args[4] == "voice_message.capability"


@pytest.mark.asyncio
async def test_remote_human_voice_region_override_runs_at_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "id": "2",
        "origin_domain": "guild.example",
        "guild_id": "1",
        "guild_domain": "guild.example",
        "type": 2,
        "name": "Voice",
        "rtc_region": "guild-edge",
    }
    upstream = AsyncMock(
        return_value=GuildManagementResult(
            request_id="kagm_" + "a" * 32,
            operation="channel.update",
            guild={"id": "1", "domain": "guild.example"},
            status_code=200,
            body=expected,
        )
    )
    monkeypatch.setattr("app.api.management.proxy_remote_guild_management", upstream)
    auth = SimpleNamespace(user=SimpleNamespace(id=3, origin_domain="user.example"))

    result = await update_channel(
        EntityRef("1@guild.example"),
        EntityRef("2@guild.example"),
        ChannelUpdate(rtc_region="guild-edge"),
        auth,  # type: ignore[arg-type]
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        SimpleNamespace(domain="user.example"),  # type: ignore[arg-type]
        '"2026-08-28T12:00:00+00:00"',
    )

    assert result == expected
    assert upstream.await_args.args[4:] == (
        "channel.update",
        {
            "channel_ref": "2@guild.example",
            "data": {"rtc_region": "guild-edge"},
            "if_match": '"2026-08-28T12:00:00+00:00"',
            "reason": None,
        },
    )


@pytest.mark.asyncio
async def test_bot_guild_voice_regions_rechecks_authoritative_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize = AsyncMock(return_value=(object(), object()))
    monkeypatch.setattr("app.api.bot_voice.installation_for_guild", authorize)
    principal = SimpleNamespace()
    settings = SimpleNamespace(
        domain="guild.example",
        voice_regions=[VoiceRegionConfiguration(id="edge", name="Edge")],
    )
    regions = await bot_guild_voice_regions(
        EntityRef("1@guild.example"),
        principal,  # type: ignore[arg-type]
        AsyncMock(),
        settings,  # type: ignore[arg-type]
    )
    assert [region.id for region in regions] == ["edge"]
    assert authorize.await_args.args[-1] == "voice.connect"


def test_bot_resource_authority_rejects_replica_mutation() -> None:
    with pytest.raises(HTTPException) as rejected:
        require_bot_resource_authority(
            SimpleNamespace(domain="replica.example"),  # type: ignore[arg-type]
            resource_domain="guild.example",
            resource_ref=EntityRef("1@guild.example"),
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail == {
        "code": "BOT_RESOURCE_AUTHORITY_REQUIRED",
        "resource_ref": "1@guild.example",
        "authority_domain": "guild.example",
    }


def test_voice_region_routes_exist_for_humans_and_bots() -> None:
    assert "/api/v1/bots/voice/regions" in {route.path for route in bot_voice_router.routes}
    assert "/api/v1/bots/guilds/{guild_ref}/voice/regions" in {
        route.path for route in bot_voice_router.routes
    }
    assert "/api/v1/voice/regions" in {route.path for route in human_voice_router.routes}


def test_bot_voice_and_soundboard_requests_are_fail_closed() -> None:
    grant = BotVoiceTokenRequest.model_validate({})
    assert not grant.listen and not grant.speak and not grant.stream
    sound = SoundboardSoundCreate.model_validate(
        {"attachment_id": "12", "name": "  Air horn  ", "volume": 0.5}
    )
    assert sound.name == "Air horn"
    with pytest.raises(ValidationError, match="mutually exclusive"):
        SoundboardSoundCreate.model_validate(
            {
                "attachment_id": "12",
                "name": "horn",
                "emoji_id": "13",
                "emoji_name": "📣",
            }
        )
    cleared = SoundboardSoundUpdate.model_validate({"emoji_id": None})
    assert cleared.model_fields_set == {"emoji_id"}


@pytest.mark.parametrize("field", ["generation"])
def test_voice_numeric_request_fields_reject_boolean_coercion(field: str) -> None:
    with pytest.raises(ValidationError, match=f"{field} must be an integer"):
        BotVoiceDisconnectRequest.model_validate({"connection_id": "a" * 43, field: True})


def test_soundboard_playback_is_voice_only_not_stage() -> None:
    assert is_soundboard_channel_type(2)
    assert not is_soundboard_channel_type(13)
    assert not is_soundboard_channel_type(1)


def test_soundboard_routes_match_the_bot_sdk_contract() -> None:
    paths = {route.path for route in soundboard_router.routes}
    assert "/api/v1/bots/guilds/{guild_ref}/soundboard-sounds" in paths
    assert "/api/v1/bots/guilds/{guild_ref}/soundboard-sounds/tickets" in paths
    assert "/api/v1/bots/channels/{channel_ref}/send-soundboard-sound" in paths
    assert "/api/v1/bots/channels/{channel_ref}/soundboard-playback-grants" in paths
    assert not any(path.startswith("/api/v1/guilds/") for path in paths)
    playback = next(
        route
        for route in soundboard_router.routes
        if route.path == "/api/v1/bots/channels/{channel_ref}/send-soundboard-sound"
    )
    grant = next(
        route
        for route in soundboard_router.routes
        if route.path == "/api/v1/bots/channels/{channel_ref}/soundboard-playback-grants"
    )
    assert playback.status_code == 204
    assert grant.status_code is None


def test_soundboard_routes_expose_complete_human_management_and_playback() -> None:
    methods = {
        (route.path, method)
        for route in human_soundboard_router.routes
        for method in (route.methods or set())
    }
    base = "/api/v1/guilds/{guild_ref}/soundboard-sounds"
    assert (base, "GET") in methods
    assert (base, "POST") in methods
    assert (f"{base}/tickets", "POST") in methods
    assert (f"{base}/{{sound_ref}}", "GET") in methods
    assert (f"{base}/{{sound_ref}}", "PATCH") in methods
    assert (f"{base}/{{sound_ref}}", "DELETE") in methods
    assert (
        "/api/v1/channels/{channel_ref}/send-soundboard-sound",
        "POST",
    ) in methods
    playback = next(
        route
        for route in human_soundboard_router.routes
        if route.path == "/api/v1/channels/{channel_ref}/send-soundboard-sound"
    )
    assert playback.status_code == 204


def test_bot_client_kind_survives_livekit_admission_validation() -> None:
    metadata = {
        "generation": 1,
        "connection_id": "a" * 43,
        "client_kind": "bot",
        "user_id": "7",
        "user_domain": "alpha.localhost",
        "bot_application_id": "4",
        "bot_application_domain": "alpha.localhost",
        "bot_worker_id": 8,
        "bot_installation_id": 9,
        "bot_installation_revision": 1,
        "guild_id": "5",
        "channel_id": "6",
        "channel_domain": "alpha.localhost",
        "e2ee": False,
        "can_speak": True,
        "can_stream": False,
        "can_use_vad": True,
        "server_mute": False,
        "server_deaf": False,
    }
    parsed = parse_minted_metadata(json.dumps(metadata), room="g.5.6", identity="7@alpha.localhost")
    assert parsed["client_kind"] == "bot"


@pytest.mark.asyncio
async def test_soundboard_duration_probe_enforces_discord_bound(monkeypatch: Any) -> None:
    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"5.200\n", b""

    async def create_process(*args: object, **kwargs: object) -> Process:
        del args, kwargs
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    assert await probe_sound_duration_ms(b"OggS-audio", "audio/ogg") == 5_200

    class TooLong(Process):
        async def communicate(self) -> tuple[bytes, bytes]:
            return b"5.201\n", b""

    async def create_long_process(*args: object, **kwargs: object) -> TooLong:
        del args, kwargs
        return TooLong()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_long_process)
    with pytest.raises(HTTPException) as exc:
        await probe_sound_duration_ms(b"OggS-audio", "audio/ogg")
    assert exc.value.detail["code"] == "SOUNDBOARD_DURATION_INVALID"
