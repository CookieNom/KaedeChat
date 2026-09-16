"""Jellyfin movie nights, using users' own linked accounts."""
import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import secrets
import signal

import kaede_bot as kaede
from kaede_bot.ui import ActionRow, Button, SelectOption, StringSelect, View

from jellyfin import Jellyfin, Store, item_id, media_label, resume_ticks, time_label
from playback import Playback

log = logging.getLogger("jellyfin_bot")
SCOPES = ["guilds.read", "channels.read", "applications.commands", "interactions.respond",
          "voice.connect", "voice.speak", "voice.stream", "voice.states.read"]
INTENTS = kaede.Intents(guild_voice_states=True, guild_messages=False, direct_messages=False,
                       direct_message_reactions=False, message_reactions=False)
NO_MENTIONS = {"parse": [], "replied_user": False}


class MovieBot:
    def __init__(self, client, store):
        self.client, self.store = client, store
        self.lock = asyncio.Lock()
        self.session = None
        self.links = {}
        for name, description, handler, options in (
            ("jellyfin-link", "Connect your Jellyfin account", self.link,
             [{"type": "string", "name": "server", "description": "Your Jellyfin HTTPS address", "required": True}]),
            ("jellyfin-unlink", "Disconnect your Jellyfin account", self.unlink, []),
            ("movie", "Find a movie to watch in this voice channel", self.search,
             [{"type": "string", "name": "title", "description": "Movie title", "required": True}]),
            ("tv", "Find a TV show and choose an episode", self.tv,
             [{"type": "string", "name": "title", "description": "TV show title", "required": True}]),
            ("continue-watching", "Resume a movie or episode from Jellyfin", self.continue_watching, []),
            ("tv-next", "Choose your next episode from Jellyfin", self.tv_next, []),
            ("movie-pause", "Pause your movie night", self.pause, []),
            ("movie-resume", "Resume your movie night", self.resume, []),
            ("movie-stop", "End your movie night", self.stop, []),
        ):
            client.command(name=name, description=description, options=options,
                           contexts=["guild"], integration_types=["guild_install"])(self.wrap(handler))

    @staticmethod
    async def say(event, text, view=None):
        await event.edit_original_response(content=text, view=view or View(timeout=None), allowed_mentions=NO_MENTIONS)

    def wrap(self, handler):
        async def run(event):
            if event.guild_ref is None or event.context != "guild" or event.integration_type != "guild_install":
                await event.respond("Use this command in a server where the bot is installed.", ephemeral=True)
                return
            await event.defer(ephemeral=True)
            try:
                await handler(event)
            except Exception as exc:
                log.warning("Movie night action failed (%s)", type(exc).__name__)
                await self.say(event, "Could not complete that action. Check your Jellyfin connection and bot permissions, then try again.")
        return run

    async def menu(self, event, text, component, callback, navigation=()):
        view = View(timeout=180)
        view.add_row(ActionRow([component]))
        owner, guild, channel = event.user.ref, event.guild_ref, event.channel_ref
        lock = asyncio.Lock()

        async def choose(other, handler=callback):
            if (other.user.ref, other.guild_ref, other.channel_ref) != (owner, guild, channel):
                await other.respond("Open your own movie menu to use these controls.", ephemeral=True)
                return
            async with lock:
                if view.is_finished():
                    await other.respond("This menu has expired. Run the command again.", ephemeral=True)
                    return
                await other.defer_update()
                try:
                    consumed = await handler(other)
                    if consumed is not False:
                        view.stop()
                except Exception as exc:
                    view.stop()
                    log.warning("Movie menu failed (%s)", type(exc).__name__)
                    await self.say(other, "Could not complete that action. Run the command again after checking Jellyfin and bot permissions.")
        view.set_callback(component.custom_id, choose)
        if navigation:
            buttons = []
            callbacks = []
            for label, handler in navigation:
                custom_id = secrets.token_hex(12)
                buttons.append(Button(label=label, custom_id=custom_id))
                async def navigate(other, handler=handler):
                    await choose(other, handler)
                callbacks.append((custom_id, navigate))
            view.add_row(ActionRow(buttons))
            for custom_id, handler in callbacks:
                view.set_callback(custom_id, handler)
        await self.say(event, text, view)

    async def link(self, event):
        owner = str(event.user.ref)
        api = Jellyfin((event.options or {}).get("server", ""), owner)
        result = await api.request("POST", "/QuickConnect/Initiate")
        secret, code = result["Secret"], result["Code"]
        if not isinstance(secret, str) or not isinstance(code, str) or not code.isdigit():
            raise ValueError("Invalid Quick Connect response")
        marker = secrets.token_hex(12)
        self.links[owner] = marker

        async def confirm(other):
            async with self.lock:
                if self.links.get(owner) != marker:
                    await self.say(other, "This connection request was replaced or cancelled. Run /jellyfin-link again.")
                    return
                status = await api.request("GET", "/QuickConnect/Connect", params={"secret": secret})
                if not status.get("Authenticated"):
                    # Keep the same private button so approval can be checked again.
                    return False
                auth = await api.request("POST", "/Users/AuthenticateWithQuickConnect", json={"Secret": secret})
                connection = {"server": api.server, "token": auth["AccessToken"], "user_id": item_id(auth["User"]["Id"])}
                Jellyfin(owner=owner, **connection)  # Validate before persisting credentials.
                if self.session and self.session["owner"] == owner:
                    await self.end()
                self.store.put(owner, connection)
                self.links.pop(owner, None)
            await self.say(other, "Jellyfin connected. Open a voice channel's chat and use /movie, /tv, or /continue-watching.")

        await self.menu(event,
            f"In Jellyfin, open your profile → Quick Connect and enter **{code}**.\n"
            "Then press **I've approved the connection**. If approval is still pending, approve in Jellyfin and press again. This menu expires in 3 minutes.",
            Button(label="I've approved the connection", custom_id=marker), confirm)

    async def unlink(self, event):
        owner = str(event.user.ref)
        async with self.lock:
            self.links.pop(owner, None)
            if self.session and self.session["owner"] == owner:
                await self.end()
            self.store.delete(owner)
        await self.say(event, "Your Jellyfin connection was removed and your movie stopped. You can also revoke Kaede Movie Night in Jellyfin's device settings.")

    async def search(self, event, kind="Movie"):
        owner = str(event.user.ref)
        connection = self.store.get(owner)
        if connection is None:
            await self.say(event, "Connect your account first with /jellyfin-link.")
            return
        if not await self.can_watch(event):
            return
        title = (event.options or {}).get("title", "")
        if kind in {"Movie", "Series"} and (not isinstance(title, str) or not 1 <= len(title.strip()) <= 200):
            await self.say(event, "Enter a title between 1 and 200 characters.")
            return
        api = Jellyfin(owner=owner, **connection)

        async def pick(other, item):
            if kind == "Series":
                async def episode(selected, episode):
                    await self.prepare(selected, connection, episode["Id"])
                await self.picker(other, "Choose an episode", api, "Episode", episode, series=item["Id"])
            else:
                await self.prepare(other, connection, item["Id"])

        await self.picker(event, {"Movie": "Choose a movie", "Series": "Choose a TV show",
            "Resume": "Continue watching", "NextUp": "Your next episodes"}[kind], api, kind, pick, query=title)

    async def tv(self, event):
        await self.search(event, "Series")

    async def continue_watching(self, event):
        await self.search(event, "Resume")

    async def tv_next(self, event):
        await self.search(event, "NextUp")

    async def can_watch(self, event):
        channel = await self.client.fetch_channel(event.channel_ref)
        permissions = event.member.permissions if event.member else 0
        if (channel.guild_ref != event.guild_ref or channel.type != 2 or channel.e2ee_required
                or not (permissions or 0) & int(kaede.Permission.CONNECT | kaede.Permission.ADMINISTRATOR)):
            await self.say(event, "Open the chat of an unencrypted voice channel you can join, then run the command again.")
            return False
        return True

    async def picker(self, event, title, api, kind, selected, *, query=None, series=None, page=0):
        if self.store.get(event.user.ref) != api.connection:
            await self.say(event, "Your Jellyfin connection changed. Run the command again.")
            return
        result = await api.browse(kind, query=query, series=series, page=page)
        choices = {item_id(item["Id"]): item for item in result["Items"][:25]}
        if not choices:
            await self.say(event, "No titles found in your Jellyfin library for this selection.")
            return

        async def choose(other):
            if len(other.values) != 1 or other.values[0] not in choices:
                raise ValueError("Invalid selection")
            await selected(other, choices[other.values[0]])

        navigation = []
        for offset, label in ((-1, "Previous page"), (1, "Next page")):
            if page + offset >= 0 and (offset < 0 or (page + 1) * 25 < result.get("TotalRecordCount", 0)):
                async def navigate(other, next_page=page + offset):
                    await self.picker(other, title, api, kind, selected, query=query, series=series, page=next_page)
                navigation.append((label, navigate))
        await self.menu(event, f"{title} · Page {page + 1}. Only your Jellyfin account's library and progress are shown.",
            StringSelect(custom_id=secrets.token_hex(12), options=[SelectOption(label=media_label(item), value=identifier,
                description=(f"Resume at {time_label(resume_ticks(item))}" if resume_ticks(item) else
                             "Watched" if (item.get("UserData") or {}).get("Played") else "Not started"))
                for identifier, item in choices.items()]), choose, navigation)

    async def prepare(self, event, connection, identifier):
        owner = str(event.user.ref)
        if self.store.get(owner) != connection:
            await self.say(event, "Your Jellyfin connection changed. Run the command again.")
            return
        item = await Jellyfin(owner=owner, **connection).item(identifier)
        if (item.get("Type") not in {"Movie", "Episode"} or item.get("IsMissing") or item.get("IsVirtualItem")
                or item_id(item["Id"]) != item_id(identifier)):
            raise ValueError("This item is not a movie or episode")
        position = resume_ticks(item)
        if not position:
            await self.start(event, connection, item, 0)
            return

        async def choose(other):
            if other.values not in (("resume",), ("restart",)):
                raise ValueError("Invalid playback choice")
            await self.start(other, connection, item, position if other.values[0] == "resume" else 0)

        await self.menu(event, f"**{media_label(item)}** — choose where to start. This updates the host's Jellyfin watch progress.",
            StringSelect(custom_id=secrets.token_hex(12), options=[
                SelectOption(label=f"Resume at {time_label(position)}", value="resume"),
                SelectOption(label="Start from beginning", value="restart")]), choose)

    async def start(self, event, connection, item, position):
        owner = str(event.user.ref)
        async with self.lock:
            if self.session:
                await self.say(event, "This bot is already hosting a watch party. End it before starting another.")
                return
            if self.store.get(owner) != connection:
                await self.say(event, "Your Jellyfin connection changed. Run the command again.")
                return
            if not await self.can_watch(event):
                return
            voice = await self.client.connect_voice(event.channel_ref, speak=True, stream=True)
            movie = Playback(Jellyfin(owner=owner, **connection), item["Id"], voice, start_ticks=position)
            session = {"owner": owner, "guild": event.guild_ref, "channel": event.channel_ref,
                       "movie": movie, "voice": voice}
            self.session = session
            session["task"] = asyncio.create_task(self.play(session, event))
        await self.say(event, f"Starting **{media_label(item)}** at {time_label(position)} in this call. Friends can watch the bot's screen share.\n"
            "Progress is saved to the host's Jellyfin account. Use /movie-pause, /movie-resume, or /movie-stop for movies and episodes.")

    async def play(self, session, event):
        try:
            await session["movie"].run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Movie playback failed (%s)", type(exc).__name__)
            with suppress(Exception):
                await self.say(event, "Playback stopped. Check that the movie has a playable video and audio track and that Jellyfin is reachable, then try /movie again.")
        finally:
            if session["movie"].progress_failed:
                with suppress(Exception):
                    await self.say(event, "Playback ended, but Jellyfin could not save your latest watch position. Your previous saved position may be used next time.")
            with suppress(Exception):
                await session["voice"].disconnect()
            if self.session is session:
                self.session = None

    async def end(self):
        if self.session:
            session = self.session
            task = session["task"]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with suppress(Exception):
                await session["voice"].disconnect()
            self.session = None

    async def control(self, event, action):
        async with self.lock:
            session = self.session
            admin = (event.member.permissions or 0) & int(kaede.Permission.ADMINISTRATOR) if event.member else False
            if (not session or session["guild"] != event.guild_ref
                    or (session["owner"] != str(event.user.ref) and not admin)):
                await self.say(event, "Only the movie host or a server administrator can control this movie night.")
                return
            if action == "stop":
                await self.end()
            else:
                session["movie"].pause(action == "pause")
        await self.say(event, {"stop": "Movie night ended.", "pause": "Movie paused.", "resume": "Movie resumed."}[action])

    async def pause(self, event):
        await self.control(event, "pause")

    async def resume(self, event):
        await self.control(event, "resume")

    async def stop(self, event):
        await self.control(event, "stop")


async def main():
    os.umask(0o077)
    data = Path(os.environ.get("JELLYFIN_BOT_DATA", "/data"))
    worker = data / "worker"
    if os.environ.get("JELLYFIN_BOT_ENROLL") == "1":
        if (worker / "worker.json").exists():
            raise ValueError("Worker already enrolled")
        await kaede.WorkerState.enroll(
            application_home=os.environ["KAEDE_APPLICATION_HOME"], application_ref=os.environ["KAEDE_APPLICATION_REF"],
            control_token=os.environ["KAEDE_BOT_CONTROL_TOKEN"], directory=worker,
            name="jellyfin", scopes=SCOPES, intents=INTENTS.names(), target_domains=[])
        return
    client = kaede.Client(worker_state=kaede.WorkerState.load(worker), intents=INTENTS)
    store = Store(data)
    bot = MovieBot(client, store)
    tasks = []
    try:
        if os.environ.get("JELLYFIN_BOT_SYNC_COMMANDS") == "1":
            await client.sync_commands(application_home=os.environ["KAEDE_APPLICATION_HOME"], control_token=os.environ["KAEDE_BOT_CONTROL_TOKEN"])
            return
        stopped = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
        tasks = [asyncio.create_task(client.start()), asyncio.create_task(stopped.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.end()
        await client.close()
        store.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    # HTTP logs can contain the one-time Quick Connect secret.
    logging.getLogger("httpx").setLevel(logging.ERROR)
    asyncio.run(main())
