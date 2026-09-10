"""Kaede-side channel pairing controls. Guild administrators manage their own guild's pairs."""
import asyncio
import logging
import secrets

import kaede_bot as kaede
from kaede_bot.ui import ActionRow, Button, SelectOption, StringSelect, View
from sqlalchemy.exc import IntegrityError

NO_MENTIONS = {"parse": [], "replied_user": False}


class Pairing:
    def __init__(self, bridge):
        self.bridge = bridge
        for name, description, handler in (
            ("bridge-pair", "Connect a Kaede channel to a Discord channel", self.pair),
            ("bridge-list", "Browse this server's channel pairs", self.list_pairs),
            ("bridge-unpair", "Disconnect a channel pair", self.unpair),
        ):
            bridge.kaede.command(name=name, description=description,
                                 contexts=["guild"], integration_types=["guild_install"],
                                 default_member_permissions=["ADMINISTRATOR"])(self.command(handler))

    async def allowed(self, interaction, owner=None, guild=None):
        permissions = interaction.member.permissions if interaction.member else 0
        valid = (interaction.guild_ref is not None and interaction.context == "guild"
                 and interaction.integration_type == "guild_install"
                 and (permissions or 0) & int(kaede.Permission.ADMINISTRATOR)
                 and (owner is None or interaction.user.ref == owner)
                 and (guild is None or interaction.guild_ref == guild))
        if not valid:
            await interaction.respond("Administrator permission in this Kaede server is required. Menus can only be used by the person who opened them.", ephemeral=True)
        return bool(valid)

    def command(self, handler):
        async def run(interaction):
            if await self.allowed(interaction):
                await interaction.defer(ephemeral=True)
                await self.safe(handler, interaction)
        return run

    async def safe(self, handler, interaction):
        try:
            await handler(interaction)
        except Exception as exc:
            logging.getLogger("bridge").warning("Pairing failed (%s)", type(exc).__name__)
            await self.say(interaction, "Could not finish setup. Check bot permissions and try the command again.")

    async def say(self, interaction, content, view=None):
        await interaction.edit_original_response(content=content, view=view or View(), allowed_mentions=NO_MENTIONS)

    async def picker(self, interaction, title, choices, selected, page=0):
        """Choices are (label, opaque value); pages respect Kaede's 25-option limit."""
        if not choices:
            await self.say(interaction, "No available choices. Check bot access and existing pairs.")
            return
        page = max(0, min(page, (len(choices) - 1) // 25))
        subset = choices[page * 25:(page + 1) * 25]
        prefix = secrets.token_hex(12)
        select_id = prefix + ":select"
        view = View(timeout=180)
        view.add_row(ActionRow([StringSelect(custom_id=select_id, options=[
            SelectOption(label=label[:100], value=str(index)) for index, (label, _) in enumerate(subset)
        ])]))
        owner, guild = interaction.user.ref, interaction.guild_ref
        lock = asyncio.Lock()

        def callback(action):
            async def run(event):
                async with lock:
                    if view.is_finished():
                        await event.respond("This menu has expired. Run the command again.", ephemeral=True)
                        return
                    if not await self.allowed(event, owner, guild):
                        return
                    await event.defer(ephemeral=True)
                    view.stop()
                    await self.safe(action, event)
            return run

        async def choose(event):
            if len(event.values) != 1 or event.values[0] not in {str(i) for i in range(len(subset))}:
                await self.say(event, "Invalid selection. Run the command again.")
                return
            await selected(event, subset[int(event.values[0])][1])

        view.set_callback(select_id, callback(choose))
        buttons = []
        navigation = []
        for offset, label in ((-1, "Previous"), (1, "Next")):
            if 0 <= page + offset <= (len(choices) - 1) // 25:
                custom_id = prefix + label
                buttons.append(Button(label=label, custom_id=custom_id))
                async def navigate(event, next_page=page + offset):
                    await self.picker(event, title, choices, selected, next_page)
                navigation.append((custom_id, callback(navigate)))
        if buttons:
            view.add_row(ActionRow(buttons))
            for key, handler in navigation:
                view.set_callback(key, handler)
        await self.say(interaction, f"{title}\nPage {page + 1}/{(len(choices) - 1) // 25 + 1}. Menus expire after 3 minutes.", view)

    def discord_channels(self, guild):
        if guild is None or guild.me is None:
            return []
        return [c for c in guild.text_channels if c.permissions_for(guild.me).view_channel
                and c.permissions_for(guild.me).send_messages]

    @staticmethod
    def kaede_channel(channel, guild):
        permissions = channel.permissions
        return (channel.guild_ref == guild and channel.type == 0 and not channel.e2ee_required
                and (permissions & int(kaede.Permission.ADMINISTRATOR)
                     or permissions & int(kaede.Permission.VIEW_CHANNEL) and permissions & int(kaede.Permission.SEND_MESSAGES)))

    async def pair(self, interaction):
        if not self.bridge.is_ready():
            await self.say(interaction, "Discord is still connecting. Try again shortly.")
            return
        channels = await self.bridge.kaede.fetch_channels(interaction.guild_ref)
        choices = [(c.name or str(c.ref), c.ref) for c in channels if self.kaede_channel(c, interaction.guild_ref)]

        async def choose_kaede(event, ref):
            async def choose_guild(event, guild_id):
                guild = self.bridge.get_guild(guild_id)
                async def choose_discord(event, channel_id):
                    async def confirm(event, confirmed):
                        if not confirmed:
                            await self.say(event, "Pairing cancelled.")
                            return
                        channel = await self.bridge.kaede.fetch_channel(ref)
                        remote = self.bridge.get_channel(channel_id)
                        if (not self.kaede_channel(channel, event.guild_ref)
                                or remote not in self.discord_channels(self.bridge.get_guild(guild_id))):
                            await self.say(event, "Channel access changed. Run /bridge-pair again.")
                            return
                        async with self.bridge.route_lock:
                            try:
                                self.bridge.store.add_pair(str(ref), str(event.guild_ref), str(channel_id))
                            except IntegrityError:
                                await self.say(event, "One of these channels is already paired. Use /bridge-unpair first.")
                                return
                        await self.say(event, "Channel pair saved. New messages now relay in both directions.")
                    remote = self.bridge.get_channel(channel_id)
                    if remote is None:
                        await self.say(event, "Discord channel is no longer available.")
                        return
                    await self.picker(event, f"Connect Kaede {ref} ↔ Discord {guild.name} / #{remote.name}? Messages will be shared in both directions.",
                                      [("Confirm pairing", True), ("Cancel", False)], confirm)
                await self.picker(event, "Choose a Discord text channel", [
                    (f"{c.category.name + ' / ' if c.category else ''}#{c.name}", c.id)
                    for c in self.discord_channels(guild)], choose_discord)
            await self.picker(event, "Choose a Discord server", [(g.name, g.id) for g in self.bridge.guilds
                                                                    if self.discord_channels(g)], choose_guild)
        await self.picker(interaction, "Choose a Kaede text channel in this server", choices, choose_kaede)

    async def pairs(self, interaction, remove=False):
        rows = self.bridge.store.routes(str(interaction.guild_ref))
        async def selected(event, row):
            if remove:
                async with self.bridge.route_lock:
                    self.bridge.store.remove_pair(row["kaede_channel_ref"], row["discord_channel_id"])
                await self.say(event, "Pair disconnected. Unsent queued messages for that pair were discarded.")
            else:
                await self.say(event, f"Kaede {row['kaede_channel_ref']} ↔ Discord channel {row['discord_channel_id']}")
        choices = []
        channels = {str(c.ref): c.name for c in await self.bridge.kaede.fetch_channels(interaction.guild_ref)}
        for row in rows:
            remote = self.bridge.get_channel(int(row['discord_channel_id']))
            label = f"{channels.get(row['kaede_channel_ref']) or row['kaede_channel_ref']} ↔ {remote.guild.name + ' / #' + remote.name if remote else row['discord_channel_id']}"
            choices.append((label, row))
        await self.picker(interaction, "Choose a pair to disconnect" if remove else "Channel pairs", choices, selected)

    async def list_pairs(self, interaction):
        await self.pairs(interaction)

    async def unpair(self, interaction):
        await self.pairs(interaction, remove=True)
