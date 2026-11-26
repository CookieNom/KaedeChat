"""Exercise the packaged Python transport against the shipped browser worker on a local SFU."""

import asyncio
import math
import os
import struct
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from interop import Handler, jwt
from kaede_bot.audio import AudioFrame
from kaede_bot.refs import EntityRef
from kaede_bot.voice import LiveKitTransport, VideoFrame, VoiceGrant
from playwright.async_api import async_playwright

SFU = "ws://127.0.0.1:17880"


class BotPage(Handler):
    page = Path(__file__).with_suffix(".html")


def bot_frames(phase):
    frequency, level = (900, 200) if phase == 0 else (1900, 52)
    samples = [
        int(9000 * math.sin(math.tau * frequency * i / 48000)) for i in range(960)
    ]
    audio = AudioFrame(struct.pack("<960h", *samples), 48000, 1)
    pixels = bytes([level]) * (160 * 120) + b"\x80" * (160 * 120 // 2)
    return audio, VideoFrame(pixels, 160, 120, "i420", "camera")


async def run(page):
    room = "g.1.2"
    grant = VoiceGrant(
        token=jwt(room, "bot"),
        url=SFU,
        room=room,
        generation=1,
        connection_id="x" * 43,
        expires_at="",
        can_listen=True,
        can_speak=True,
        can_stream=True,
        can_priority_speak=False,
        can_use_vad=True,
        bitrate=64000,
        user_limit=2,
        rtc_region=None,
        video_quality_mode=1,
        e2ee=True,
        guild_ref=None,
        channel_ref=EntityRef.from_wire("2", "fixture.localhost"),
        encryption_policy_generation=1,
        encryption_epoch=1,
        media_protocol="livekit-e2ee-v1",
        media_suite="AES-256-GCM",
        media_session_id="x" * 43,
        media_epoch=1,
        move_session_id=None,
    )
    # Start at the media boundary. The MLS exporter supplies these bytes in production.
    key = os.urandom(32)
    bot = LiveKitTransport(kaede_e2ee=True)
    bot.configure_e2ee(grant, bytearray(key))
    phase = 0
    heard, saw = asyncio.Event(), asyncio.Event()

    async def on_audio(frame, identity):
        if identity != "browser" or heard.is_set():
            return
        samples = memoryview(frame.data).cast("h")[:: frame.channels]
        if max(samples, default=0) < 350:
            return
        crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
        frequency = crossings * frame.sample_rate / len(samples)
        if abs(frequency - (600 if phase == 0 else 1600)) <= 160:
            heard.set()

    async def on_video(frame, identity):
        if identity != "browser" or frame.source != "camera":
            return
        # VP8 decodes to I420 on the software runner. Sample a real luma plane.
        if frame.pixel_format == "i420":
            luma = frame.data[(frame.height // 2) * frame.width + frame.width // 2]
            if (phase == 0 and 20 < luma < 100) or (phase == 1 and luma > 150):
                saw.set()

    bot.set_video_listener(on_video)
    frames = [bot_frames(i) for i in range(2)]

    async def publish():
        tick = 0
        while True:
            audio, video = frames[phase]
            await bot.send_frame(audio)
            if tick % 5 == 0:
                await bot.send_video_frame(video)
            tick += 1
            await asyncio.sleep(0.020)

    await bot.connect(grant, on_audio)
    try:
        await page.evaluate(
            "options => window.start(options)",
            {"url": SFU, "token": jwt(room, "browser"), "key": list(key)},
        )
        async with asyncio.TaskGroup() as tasks:
            sender = tasks.create_task(publish())
            try:
                for next_phase in range(2):
                    if next_phase:
                        # Rotate the native sender first to avoid recovery racing an explicit ratchet.
                        native_key = (
                            bot._room.e2ee_manager.key_provider.ratchet_shared_key(0)
                        )
                        expected = HKDF(
                            hashes.SHA256(), 32, b"kaede-livekit-v1", bytes(128)
                        ).derive(key)
                        assert native_key == expected and native_key != key
                        await page.evaluate("window.ratchet()")
                        phase = next_phase
                        heard.clear()
                        saw.clear()
                        await page.evaluate("phase => window.sendPhase(phase)", phase)
                    try:
                        async with asyncio.timeout(45):
                            await asyncio.gather(
                                heard.wait(),
                                saw.wait(),
                                page.wait_for_function(
                                    "window.codecsMatch()", timeout=0
                                ),
                                page.wait_for_function(
                                    "phase => window.received(phase)",
                                    arg=phase,
                                    timeout=0,
                                ),
                            )
                    except TimeoutError:
                        observed = await page.evaluate("window.observed")
                        raise AssertionError(
                            f"Phase {phase} timed out: bot audio={heard.is_set()}, "
                            f"video={saw.is_set()}, browser={observed}"
                        ) from None
                    print(
                        f"Decoded encrypted audio and video in both directions, ratchets={phase}",
                        flush=True,
                    )
            finally:
                sender.cancel()
    finally:
        await bot.disconnect()


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), BotPage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
                args=["--autoplay-policy=no-user-gesture-required"],
            )
            try:
                page = await browser.new_page()
                page.on(
                    "pageerror",
                    lambda error: print("Browser error:", error, flush=True),
                )
                await page.goto(f"http://127.0.0.1:{server.server_port}")
                await page.wait_for_function("typeof window.start === 'function'")
                async with asyncio.timeout(135):
                    await run(page)
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
