from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """One frame of little-endian signed 16-bit interleaved PCM audio."""

    data: bytes
    sample_rate: int = 48_000
    channels: int = 2

    def __post_init__(self) -> None:
        if self.sample_rate < 8_000 or self.sample_rate > 192_000:
            raise ValueError("sample_rate must be between 8000 and 192000")
        if self.channels not in {1, 2}:
            raise ValueError("channels must be 1 or 2")
        if not self.data or len(self.data) % (2 * self.channels):
            raise ValueError("audio frame must contain complete signed 16-bit samples")

    @property
    def samples_per_channel(self) -> int:
        return len(self.data) // (2 * self.channels)


class AudioSource(Protocol):
    def __aiter__(self) -> AsyncIterator[AudioFrame]: ...


class PCM16AudioSource:
    def __init__(
        self,
        data: bytes,
        *,
        sample_rate: int = 48_000,
        channels: int = 2,
        frame_duration_ms: int = 20,
    ) -> None:
        if not data:
            raise ValueError("PCM audio cannot be empty")
        if not 5 <= frame_duration_ms <= 100:
            raise ValueError("frame_duration_ms must be between 5 and 100")
        self.data = bytes(data)
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        # AudioFrame performs the common format validation.
        AudioFrame(b"\0" * (2 * channels), sample_rate, channels)

    async def _frames(self) -> AsyncIterator[AudioFrame]:
        frame_bytes = (
            self.sample_rate * self.channels * 2 * self.frame_duration_ms // 1000
        )
        for offset in range(0, len(self.data), frame_bytes):
            chunk = self.data[offset : offset + frame_bytes]
            if len(chunk) % (2 * self.channels):
                chunk += b"\0" * (
                    (2 * self.channels) - len(chunk) % (2 * self.channels)
                )
            yield AudioFrame(chunk, self.sample_rate, self.channels)
            await asyncio.sleep(0)

    def __aiter__(self) -> AsyncIterator[AudioFrame]:
        return self._frames()


class FFmpegAudioSource:
    """Decode a local file or in-memory bytes; network URLs are never accepted."""

    def __init__(
        self,
        source: str | Path | bytes,
        *,
        sample_rate: int = 48_000,
        channels: int = 2,
        frame_duration_ms: int = 20,
    ) -> None:
        if isinstance(source, str):
            parsed = urlsplit(source)
            if parsed.scheme or parsed.netloc:
                raise ValueError("FFmpegAudioSource accepts local files, not URLs")
            source = Path(source)
        if isinstance(source, bytes) and not source:
            raise ValueError("audio source cannot be empty")
        if channels not in {1, 2}:
            raise ValueError("channels must be 1 or 2")
        if not 5 <= frame_duration_ms <= 100:
            raise ValueError("frame_duration_ms must be between 5 and 100")
        self.source = source
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms

    async def _decode(self, input_path: Path) -> AsyncIterator[AudioFrame]:
        frame_bytes = (
            self.sample_rate * self.channels * 2 * self.frame_duration_ms // 1000
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-i",
                str(input_path),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg is required for encoded audio") from exc
        assert process.stdout is not None
        try:
            while True:
                chunk = await process.stdout.read(frame_bytes)
                if not chunk:
                    break
                if len(chunk) % (2 * self.channels):
                    chunk += b"\0" * (
                        (2 * self.channels) - len(chunk) % (2 * self.channels)
                    )
                yield AudioFrame(chunk, self.sample_rate, self.channels)
            return_code = await process.wait()
            if return_code:
                raise RuntimeError("ffmpeg rejected the audio source")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _frames(self) -> AsyncIterator[AudioFrame]:
        if isinstance(self.source, bytes):
            with tempfile.TemporaryDirectory(prefix="kaede-bot-audio-") as directory:
                input_path = Path(directory) / "input.audio"
                input_path.write_bytes(self.source)
                async for frame in self._decode(input_path):
                    yield frame
            return
        input_path = self.source.expanduser().resolve(strict=True)
        if not input_path.is_file():
            raise ValueError("audio source must be a regular local file")
        async for frame in self._decode(input_path):
            yield frame

    def __aiter__(self) -> AsyncIterator[AudioFrame]:
        return self._frames()
