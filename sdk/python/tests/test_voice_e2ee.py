from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.audio import AudioFrame
from kaede_bot.client import Client
from kaede_bot.e2ee import E2EEProvider, NativeOpenMLSProvider
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState
from kaede_bot.voice import (
    MEDIA_KEY_BYTES,
    MEDIA_RATCHET_SALT,
    LiveKitTransport,
    VoiceClient,
    VoiceE2EEContext,
    VoiceGrant,
    _expected_media_session_id,
)


class DeterministicMLSProvider:
    """Complete protocol test double; production requires the native provider."""

    def __init__(self, epoch: int) -> None:
        self.epoch = epoch
        self.exports: list[tuple[bytes, str, bytes, int]] = []

    def export_state(self) -> bytes:
        return b"state"

    def public_identity_key(self) -> bytes:
        return b"i" * 32

    def sign(self, value: bytes) -> bytes:
        del value
        return b"s" * 64

    def generate_key_package(self) -> bytes:
        return b"package"

    def inspect_key_package(self, package: bytes) -> tuple[bytes, bytes]:
        del package
        return b"credential", b"i" * 32

    def create_group(self, group_id: bytes) -> None:
        del group_id

    def add_members(
        self, group_id: bytes, packages: Sequence[bytes]
    ) -> tuple[bytes, bytes]:
        del group_id, packages
        return b"commit", b"welcome"

    def remove_accounts(
        self, group_id: bytes, accounts: Sequence[str]
    ) -> tuple[bytes, bytes]:
        del group_id, accounts
        return b"commit", b""

    def merge_pending_commit(self, group_id: bytes) -> None:
        del group_id

    def join_group(self, welcome: bytes) -> bytes:
        del welcome
        return b"g" * 32

    def encrypt(self, group_id: bytes, plaintext: bytes, aad: bytes) -> bytes:
        del group_id, aad
        return plaintext

    def process(self, group_id: bytes, message: bytes) -> dict[str, object]:
        del group_id, message
        return {"kind": "commit"}

    def group_epoch(self, group_id: bytes) -> int:
        del group_id
        return self.epoch

    def export_epoch_secret(
        self, group_id: bytes, label: str, context: bytes, length: int
    ) -> bytes:
        self.exports.append((group_id, label, context, length))
        return bytes([self.epoch & 0xFF]) * length

    def close(self) -> None:
        return None


def encrypted_grant(
    group_id: bytes,
    *,
    epoch: int = 7,
    policy_generation: int = 4,
) -> VoiceGrant:
    grant = VoiceGrant.from_payload(
        {
            "token": "x" * 32,
            "url": "wss://chat.example/livekit",
            "room": "g.1.2",
            "generation": 3,
            "connection_id": "c" * 43,
            "expires_at": "2026-08-28T00:00:00+00:00",
            "can_listen": True,
            "can_speak": True,
            "can_stream": False,
            "e2ee": True,
            "channel_id": "2",
            "channel_domain": "chat.example",
            "encryption_policy_generation": str(policy_generation),
            "encryption_epoch": str(epoch),
            "media_protocol": "livekit-e2ee-v1",
            "media_suite": "AES-256-GCM",
            "media_session_id": "a" * 43,
            "media_epoch": str(epoch),
        }
    )
    return replace(
        grant,
        media_session_id=_expected_media_session_id(grant, group_id),
    )


def e2ee_context(
    provider: E2EEProvider,
    group_id: bytes,
    *,
    epoch: int = 7,
) -> VoiceE2EEContext:
    return VoiceE2EEContext(
        provider=provider,
        device_id="kbe_" + "d" * 43,
        channel_ref=EntityRef(2, "chat.example"),
        group_id=group_id,
        epoch=epoch,
    )


def test_voice_context_binds_group_epoch_and_exporter_context() -> None:
    group_id = b"g" * 32
    provider = DeterministicMLSProvider(7)
    context = e2ee_context(provider, group_id)
    grant = encrypted_grant(group_id)

    assert grant.media_session_id == "7pR_gBnDK_-lcbHj8leDdqAjYeNB3ZehUM7EPhdhEJs"
    key = context.derive_media_key(grant)

    assert key == bytearray(b"\x07" * MEDIA_KEY_BYTES)
    assert provider.exports == [
        (
            group_id,
            "kaede livekit v1",
            (
                "kaede-livekit-key-v1\0livekit-e2ee-v1\0AES-256-GCM\0"
                "7pR_gBnDK_-lcbHj8leDdqAjYeNB3ZehUM7EPhdhEJs\x00"
                "7\0g.1.2"
            ).encode(),
            MEDIA_KEY_BYTES,
        )
    ]


def test_rotated_voice_channel_uses_first_commit_of_new_group() -> None:
    group_id = b"g" * 32
    provider = DeterministicMLSProvider(1)
    context = replace(e2ee_context(provider, group_id, epoch=7), mls_epoch=1)
    assert context.derive_media_key(encrypted_grant(group_id, epoch=7)) == bytes(
        [1] * MEDIA_KEY_BYTES
    )
    provider.epoch = 2
    with pytest.raises(ValueError, match="stale"):
        context.derive_media_key(encrypted_grant(group_id, epoch=7))


def test_voice_context_rejects_mismatched_group_and_stale_provider_epoch() -> None:
    provider = DeterministicMLSProvider(7)
    context = e2ee_context(provider, b"a" * 32)

    with pytest.raises(ValueError, match="current MLS group"):
        context.derive_media_key(encrypted_grant(b"b" * 32))
    assert provider.exports == []

    matching = encrypted_grant(b"a" * 32)
    provider.epoch = 8
    with pytest.raises(ValueError, match="provider state is stale"):
        context.derive_media_key(matching)
    assert provider.exports == []


def test_epoch_and_policy_changes_bind_exporter_context_independently() -> None:
    group_id = b"g" * 32
    provider = DeterministicMLSProvider(7)
    old_context = e2ee_context(provider, group_id)
    old_key = old_context.derive_media_key(encrypted_grant(group_id))

    provider.epoch = 8
    old_context.invalidate("MLS epoch advanced")
    with pytest.raises(RuntimeError, match="MLS epoch advanced"):
        old_context.derive_media_key(encrypted_grant(group_id))

    rotated_context = e2ee_context(provider, group_id, epoch=8)
    rotated_key = rotated_context.derive_media_key(
        encrypted_grant(group_id, epoch=8, policy_generation=4)
    )
    assert old_key == bytearray(b"\x07" * MEDIA_KEY_BYTES)
    assert rotated_key == bytearray(b"\x08" * MEDIA_KEY_BYTES)
    epoch_export = provider.exports[-1]
    rotated_context.derive_media_key(
        encrypted_grant(group_id, epoch=8, policy_generation=5)
    )
    assert provider.exports[-1][2] != epoch_export[2]
    assert provider.exports[-1][0:2] == epoch_export[0:2]


class FakeKeyProvider:
    def __init__(self) -> None:
        self.keys: list[tuple[bytes, int]] = []

    def set_shared_key(self, key: bytes, key_index: int) -> None:
        self.keys.append((key, key_index))


class FakeE2EEManager:
    def __init__(self) -> None:
        self.enabled = True
        self.key_provider = FakeKeyProvider()
        self.enabled_updates: list[bool] = []

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.enabled_updates.append(enabled)


class FakeRoom:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.remote_participants: dict[str, Any] = {}
        self.e2ee_manager = FakeE2EEManager()
        self.connected_key: bytes | None = None
        self.ratchet_salt: bytes | None = None
        self.key_derivation_function: int | None = None
        self.auto_subscribe: bool | None = None
        self.disconnected = False

    def on(self, event: str) -> Any:
        def register(callback: Any) -> Any:
            self.handlers[event] = callback
            return callback

        return register

    async def connect(self, _url: str, _token: str, *, options: Any) -> None:
        if options.encryption is not None:
            provider = options.encryption.key_provider_options
            self.connected_key = provider.shared_key
            self.ratchet_salt = provider.ratchet_salt
            self.key_derivation_function = provider.key_derivation_function
        self.auto_subscribe = options.auto_subscribe

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeE2EEOptions:
    def __init__(self) -> None:
        self.key_provider_options = SimpleNamespace(
            shared_key=None,
            ratchet_salt=b"default",
            key_derivation_function=0,
        )


class FakeRoomOptions:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class FakeRTC:
    E2EEOptions = FakeE2EEOptions
    RoomOptions = FakeRoomOptions
    KeyDerivationFunction = SimpleNamespace(HKDF=1)
    EncryptionType = SimpleNamespace(GCM=1)
    TrackKind = SimpleNamespace(KIND_AUDIO=1, KIND_VIDEO=2)
    __version__ = "1.1.18"

    def __init__(self) -> None:
        self.rooms: list[FakeRoom] = []

    def Room(self) -> FakeRoom:  # noqa: N802 - mirrors LiveKit's public class
        room = FakeRoom()
        self.rooms.append(room)
        return room

    def install(self, monkeypatch: pytest.MonkeyPatch, core: Any) -> None:
        monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=self))
        ffi = SimpleNamespace(instance=SimpleNamespace(_ffi_lib=core))
        monkeypatch.setitem(
            sys.modules, "livekit.rtc._ffi_client", SimpleNamespace(FfiClient=ffi)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kaede_e2ee", [False, True])
async def test_livekit_transport_installs_key_before_connect_and_clears_on_leave(
    monkeypatch: pytest.MonkeyPatch,
    kaede_e2ee: bool,
) -> None:
    rtc = FakeRTC()
    marker = Mock(return_value=256)
    rtc.install(monkeypatch, SimpleNamespace(kaede_livekit_ffi_abi_v1=marker))
    grant = encrypted_grant(b"g" * 32)
    transport = LiveKitTransport(kaede_e2ee=kaede_e2ee)
    staged = bytearray(b"k" * MEDIA_KEY_BYTES)
    transport.configure_e2ee(grant, staged)
    owned_key = transport._configured_media_key
    assert owned_key == staged

    async def listener(_frame: AudioFrame, _participant: str) -> None:
        return None

    if not kaede_e2ee:
        with pytest.raises(RuntimeError, match="kaede_e2ee=True"):
            await transport.connect(grant, listener)
        assert not rtc.rooms
        assert owned_key == bytearray(MEDIA_KEY_BYTES)
        assert transport._configured_media_key is None
        return

    await transport.connect(grant, listener)
    room = rtc.rooms[0]
    assert room.connected_key == b"k" * MEDIA_KEY_BYTES
    assert room.ratchet_salt == MEDIA_RATCHET_SALT
    assert room.key_derivation_function == 1
    marker.assert_called_once_with()
    assert not room.auto_subscribe

    await transport.disconnect()
    assert room.e2ee_manager.enabled_updates == [False]
    assert room.e2ee_manager.key_provider.keys == [(b"\0" * MEDIA_KEY_BYTES, 0)]
    assert room.disconnected
    assert owned_key == bytearray(MEDIA_KEY_BYTES)
    assert transport._configured_media_key is None


@pytest.mark.asyncio
async def test_opt_in_receives_encrypted_audio_and_vp8_without_a_codec_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rtc = FakeRTC()
    rtc.install(
        monkeypatch, SimpleNamespace(kaede_livekit_ffi_abi_v1=Mock(return_value=256))
    )
    room = FakeRoom()
    monkeypatch.setattr(rtc, "Room", lambda: room)

    def publication(kind: int, name: str, mime: str, encryption: int = 1) -> Any:
        return SimpleNamespace(
            kind=kind,
            name=name,
            mime_type=mime,
            encryption_type=encryption,
            set_subscribed=Mock(),
        )

    audio = publication(1, "microphone", "audio/opus")
    screen = publication(2, "kaede.video.v1:screen_share:vp8", "")
    camera = publication(2, "camera", "video/VP8; test=1")
    premium = publication(2, "kaede.video.v1:camera:av1", "video/av1")
    plaintext = publication(1, "microphone", "audio/opus", encryption=0)
    mislabeled = publication(2, "kaede.video.v1:camera:vp8", "video/h264")
    peer = SimpleNamespace(track_publications={"a": audio, "v": screen, "p": premium})
    room.remote_participants["alice"] = peer
    grant = encrypted_grant(b"g" * 32)
    transport = LiveKitTransport(kaede_e2ee=True)
    transport.configure_e2ee(grant, bytearray(b"k" * MEDIA_KEY_BYTES))
    await transport.connect(grant, AsyncMock())
    for late in (camera, plaintext, mislabeled):
        room.handlers["track_published"](late, peer)
    for allowed in (audio, screen, camera):
        allowed.set_subscribed.assert_called_once_with(True)
    for ignored in (premium, plaintext, mislabeled):
        ignored.set_subscribed.assert_not_called()
        room.handlers["track_subscribed"](
            SimpleNamespace(kind=ignored.kind), ignored, peer
        )
    assert not transport._receive_tasks
    await transport.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "native", "message"),
    [
        ("1.1.19", SimpleNamespace(), "livekit==1.1.18"),
        ("1.1.18", SimpleNamespace(), "LIVEKIT_LIB_PATH"),
        (
            "1.1.18",
            SimpleNamespace(kaede_livekit_ffi_abi_v1=Mock(return_value=128)),
            "LIVEKIT_LIB_PATH",
        ),
    ],
)
async def test_opt_in_rejects_incompatible_core_before_joining_an_encrypted_room(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    native: Any,
    message: str,
) -> None:
    rtc = FakeRTC()
    rtc.__version__ = version
    rtc.install(monkeypatch, native)
    grant = encrypted_grant(b"g" * 32)
    transport = LiveKitTransport(kaede_e2ee=True)
    transport.configure_e2ee(grant, bytearray(b"k" * MEDIA_KEY_BYTES))
    owned_key = transport._configured_media_key
    listener = AsyncMock()
    with pytest.raises(RuntimeError, match=message):
        await transport.connect(grant, listener)
    assert rtc.rooms == []
    assert owned_key == bytearray(MEDIA_KEY_BYTES)

    await transport.connect(replace(grant, e2ee=False), listener)
    assert rtc.rooms[0].connected_key is None
    await transport.disconnect()


class EncryptedFakeTransport:
    def __init__(self) -> None:
        self.configured_key: bytes | None = None
        self.connected = False
        self.disconnected = asyncio.Event()
        self.cleared = False

    def configure_e2ee(self, _grant: VoiceGrant, media_key: bytearray) -> None:
        self.configured_key = bytes(media_key)

    def clear_e2ee(self) -> None:
        self.cleared = True
        self.configured_key = None

    async def connect(self, _grant: VoiceGrant, _listener: Any) -> None:
        self.connected = True

    async def send_frame(self, _frame: AudioFrame) -> None:
        return None

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected.set()


class BlockingEncryptedFakeTransport(EncryptedFakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.connect_started = asyncio.Event()

    async def connect(self, _grant: VoiceGrant, _listener: Any) -> None:
        self.connect_started.set()
        await asyncio.Future()


@pytest.mark.asyncio
async def test_context_invalidation_while_connecting_fails_closed_and_clears_key() -> (
    None
):
    bot = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    group_id = b"g" * 32
    context = e2ee_context(DeterministicMLSProvider(7), group_id)
    transport = BlockingEncryptedFakeTransport()
    voice = VoiceClient(
        bot,
        "https://chat.example",
        encrypted_grant(group_id),
        transport,
        e2ee_context=context,
    )

    connect_task = asyncio.create_task(voice.connect())
    await transport.connect_started.wait()
    context.invalidate("MLS epoch advanced")

    with pytest.raises(RuntimeError, match="MLS epoch advanced"):
        await connect_task
    assert transport.disconnected.is_set()
    assert transport.cleared
    assert transport.configured_key is None


@pytest.mark.asyncio
async def test_cancelled_encrypted_connect_clears_transport_key() -> None:
    bot = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    group_id = b"g" * 32
    transport = BlockingEncryptedFakeTransport()
    voice = VoiceClient(
        bot,
        "https://chat.example",
        encrypted_grant(group_id),
        transport,
        e2ee_context=e2ee_context(DeterministicMLSProvider(7), group_id),
    )

    connect_task = asyncio.create_task(voice.connect())
    await transport.connect_started.wait()
    connect_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await connect_task
    assert transport.disconnected.is_set()
    assert transport.cleared
    assert transport.configured_key is None


@pytest.mark.asyncio
async def test_device_revocation_disconnects_and_clears_encrypted_voice() -> None:
    bot = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    group_id = b"g" * 32
    context = e2ee_context(DeterministicMLSProvider(7), group_id)
    transport = EncryptedFakeTransport()
    voice = VoiceClient(
        bot,
        "https://chat.example",
        encrypted_grant(group_id),
        transport,
        e2ee_context=context,
    )

    await voice.connect()
    assert transport.connected
    context.revoke()
    await asyncio.wait_for(transport.disconnected.wait(), 1)

    assert transport.cleared
    bot.request.assert_awaited_once_with(
        "DELETE",
        "/api/v1/bots/channels/2@chat.example/voice",
        target="https://chat.example",
        json={"connection_id": "c" * 43, "generation": 3},
        headers={},
    )


@pytest.mark.asyncio
async def test_provider_epoch_change_disconnects_active_encrypted_voice() -> None:
    bot = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    group_id = b"g" * 32
    provider = DeterministicMLSProvider(7)
    context = e2ee_context(provider, group_id)
    transport = EncryptedFakeTransport()
    voice = VoiceClient(
        bot,
        "https://chat.example",
        encrypted_grant(group_id),
        transport,
        e2ee_context=context,
    )

    await voice.connect()
    provider.epoch = 8
    await asyncio.wait_for(transport.disconnected.wait(), 1)

    assert context.invalidated
    assert context.invalidation_reason == "bot MLS voice group epoch changed"
    assert transport.cleared


def test_real_livekit_room_options_accept_encryption_provider_when_installed() -> None:
    rtc = pytest.importorskip("livekit.rtc")
    options = rtc.E2EEOptions()
    options.key_provider_options.shared_key = b"k" * MEDIA_KEY_BYTES
    options.key_provider_options.ratchet_salt = MEDIA_RATCHET_SALT
    room_options = rtc.RoomOptions(encryption=options)

    assert room_options.encryption is options
    assert room_options.encryption.key_provider_options.shared_key == (
        b"k" * MEDIA_KEY_BYTES
    )


def test_real_openmls_ffi_derives_voice_key_when_library_is_available() -> None:
    library_path = os.environ.get("KAEDE_E2EE_LIBRARY")
    if library_path is None:
        pytest.skip("real OpenMLS C ABI was not built for this test run")

    provider = NativeOpenMLSProvider.generate(
        b"voice-bot@example.test",
        library_path=library_path,
    )
    group_id = b"g" * 32
    try:
        provider.create_group(group_id)
        assert provider.group_epoch(group_id) == 0
        context = e2ee_context(provider, group_id, epoch=0)
        media_key = context.derive_media_key(encrypted_grant(group_id, epoch=0))
        try:
            assert len(media_key) == MEDIA_KEY_BYTES
        finally:
            media_key[:] = b"\0" * len(media_key)
    finally:
        provider.close()
