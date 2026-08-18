# Python bot API quickstart

The `kaede-bot` package connects your worker directly to every Kaede instance where your application is installed. The application home only manages identity and configuration. It never proxies traffic.

## Create the application

Open **User settings → Developer Portal** and create an application. Pick the scopes and Gateway intents it needs. Then create an invite template and use its link to install the bot in a guild. Remote guilds go through the same consent page.

Message content is a separate scope and intent. Don't request it for a command-only bot.

E2EE channels have two modes you should understand before choosing one. `interaction_only` accepts only encrypted command payloads that a user submits to the bot on purpose. `participant` reserves the permission boundary for Kaede's forthcoming bot-device key protocol; until a verified device is admitted, the SDK receives opaque encrypted envelopes and no plaintext history.

Create a control credential on the application page and store it as
`KAEDE_BOT_CONTROL_TOKEN`. The token is shown once. It can only enroll workers
and publish commands.

## Enroll a worker once

Generate the worker key on the machine that will run the bot. `enroll` sends only the public key and writes the private state with mode `0600`.

```python
import asyncio
import os

from kaede_bot import WorkerState

async def enroll() -> None:
    await WorkerState.enroll(
        application_home="https://apps.example",
        application_ref="123@apps.example",
        control_token=os.environ["KAEDE_BOT_CONTROL_TOKEN"],
        directory="/run/secrets/kaede-worker",
        name="production",
        scopes=[
            "applications.commands",
            "interactions.respond",
            "guilds.read",
            "channels.read",
            "messages.send",
        ],
        intents=["guilds", "interactions"],
        target_domains=[],
    )

asyncio.run(enroll())
```

Keep `KAEDE_BOT_CONTROL_TOKEN` in your deployment secret store, and rotate it from the Developer Portal when needed. Your bot never uses a human session during normal operation.

`application_home` is security-sensitive: the SDK accepts only a canonical
HTTPS origin whose hostname exactly matches the domain in `application_ref`.
Enrollment and command sync disable redirects and environment proxies before
sending a control credential, so do not point these operations at a vanity,
redirect, or proxy URL.

## Run the bot

```python
import asyncio

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
        await message.reply("Hello from the correct instance!")

@bot.event
async def on_reaction_add(event: kaede.ReactionEvent) -> None:
    print(event.user_ref, event.emoji)

asyncio.run(bot.start("https://chat.example", "https://community.example"))
```

When your command definitions change, call `sync_commands(application_home=..., control_token=...)` from a controlled deployment job. The home must match the domain stored in `WorkerState.application_ref`. You don't need to run it on every process start.

## IDs and usernames

Kaede resources use composite references such as `987654321@chat.example`. A snowflake is an opaque database and ordering identifier; it can't be decoded into a username. Fetch the user once and use the returned handle:

```python
ref = kaede.EntityRef.parse("987654321@chat.example")
user = await bot.fetch_user(ref, target="https://chat.example")
print(user.handle)   # regular username@instance formatting
print(user.mention)  # <@987654321@chat.example>
```

Always keep the full composite reference. Two instances may issue the same numeric snowflake.

## Rate limits and reconnects

The SDK handles the connection plumbing for you. It reads `Retry-After`, obtains short-lived target tokens, and signs every request with the enrolled worker key. It also sends Gateway heartbeats and resumes from per-topic sequence cursors. Event delivery is at least once, so persistent bots should make their handlers idempotent.

Kaede never forwards bot tokens between instances. To cut off a worker, revoke
it in the Developer Portal; new token issuance stops and connected Gateway
sessions close promptly at every target.

An already-connected Gateway also reloads the current application, worker,
token, and exact installation grants. Suspension, revocation, or a grant
revision closes that connection promptly and requires a fresh identify.

## Resources and events

The wrapper exposes typed fetch and action methods instead of requiring raw
HTTP calls:

```python
guild = await bot.fetch_guild(kaede.EntityRef.parse("42@chat.example"), target="https://chat.example")
channels = await guild.channels()
members = await guild.members(limit=250)
roles = await guild.roles()

await channels[0].send("Deployment complete")
await channels[0].trigger_typing()
pins = await channels[0].pins()
occupancy = await channels[0].voice_occupancy()
```

Management operations use narrow scopes rather than one administrator grant:

```python
# Requires channels.manage and the bot's live MANAGE_CHANNELS permission.
created = await guild.create_channel("build-status", topic="Release automation")
created = await created.edit(topic="Current release automation")

# Requires roles.manage and the relevant live role permissions/hierarchy.
release_role = await guild.create_role("release-manager", permissions=0)
await members[0].add_role(release_role.ref)
```

Fetched guilds, channels, and roles retain their server `version`; their
convenience `edit()` methods automatically send it as `If-Match`. A stale
resource fails instead of overwriting another moderator's update.

File uploads require `attachments.write` and an authoritative guild
installation. Bytes are charged to that exact installation, never to a fake
local-human account:

```python
upload = await channels[0].upload(
    b"release notes",
    filename="release.txt",
    content_type="text/plain",
)
message = await channels[0].send("Artifacts", attachment_ids=[upload.ref.id])

# attachments.read is independent from messages.content.
body = await message.attachments[0].read(max_bytes=1_000_000)
```

The SDK uploads directly to the short-lived HTTPS storage URL without sending
the bot token or DPoP headers to storage. Kaede accepts the attachment in a
message only from the installation that reserved its quota. Plaintext media
passes the normal scan/quarantine pipeline. E2EE uploads require an already
encrypted `kaede-file-v1` payload and opaque metadata; the SDK never falls back
to plaintext. Bot attachment uploads in DMs are not currently supported.

Outbound DMs are explicitly bound to an active installation that granted both
`dm.send` and `messages.send`. Use a fetched guild so the SDK carries its exact
installation ID into both DM creation and later writes:

```python
dm = await guild.open_dm("alice@chat.example")
await dm.send("Your scheduled export is ready")
```

The resulting `Channel` retains `bot_installation_id`; a revoked or
scope-reduced installation cannot be replaced by some other installation of
the same application.

Supported listener aliases include `on_ready`, `on_message`,
`on_message_edit`, `on_message_delete`, `on_reaction_add`,
`on_reaction_remove`, `on_member_join`, `on_member_update`,
`on_member_remove`, `on_guild_join`, `on_guild_update`, `on_guild_remove`,
channel and role create/update/delete listeners, `on_presence`, `on_typing`,
`on_voice_state`, and `on_interaction`. `Client.listen()` registers additional
listeners and `Client.wait_for()` waits for a filtered event.

Reaction events use the independent `message_reactions` intent. Typing events
use `guild_typing`; they are disabled by default. Gateway cursors are persisted
next to the owner-only worker key so a process restart resumes each guild topic
instead of silently starting from the live edge.

Moderation helpers (`edit_member`, `kick_member`, `ban_member`, `unban_member`,
and `bans`) require both the `moderation.members` scope and the corresponding
live guild permission. Deleting another author's message and bulk deletion use
`moderation.messages`. Pin changes and removing another user's reaction use
`messages.manage`. Voice mute/deafen, disconnect, and move operations require
`voice.moderate`; each still enforces live permissions, hierarchy, audit logs,
and the authoritative guild/federation target.
