from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from ._encoding import encode_base64url as _base64url
from .audio import AudioFrame, AudioSource, FFmpegAudioSource
from .e2ee import E2EEProvider, require_real_e2ee_provider
from .generated import (
    PRIORITY_SPEAKER_ACTIVE_PAYLOAD,
    PRIORITY_SPEAKER_INACTIVE_PAYLOAD,
    PRIORITY_SPEAKER_TOPIC,
)
from .refs import EntityRef

if TYPE_CHECKING:
    from .client import Client
    from .soundboard import SoundboardSound

AudioListener = Callable[[AudioFrame, str], Awaitable[None]]

MEDIA_E2EE_PROTOCOL = "livekit-e2ee-v1"
MEDIA_E2EE_SUITE = "AES-256-GCM"
MEDIA_EXPORTER_LABEL = "kaede livekit v1"
MEDIA_EXPORTER_CONTEXT_VERSION = "kaede-livekit-key-v1"
MEDIA_SESSION_CONTEXT_VERSION = "kaede-livekit-session-v1"
MEDIA_RATCHET_SALT = b"kaede-livekit-v1"
MEDIA_KEY_BYTES = 32
MEDIA_EPOCH_POLL_SECONDS = 0.5
VIDEO_SOURCES = frozenset({"camera", "screen_share"})
VIDEO_PIXEL_FORMATS = frozenset(
    {
        "abgr",
        "argb",
        "bgra",
        "i010",
        "i420",
        "i420a",
        "i422",
        "i444",
        "nv12",
        "rgb24",
        "rgba",
    }
)


def _optional_wire_uint(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 20
    ):
        raise ValueError(f"voice grant contained an invalid {field_name}")
    parsed = int(value)
    if str(parsed) != value or parsed > (1 << 64) - 1:
        raise ValueError(f"voice grant contained an invalid {field_name}")
    return parsed


def _clear_bytes(value: bytearray | None) -> None:
    if value is not None:
        value[:] = b"\0" * len(value)


@dataclass(frozen=True, slots=True)
class VoiceRegion:
    id: str
    name: str
    optimal: bool
    deprecated: bool
    custom: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VoiceRegion:
        region_id = payload.get("id")
        name = payload.get("name")
        if (
            not isinstance(region_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", region_id)
            or not isinstance(name, str)
            or not name.strip()
            or name != name.strip()
            or len(name) > 100
            or any(ord(character) < 32 for character in name)
        ):
            raise ValueError("voice region payload is invalid")
        for flag_name in ("optimal", "deprecated", "custom"):
            if not isinstance(payload.get(flag_name), bool):
                raise ValueError("voice region payload is invalid")
        return cls(
            id=region_id,
            name=name,
            optimal=payload["optimal"],
            deprecated=payload["deprecated"],
            custom=payload["custom"],
        )


@dataclass(frozen=True, slots=True)
class VideoFrame:
    """One decoded or outbound camera/screen-share frame."""

    data: bytes
    width: int
    height: int
    pixel_format: str
    source: str

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("video frame data cannot be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("video frame dimensions must be positive")
        if not self.pixel_format:
            raise ValueError("video frame pixel format is required")
        if self.source not in {"camera", "screen_share", "unknown"}:
            raise ValueError(
                "video frame source must be camera, screen_share, or unknown"
            )


VideoListener = Callable[[VideoFrame, str], Awaitable[None]]


def _video_frame_data_size(frame: VideoFrame) -> int:
    """Return the exact packed byte length LiveKit expects for ``frame``."""

    width = frame.width
    height = frame.height
    chroma_width = (width + 1) // 2
    chroma_height = (height + 1) // 2
    pixel_format = frame.pixel_format.strip().lower()
    if pixel_format in {"abgr", "argb", "bgra", "rgba"}:
        return width * height * 4
    if pixel_format in {"i444", "rgb24"}:
        return width * height * 3
    if pixel_format in {"i420", "nv12"}:
        return width * height + chroma_width * chroma_height * 2
    if pixel_format == "i420a":
        return width * height * 2 + chroma_width * chroma_height * 2
    if pixel_format == "i422":
        return width * height + chroma_width * height * 2
    if pixel_format == "i010":
        return width * height * 2 + chroma_width * chroma_height * 4
    raise ValueError(
        "outbound video pixel format must be one of "
        + ", ".join(sorted(VIDEO_PIXEL_FORMATS))
    )


def _validate_outbound_video_frame(frame: VideoFrame) -> str:
    if frame.source not in VIDEO_SOURCES:
        raise ValueError("outbound video source must be camera or screen_share")
    pixel_format = frame.pixel_format.strip().lower()
    expected = _video_frame_data_size(frame)
    if len(frame.data) != expected:
        raise ValueError(
            f"{pixel_format} video frame requires exactly {expected} bytes"
        )
    return pixel_format


@dataclass(frozen=True, slots=True)
class VoiceGrant:
    token: str
    url: str
    room: str
    generation: int
    connection_id: str
    expires_at: str
    can_listen: bool
    can_speak: bool
    can_stream: bool
    can_priority_speak: bool
    can_use_vad: bool
    bitrate: int
    user_limit: int
    rtc_region: str | None
    video_quality_mode: int
    e2ee: bool
    channel_ref: EntityRef
    encryption_policy_generation: int | None
    encryption_epoch: int | None
    media_protocol: str | None
    media_suite: str | None
    media_session_id: str | None
    media_epoch: int | None
    move_session_id: str | None
    guild_ref: EntityRef | None = None
    bot_installation_revision: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VoiceGrant:
        parsed = urlsplit(str(payload["url"]))
        if (
            parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("voice server returned an unsafe LiveKit URL")
        e2ee = payload.get("e2ee")
        if type(e2ee) is not bool:
            raise ValueError("voice grant omitted its encryption policy")
        can_listen = payload.get("can_listen", True)
        can_speak = payload.get("can_speak", False)
        can_stream = payload.get("can_stream", False)
        can_priority_speak = payload.get("can_priority_speak", False)
        can_use_vad = payload.get("can_use_vad", True)
        if any(
            type(value) is not bool
            for value in (
                can_listen,
                can_speak,
                can_stream,
                can_priority_speak,
                can_use_vad,
            )
        ):
            raise ValueError("voice grant contained an invalid media permission")
        room = str(payload["room"])
        connection_id = str(payload["connection_id"])
        generation = int(payload["generation"])
        token = str(payload["token"])
        if not re.fullmatch(r"[gd]\.[0-9]+\.[0-9]+", room):
            raise ValueError("voice grant contained an invalid room")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", connection_id):
            raise ValueError("voice grant contained an invalid connection ID")
        if generation < 0 or not 20 <= len(token) <= 4096:
            raise ValueError("voice grant contained invalid capability metadata")
        domain = payload.get("channel_domain")
        channel_id = payload.get("channel_id")
        if channel_id is None or not isinstance(domain, str):
            raise ValueError("voice grant is missing its channel reference")
        guild_domain = payload.get("guild_domain")
        guild_id = payload.get("guild_id")
        if (guild_id is None) != (guild_domain is None) or (
            guild_domain is not None and not isinstance(guild_domain, str)
        ):
            raise ValueError("voice grant contained an incomplete guild reference")
        if can_priority_speak and (not can_speak or guild_id is None):
            raise ValueError("voice grant contained invalid priority speaking access")
        installation_revision = _optional_wire_uint(
            payload.get("bot_installation_revision"), "bot installation revision"
        )
        bitrate_raw = payload.get("bitrate", 64_000)
        user_limit_raw = payload.get("user_limit", 0)
        video_quality_mode_raw = payload.get("video_quality_mode", 1)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (bitrate_raw, user_limit_raw, video_quality_mode_raw)
        ):
            raise ValueError("voice grant contained invalid numeric media policy")
        bitrate = bitrate_raw
        user_limit = user_limit_raw
        video_quality_mode = video_quality_mode_raw
        rtc_region_raw = payload.get("rtc_region")
        if not 8_000 <= bitrate <= 384_000:
            raise ValueError("voice grant contained an invalid audio bitrate")
        # The grant intentionally omits channel_type. Voice channels are capped
        # at 99 by their authority while Stage channels support up to 10,000.
        if not 0 <= user_limit <= 10_000:
            raise ValueError("voice grant contained an invalid user limit")
        if video_quality_mode not in {1, 2}:
            raise ValueError("voice grant contained an invalid video quality mode")
        if rtc_region_raw is not None and (
            not isinstance(rtc_region_raw, str)
            or not rtc_region_raw.strip()
            or len(rtc_region_raw) > 64
        ):
            raise ValueError("voice grant contained an invalid RTC region")
        policy_generation = _optional_wire_uint(
            payload.get("encryption_policy_generation"),
            "encryption policy generation",
        )
        encryption_epoch = _optional_wire_uint(
            payload.get("encryption_epoch"), "encryption epoch"
        )
        media_epoch = _optional_wire_uint(payload.get("media_epoch"), "media epoch")
        media_protocol = payload.get("media_protocol")
        media_suite = payload.get("media_suite")
        media_session_id = payload.get("media_session_id")
        move_session_id = payload.get("move_session_id")
        if move_session_id is not None and (
            not isinstance(move_session_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,64}", move_session_id) is None
        ):
            raise ValueError("voice grant contained an invalid move session ID")
        encryption_context = (
            policy_generation,
            encryption_epoch,
            media_protocol,
            media_suite,
            media_session_id,
            media_epoch,
        )
        if e2ee:
            if (
                policy_generation is None
                or encryption_epoch is None
                or media_epoch != encryption_epoch
                or media_protocol != MEDIA_E2EE_PROTOCOL
                or media_suite != MEDIA_E2EE_SUITE
                or not isinstance(media_session_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", media_session_id) is None
            ):
                raise ValueError(
                    "encrypted E2EE voice grant contained an invalid MLS media context"
                )
        elif any(item is not None for item in encryption_context):
            raise ValueError("plaintext voice grant contained an encryption context")
        return cls(
            token=token,
            url=str(payload["url"]),
            room=room,
            generation=generation,
            connection_id=connection_id,
            expires_at=str(payload["expires_at"]),
            can_listen=can_listen,
            can_speak=can_speak,
            can_stream=can_stream,
            can_priority_speak=can_priority_speak,
            can_use_vad=can_use_vad,
            bitrate=bitrate,
            user_limit=user_limit,
            rtc_region=(
                rtc_region_raw.strip() if isinstance(rtc_region_raw, str) else None
            ),
            video_quality_mode=video_quality_mode,
            e2ee=e2ee,
            channel_ref=EntityRef.from_wire(channel_id, domain),
            encryption_policy_generation=policy_generation,
            encryption_epoch=encryption_epoch,
            media_protocol=(
                media_protocol if isinstance(media_protocol, str) else None
            ),
            media_suite=media_suite if isinstance(media_suite, str) else None,
            media_session_id=(
                media_session_id if isinstance(media_session_id, str) else None
            ),
            media_epoch=media_epoch,
            move_session_id=move_session_id,
            guild_ref=(
                EntityRef.from_wire(guild_id, guild_domain)
                if guild_id is not None and isinstance(guild_domain, str)
                else None
            ),
            bot_installation_revision=installation_revision,
        )


def _expected_media_session_id(grant: VoiceGrant, group_id: bytes) -> str:
    if (
        grant.encryption_policy_generation is None
        or grant.encryption_epoch is None
        or grant.media_protocol is None
        or grant.media_suite is None
    ):
        raise ValueError("encrypted voice grant omitted its MLS media context")
    context = "\0".join(
        (
            MEDIA_SESSION_CONTEXT_VERSION,
            grant.room,
            str(grant.channel_ref),
            _base64url(group_id),
            str(grant.encryption_policy_generation),
            str(grant.encryption_epoch),
            grant.media_protocol,
            grant.media_suite,
        )
    ).encode()
    return _base64url(hashlib.sha256(context).digest())


def _media_exporter_context(grant: VoiceGrant) -> bytes:
    if (
        grant.media_protocol is None
        or grant.media_suite is None
        or grant.media_session_id is None
        or grant.media_epoch is None
    ):
        raise ValueError("encrypted voice grant omitted its MLS media context")
    return "\0".join(
        (
            MEDIA_EXPORTER_CONTEXT_VERSION,
            grant.media_protocol,
            grant.media_suite,
            grant.media_session_id,
            str(grant.media_epoch),
            grant.room,
        )
    ).encode()


@dataclass(frozen=True, slots=True)
class VoiceE2EEContext:
    """Verified bot-device MLS state for one encrypted voice channel.

    The context is intentionally separate from the LiveKit bearer grant. The
    grant proves the media authority's current room policy; this object proves
    that the worker owns the matching approved MLS device and current group
    state. ``invalidate`` is called when that device is revoked or the control
    log advances, which fences an attached voice client immediately.
    """

    provider: E2EEProvider
    device_id: str
    channel_ref: EntityRef
    group_id: bytes
    epoch: int
    _invalidated: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    _invalidation_reason: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", require_real_e2ee_provider(self.provider))
        if re.fullmatch(r"kbe_[A-Za-z0-9_-]{43}", self.device_id) is None:
            raise ValueError("bot E2EE device ID is invalid")
        if not isinstance(self.group_id, bytes) or len(self.group_id) != 32:
            raise ValueError("bot voice MLS group ID must contain exactly 32 bytes")
        if (
            not isinstance(self.epoch, int)
            or isinstance(self.epoch, bool)
            or not 0 <= self.epoch <= (1 << 64) - 1
        ):
            raise ValueError("bot voice MLS epoch is invalid")

    @property
    def invalidated(self) -> bool:
        return self._invalidated.is_set()

    @property
    def invalidation_reason(self) -> str | None:
        return self._invalidation_reason

    def invalidate(self, reason: str = "MLS voice context changed") -> None:
        """Fence this context after device revocation or an MLS state change."""

        if not self._invalidated.is_set():
            object.__setattr__(self, "_invalidation_reason", reason)
            self._invalidated.set()

    def revoke(self) -> None:
        self.invalidate("bot E2EE device was revoked")

    async def wait_invalidated(self) -> None:
        while not self._invalidated.is_set():
            try:
                current_epoch = self.provider.group_epoch(self.group_id)
            except Exception:
                self.invalidate("bot MLS provider became unavailable")
                break
            if current_epoch != self.epoch:
                self.invalidate("bot MLS voice group epoch changed")
                break
            try:
                await asyncio.wait_for(
                    self._invalidated.wait(),
                    timeout=MEDIA_EPOCH_POLL_SECONDS,
                )
            except TimeoutError:
                continue

    def derive_media_key(self, grant: VoiceGrant) -> bytearray:
        """Validate the full grant/group/epoch binding and derive its key."""

        if self.invalidated:
            raise RuntimeError(
                self.invalidation_reason or "bot E2EE context is invalid"
            )
        if not grant.e2ee:
            raise ValueError(
                "an MLS voice context cannot be used with a plaintext grant"
            )
        if grant.channel_ref != self.channel_ref:
            raise ValueError("voice grant does not match the MLS channel")
        if grant.encryption_epoch != self.epoch or grant.media_epoch != self.epoch:
            raise ValueError("voice grant does not match the current MLS epoch")
        if grant.media_session_id != _expected_media_session_id(grant, self.group_id):
            raise ValueError("voice grant does not match the current MLS group")
        try:
            provider_epoch = self.provider.group_epoch(self.group_id)
        except Exception as exc:
            self.invalidate("bot MLS provider became unavailable")
            raise RuntimeError("bot MLS provider became unavailable") from exc
        if provider_epoch != self.epoch:
            self.invalidate("bot MLS voice group epoch changed")
            raise ValueError("bot MLS provider state is stale for this voice grant")

        secret = bytearray(
            self.provider.export_epoch_secret(
                self.group_id,
                MEDIA_EXPORTER_LABEL,
                _media_exporter_context(grant),
                MEDIA_KEY_BYTES,
            )
        )
        valid = False
        try:
            try:
                provider_epoch = self.provider.group_epoch(self.group_id)
            except Exception as exc:
                self.invalidate("bot MLS provider became unavailable")
                raise RuntimeError("bot MLS provider became unavailable") from exc
            if provider_epoch != self.epoch:
                self.invalidate("bot MLS voice group epoch changed")
            if (
                len(secret) != MEDIA_KEY_BYTES
                or provider_epoch != self.epoch
                or self.invalidated
            ):
                raise RuntimeError(
                    "MLS state changed while deriving the voice media key"
                )
            valid = True
            return secret
        finally:
            if not valid:
                _clear_bytes(secret)


class VoiceTransport(Protocol):
    async def connect(self, grant: VoiceGrant, listener: AudioListener) -> None: ...

    async def send_frame(self, frame: AudioFrame) -> None: ...

    async def disconnect(self) -> None: ...


@runtime_checkable
class EncryptedVoiceTransport(Protocol):
    def configure_e2ee(self, grant: VoiceGrant, media_key: bytearray) -> None: ...

    def clear_e2ee(self) -> None: ...


@runtime_checkable
class VideoPublishTransport(Protocol):
    async def send_video_frame(self, frame: VideoFrame) -> None: ...

    async def stop_video(self, source: str) -> None: ...


@runtime_checkable
class PrioritySpeakerTransport(Protocol):
    async def send_priority_speaker(self, active: bool) -> None: ...


@dataclass(slots=True)
class _PublishedVideo:
    source: Any
    publication: Any
    width: int
    height: int


class LiveKitTransport:
    """Optional LiveKit transport, imported only when voice is used."""

    def __init__(self) -> None:
        self._room: Any = None
        self._source: Any = None
        self._listener: AudioListener | None = None
        self._video_listener: VideoListener | None = None
        self._receive_tasks: set[asyncio.Task[None]] = set()
        self._grant: VoiceGrant | None = None
        self._configured_media_key: bytearray | None = None
        self._published_video: dict[str, _PublishedVideo] = {}
        self._video_publish_lock = asyncio.Lock()

    def configure_e2ee(self, grant: VoiceGrant, media_key: bytearray) -> None:
        """Stage a validated MLS exporter key for the next LiveKit connect."""

        self.clear_e2ee()
        if not grant.e2ee or len(media_key) != MEDIA_KEY_BYTES:
            raise ValueError("encrypted LiveKit transport requires a 32-byte media key")
        self._configured_media_key = bytearray(media_key)

    def clear_e2ee(self) -> None:
        _clear_bytes(self._configured_media_key)
        self._configured_media_key = None

    def set_video_listener(self, listener: VideoListener) -> None:
        self._video_listener = listener

    @staticmethod
    def _video_source(rtc: Any, publication: Any) -> str:
        value = getattr(publication, "source", None)
        try:
            raw = str(rtc.TrackSource.Name(value)).lower()
        except (AttributeError, TypeError, ValueError):
            raw = str(value if value is not None else "").lower()
        if "screen" in raw:
            return "screen_share"
        if "camera" in raw:
            return "camera"
        return "unknown"

    @staticmethod
    def _video_pixel_format(rtc: Any, frame: Any) -> str:
        raw = getattr(frame, "type", None)
        try:
            return str(rtc.VideoBufferType.Name(raw)).lower()
        except (AttributeError, TypeError, ValueError):
            return str(raw if raw is not None else "unknown").lower()

    async def connect(self, grant: VoiceGrant, listener: AudioListener) -> None:
        try:
            from livekit import rtc  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "LiveKit voice support is optional; install kaede-bot[voice]"
            ) from exc
        if self._room is not None:
            raise RuntimeError("voice transport is already connected")
        if grant.e2ee != (self._configured_media_key is not None):
            self.clear_e2ee()
            raise RuntimeError("voice transport encryption does not match its grant")
        self._listener = listener
        self._grant = grant
        e2ee_options: Any = None
        room: Any = None
        try:
            if grant.e2ee:
                # Supplying the shared key in RoomOptions installs it in
                # LiveKit's native key provider as part of connect, before any
                # subscribed or locally-published frame can flow. `encryption`
                # is the current API; the deprecated `e2ee` alias is avoided.
                e2ee_options = rtc.E2EEOptions()
                e2ee_options.key_provider_options.shared_key = bytes(
                    self._configured_media_key or b""
                )
                e2ee_options.key_provider_options.ratchet_salt = MEDIA_RATCHET_SALT
            room_options = rtc.RoomOptions(
                auto_subscribe=grant.can_listen,
                encryption=e2ee_options,
            )
            room = rtc.Room()
            self._room = room

            @room.on("track_subscribed")  # type: ignore[untyped-decorator]
            def on_track_subscribed(
                track: Any, publication: Any, participant: Any
            ) -> None:
                if not grant.can_listen:
                    return
                kind = getattr(track, "kind", None)
                if kind == rtc.TrackKind.KIND_AUDIO:

                    async def consume_audio() -> None:
                        async for event in rtc.AudioStream(track):
                            raw = event.frame
                            frame = AudioFrame(
                                bytes(raw.data),
                                int(raw.sample_rate),
                                int(raw.num_channels),
                            )
                            if self._listener is not None:
                                await self._listener(frame, str(participant.identity))

                    coroutine = consume_audio()
                    task_name = "kaede-voice-receive-audio"
                elif kind == rtc.TrackKind.KIND_VIDEO:
                    source = self._video_source(rtc, publication)

                    async def consume_video() -> None:
                        async for event in rtc.VideoStream(track):
                            raw = event.frame
                            frame = VideoFrame(
                                data=bytes(raw.data),
                                width=int(raw.width),
                                height=int(raw.height),
                                pixel_format=self._video_pixel_format(rtc, raw),
                                source=source,
                            )
                            if self._video_listener is not None:
                                await self._video_listener(
                                    frame, str(participant.identity)
                                )

                    coroutine = consume_video()
                    task_name = "kaede-voice-receive-video"
                else:
                    return
                task = asyncio.create_task(coroutine, name=task_name)
                self._receive_tasks.add(task)
                task.add_done_callback(self._receive_tasks.discard)

            await room.connect(grant.url, grant.token, options=room_options)
            if grant.e2ee:
                manager = room.e2ee_manager
                if not manager.enabled or manager.key_provider is None:
                    raise RuntimeError("LiveKit did not enable its E2EE key provider")
        except BaseException:
            self._room = None
            if room is not None:
                with suppress(Exception):
                    await room.disconnect()
            self._grant = None
            self._listener = None
            raise
        finally:
            if e2ee_options is not None:
                # LiveKit has copied the key into the native provider. Drop the
                # Python options reference and erase our mutable staging copy.
                e2ee_options.key_provider_options.shared_key = None
            self.clear_e2ee()

    async def send_frame(self, frame: AudioFrame) -> None:
        if self._room is None:
            raise RuntimeError("voice transport is not connected")
        try:
            from livekit import rtc
        except ImportError as exc:  # pragma: no cover - guarded during connect
            raise RuntimeError("LiveKit voice support is unavailable") from exc
        if self._source is None:
            self._source = rtc.AudioSource(frame.sample_rate, frame.channels)
            track = rtc.LocalAudioTrack.create_audio_track("kaede-bot", self._source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            options.audio_encoding = rtc.AudioEncoding(
                max_bitrate=self._grant.bitrate if self._grant is not None else 64_000
            )
            await self._room.local_participant.publish_track(track, options)
        raw = rtc.AudioFrame(
            data=frame.data,
            sample_rate=frame.sample_rate,
            num_channels=frame.channels,
            samples_per_channel=frame.samples_per_channel,
        )
        await self._source.capture_frame(raw)

    async def _unpublish_video(self, source: str) -> None:
        published = self._published_video.pop(source, None)
        if published is None:
            return
        try:
            if self._room is not None:
                await self._room.local_participant.unpublish_track(
                    str(published.publication.sid)
                )
        finally:
            close = getattr(published.source, "aclose", None)
            if callable(close):
                await close()

    async def send_video_frame(self, frame: VideoFrame) -> None:
        """Publish one camera or screen-share frame through LiveKit."""

        if self._room is None or self._grant is None:
            raise RuntimeError("voice transport is not connected")
        if not self._grant.can_stream:
            raise RuntimeError("this voice grant does not allow video publishing")
        pixel_format = _validate_outbound_video_frame(frame)
        try:
            from livekit import rtc
        except ImportError as exc:  # pragma: no cover - guarded during connect
            raise RuntimeError("LiveKit voice support is unavailable") from exc

        async with self._video_publish_lock:
            published = self._published_video.get(frame.source)
            if published is not None and (
                published.width != frame.width or published.height != frame.height
            ):
                await self._unpublish_video(frame.source)
                published = None
            created = False
            if published is None:
                source = rtc.VideoSource(
                    frame.width,
                    frame.height,
                    is_screencast=frame.source == "screen_share",
                )
                track = rtc.LocalVideoTrack.create_video_track(frame.source, source)
                options = rtc.TrackPublishOptions()
                options.source = (
                    rtc.TrackSource.SOURCE_CAMERA
                    if frame.source == "camera"
                    else rtc.TrackSource.SOURCE_SCREENSHARE
                )
                options.simulcast = True
                try:
                    publication = await self._room.local_participant.publish_track(
                        track, options
                    )
                except BaseException:
                    close = getattr(source, "aclose", None)
                    if callable(close):
                        await close()
                    raise
                published = _PublishedVideo(
                    source=source,
                    publication=publication,
                    width=frame.width,
                    height=frame.height,
                )
                self._published_video[frame.source] = published
                created = True
            try:
                raw = rtc.VideoFrame(
                    frame.width,
                    frame.height,
                    getattr(rtc.VideoBufferType, pixel_format.upper()),
                    frame.data,
                )
                published.source.capture_frame(raw)
            except BaseException:
                if created:
                    await self._unpublish_video(frame.source)
                raise

    async def stop_video(self, source: str) -> None:
        if source not in VIDEO_SOURCES:
            raise ValueError("video source must be camera or screen_share")
        async with self._video_publish_lock:
            await self._unpublish_video(source)

    async def send_priority_speaker(self, active: bool) -> None:
        """Publish the bounded reliable Kaede priority-speaking signal."""

        if type(active) is not bool:
            raise ValueError("priority speaking state must be a boolean")
        if self._room is None or self._grant is None:
            raise RuntimeError("voice transport is not connected")
        await self._room.local_participant.publish_data(
            (
                PRIORITY_SPEAKER_ACTIVE_PAYLOAD
                if active
                else PRIORITY_SPEAKER_INACTIVE_PAYLOAD
            ),
            reliable=True,
            topic=PRIORITY_SPEAKER_TOPIC,
        )

    async def disconnect(self) -> None:
        for task in self._receive_tasks:
            task.cancel()
        if self._receive_tasks:
            await asyncio.gather(*self._receive_tasks, return_exceptions=True)
        self._receive_tasks.clear()
        room = self._room
        self._room = None
        try:
            if room is not None:
                manager = getattr(room, "e2ee_manager", None)
                if manager is not None:
                    # LiveKit exposes no delete-key call. Disable every frame
                    # cryptor, replace the active key slot, then destroy the
                    # room/FFI handle so the prior exporter key cannot be reused.
                    if getattr(manager, "enabled", False):
                        with suppress(Exception):
                            manager.set_enabled(False)
                    provider = getattr(manager, "key_provider", None)
                    if provider is not None:
                        with suppress(Exception):
                            provider.set_shared_key(b"\0" * MEDIA_KEY_BYTES, 0)
                await room.disconnect()
        finally:
            for published in self._published_video.values():
                close = getattr(published.source, "aclose", None)
                if callable(close):
                    with suppress(Exception):
                        await close()
            self._published_video.clear()
            self.clear_e2ee()
            self._source = None
            self._grant = None
            self._listener = None


def _scaled_frame(frame: AudioFrame, volume: float) -> AudioFrame:
    if volume == 1:
        return frame
    if not 0 <= volume <= 1:
        raise ValueError("volume must be between 0 and 1")
    output = bytearray(len(frame.data))
    for offset in range(0, len(frame.data), 2):
        sample = int.from_bytes(frame.data[offset : offset + 2], "little", signed=True)
        output[offset : offset + 2] = int(sample * volume).to_bytes(
            2, "little", signed=True
        )
    return AudioFrame(bytes(output), frame.sample_rate, frame.channels)


class VoiceClient:
    def __init__(
        self,
        client: Client,
        target: str,
        grant: VoiceGrant,
        transport: VoiceTransport,
        e2ee_context: VoiceE2EEContext | None = None,
        runtime_headers: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.target = target
        self.grant = grant
        self.transport = transport
        self.e2ee_context = e2ee_context
        self._runtime_headers = dict(runtime_headers or {})
        self._listeners: list[AudioListener] = []
        self._video_listeners: list[VideoListener] = []
        self._play_lock = asyncio.Lock()
        self._video_lock = asyncio.Lock()
        self._priority_lock = asyncio.Lock()
        self._playback_gate = asyncio.Event()
        self._playback_gate.set()
        self._playback_done = asyncio.Event()
        self._playback_done.set()
        self._stop_playback = False
        self._closed = False
        self._connected = False
        self.self_mute = False
        self.self_deaf = False
        self.server_mute = False
        self.server_deaf = False
        self._priority_speaking = False
        self._e2ee_fence_task: asyncio.Task[None] | None = None
        configure_video = getattr(self.transport, "set_video_listener", None)
        if callable(configure_video):
            configure_video(self._receive_video)

    async def _receive(self, frame: AudioFrame, participant: str) -> None:
        if self.self_deaf or self.server_deaf or not self.grant.can_listen:
            return
        for listener in tuple(self._listeners):
            await listener(frame, participant)

    async def _receive_video(self, frame: VideoFrame, participant: str) -> None:
        if self.self_deaf or self.server_deaf or not self.grant.can_listen:
            return
        for listener in tuple(self._video_listeners):
            await listener(frame, participant)

    def _clear_transport_e2ee(self) -> None:
        clear_e2ee = getattr(self.transport, "clear_e2ee", None)
        if callable(clear_e2ee):
            clear_e2ee()

    async def _disconnect_transport(self) -> None:
        try:
            await self.transport.disconnect()
        finally:
            self._clear_transport_e2ee()

    async def _quiesce_transport(self) -> None:
        self.stop()
        async with self._priority_lock:
            await self._clear_priority_speaking_unlocked()
            self._connected = False
        await self._playback_done.wait()
        fence_task = self._e2ee_fence_task
        self._e2ee_fence_task = None
        if fence_task is not None and fence_task is not asyncio.current_task():
            fence_task.cancel()
            await asyncio.gather(fence_task, return_exceptions=True)
        async with self._video_lock:
            # Let an in-flight video publisher finish before disconnecting.
            await self._disconnect_transport()

    async def _release_grant(self, grant: VoiceGrant) -> None:
        await self.client.request(
            "DELETE",
            f"/api/v1/bots/channels/{grant.channel_ref}/voice",
            target=self.target,
            json={
                "connection_id": grant.connection_id,
                "generation": grant.generation,
            },
            headers=self._runtime_headers,
        )

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def move_to(
        self,
        grant: VoiceGrant,
        *,
        e2ee_context: VoiceE2EEContext | None,
    ) -> None:
        """Reconnect this exact voice session after an authority moderator move."""

        if self._closed:
            return
        if grant.connection_id != self.grant.connection_id:
            raise ValueError("voice move changed the connection identity")
        context_error: Exception | None = None
        if grant.e2ee != (e2ee_context is not None):
            context_error = RuntimeError(
                "encrypted moderator move requires registered MLS state for the target channel"
            )
        elif e2ee_context is not None and e2ee_context.channel_ref != grant.channel_ref:
            context_error = ValueError(
                "voice move MLS state does not match the target channel"
            )
        if context_error is not None:
            await self.authority_disconnect()
            with suppress(Exception):
                await self._release_grant(grant)
            raise context_error
        await self._quiesce_transport()
        self.grant = grant
        self.e2ee_context = e2ee_context
        try:
            await self.connect()
        except BaseException:
            self._closed = True
            self.client._forget_voice_client(self)
            with suppress(Exception):
                await self._release_grant(grant)
            raise

    async def apply_authority_state(
        self,
        *,
        generation: int,
        server_mute: bool | None,
        server_deaf: bool | None,
        can_listen: bool | None,
        can_speak: bool | None,
        can_stream: bool | None,
        can_priority_speak: bool | None = None,
    ) -> None:
        """Advance a private reconnect fence after mute, deafen, or Stage changes."""

        async with self._priority_lock:
            if self._closed or generation < self.grant.generation:
                return
            previous_can_stream = self.grant.can_stream
            next_can_speak = self.grant.can_speak if can_speak is None else can_speak
            next_can_priority = (
                self.grant.can_priority_speak
                if can_priority_speak is None
                else can_priority_speak
            )
            if self._priority_speaking and (
                server_mute is True or not next_can_speak or not next_can_priority
            ):
                await self._clear_priority_speaking_unlocked()
            self.grant = replace(
                self.grant,
                generation=generation,
                can_listen=(
                    self.grant.can_listen if can_listen is None else can_listen
                ),
                can_speak=next_can_speak,
                can_stream=(
                    self.grant.can_stream if can_stream is None else can_stream
                ),
                can_priority_speak=next_can_priority,
            )
            if server_mute is not None:
                self.server_mute = server_mute
        if self.server_mute or not self.grant.can_speak:
            self.pause()
        if server_deaf is not None:
            self.server_deaf = server_deaf
        if previous_can_stream and not self.grant.can_stream:
            if not isinstance(self.transport, VideoPublishTransport):
                await self.authority_disconnect()
                raise RuntimeError(
                    "video permission was revoked from an unsupported voice transport"
                )
            try:
                async with self._video_lock:
                    await self.transport.stop_video("camera")
                    await self.transport.stop_video("screen_share")
            except BaseException:
                await self.authority_disconnect()
                raise

    async def authority_disconnect(self) -> None:
        """Close locally after an exact, server-revoked connection fence."""

        if self._closed:
            return
        self._closed = True
        try:
            await self._quiesce_transport()
        finally:
            self.client._forget_voice_client(self)

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("voice client is closed")
        if self._connected:
            raise RuntimeError("voice client is already connected")
        if self.grant.e2ee:
            if self.e2ee_context is None:
                raise RuntimeError(
                    "encrypted bot voice requires a verified MLS device context"
                )
            if not isinstance(self.transport, EncryptedVoiceTransport):
                raise RuntimeError(
                    "this voice transport cannot install LiveKit E2EE keys"
                )
            media_key = self.e2ee_context.derive_media_key(self.grant)
            try:
                self.transport.configure_e2ee(self.grant, media_key)
            finally:
                _clear_bytes(media_key)
        elif self.e2ee_context is not None:
            raise ValueError("a plaintext voice grant cannot use an MLS device context")

        context = self.e2ee_context
        if context is None:
            try:
                await self.transport.connect(self.grant, self._receive)
            except BaseException:
                with suppress(Exception):
                    await self._disconnect_transport()
                raise
            self._connected = True
            return

        connect_task = asyncio.create_task(
            self.transport.connect(self.grant, self._receive),
            name="kaede-voice-connect",
        )
        invalidated_task = asyncio.create_task(
            context.wait_invalidated(),
            name="kaede-voice-connect-fence",
        )
        connected = False
        try:
            done, _ = await asyncio.wait(
                (connect_task, invalidated_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if invalidated_task in done:
                raise RuntimeError(
                    context.invalidation_reason
                    or "bot E2EE context was invalidated while connecting"
                )
            await connect_task
            if context.invalidated:
                raise RuntimeError(
                    context.invalidation_reason
                    or "bot E2EE context was invalidated while connecting"
                )
            connected = True
        finally:
            if not connect_task.done():
                connect_task.cancel()
            invalidated_task.cancel()
            await asyncio.gather(
                connect_task,
                invalidated_task,
                return_exceptions=True,
            )
            if not connected:
                with suppress(Exception):
                    await self._disconnect_transport()
        self._connected = True
        self._e2ee_fence_task = asyncio.create_task(
            self._fence_invalidated_e2ee(),
            name="kaede-voice-e2ee-fence",
        )

    async def _fence_invalidated_e2ee(self) -> None:
        context = self.e2ee_context
        if context is None:
            return
        await context.wait_invalidated()
        # Revocation cleanup must not leave an unobserved task exception if the
        # best-effort reservation release fails after the transport is fenced.
        with suppress(Exception):
            await self.disconnect()

    def listen(self, listener: AudioListener) -> AudioListener:
        if not self.grant.can_listen:
            raise RuntimeError("this voice grant does not allow listening")
        self._listeners.append(listener)
        return listener

    def listen_video(self, listener: VideoListener) -> VideoListener:
        """Register a decoded camera/screen-share frame listener."""

        if not self.grant.can_listen:
            raise RuntimeError("this voice grant does not allow listening")
        if not callable(getattr(self.transport, "set_video_listener", None)):
            raise RuntimeError("this voice transport does not support video tracks")
        self._video_listeners.append(listener)
        return listener

    async def publish_video(self, frame: VideoFrame) -> None:
        """Publish a camera or screen-share frame for this bot participant."""

        _validate_outbound_video_frame(frame)
        if not isinstance(self.transport, VideoPublishTransport):
            raise RuntimeError("this voice transport does not support video publishing")
        async with self._video_lock:
            if self._closed or not self._connected:
                raise RuntimeError("voice client is not connected")
            if not self.grant.can_stream:
                raise RuntimeError("this voice grant does not allow video publishing")
            await self.transport.send_video_frame(frame)

    async def stop_video(self, source: str) -> None:
        """Unpublish this bot's camera or screen-share track."""

        if source not in VIDEO_SOURCES:
            raise ValueError("video source must be camera or screen_share")
        if not isinstance(self.transport, VideoPublishTransport):
            raise RuntimeError("this voice transport does not support video publishing")
        async with self._video_lock:
            await self.transport.stop_video(source)

    async def set_priority_speaking(self, active: bool) -> None:
        """Signal whether this participant is currently using priority speech."""

        if type(active) is not bool:
            raise ValueError("priority speaking state must be a boolean")
        async with self._priority_lock:
            if not isinstance(self.transport, PrioritySpeakerTransport):
                raise RuntimeError(
                    "this voice transport does not support priority speaking"
                )
            if self._closed or not self._connected:
                raise RuntimeError("voice client is not connected")
            if not self.grant.can_priority_speak:
                raise RuntimeError("this voice grant does not allow priority speaking")
            if active and (
                self.self_mute or self.server_mute or not self.grant.can_speak
            ):
                raise RuntimeError("priority speaking is unavailable while muted")
            if active == self._priority_speaking:
                return
            await self.transport.send_priority_speaker(active)
            self._priority_speaking = active

    async def _clear_priority_speaking(self) -> None:
        async with self._priority_lock:
            await self._clear_priority_speaking_unlocked()

    async def _clear_priority_speaking_unlocked(self) -> None:
        if not self._priority_speaking:
            return
        try:
            if isinstance(self.transport, PrioritySpeakerTransport):
                await self.transport.send_priority_speaker(False)
        except Exception:
            pass
        finally:
            self._priority_speaking = False

    async def play(self, source: AudioSource, *, volume: float = 1) -> None:
        """Play one source to completion with pause/resume/stop controls.

        Run this coroutine in a task when playback should continue alongside
        command handling. Only one source may play at a time, matching
        Discord.py's voice-client contract.
        """

        if self._closed:
            raise RuntimeError("voice client is closed")
        if not self.grant.can_speak:
            raise RuntimeError("this voice grant does not allow speaking")
        if self.self_mute:
            raise RuntimeError("voice playback is disabled while self-muted")
        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        if self._play_lock.locked():
            raise RuntimeError("voice client is already playing audio")
        async with self._play_lock:
            self._stop_playback = False
            self._playback_gate.set()
            self._playback_done.clear()
            try:
                async for frame in source:
                    await self._playback_gate.wait()
                    if self._stop_playback:
                        break
                    await self.transport.send_frame(_scaled_frame(frame, volume))
            finally:
                self._stop_playback = False
                self._playback_gate.set()
                self._playback_done.set()

    async def play_file(
        self,
        source: str | bytes,
        *,
        volume: float = 1,
        sample_rate: int = 48_000,
        channels: int = 2,
    ) -> None:
        """Decode and play a local file path or encoded in-memory audio."""

        await self.play(
            FFmpegAudioSource(source, sample_rate=sample_rate, channels=channels),
            volume=volume,
        )

    @property
    def is_playing(self) -> bool:
        return (
            self._play_lock.locked()
            and self._playback_gate.is_set()
            and not self._stop_playback
        )

    @property
    def is_paused(self) -> bool:
        return self._play_lock.locked() and not self._playback_gate.is_set()

    def pause(self) -> None:
        if self._play_lock.locked() and not self._stop_playback:
            self._playback_gate.clear()

    def resume(self) -> None:
        if self._play_lock.locked() and not self._stop_playback:
            self._playback_gate.set()

    def stop(self) -> None:
        if self._play_lock.locked():
            self._stop_playback = True
            self._playback_gate.set()

    async def play_soundboard(
        self, sound: SoundboardSound, *, volume: float | None = None
    ) -> None:
        await sound.play(self, volume=volume)

    async def set_self_state(
        self,
        *,
        mute: bool | None = None,
        deaf: bool | None = None,
    ) -> None:
        """Update mute/deaf and replace the authority-rotated grant fence."""

        if mute is None and deaf is None:
            raise ValueError("mute or deaf is required")
        async with self._priority_lock:
            requested_deaf = self.self_deaf if deaf is None else deaf
            requested_mute = self.self_mute if mute is None else mute
            requested_mute = requested_mute or requested_deaf
            if requested_mute:
                await self._clear_priority_speaking_unlocked()
            raw = await self.client.request(
                "PATCH",
                f"/api/v1/bots/channels/{self.grant.channel_ref}/voice/@me",
                target=self.target,
                json={
                    "connection_id": self.grant.connection_id,
                    "generation": self.grant.generation,
                    "self_mute": requested_mute,
                    "self_deaf": requested_deaf,
                },
                headers=self._runtime_headers,
            )
            if not isinstance(raw, dict) or not isinstance(raw.get("state"), dict):
                raise ValueError("voice self-state response is invalid")
            state = raw["state"]
            generation = raw.get("generation")
            if type(generation) is not int or generation != self.grant.generation + 1:
                raise ValueError("voice self-state response has an invalid generation")
            if (
                state.get("room") != self.grant.room
                or state.get("self_mute") is not requested_mute
                or state.get("self_deaf") is not requested_deaf
            ):
                raise ValueError("voice self-state response does not match the request")
            self.grant = replace(self.grant, generation=generation)
            self.self_mute = requested_mute
            self.self_deaf = requested_deaf
        if requested_mute:
            self.pause()

    async def set_self_mute(self, muted: bool) -> None:
        await self.set_self_state(mute=muted)

    async def set_self_deaf(self, deafened: bool) -> None:
        await self.set_self_state(deaf=deafened)

    async def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._quiesce_transport()
        finally:
            try:
                await self._release_grant(self.grant)
            finally:
                self.client._forget_voice_client(self)

    async def __aenter__(self) -> VoiceClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()
