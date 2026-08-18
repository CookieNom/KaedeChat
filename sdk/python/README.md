# kaede-bot

Asynchronous Python wrapper for Kaede's direct, federated bot API.

```python
import kaede_bot as kaede

bot = kaede.Client(
    worker_state=kaede.WorkerState.load("/run/secrets/kaede-worker"),
    intents=kaede.Intents.default(),
)

@bot.command(name="ping", description="Check whether the bot is awake")
async def ping(interaction: kaede.Interaction) -> None:
    await interaction.respond("Pong!")

@bot.event
async def on_message(message: kaede.Message) -> None:
    if message.content == "hello":
        await message.reply("Hello!")

@bot.event
async def on_member_join(member: kaede.Member) -> None:
    print(f"{member.user.handle} joined {member.guild_ref}")

```

Worker enrollment is intentionally separate from normal startup. See `docs/bot-api-quickstart.md` in the Kaede repository.

The SDK includes typed `Guild`, `Channel`, `Member`, `Role`, `Message`,
`Attachment`, `Invite`, `Webhook`, `Emoji`, `Interaction`, reaction, pin,
presence, typing, deletion, ban, voice-state, and voice-occupancy resources.
Every resource remembers its target instance, so convenience methods remain
safe when one worker connects to several federated Kaede instances.

Scoped helpers cover channel and role CRUD, member roles, invites, webhooks,
emojis, message moderation, voice moderation, outbound DM creation, and safe
attachment upload/download. Guild/channel/role updates use the version returned
by Kaede, and object-storage redirects never receive bot credentials. See the
repository quickstart for required scopes, installation-level media quotas,
E2EE exclusions, and the full endpoint contract.
