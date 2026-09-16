"""One FFmpeg decoder publishes movie audio and screen video through the SDK."""
import asyncio
from contextlib import suppress
import os
import signal
import re
import secrets
import logging

from kaede_bot import AudioFrame, VideoFrame
from jellyfin import item_id

WIDTH, HEIGHT, FPS = 1280, 720, 24


class Playback:
    def __init__(self, jellyfin, movie, voice, *, start_ticks=0):
        self.jellyfin, self.movie, self.voice = jellyfin, item_id(movie), voice
        if type(start_ticks) is not int or not 0 <= start_ticks < 2**63:
            raise ValueError("Invalid playback position")
        self.start_ticks = start_ticks
        self.video_frames = self.audio_samples = 0
        self.ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.play_session = secrets.token_hex(16)
        self.progress_failed = False
        self.process = None
        self.channel = voice.grant.channel_ref
        self.gate = asyncio.Event()
        self.gate.set()

    @property
    def position_ticks(self):
        # Use delivered media, not elapsed wall time: buffering and pauses do not advance progress.
        return self.start_ticks + min(self.video_frames * 10_000_000 // FPS,
                                      self.audio_samples * 10_000_000 // 48000)

    async def report(self, action):
        try:
            async with asyncio.timeout(5):
                await self.jellyfin.report(action, self.movie, self.position_ticks,
                    self.play_session, paused=not self.gate.is_set())
        except Exception as exc:
            self.progress_failed = True
            logging.getLogger("jellyfin_bot").warning("Watch progress could not be saved (%s)", type(exc).__name__)
        else:
            self.progress_failed = False

    async def report_progress(self):
        await self.ready.wait()
        await self.report("start")
        while True:
            try:
                async with asyncio.timeout(10):
                    await self.changed.wait()
            except TimeoutError:
                pass
            self.changed.clear()
            await self.report("progress")

    def pause(self, paused):
        if self.process is None or self.process.returncode is not None:
            raise ValueError("The movie is still starting or has ended.")
        self.process.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)
        self.gate.clear() if paused else self.gate.set()
        self.changed.set()

    async def run(self):
        read_fd, write_fd = os.pipe()
        pipe = os.fdopen(read_fd, "rb", buffering=0)
        audio = asyncio.StreamReader()
        transport = None
        tasks = []
        requests = set()
        reporting = None
        secret = secrets.token_urlsafe(32)

        async def serve(reader, writer):
            task = asyncio.current_task()
            requests.add(task)
            try:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                lines = header.decode("ascii").split("\r\n")
                if lines[0] != f"GET /{secret} HTTP/1.1":
                    writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    return
                headers = {}
                for line in lines[1:]:
                    if line.lower().startswith("range:"):
                        value = line.split(":", 1)[1].strip()
                        if not re.fullmatch(r"bytes=\d+-\d*", value):
                            raise ValueError("Invalid media range")
                        headers["Range"] = value
                async with self.jellyfin.stream("GET", f"/Videos/{self.movie}/stream",
                        params={"static": "true"}, extra_headers=headers) as response:
                    writer.write(f"HTTP/1.1 {response.status_code} OK\r\nConnection: close\r\n".encode())
                    for key in ("content-length", "content-range", "accept-ranges", "content-type"):
                        if key in response.headers:
                            writer.write(f"{key}: {response.headers[key]}\r\n".encode())
                    writer.write(b"\r\n")
                    async for chunk in response.aiter_raw():
                        writer.write(chunk)
                        await writer.drain()
            except Exception:
                # FFmpeg reports the failed read; never log an authenticated URL.
                pass
            finally:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                requests.discard(task)

        server = None
        try:
            server = await asyncio.start_server(serve, "127.0.0.1", 0, limit=8192)
            port = server.sockets[0].getsockname()[1]
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(audio), pipe)
            self.process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                "-protocol_whitelist", "http,tcp",
                "-format_whitelist", "matroska,webm,mov,avi,mpegts,mpeg,ogg,asf",
                "-ss", f"{self.start_ticks / 10_000_000:.7f}",
                "-i", f"http://127.0.0.1:{port}/{secret}",
                "-map", "0:v:0", "-vf",
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS}",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                "-map", "0:a:0", "-ac", "2", "-ar", "48000", "-f", "s16le", f"pipe:{write_fd}",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, pass_fds=(write_fd,))
            os.close(write_fd)
            write_fd = None

            async def frames(reader, size):
                while True:
                    await self.gate.wait()
                    try:
                        frame = await reader.readexactly(size)
                    except asyncio.IncompleteReadError as exc:
                        if exc.partial:
                            yield exc.partial
                        return
                    await self.gate.wait()
                    if not self.voice.is_connected or not self.voice.grant.can_stream or not self.voice.grant.can_speak or self.voice.server_mute:
                        raise RuntimeError("Movie call access changed")
                    yield frame

            async def video():
                async for data in frames(self.process.stdout, WIDTH * HEIGHT * 3):
                    await self.voice.publish_video(VideoFrame(data, WIDTH, HEIGHT, "rgb24", "screen_share"))
                    self.video_frames += 1
                    self.ready.set()

            async def sound():
                async for data in frames(audio, 3840):
                    frame = AudioFrame(data)
                    yield frame
                    self.audio_samples += frame.samples_per_channel

            reporting = asyncio.create_task(self.report_progress())
            tasks = [asyncio.create_task(video()),
                     asyncio.create_task(self.voice.play(sound()))]
            async def watch_access():
                while any(not task.done() for task in tasks[:2]):
                    if (not self.voice.is_connected or not self.voice.grant.can_stream
                            or not self.voice.grant.can_speak or self.voice.server_mute
                            or self.voice.grant.channel_ref != self.channel):
                        raise RuntimeError("Movie call access changed")
                    await asyncio.sleep(0.25)

            tasks.append(asyncio.create_task(watch_access()))
            await asyncio.gather(*tasks)
            if await self.process.wait():
                raise RuntimeError("Movie could not be decoded")
        finally:
            if reporting is not None:
                reporting.cancel()
                await asyncio.gather(reporting, return_exceptions=True)
            if server is not None:
                server.close()
                await server.wait_closed()
            pending = list(requests)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in tasks:
                task.cancel()
            if self.process is not None and self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.kill()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.process is not None:
                await self.process.communicate()
            if write_fd is not None:
                os.close(write_fd)
            if transport is not None:
                transport.close()
            else:
                pipe.close()
            if self.ready.is_set():
                await self.report("stop")
