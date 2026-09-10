"""Discord ↔ Kaede plaintext bridge configured through Kaede slash commands."""
import asyncio
import hashlib
import logging
import os
from pathlib import Path
import signal
import time

import discord
import kaede_bot as kaede
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.dialects.sqlite import insert

from pairing import Pairing

log = logging.getLogger("bridge")
SCOPES = ["guilds.read", "channels.read", "messages.metadata", "messages.content", "messages.send",
          "applications.commands", "interactions.respond"]
INTENTS = kaede.Intents(message_content=True, direct_messages=False,
                       direct_message_reactions=False, message_reactions=False,
                       interactions=True)


def chunks(author, content):
    # Keep room for labels, including names containing non-BMP Unicode.
    label = discord.utils.escape_markdown(author.replace("\n", " ").replace("\r", " ")[:150])
    prefix = f"[{label}] "
    # 750 code points also fit Discord's 2000 UTF-16-unit limit with the label.
    return [prefix + content[i:i + 750] for i in range(0, len(content), 750)]


class Store:
    def __init__(self, path):
        self.engine = create_engine("sqlite+pysqlite:///" + str(path))
        metadata = MetaData()
        self.deliveries = Table(
            "deliveries", metadata,
            Column("sequence", Integer, primary_key=True),
            Column("id", String, nullable=False, unique=True),
            Column("platform", String, nullable=False),
            Column("destination", String, nullable=False),
            Column("content", Text, nullable=False),
            Column("done", Integer, nullable=False, default=0),
            Column("attempts", Integer, nullable=False, default=0),
            Column("retry_at", Integer, nullable=False, default=0),
        )
        self.pairs = Table(
            "channel_pairs", metadata,
            Column("kaede_channel_ref", String, primary_key=True),
            Column("kaede_guild_ref", String, nullable=False),
            Column("discord_channel_id", String, nullable=False, unique=True),
        )
        metadata.create_all(self.engine)

    def routes(self, guild=None):
        query = select(self.pairs)
        if guild is not None:
            query = query.where(self.pairs.c.kaede_guild_ref == guild)
        with self.engine.connect() as conn:
            return conn.execute(query).mappings().all()

    def add_pair(self, channel, guild, discord_channel):
        with self.engine.begin() as conn:
            conn.execute(self.pairs.insert().values(kaede_channel_ref=channel,
                         kaede_guild_ref=guild, discord_channel_id=discord_channel))

    def remove_pair(self, channel, discord_channel):
        with self.engine.begin() as conn:
            removed = conn.execute(self.pairs.delete().where(
                self.pairs.c.kaede_channel_ref == channel,
                self.pairs.c.discord_channel_id == discord_channel,
            ))
            if removed.rowcount:
                conn.execute(self.deliveries.update().where(
                    self.deliveries.c.done == 0,
                    ((self.deliveries.c.platform == "kaede") & (self.deliveries.c.destination == channel))
                    | ((self.deliveries.c.platform == "discord") & (self.deliveries.c.destination == discord_channel)),
                ).values(done=1, content=""))

    def enqueue(self, source, platform, destination, parts):
        with self.engine.begin() as conn:
            for index, content in enumerate(parts):
                key = hashlib.sha256(f"{source}:{platform}:{destination}:{index}".encode()).hexdigest()
                conn.execute(insert(self.deliveries).values(
                    id=key, platform=platform, destination=destination, content=content,
                ).on_conflict_do_nothing(index_elements=["id"]))

    def pending(self):
        with self.engine.connect() as conn:
            return conn.execute(select(self.deliveries).where(
                self.deliveries.c.done == 0,
                self.deliveries.c.retry_at <= int(time.time()),
            ).order_by(self.deliveries.c.sequence).limit(100)).mappings().all()

    def is_pending(self, identifier):
        with self.engine.connect() as conn:
            return conn.execute(select(self.deliveries.c.id).where(
                self.deliveries.c.id == identifier, self.deliveries.c.done == 0,
            )).first() is not None

    def finish(self, row, success):
        values = {"done": 1, "content": ""} if success else {
            "attempts": row["attempts"] + 1,
            "retry_at": int(time.time()) + min(300, 2 ** min(row["attempts"] + 1, 9)),
        }
        with self.engine.begin() as conn:
            conn.execute(self.deliveries.update().where(self.deliveries.c.id == row["id"]).values(**values))


class Bridge(discord.Client):
    def __init__(self, store, bot):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.store, self.kaede = store, bot
        self.route_lock = asyncio.Lock()
        bot.listen("on_message")(self.on_kaede_message)
        bot.listen("on_ready")(self.on_kaede_ready)
        bot.listen("on_gateway_error")(self.on_kaede_connection_error)
        bot.listen("on_target_discovery_error")(self.on_kaede_connection_error)
        bot.listen("on_error")(self.on_kaede_handler_error)

    async def on_kaede_ready(self, event):
        log.info("Kaede gateway ready on %s (%s guild installations)",
                 event.target, len(event.installations))

    async def on_kaede_connection_error(self, event):
        log.warning("Kaede %s on %s: %s", event.type, event.target,
                    event.data.get("error", "unknown error"))

    async def on_kaede_handler_error(self, event):
        error = event.data.get("error")
        log.error("Kaede handler failed: event=%s target=%s exception=%s status=%s code=%s",
                  event.data.get("event_type"), event.target, type(error).__name__,
                  getattr(error, "status", None), getattr(error, "code", None))

    async def on_message(self, message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        for route in self.store.routes():
            if str(message.channel.id) == route["discord_channel_id"]:
                content = message.clean_content
                if message.attachments:
                    content += "\n" + "\n".join(a.url for a in message.attachments)
                self.store.enqueue(f"discord:{message.id}", "kaede", route["kaede_channel_ref"],
                                   chunks(f"Discord · {message.author.display_name}", content))

    async def on_kaede_message(self, message):
        if (message.author is None or message.author.bot or message.bot_installation_id
                or message.application_ref or message.webhook_ref or message.e2ee
                or message.content_unavailable):
            return
        for route in self.store.routes():
            if str(message.channel_ref) == route["kaede_channel_ref"]:
                content = message.content or ""
                if message.attachments:
                    content += "\n[Attachments available in KaedeChat]"
                self.store.enqueue(f"kaede:{message.ref}", "discord", route["discord_channel_id"],
                                   chunks(f"Kaede · {message.author.handle}", content))

    async def deliver(self):
        await self.wait_until_ready()
        while True:
            for row in self.store.pending():
                # Serialize each send with unpair so no queued send starts after removal.
                async with self.route_lock:
                    if not self.store.is_pending(row["id"]):
                        continue
                    field = "discord_channel_id" if row["platform"] == "discord" else "kaede_channel_ref"
                    if not any(route[field] == row["destination"] for route in self.store.routes()):
                        self.store.finish(row, True)
                        continue
                    try:
                        if row["platform"] == "discord":
                            channel = self.get_channel(int(row["destination"]))
                            if channel is None:
                                channel = await self.fetch_channel(int(row["destination"]))
                            await channel.send(row["content"], allowed_mentions=discord.AllowedMentions.none())
                        else:
                            await self.kaede.send_message(
                                kaede.EntityRef.parse(row["destination"]), row["content"],
                                allowed_mentions={"parse": [], "replied_user": False},
                                client_nonce=row["id"],
                            )
                    except Exception as exc:
                        # Do not log message bodies, credentials, or signed attachment URLs.
                        log.warning("Delivery %s failed (%s); retrying", row["id"], type(exc).__name__)
                        self.store.finish(row, False)
                    else:
                        self.store.finish(row, True)
            await asyncio.sleep(1)


async def main():
    data = Path(os.environ.get("BRIDGE_DATA", "/data"))
    data.mkdir(parents=True, exist_ok=True)
    worker_dir = data / "worker"
    if os.environ.get("BRIDGE_ENROLL") == "1":
        if (worker_dir / "worker.json").exists():
            raise ValueError("Worker already enrolled; refusing to overwrite its identity")
        await kaede.WorkerState.enroll(
            application_home=os.environ["KAEDE_APPLICATION_HOME"],
            application_ref=os.environ["KAEDE_APPLICATION_REF"],
            control_token=os.environ["KAEDE_BOT_CONTROL_TOKEN"], directory=worker_dir,
            name="discord-bridge", scopes=SCOPES, intents=INTENTS.names(), target_domains=[],
        )
        return
    store = Store(data / "bridge.sqlite3")
    bot = kaede.Client(worker_state=kaede.WorkerState.load(worker_dir), intents=INTENTS)
    bridge = Bridge(store, bot)
    Pairing(bridge)
    if os.environ.get("BRIDGE_SYNC_COMMANDS") == "1":
        try:
            await bot.sync_commands(application_home=os.environ["KAEDE_APPLICATION_HOME"],
                                    control_token=os.environ["KAEDE_BOT_CONTROL_TOKEN"])
            log.info("Kaede bridge commands published")
        finally:
            await bridge.close()
            await bot.close()
            store.engine.dispose()
        return
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        async with bridge:
            tasks = [asyncio.create_task(bridge.start(os.environ["DISCORD_TOKEN"])),
                     asyncio.create_task(bot.start()),
                     asyncio.create_task(bridge.deliver()), asyncio.create_task(stopped.wait())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await bot.close()
        store.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
