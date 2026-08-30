from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.audio import AudioFrame, FFmpegAudioSource, PCM16AudioSource
from kaede_bot.client import Client
from kaede_bot.e2ee import E2EEProtocolError
from kaede_bot.generated import GatewayOp
from kaede_bot.generated import (
    PRIORITY_SPEAKER_ACTIVE_PAYLOAD,
    PRIORITY_SPEAKER_INACTIVE_PAYLOAD,
    PRIORITY_SPEAKER_TOPIC,
)
from kaede_bot.models import (
    Channel,
    ChannelInfoEvent,
    GuildMembersChunkEvent,
    PresenceEvent,
    RawEvent,
)
from kaede_bot.refs import EntityRef
from kaede_bot.soundboard import SoundboardSound, _validate_soundboard_download_url
from kaede_bot.state import WorkerState
from kaede_bot.voice import (
    LiveKitTransport,
    VideoFrame,
    VoiceClient,
    VoiceGrant,
)


def test_soundboard_sdk_uses_kaede_playback_grant_extension() -> None:
    source = inspect.getsource(SoundboardSound.play)

    assert "/soundboard-playback-grants" in source
    assert "/send-soundboard-sound" not in source


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def soundboard_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "4",
        "origin_domain": "chat.example",
        "guild_id": "1",
        "guild_domain": "chat.example",
        "name": "Horn",
        "media_hash": "a" * 64,
        "content_type": "audio/ogg",
        "volume": 0.75,
        "duration_ms": 900,
        "emoji_id": None,
        "emoji_domain": None,
        "emoji_name": None,
        "available": True,
        "created_by_id": "2",
        "created_by_domain": "users.example",
        "version": "2",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "override",
    [
        {"name": 7},
        {"media_hash": True},
        {"media_hash": "A" * 64},
        {"content_type": "audio/wav"},
        {"volume": True},
        {"volume": "0.75"},
        {"volume": float("nan")},
        {"volume": float("inf")},
        {"volume": -0.1},
        {"volume": 1.1},
        {"duration_ms": True},
        {"duration_ms": "900"},
        {"duration_ms": 900.5},
        {"duration_ms": 0},
        {"duration_ms": 5_201},
        {"emoji_name": 7},
        {"available": 1},
        {"version": True},
        {"version": 2},
        {"version": "01"},
        {"version": "0"},
        {"guild_domain": "other.example"},
        {"user": []},
        {
            "user": {
                "id": "3",
                "origin_domain": "users.example",
                "username": "mallory",
            }
        },
    ],
)
def test_soundboard_parser_rejects_ambiguous_or_substituted_values(
    override: dict[str, object],
) -> None:
    payload = soundboard_payload(**override)

    with pytest.raises(ValueError):
        SoundboardSound.from_payload(client(), "https://chat.example", payload)


def test_soundboard_parser_rejects_partial_and_conflicting_emoji_refs() -> None:
    partial = soundboard_payload()
    partial.pop("emoji_domain")
    with pytest.raises(ValueError, match="incomplete emoji reference"):
        SoundboardSound.from_payload(client(), "https://chat.example", partial)

    conflicting = soundboard_payload(
        emoji_id="5",
        emoji_domain="chat.example",
        emoji_name="👋",
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        SoundboardSound.from_payload(client(), "https://chat.example", conflicting)


def test_soundboard_parser_preserves_legacy_and_remote_creator_refs() -> None:
    legacy = soundboard_payload()
    for key in (
        "emoji_id",
        "emoji_domain",
        "emoji_name",
        "created_by_id",
        "created_by_domain",
        "available",
        "version",
        "volume",
    ):
        legacy.pop(key)
    parsed_legacy = SoundboardSound.from_payload(
        client(), "https://chat.example", legacy
    )
    assert parsed_legacy.emoji_ref is None
    assert parsed_legacy.creator_ref is None
    assert parsed_legacy.available
    assert parsed_legacy.version == 1
    assert parsed_legacy.volume == 1

    remote = soundboard_payload(
        origin_domain="remote.example",
        guild_domain="remote.example",
        created_by_domain="users.example",
        user={
            "id": "2",
            "origin_domain": "users.example",
            "username": "alice",
        },
    )
    parsed_remote = SoundboardSound.from_payload(
        client(), "https://remote.example", remote
    )
    assert parsed_remote.ref == EntityRef(4, "remote.example")
    assert parsed_remote.guild_ref == EntityRef(1, "remote.example")
    assert parsed_remote.creator_ref == EntityRef(2, "users.example")


@pytest.mark.asyncio
async def test_voice_state_control_is_bound_to_the_connection_target() -> None:
    bot = client()
    voice = SimpleNamespace(
        target="https://one.example",
        grant=SimpleNamespace(generation=4),
        authority_disconnect=AsyncMock(),
        apply_authority_state=AsyncMock(),
    )
    bot._voice_clients["connection"] = voice  # noqa: SLF001

    with pytest.raises(E2EEProtocolError, match="another target"):
        await bot._handle_authoritative_voice_event(  # noqa: SLF001
            "VOICE_STATE_UPDATE",
            {
                "connection_id": "connection",
                "generation": 4,
                "connected": False,
            },
            "https://two.example",
        )

    voice.authority_disconnect.assert_not_awaited()
    voice.apply_authority_state.assert_not_awaited()


class FakeTransport:
    def __init__(self) -> None:
        self.connected = False
        self.connect_grants: list[VoiceGrant] = []
        self.frames: list[AudioFrame] = []
        self.video_listener: Any = None
        self.video_frames: list[VideoFrame] = []
        self.stopped_video: list[str] = []
        self.priority_states: list[bool] = []

    def set_video_listener(self, listener: Any) -> None:
        self.video_listener = listener

    async def connect(self, grant: VoiceGrant, listener: Any) -> None:
        del listener
        self.connect_grants.append(grant)
        self.connected = True

    async def send_frame(self, frame: AudioFrame) -> None:
        self.frames.append(frame)

    async def send_video_frame(self, frame: VideoFrame) -> None:
        self.video_frames.append(frame)

    async def stop_video(self, source: str) -> None:
        self.stopped_video.append(source)

    async def send_priority_speaker(self, active: bool) -> None:
        self.priority_states.append(active)

    async def disconnect(self) -> None:
        self.connected = False


class FailingTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.disconnected = False

    async def connect(self, grant: VoiceGrant, listener: Any) -> None:
        del grant, listener
        raise RuntimeError("transport failed")

    async def disconnect(self) -> None:
        self.disconnected = True


class DelayedPriorityTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.priority_started = asyncio.Event()
        self.release_priority = asyncio.Event()

    async def send_priority_speaker(self, active: bool) -> None:
        if active:
            self.priority_started.set()
            await self.release_priority.wait()
        await super().send_priority_speaker(active)


class ControlledAudioSource:
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def _frames(self) -> AsyncIterator[AudioFrame]:
        frame = AudioFrame(b"\x01\x00" * 960, channels=1)
        yield frame
        self.waiting.set()
        await self.release.wait()
        yield frame

    def __aiter__(self) -> AsyncIterator[AudioFrame]:
        return self._frames()


def voice_grant(
    *,
    can_stream: bool = False,
    can_priority_speak: object = False,
    user_limit: int = 12,
    video_quality_mode: int | bool = 2,
) -> VoiceGrant:
    return VoiceGrant.from_payload(
        {
            "token": "x" * 32,
            "url": "wss://chat.example/livekit",
            "room": "g.1.2",
            "generation": 3,
            "connection_id": "c" * 43,
            "expires_at": "2026-08-26T00:00:00+00:00",
            "can_listen": True,
            "can_speak": True,
            "can_stream": can_stream,
            "can_priority_speak": can_priority_speak,
            "can_use_vad": False,
            "bitrate": 96_000,
            "user_limit": user_limit,
            "rtc_region": "opaque-region",
            "video_quality_mode": video_quality_mode,
            "e2ee": False,
            "channel_id": "2",
            "channel_domain": "chat.example",
            "guild_id": "1",
            "guild_domain": "chat.example",
            "move_session_id": "m" * 32,
        }
    )


def test_voice_grant_retains_vad_and_move_session_contract() -> None:
    grant = voice_grant()

    assert grant.can_use_vad is False
    assert grant.move_session_id == "m" * 32
    assert grant.can_priority_speak is False


def test_voice_grant_priority_access_is_strict_and_requires_speaking() -> None:
    assert voice_grant(can_priority_speak=True).can_priority_speak
    with pytest.raises(ValueError, match="media permission"):
        voice_grant(can_priority_speak=1)


@pytest.mark.asyncio
async def test_priority_signal_is_bounded_reliable_and_namespaced() -> None:
    transport = LiveKitTransport()
    publish = AsyncMock()
    transport._room = SimpleNamespace(  # noqa: SLF001
        local_participant=SimpleNamespace(publish_data=publish)
    )
    transport._grant = voice_grant(can_priority_speak=True)  # noqa: SLF001

    await transport.send_priority_speaker(True)
    await transport.send_priority_speaker(False)

    assert publish.await_args_list[0].args == (PRIORITY_SPEAKER_ACTIVE_PAYLOAD,)
    assert publish.await_args_list[1].args == (PRIORITY_SPEAKER_INACTIVE_PAYLOAD,)
    assert all(
        call.kwargs == {"reliable": True, "topic": PRIORITY_SPEAKER_TOPIC}
        for call in publish.await_args_list
    )
    with pytest.raises(ValueError, match="boolean"):
        await transport.send_priority_speaker(1)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_priority_signal_tracks_rotated_authority_and_clears_on_revoke() -> None:
    transport = FakeTransport()
    voice = VoiceClient(client(), "https://chat.example", voice_grant(), transport)
    await voice.connect()

    await voice.apply_authority_state(
        generation=voice.grant.generation + 1,
        server_mute=None,
        server_deaf=None,
        can_listen=None,
        can_speak=True,
        can_stream=None,
        can_priority_speak=True,
    )
    await voice.set_priority_speaking(True)
    await voice.apply_authority_state(
        generation=voice.grant.generation + 1,
        server_mute=None,
        server_deaf=None,
        can_listen=None,
        can_speak=True,
        can_stream=None,
        can_priority_speak=False,
    )

    assert transport.priority_states == [True, False]
    assert voice.grant.can_priority_speak is False
    with pytest.raises(RuntimeError, match="does not allow"):
        await voice.set_priority_speaking(True)


@pytest.mark.asyncio
async def test_priority_signal_only_publishes_state_edges() -> None:
    transport = FakeTransport()
    voice = VoiceClient(
        client(),
        "https://chat.example",
        voice_grant(can_priority_speak=True),
        transport,
    )
    await voice.connect()

    await voice.set_priority_speaking(True)
    await voice.set_priority_speaking(True)
    await voice.set_priority_speaking(False)
    await voice.set_priority_speaking(False)

    assert transport.priority_states == [True, False]


@pytest.mark.asyncio
async def test_priority_signal_cannot_outlive_concurrent_authority_revoke() -> None:
    transport = DelayedPriorityTransport()
    voice = VoiceClient(
        client(),
        "https://chat.example",
        voice_grant(can_priority_speak=True),
        transport,
    )
    await voice.connect()

    publish = asyncio.create_task(voice.set_priority_speaking(True))
    await transport.priority_started.wait()
    revoke = asyncio.create_task(
        voice.apply_authority_state(
            generation=voice.grant.generation + 1,
            server_mute=None,
            server_deaf=None,
            can_listen=None,
            can_speak=True,
            can_stream=None,
            can_priority_speak=False,
        )
    )
    transport.release_priority.set()
    await asyncio.gather(publish, revoke)

    assert transport.priority_states == [True, False]
    assert voice.grant.can_priority_speak is False


@pytest.mark.asyncio
async def test_priority_signal_clears_before_stalled_playback_quiesces() -> None:
    transport = FakeTransport()
    voice = VoiceClient(
        client(),
        "https://chat.example",
        voice_grant(can_priority_speak=True),
        transport,
    )
    await voice.connect()
    await voice.set_priority_speaking(True)
    source = ControlledAudioSource()
    playback = asyncio.create_task(voice.play(source))
    await source.waiting.wait()

    quiesce = asyncio.create_task(voice._quiesce_transport())  # noqa: SLF001
    for _ in range(10):
        if transport.priority_states == [True, False]:
            break
        await asyncio.sleep(0)

    assert transport.priority_states == [True, False]
    assert not quiesce.done()
    source.release.set()
    await asyncio.gather(playback, quiesce)
    assert transport.connected is False


@pytest.mark.asyncio
async def test_gateway_voice_info_and_soundboard_requests_route_to_guild_authority() -> (
    None
):
    bot = client()
    socket = SimpleNamespace(send=AsyncMock())
    bot._gateway_sockets["https://guild.example"] = socket

    await bot.request_channel_info(
        EntityRef(7, "guild.example"),
        fields=("status", "voice_start_time"),
        target="https://replica.example",
    )
    await bot.request_soundboard_sounds(
        [EntityRef(7, "guild.example"), EntityRef(8, "guild.example")],
        target="https://replica.example",
    )

    assert [json.loads(call.args[0]) for call in socket.send.await_args_list] == [
        {
            "op": 43,
            "d": {
                "guild_id": "7",
                "fields": ["status", "voice_start_time"],
            },
        },
        {"op": 31, "d": {"guild_ids": ["7", "8"]}},
    ]


@pytest.mark.asyncio
async def test_documented_bot_gateway_commands_are_strict_and_authority_routed() -> (
    None
):
    bot = client()
    guild_socket = SimpleNamespace(send=AsyncMock())
    other_socket = SimpleNamespace(send=AsyncMock())
    bot._gateway_sockets.update(  # noqa: SLF001
        {
            "https://guild.example": guild_socket,
            "https://other.example": other_socket,
        }
    )

    await bot.update_presence(
        status="idle",
        since=123,
        afk=True,
        activities=[{"name": "Build queue", "type": 0, "state": "Federating"}],
    )
    await bot.update_voice_state(
        EntityRef(7, "guild.example"),
        EntityRef(8, "guild.example"),
        self_mute=True,
        target="https://replica.example",
    )
    await bot.request_guild_members(
        EntityRef(7, "guild.example"),
        user_ids=[
            EntityRef(9, "users.example"),
            EntityRef(10, "remote.example"),
        ],
        presences=True,
        nonce="lookup",
        target="https://replica.example",
    )

    assert json.loads(guild_socket.send.await_args_list[0].args[0]) == {
        "op": 3,
        "d": {
            "since": 123,
            "activities": [{"name": "Build queue", "type": 0, "state": "Federating"}],
            "status": "idle",
            "afk": True,
        },
    }
    assert json.loads(other_socket.send.await_args.args[0])["op"] == 3
    assert [
        json.loads(call.args[0]) for call in guild_socket.send.await_args_list[1:]
    ] == [
        {
            "op": 4,
            "d": {
                "guild_id": "7",
                "channel_id": "8",
                "self_mute": True,
                "self_deaf": False,
            },
        },
        {
            "op": 8,
            "d": {
                "guild_id": "7",
                "user_ids": ["9@users.example", "10@remote.example"],
                "presences": True,
                "nonce": "lookup",
            },
        },
    ]
    assert not hasattr(GatewayOp, "SUBSCRIBE_MEMBER_LIST")

    with pytest.raises(ValueError):
        await bot.update_presence(
            activities=[{"name": "Build", "type": True}],
            target="https://guild.example",
        )
    with pytest.raises(TypeError):
        await bot.update_voice_state(  # type: ignore[arg-type]
            EntityRef(7, "guild.example"),
            None,
            self_mute=1,
        )
    with pytest.raises(ValueError):
        await bot.request_guild_members(
            EntityRef(7, "guild.example"),
            query="map",
            limit=0,
        )


def test_member_chunk_and_presence_events_retain_full_gateway_contract() -> None:
    bot = client()
    member = {
        "guild_id": "7",
        "guild_domain": "guild.example",
        "user": {
            "id": "9",
            "origin_domain": "users.example",
            "username": "maple",
        },
        "nickname": None,
        "joined_at": "2026-08-29T00:00:00+00:00",
        "role_ids": [],
        "presence": "idle",
    }
    chunk_payload = {
        "guild_id": "7",
        "guild_domain": "guild.example",
        "members": [member],
        "presences": [
            {
                "user": {"id": "9", "origin_domain": "users.example"},
                "status": "idle",
                "activities": [
                    {"name": "Build queue", "type": 0, "state": "Federating"}
                ],
                "client_status": {"web": "idle"},
            }
        ],
        "chunk_index": 0,
        "chunk_count": 1,
        "not_found": ["10@users.example"],
        "nonce": "lookup",
    }
    parsed = bot._event_model(  # noqa: SLF001
        "GUILD_MEMBERS_CHUNK",
        chunk_payload,
        target="https://guild.example",
        topic="guild:guild.example:7",
        sequence=0,
    )
    assert isinstance(parsed, GuildMembersChunkEvent)
    assert parsed.guild_ref == EntityRef(7, "guild.example")
    assert parsed.members[0].user.ref == EntityRef(9, "users.example")
    assert parsed.presences[0]["activities"] == [
        {"name": "Build queue", "type": 0, "state": "Federating"}
    ]

    presence = bot._event_model(  # noqa: SLF001
        "PRESENCE_UPDATE",
        {
            "user_id": "9",
            "user_domain": "users.example",
            "status": "idle",
            "activities": [{"name": "Build queue", "type": 0}],
            "since": 123,
            "afk": True,
            "client_status": {"web": "idle"},
        },
        target="https://guild.example",
        topic="guild:guild.example:7",
        sequence=0,
    )
    assert isinstance(presence, PresenceEvent)
    assert presence.activities == ({"name": "Build queue", "type": 0},)
    assert (presence.since, presence.afk) == (123, True)

    hostile = [
        {**chunk_payload, "chunk_index": True},
        {**chunk_payload, "guild_id": "8"},
        {
            **chunk_payload,
            "presences": [
                {
                    "user": {"id": "10", "origin_domain": "users.example"},
                    "status": "online",
                    "activities": [],
                    "client_status": {"web": "online"},
                }
            ],
        },
    ]
    for candidate in hostile:
        with pytest.raises(ValueError):
            bot._event_model(  # noqa: SLF001
                "GUILD_MEMBERS_CHUNK",
                candidate,
                target="https://guild.example",
                topic="guild:guild.example:7",
                sequence=0,
            )


def test_channel_info_event_is_exact_typed_and_rejects_ambiguous_values() -> None:
    bot = client()
    payload = {
        "guild_id": "7",
        "channels": [
            {"id": "8", "status": "Pairing 🎧", "voice_start_time": 1_787_916_000}
        ],
    }
    parsed = bot._event_model(
        "CHANNEL_INFO",
        payload,
        target="https://guild.example",
        topic="guild:guild.example:7",
        sequence=1,
    )
    assert isinstance(parsed, ChannelInfoEvent)
    assert parsed.guild_ref == EntityRef(7, "guild.example")
    assert parsed.channels[0].channel_ref == EntityRef(8, "guild.example")
    assert parsed.channels[0].status == "Pairing 🎧"
    assert parsed.channels[0].voice_start_time == 1_787_916_000

    malformed = [
        {**payload, "guild_id": True},
        {**payload, "guild_id": "07"},
        {**payload, "channels": [{"id": True, "status": None}]},
        {**payload, "channels": [{"id": "8", "status": 1}]},
        {**payload, "channels": [{"id": "8", "voice_start_time": True}]},
        {**payload, "channels": [{"id": "8", "voice_start_time": 0}]},
    ]
    for candidate in malformed:
        rejected = bot._event_model(
            "CHANNEL_INFO",
            candidate,
            target="https://guild.example",
            topic="guild:guild.example:7",
            sequence=1,
        )
        assert isinstance(rejected, RawEvent)


def test_voice_grant_accepts_stage_capacity_but_rejects_larger_values() -> None:
    assert voice_grant(user_limit=10_000).user_limit == 10_000
    with pytest.raises(ValueError, match="user limit"):
        voice_grant(user_limit=10_001)
    with pytest.raises(ValueError, match="numeric media policy"):
        voice_grant(video_quality_mode=True)


def test_audio_sources_reject_urls_and_frame_invalid_pcm() -> None:
    with pytest.raises(ValueError, match="not URLs"):
        FFmpegAudioSource("https://media.example/song.mp3")
    with pytest.raises(ValueError, match="complete signed 16-bit"):
        AudioFrame(b"odd", channels=2)
    with pytest.raises(ValueError, match="E2EE"):
        VoiceGrant.from_payload(
            {
                "token": "x" * 32,
                "url": "wss://voice.example",
                "room": "g.1.2",
                "generation": 1,
                "connection_id": "c" * 43,
                "expires_at": "2026-08-26T00:00:00+00:00",
                "e2ee": True,
                "channel_id": "2",
                "channel_domain": "chat.example",
            }
        )
    invalid_permissions = {
        "token": "x" * 32,
        "url": "wss://voice.example",
        "room": "g.1.2",
        "generation": 1,
        "connection_id": "c" * 43,
        "expires_at": "2026-08-26T00:00:00+00:00",
        "e2ee": False,
        "channel_id": "2",
        "channel_domain": "chat.example",
        "can_use_vad": "false",
    }
    with pytest.raises(ValueError, match="media permission"):
        VoiceGrant.from_payload(invalid_permissions)


@pytest.mark.asyncio
async def test_voice_client_plays_pcm_and_disconnects_with_generation() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    transport = FakeTransport()
    voice = VoiceClient(bot, "https://chat.example", voice_grant(), transport)
    await voice.connect()
    await voice.play(PCM16AudioSource(b"\x01\x00" * 1_920), volume=0.5)
    assert transport.connected
    assert len(transport.frames) == 1
    assert transport.frames[0].data[:2] == b"\x00\x00"
    await voice.disconnect()
    bot.request.assert_awaited_once_with(
        "DELETE",
        "/api/v1/bots/channels/2@chat.example/voice",
        target="https://chat.example",
        json={"connection_id": "c" * 43, "generation": 3},
        headers={},
    )


@pytest.mark.asyncio
async def test_gateway_moderator_controls_move_mute_and_disconnect_active_voice() -> (
    None
):
    bot = client()
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    transport = FakeTransport()
    voice = VoiceClient(bot, "https://chat.example", voice_grant(), transport)
    await voice.connect()
    voice.self_mute = True
    voice.server_deaf = True
    bot._register_voice_client(voice)

    moved_payload = {
        "token": "m" * 32,
        "url": "wss://chat.example/livekit",
        "room": "g.1.3",
        "generation": 1,
        "connection_id": "c" * 43,
        "expires_at": "2026-08-26T00:00:00+00:00",
        "can_listen": True,
        "can_speak": True,
        "can_stream": False,
        "can_use_vad": False,
        "bitrate": 96_000,
        "user_limit": 12,
        "rtc_region": "opaque-region",
        "video_quality_mode": 2,
        "e2ee": False,
        "channel_id": "3",
        "channel_domain": "chat.example",
    }
    await bot.dispatch(
        "VOICE_TOKEN",
        {
            "guild_id": "1",
            "guild_domain": "chat.example",
            "channel_id": "3",
            "channel_domain": "chat.example",
            "grant": moved_payload,
        },
        target="https://chat.example",
        topic="user:apps.example:10",
    )

    assert voice.grant.channel_ref == EntityRef(3, "chat.example")
    assert voice.grant.generation == 1
    assert [grant.channel_ref.id for grant in transport.connect_grants] == [2, 3]
    assert voice.is_connected
    assert voice.self_mute
    assert voice.server_deaf

    await bot.dispatch(
        "VOICE_STATE_UPDATE",
        {
            "guild_id": "1",
            "guild_domain": "chat.example",
            "channel_id": "3",
            "channel_domain": "chat.example",
            "user_id": "10",
            "user_domain": "apps.example",
            "connected": True,
            "connection_id": "c" * 43,
            "generation": 2,
            "server_mute": True,
            "server_deaf": True,
            "can_listen": False,
            "can_speak": False,
            "can_stream": False,
        },
        target="https://chat.example",
        topic="user:apps.example:10",
    )

    assert voice.grant.generation == 2
    assert not voice.grant.can_listen
    assert not voice.grant.can_speak
    assert voice.server_mute and voice.server_deaf

    await bot.dispatch(
        "VOICE_STATE_UPDATE",
        {
            "guild_id": "1",
            "guild_domain": "chat.example",
            "channel_id": "3",
            "channel_domain": "chat.example",
            "user_id": "10",
            "user_domain": "apps.example",
            "connected": False,
            "connection_id": "c" * 43,
            "generation": 3,
        },
        target="https://chat.example",
        topic="user:apps.example:10",
    )

    assert voice.is_closed
    assert not transport.connected
    assert "c" * 43 not in bot._voice_clients
    bot.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_playback_can_pause_resume_and_stop() -> None:
    bot = client()
    transport = FakeTransport()
    voice = VoiceClient(bot, "https://chat.example", voice_grant(), transport)

    paused_source = ControlledAudioSource()
    paused_task = asyncio.create_task(voice.play(paused_source))
    await paused_source.waiting.wait()
    assert voice.is_playing
    voice.pause()
    assert voice.is_paused
    paused_source.release.set()
    await asyncio.sleep(0)
    assert len(transport.frames) == 1
    voice.resume()
    await paused_task
    assert len(transport.frames) == 2
    assert not voice.is_playing

    stopped_source = ControlledAudioSource()
    stopped_task = asyncio.create_task(voice.play(stopped_source))
    await stopped_source.waiting.wait()
    voice.stop()
    stopped_source.release.set()
    await stopped_task
    assert len(transport.frames) == 3


@pytest.mark.asyncio
async def test_voice_client_receives_camera_and_screen_share_frames() -> None:
    bot = client()
    transport = FakeTransport()
    voice = VoiceClient(bot, "https://chat.example", voice_grant(), transport)
    received: list[tuple[VideoFrame, str]] = []

    async def receive(frame: VideoFrame, participant: str) -> None:
        received.append((frame, participant))

    assert voice.listen_video(receive) is receive
    assert transport.video_listener is not None
    frame = VideoFrame(b"rgba", 1, 1, "rgba", "camera")
    await transport.video_listener(frame, "9@chat.example")
    assert received == [(frame, "9@chat.example")]


@pytest.mark.asyncio
async def test_voice_client_publishes_and_stops_camera_and_screen_frames() -> None:
    bot = client()
    transport = FakeTransport()
    voice = VoiceClient(
        bot,
        "https://chat.example",
        voice_grant(can_stream=True),
        transport,
    )
    await voice.connect()
    camera = VideoFrame(b"\x00" * 16, 2, 2, "rgba", "camera")
    screen = VideoFrame(b"\x00" * 12, 2, 2, "rgb24", "screen_share")

    await voice.publish_video(camera)
    await voice.publish_video(screen)
    await voice.stop_video("camera")

    assert transport.video_frames == [camera, screen]
    assert transport.stopped_video == ["camera"]


@pytest.mark.asyncio
async def test_voice_video_publish_validates_capability_and_packed_frame_size() -> None:
    bot = client()
    transport = FakeTransport()
    voice = VoiceClient(bot, "https://chat.example", voice_grant(), transport)
    await voice.connect()
    frame = VideoFrame(b"\x00" * 16, 2, 2, "rgba", "camera")
    with pytest.raises(RuntimeError, match="does not allow video"):
        await voice.publish_video(frame)

    voice.grant = voice_grant(can_stream=True)
    with pytest.raises(ValueError, match="exactly 16 bytes"):
        await voice.publish_video(VideoFrame(b"\x00" * 15, 2, 2, "rgba", "camera"))
    with pytest.raises(ValueError, match="camera or screen_share"):
        await voice.publish_video(VideoFrame(b"x", 1, 1, "rgba", "unknown"))


@pytest.mark.asyncio
async def test_voice_video_permission_revocation_unpublishes_all_tracks() -> None:
    bot = client()
    transport = FakeTransport()
    voice = VoiceClient(
        bot,
        "https://chat.example",
        voice_grant(can_stream=True),
        transport,
    )
    await voice.connect()

    await voice.apply_authority_state(
        generation=voice.grant.generation + 1,
        server_mute=None,
        server_deaf=None,
        can_listen=None,
        can_speak=None,
        can_stream=False,
    )

    assert transport.stopped_video == ["camera", "screen_share"]
    assert not voice.grant.can_stream


@pytest.mark.asyncio
async def test_voice_video_revocation_fences_inflight_publication() -> None:
    class GatedVideoTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.publication_started = asyncio.Event()
            self.release_publication = asyncio.Event()

        async def send_video_frame(self, frame: VideoFrame) -> None:
            self.publication_started.set()
            await self.release_publication.wait()
            await super().send_video_frame(frame)

    transport = GatedVideoTransport()
    voice = VoiceClient(
        client(),
        "https://chat.example",
        voice_grant(can_stream=True),
        transport,
    )
    await voice.connect()
    frame = VideoFrame(b"\x00" * 16, 2, 2, "rgba", "camera")
    publication = asyncio.create_task(voice.publish_video(frame))
    await transport.publication_started.wait()
    revocation = asyncio.create_task(
        voice.apply_authority_state(
            generation=voice.grant.generation + 1,
            server_mute=None,
            server_deaf=None,
            can_listen=None,
            can_speak=None,
            can_stream=False,
        )
    )
    await asyncio.sleep(0)
    assert not revocation.done()

    transport.release_publication.set()
    await publication
    await revocation

    assert transport.video_frames == [frame]
    assert transport.stopped_video == ["camera", "screen_share"]
    with pytest.raises(RuntimeError, match="does not allow video"):
        await voice.publish_video(frame)


@pytest.mark.asyncio
async def test_voice_disconnect_fences_inflight_video_publication() -> None:
    class GatedVideoTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.publication_started = asyncio.Event()
            self.release_publication = asyncio.Event()

        async def send_video_frame(self, frame: VideoFrame) -> None:
            self.publication_started.set()
            await self.release_publication.wait()
            await super().send_video_frame(frame)

    transport = GatedVideoTransport()
    voice = VoiceClient(
        client(),
        "https://chat.example",
        voice_grant(can_stream=True),
        transport,
    )
    await voice.connect()
    frame = VideoFrame(b"\x00" * 16, 2, 2, "rgba", "screen_share")
    publication = asyncio.create_task(voice.publish_video(frame))
    await transport.publication_started.wait()
    disconnection = asyncio.create_task(voice.authority_disconnect())
    await asyncio.sleep(0)
    assert not disconnection.done()

    transport.release_publication.set()
    await publication
    await disconnection

    assert transport.video_frames == [frame]
    assert not transport.connected
    with pytest.raises(RuntimeError, match="not connected"):
        await voice.publish_video(frame)


@pytest.mark.asyncio
async def test_authority_media_revocation_gates_inflight_bot_callbacks() -> None:
    bot = client()
    transport = FakeTransport()
    voice = VoiceClient(bot, "https://chat.example", voice_grant(), transport)
    received_audio: list[str] = []
    received_video: list[str] = []

    async def on_audio(_frame: AudioFrame, participant: str) -> None:
        received_audio.append(participant)

    async def on_video(_frame: VideoFrame, participant: str) -> None:
        received_video.append(participant)

    voice.listen(on_audio)
    voice.listen_video(on_video)
    await voice.apply_authority_state(
        generation=voice.grant.generation + 1,
        server_mute=False,
        server_deaf=True,
        can_listen=False,
        can_speak=False,
        can_stream=None,
    )
    await voice._receive(AudioFrame(b"\x00\x00" * 960, channels=1), "remote")
    await voice._receive_video(
        VideoFrame(b"\x00" * 4, 1, 1, "rgba", "camera"), "remote"
    )

    assert received_audio == []
    assert received_video == []
    assert voice.server_deaf


@pytest.mark.asyncio
async def test_livekit_transport_publishes_distinct_camera_and_screen_tracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources: list[Any] = []

    class Source:
        def __init__(
            self, width: int, height: int, *, is_screencast: bool = False
        ) -> None:
            self.width = width
            self.height = height
            self.is_screencast = is_screencast
            self.frames: list[Any] = []
            self.closed = False
            sources.append(self)

        def capture_frame(self, frame: Any) -> None:
            self.frames.append(frame)

        async def aclose(self) -> None:
            self.closed = True

    class Track:
        @staticmethod
        def create_video_track(name: str, source: Any) -> Any:
            return SimpleNamespace(name=name, source=source)

    class Options:
        source: str | None = None
        simulcast = False

    class Participant:
        def __init__(self) -> None:
            self.published: list[tuple[Any, Any]] = []
            self.unpublished: list[str] = []

        async def publish_track(self, track: Any, options: Any) -> Any:
            self.published.append((track, options))
            return SimpleNamespace(sid=f"track-{len(self.published)}")

        async def unpublish_track(self, sid: str) -> None:
            self.unpublished.append(sid)

    rtc = SimpleNamespace(
        VideoSource=Source,
        LocalVideoTrack=Track,
        TrackPublishOptions=Options,
        TrackSource=SimpleNamespace(
            SOURCE_CAMERA="camera",
            SOURCE_SCREENSHARE="screen",
        ),
        VideoBufferType=SimpleNamespace(RGBA="rgba"),
        VideoFrame=lambda width, height, pixel_format, data: SimpleNamespace(
            width=width,
            height=height,
            pixel_format=pixel_format,
            data=data,
        ),
    )
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    participant = Participant()
    transport = LiveKitTransport()
    transport._room = SimpleNamespace(local_participant=participant)
    transport._grant = voice_grant(can_stream=True)

    await transport.send_video_frame(VideoFrame(b"\x00" * 16, 2, 2, "rgba", "camera"))
    await transport.send_video_frame(VideoFrame(b"\x01" * 16, 2, 2, "rgba", "camera"))
    await transport.send_video_frame(VideoFrame(b"\x02" * 64, 4, 4, "rgba", "camera"))
    await transport.send_video_frame(
        VideoFrame(b"\x03" * 16, 2, 2, "rgba", "screen_share")
    )
    await transport.stop_video("screen_share")

    assert len(participant.published) == 3
    assert participant.unpublished == ["track-1", "track-3"]
    assert [source.is_screencast for source in sources] == [False, False, True]
    assert [len(source.frames) for source in sources] == [2, 1, 1]
    assert sources[0].closed and sources[2].closed


@pytest.mark.asyncio
async def test_real_livekit_screen_share_frame_contract_when_installed() -> None:
    rtc = pytest.importorskip("livekit.rtc")
    assert (
        LiveKitTransport._video_source(
            rtc,
            SimpleNamespace(source=rtc.TrackSource.SOURCE_SCREENSHARE),
        )
        == "screen_share"
    )
    source = rtc.VideoSource(1, 1, is_screencast=True)
    try:
        frame = rtc.VideoFrame(1, 1, rtc.VideoBufferType.RGBA, b"\x00" * 4)
        assert rtc.TrackSource.SOURCE_SCREENSHARE != rtc.TrackSource.SOURCE_CAMERA
        source.capture_frame(frame)
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_client_voice_join_requests_only_explicit_capabilities() -> None:
    bot = client()
    transport = FakeTransport()
    grant = voice_grant()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "token": grant.token,
            "url": grant.url,
            "room": grant.room,
            "generation": grant.generation,
            "connection_id": "placeholder",
            "expires_at": grant.expires_at,
            "can_listen": True,
            "can_speak": True,
            "can_stream": False,
            "e2ee": False,
            "channel_id": "2",
            "channel_domain": "chat.example",
        }
    )

    async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        payload = cast(dict[str, Any], kwargs["json"])
        response = cast(dict[str, Any], bot.request.return_value)
        response["connection_id"] = payload["connection_id"]
        return response

    bot.request.side_effect = request
    voice = await bot.connect_voice(
        EntityRef(2, "chat.example"),
        target="https://chat.example",
        listen=True,
        speak=True,
        transport=transport,
    )
    assert voice.grant.can_listen and voice.grant.can_speak
    sent = cast(dict[str, Any], bot.request.await_args.kwargs["json"])
    assert sent == {
        "connection_id": sent["connection_id"],
        "takeover": False,
        "listen": True,
        "speak": True,
        "stream": False,
    }


@pytest.mark.asyncio
async def test_failed_transport_connect_releases_backend_voice_reservation() -> None:
    bot = client()
    transport = FailingTransport()
    grant = voice_grant()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append((method, path, kwargs))
        if method == "DELETE":
            return None
        payload = cast(dict[str, Any], kwargs["json"])
        return {
            "token": grant.token,
            "url": grant.url,
            "room": grant.room,
            "generation": grant.generation,
            "connection_id": payload["connection_id"],
            "expires_at": grant.expires_at,
            "can_listen": True,
            "can_speak": False,
            "can_stream": False,
            "e2ee": False,
            "channel_id": "2",
            "channel_domain": "chat.example",
        }

    bot.request = AsyncMock(side_effect=request)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="transport failed"):
        await bot.connect_voice(
            EntityRef(2, "chat.example"),
            target="https://chat.example",
            transport=transport,
        )

    assert transport.disconnected
    assert calls[1][0:2] == (
        "DELETE",
        "/api/v1/bots/channels/2@chat.example/voice",
    )
    assert calls[1][2]["json"]["generation"] == grant.generation


@pytest.mark.asyncio
async def test_invalid_voice_grant_releases_backend_reservation() -> None:
    bot = client()
    grant = voice_grant()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append((method, path, kwargs))
        if method == "DELETE":
            return None
        payload = cast(dict[str, Any], kwargs["json"])
        return {
            "token": grant.token,
            "url": "http://unsafe.example/livekit",
            "room": grant.room,
            "generation": grant.generation,
            "connection_id": payload["connection_id"],
            "expires_at": grant.expires_at,
            "can_listen": True,
            "can_speak": False,
            "can_stream": False,
            "e2ee": False,
            "channel_id": "2",
            "channel_domain": "chat.example",
        }

    bot.request = AsyncMock(side_effect=request)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="unsafe LiveKit URL"):
        await bot.connect_voice(
            EntityRef(2, "chat.example"),
            target="https://chat.example",
            transport=FakeTransport(),
        )

    requested_connection = calls[0][2]["json"]["connection_id"]
    assert calls[1] == (
        "DELETE",
        "/api/v1/bots/channels/2@chat.example/voice",
        {
            "target": "https://chat.example",
            "json": {
                "connection_id": requested_connection,
                "generation": grant.generation,
            },
            "headers": {},
        },
    )


def test_channel_and_soundboard_models_preserve_voice_contract() -> None:
    bot = client()
    channel = Channel.from_payload(
        bot,
        "https://chat.example",
        {
            "id": "2",
            "origin_domain": "chat.example",
            "guild_id": "1",
            "guild_domain": "chat.example",
            "type": 2,
            "name": "Voice",
            "bitrate": 96_000,
            "user_limit": 10,
            "rtc_region": "opaque-region",
            "video_quality_mode": 2,
        },
    )
    assert channel.is_voice
    assert not channel.is_stage
    assert channel.bitrate == 96_000
    assert channel.rtc_region == "opaque-region"
    assert not hasattr(channel, "voice_status")
    grant = voice_grant()
    assert grant.bitrate == 96_000
    assert grant.user_limit == 12
    assert grant.rtc_region == "opaque-region"
    assert grant.video_quality_mode == 2
    sound = SoundboardSound.from_payload(
        bot,
        "https://chat.example",
        {
            "id": "4",
            "origin_domain": "chat.example",
            "guild_id": "1",
            "guild_domain": "chat.example",
            "name": "Horn",
            "media_hash": "a" * 64,
            "content_type": "audio/ogg",
            "volume": 0.75,
            "duration_ms": 900,
            "created_by_id": "2",
            "created_by_domain": "apps.example",
            "version": "2",
        },
    )
    assert sound.ref == EntityRef(4, "chat.example")
    assert sound.duration_ms == 900
    assert sound.creator_ref == EntityRef(2, "apps.example")

    stage = Channel.from_payload(
        bot,
        "https://chat.example",
        {
            "id": "3",
            "origin_domain": "chat.example",
            "guild_id": "1",
            "guild_domain": "chat.example",
            "type": 13,
            "name": "Town hall",
        },
    )
    assert stage.is_voice
    assert stage.is_stage


@pytest.mark.asyncio
async def test_voice_status_uses_dedicated_route_and_audit_reason() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    channel = Channel.from_payload(
        bot,
        "https://chat.example",
        {
            "id": "2",
            "origin_domain": "chat.example",
            "guild_id": "1",
            "guild_domain": "chat.example",
            "type": 2,
            "name": "Voice",
        },
    )

    assert (
        await channel.set_voice_status("  🎉 Party  ", reason="  event night  ") is None
    )
    call = bot.request.await_args
    assert call.args == (
        "PUT",
        "/api/v1/bots/guilds/1@chat.example/channels/2@chat.example/voice-status",
    )
    assert call.kwargs["json"] == {"status": "🎉 Party"}
    assert call.kwargs["headers"] == {"X-Audit-Log-Reason": "event night"}
    with pytest.raises(ValueError, match="500"):
        await channel.set_voice_status("x" * 501)


@pytest.mark.asyncio
async def test_voice_region_catalog_is_typed_and_strict() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "id": "edge-eu-1",
                "name": "Europe Edge",
                "optimal": True,
                "deprecated": False,
                "custom": True,
            }
        ]
    )
    regions = await bot.voice_regions(target="https://chat.example")
    assert regions[0].id == "edge-eu-1"
    assert regions[0].optimal

    bot.request = AsyncMock(return_value=["invalid"])  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="invalid item"):
        await bot.voice_regions(target="https://chat.example")


@pytest.mark.asyncio
async def test_voice_region_catalog_uses_guild_authority_route() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "id": "edge",
                "name": "Edge",
                "optimal": False,
                "deprecated": False,
                "custom": True,
            }
        ]
    )
    guild = EntityRef(9, "guild.example")
    await bot.voice_regions(guild, target="https://replica.example")
    assert bot.request.await_args.args == (  # type: ignore[attr-defined]
        "GET",
        "/api/v1/bots/guilds/9@guild.example/voice/regions",
    )


def test_bot_resource_paths_route_to_authority_across_three_instances() -> None:
    bot = client()
    bot._targets["https://guild.example:8443"] = cast(Any, object())
    path = "/api/v1/bots/guilds/9@guild.example/scheduled-events"
    assert (
        bot._request_target(path, "https://user-replica.example")
        == "https://guild.example:8443"
    )
    assert (
        bot._request_target(
            "/api/v1/bots/channels/4@voice.example/voice/token",
            "https://guild.example:8443",
        )
        == "https://voice.example"
    )


def test_soundboard_download_is_bound_to_explicit_media_authority() -> None:
    valid = "https://media.chat.example/sounds/4?X-Amz-Signature=secret"
    assert (
        _validate_soundboard_download_url(
            valid,
            "chat.example",
            "https://media.chat.example",
        )
        == valid
    )
    development = "https://media.alpha.localhost:18443/sounds/4?signature=secret"
    assert (
        _validate_soundboard_download_url(
            development,
            "alpha.localhost",
            "https://media.alpha.localhost:18443",
        )
        == development
    )
    third_instance = "https://media.source.example/sounds/4?signature=secret"
    assert (
        _validate_soundboard_download_url(
            third_instance,
            "source.example",
            "https://media.source.example",
        )
        == third_instance
    )
    external = "https://kaede-sounds.s3.example.com/sounds/4?signature=secret"
    assert (
        _validate_soundboard_download_url(
            external,
            "source.example",
            "https://kaede-sounds.s3.example.com",
        )
        == external
    )
    with pytest.raises(RuntimeError, match="signed HTTPS media origin"):
        _validate_soundboard_download_url(
            third_instance,
            "voice-authority.example",
            "https://media.voice-authority.example",
        )

    for hostile in (
        "https://127.0.0.1/latest/meta-data",
        "https://metadata.internal/sound",
        "https://media.chat.example.attacker.test/sound",
        "https://media.chat.example:8443/sound",
        "https://chat.example@media.chat.example/sound",
    ):
        with pytest.raises(RuntimeError, match="signed HTTPS media origin"):
            _validate_soundboard_download_url(
                hostile,
                "chat.example",
                "https://media.chat.example",
            )


@pytest.mark.asyncio
async def test_default_soundboard_catalog_is_guildless_and_uses_global_route() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "id": "4",
                "origin_domain": "chat.example",
                "guild_id": None,
                "guild_domain": None,
                "name": "Quack",
                "media_hash": "d" * 64,
                "content_type": "audio/ogg",
                "volume": 0.8,
                "duration_ms": 900,
                "emoji_name": "🦆",
                "available": True,
                "version": "1",
            }
        ]
    )
    [sound] = await bot.default_soundboard_sounds(target="https://chat.example")
    assert sound.guild_ref is None
    assert sound.name == "Quack"
    assert bot.request.await_args.args == (
        "GET",
        "/api/v1/bots/soundboard-default-sounds",
    )
