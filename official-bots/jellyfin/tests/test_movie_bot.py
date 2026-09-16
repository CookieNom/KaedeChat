import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import kaede_bot as kaede

from bot import MovieBot
from jellyfin import Jellyfin, Store, server_url, resume_ticks, media_label, time_label
from playback import Playback, WIDTH, HEIGHT


class AccountTests(unittest.TestCase):
    def test_persistent_encrypted_accounts_are_isolated_by_full_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            first = {"server": "https://media.example", "token": "secret", "user_id": "123"}
            store.put("1@a.example", first)
            store.put("1@b.example", {**first, "token": "other"})
            self.assertNotIn(b'secret', Path(directory, "accounts.sqlite3").read_bytes())
            store.db.close()
            store = Store(directory)
            self.assertEqual(store.get("1@a.example"), first)
            store.delete("1@a.example")
            self.assertIsNone(store.get("1@a.example"))
            self.assertEqual(store.get("1@b.example")["token"], "other")
            store.db.close()
            Path(directory, "accounts.key").unlink()
            with self.assertRaises(ValueError):
                Store(directory)

    def test_resume_positions_and_episode_labels(self):
        episode = {"Type": "Episode", "Name": "Pilot", "SeriesName": "Example", "ParentIndexNumber": 0,
                   "IndexNumber": 1, "RunTimeTicks": 200, "UserData": {"PlaybackPositionTicks": 100}}
        self.assertEqual(resume_ticks(episode), 100)
        self.assertIn("S00E01", media_label(episode))
        for position in (-1, 0, True, "10", 200, 2**63):
            self.assertEqual(resume_ticks({**episode, "UserData": {"PlaybackPositionTicks": position}}), 0)
        self.assertEqual(time_label(3661 * 10_000_000), "1:01:01")

    def test_server_url_validation(self):
        self.assertEqual(server_url("https://media.example/jellyfin/"), "https://media.example/jellyfin")
        for url in ("http://media.example", "https://me:pw@media.example", "https://media.example/?key=x", "https://media.example/#x", "https://media.example/\r\nx"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                server_url(url)


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_addresses_and_redirects_are_rejected_and_dns_is_pinned(self):
        api = Jellyfin("https://media.example/jellyfin", "1@chat.example", "abc123")
        loop = asyncio.get_running_loop()
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[(0, 0, 0, "", (ip, 443))])):
                with self.assertRaises(ValueError):
                    await api.request("GET", "/Items")
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://127.0.0.1/"})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[(0, 0, 0, "", ("8.8.8.8", 443))])), patch("jellyfin.httpx.AsyncClient", return_value=client):
            with self.assertRaises(httpx.HTTPStatusError):
                await api.request("GET", "/Items")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, "8.8.8.8")
        self.assertEqual(seen[0].headers["host"], "media.example")
        self.assertEqual(seen[0].extensions["sni_hostname"], "media.example")

    async def test_quick_connect_requires_approval_and_binds_the_private_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            bot = MovieBot(SimpleNamespace(command=lambda **kw: lambda fn: fn), store)
            event = SimpleNamespace(user=SimpleNamespace(ref="1@chat.example"), guild_ref="10@chat.example",
                channel_ref="20@chat.example", options={"server": "https://media.example"},
                edit_original_response=AsyncMock(), defer_update=AsyncMock(), respond=AsyncMock())
            request = AsyncMock(side_effect=[{"Secret": "private-secret", "Code": "123456"},
                {"Authenticated": False}, {"Authenticated": True},
                {"AccessToken": "abc123", "User": {"Id": "a" * 32}}])
            with patch.object(Jellyfin, "request", request):
                await bot.link(event)
                payload = event.edit_original_response.call_args.kwargs
                self.assertIn("123456", payload["content"])
                self.assertNotIn("private-secret", payload["content"])
                view = payload["view"]
                event.custom_id = next(iter(view.callbacks))
                stranger = SimpleNamespace(**vars(event))
                stranger.user = SimpleNamespace(ref="1@other.example")
                await view.dispatch(stranger)
                self.assertEqual(request.await_count, 1)
                await view.dispatch(event)
                self.assertIsNone(store.get(event.user.ref))
                self.assertFalse(view.is_finished())
                await view.dispatch(event)
                self.assertEqual(store.get(event.user.ref)["token"], "abc123")
                self.assertTrue(view.is_finished())
                self.assertNotIn("abc123", event.edit_original_response.call_args.kwargs["content"])
            store.db.close()

    async def test_progress_failures_are_visible_and_can_recover(self):
        api = SimpleNamespace(report=AsyncMock(side_effect=OSError("unreachable")))
        voice = SimpleNamespace(grant=SimpleNamespace(channel_ref="20@chat.example"))
        playback = Playback(api, "a" * 32, voice, start_ticks=50_000_000)
        playback.video_frames = 48
        playback.audio_samples = 48000
        self.assertEqual(playback.position_ticks, 60_000_000)
        with self.assertLogs("jellyfin_bot", level="WARNING"):
            await playback.report("progress")
        self.assertTrue(playback.progress_failed)
        api.report.side_effect = None
        await playback.report("stop")
        self.assertFalse(playback.progress_failed)

    async def test_browsing_and_progress_requests_use_the_hosts_account(self):
        api = Jellyfin("https://media.example", "1@chat.example", "abc123", "b" * 32)
        api.request = AsyncMock(return_value={"Items": [], "TotalRecordCount": 0})
        await api.browse("Episode", series="a" * 32, page=2)
        args, kwargs = api.request.call_args
        self.assertEqual(args, ("GET", f"/Shows/{'a' * 32}/Episodes"))
        self.assertEqual(kwargs["params"]["startIndex"], 50)
        self.assertEqual(kwargs["params"]["userId"], "b" * 32)
        self.assertEqual(kwargs["params"]["isMissing"], "false")
        for kind, path in (("Resume", f"/Users/{'b' * 32}/Items/Resume"), ("NextUp", "/Shows/NextUp")):
            await api.browse(kind)
            self.assertEqual(api.request.call_args.args[1], path)
        await api.item("a" * 32)
        self.assertEqual(api.request.call_args.args[1], f"/Users/{'b' * 32}/Items/{'a' * 32}")
        await api.report("stop", "a" * 32, 123456, "session")
        args, kwargs = api.request.call_args
        self.assertEqual(args, ("POST", "/Sessions/Playing/Stopped"))
        self.assertEqual(kwargs["json"]["PositionTicks"], 123456)
        self.assertEqual(kwargs["json"]["ItemId"], "a" * 32)
        self.assertNotIn("AdditionalUsers", kwargs["json"])

    async def test_tv_pagination_and_resume_restart_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            connection = {"server": "https://media.example", "token": "abc123", "user_id": "b" * 32}
            owner = "1@chat.example"
            store.put(owner, connection)
            bot = MovieBot(SimpleNamespace(command=lambda **kw: lambda fn: fn), store)
            bot.can_watch = AsyncMock(return_value=True)
            bot.start = AsyncMock()
            event = SimpleNamespace(user=SimpleNamespace(ref=owner), guild_ref="10@chat.example",
                channel_ref="20@chat.example", options={"title": "Example"},
                edit_original_response=AsyncMock(), defer_update=AsyncMock(), respond=AsyncMock())
            series = {"Id": "a" * 32, "Name": "Example", "Type": "Series"}
            episode = {"Id": "c" * 32, "Name": "Pilot", "Type": "Episode", "SeriesName": "Example",
                "ParentIndexNumber": 1, "IndexNumber": 26, "RunTimeTicks": 900_000_000,
                "UserData": {"PlaybackPositionTicks": 300_000_000}}
            browse = AsyncMock(side_effect=[{"Items": [series], "TotalRecordCount": 1},
                {"Items": [{**episode, "Id": f"{i:032x}", "IndexNumber": i + 1} for i in range(25)], "TotalRecordCount": 26},
                {"Items": [episode], "TotalRecordCount": 26}])
            async def choose(value=None, button=None):
                view = event.edit_original_response.call_args.kwargs["view"]
                component = view.rows[0].components[0] if button is None else next(
                    item for row in view.rows for item in row.components if getattr(item, "label", None) == button)
                event.custom_id = component.custom_id
                event.values = (value,) if value is not None else ()
                await view.dispatch(event)
            with patch.object(Jellyfin, "browse", browse), patch.object(Jellyfin, "item", AsyncMock(return_value=episode)):
                await bot.tv(event)
                await choose(series["Id"])
                await choose(button="Next page")
                self.assertEqual(browse.call_args.kwargs["page"], 1)
                self.assertEqual(browse.call_args.kwargs["series"], series["Id"])
                payload = event.edit_original_response.call_args.kwargs
                option = payload["view"].rows[0].components[0].options[0]
                self.assertIn("S01E26", option.label)
                self.assertEqual(option.description, "Resume at 0:30")
                await choose(episode["Id"])
                bot.start.assert_not_awaited()
                await choose("resume")
                self.assertEqual(bot.start.call_args.args[-1], 300_000_000)
                await bot.prepare(event, connection, episode["Id"])
                await choose("restart")
                self.assertEqual(bot.start.call_args.args[-1], 0)
                # Relinking invalidates menus, even when the new account is on the same server.
                api = Jellyfin(owner=owner, **connection)
                store.put(owner, {**connection, "token": "newtoken"})
                await bot.picker(event, "Episodes", api, "Episode", AsyncMock(), series=series["Id"])
                self.assertIn("connection changed", event.edit_original_response.call_args.kwargs["content"])
            store.db.close()

    async def test_controls_and_unlink_do_not_cross_accounts_or_guilds(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            bot = MovieBot(SimpleNamespace(command=lambda **kw: lambda fn: fn, close=AsyncMock()), store)
            movie = SimpleNamespace(pause=unittest.mock.Mock())
            bot.session = {"owner": "1@chat.example", "guild": "10@chat.example", "movie": movie}
            bot.say = AsyncMock()
            event = SimpleNamespace(user=SimpleNamespace(ref="2@chat.example"), guild_ref="10@chat.example", member=None)
            await bot.control(event, "pause")
            movie.pause.assert_not_called()
            event.user.ref = "1@chat.example"
            event.guild_ref = "11@chat.example"
            await bot.control(event, "pause")
            movie.pause.assert_not_called()
            event.guild_ref = "10@chat.example"
            await bot.control(event, "pause")
            movie.pause.assert_called_once_with(True)
            bot.end = AsyncMock()
            bot.links[event.user.ref] = "pending"
            store.put(event.user.ref, {"token": "abc"})
            await bot.unlink(event)
            self.assertIsNone(store.get(event.user.ref))
            self.assertNotIn(event.user.ref, bot.links)
            bot.end.assert_awaited_once()
            await bot.client.close()
            store.db.close()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_decoder_audio_video_pause_resume_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "movie.mp4")
            await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=24",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2",
                            "-c:v", "mpeg4", "-c:a", "aac", str(path)], check=True)
            data = path.read_bytes()
            ranges = []
            class Media:
                report = AsyncMock()
                @asynccontextmanager
                async def stream(self, method, url, **kwargs):
                    requested = kwargs.get("extra_headers", {}).get("Range", "bytes=0-")
                    ranges.append(requested)
                    start = int(requested[6:].split("-")[0])
                    response = httpx.Response(206, headers={"content-length": str(len(data) - start),
                        "content-range": f"bytes {start}-{len(data)-1}/{len(data)}", "accept-ranges": "bytes"},
                        stream=httpx.ByteStream(data[start:]))
                    # A lightweight asynchronous response matching a live streamed body.
                    async def chunks():
                        for offset in range(start, len(data), 4096):
                            yield data[offset:offset + 4096]
                    response.aiter_raw = chunks
                    yield response
            class Voice:
                is_connected = True
                server_mute = False
                grant = SimpleNamespace(can_stream=True, can_speak=True, channel_ref="20@chat.example")
                def __init__(self):
                    self.video = []
                    self.audio = []
                async def publish_video(self, frame):
                    self.video.append((frame.width, frame.height, len(frame.data)))
                async def play(self, source):
                    async for frame in source:
                        self.audio.append(frame.samples_per_channel)
            voice = Voice()
            playback = Playback(Media(), "a" * 32, voice)
            task = asyncio.create_task(playback.run())
            async with asyncio.timeout(15):
                while not voice.video:
                    if task.done():
                        await task
                    await asyncio.sleep(.05)
                playback.pause(True)
                await asyncio.sleep(.1)
                before = (len(voice.video), len(voice.audio))
                position = playback.position_ticks
                await asyncio.sleep(.2)
                self.assertEqual(before, (len(voice.video), len(voice.audio)))
                self.assertEqual(playback.position_ticks, position)
                playback.pause(False)
                await task
            self.assertTrue(ranges)
            self.assertGreater(sum(voice.audio), 90000)
            self.assertGreater(len(voice.video), 40)
            self.assertEqual(voice.video[0], (WIDTH, HEIGHT, WIDTH * HEIGHT * 3))
            self.assertEqual(playback.process.returncode, 0)
            calls = Media.report.call_args_list
            self.assertEqual(calls[0].args[0], "start")
            self.assertEqual(calls[-1].args[0], "stop")
            self.assertGreater(calls[-1].args[2], 19_000_000)
            self.assertTrue(any(call.args[0] == "progress" and call.kwargs["paused"] for call in calls))
            # Input seeking must actually skip the first second, for both tracks.
            resumed_voice = Voice()
            resumed = Playback(Media(), "a" * 32, resumed_voice, start_ticks=10_000_000)
            await asyncio.wait_for(resumed.run(), timeout=15)
            self.assertTrue(20 <= len(resumed_voice.video) <= 26)
            self.assertTrue(45000 <= sum(resumed_voice.audio) <= 51000)
            self.assertTrue(19_000_000 <= resumed.position_ticks <= 21_000_000)
            # Moving a paused bot must stop sharing the host's movie into another channel.
            voice.video.clear()
            playback = Playback(Media(), "a" * 32, voice)
            task = asyncio.create_task(playback.run())
            async with asyncio.timeout(15):
                while not voice.video:
                    if task.done():
                        await task
                    await asyncio.sleep(.05)
                playback.pause(True)
                voice.grant.channel_ref = "21@chat.example"
                with self.assertRaises(RuntimeError):
                    await task
            self.assertIsNotNone(playback.process.returncode)
            self.assertEqual(Media.report.call_args.args[0], "stop")
            voice.video.clear()
            playback = Playback(Media(), "a" * 32, voice)
            task = asyncio.create_task(playback.run())
            async with asyncio.timeout(15):
                while not voice.video:
                    if task.done():
                        await task
                    await asyncio.sleep(.05)
                playback.pause(True)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(Media.report.call_args.args[0], "stop")
            self.assertEqual(Media.report.call_args.args[2], playback.position_ticks)
            self.assertIsNotNone(playback.process.returncode)


if __name__ == "__main__":
    unittest.main()
