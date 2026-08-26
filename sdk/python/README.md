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

The SDK includes typed `Guild`, `Channel`, `Member`, `Role`, `Message`, and
`Attachment` resources. Forums and threads reuse `Channel`, with typed tags,
thread metadata, membership, pagination, and Gateway events. `Invite`,
`Webhook`, `Emoji`, and `Interaction` have their own classes, and there are
types for reactions, pins, presence, typing, deletions, bans, voice state, and
voice occupancy. Task tracker channels expose typed `TrackerBoard`,
`TrackerLane`, and `TrackerTask` resources, version-safe CRUD helpers,
idempotent creation nonces, and `guild_tasks` Gateway events. Lane and task
events also expose their parent `board_version`, so consumers can version-fence
incremental state before applying it. Every resource remembers its target
instance, so convenience methods stay safe when one worker connects to several
federated Kaede instances.

Forum listings use `Channel.threads(include_archived=True)` for one globally
ordered active-and-archived feed. They expose an opaque `ThreadPage.next_cursor`;
pass it unchanged to `Channel.threads(cursor=..., include_archived=True)` to
continue the same pinned-and-timestamp-ordered listing. Thread member helpers
support `after`/`limit` pagination and optional typed guild-member envelopes.

Scoped helpers cover channel and role CRUD, forum policy and post creation,
thread lifecycle and membership, member roles, and message and voice
moderation. Others manage invites, webhooks, and emojis, open outbound DMs, and
handle safe attachment upload/download. Guild, channel, and role updates use
the version returned by Kaede, and object-storage redirects never receive bot
credentials. See the repository quickstart for required scopes,
installation-level media quotas, the fail-closed E2EE boundary, and the full
endpoint contract.
