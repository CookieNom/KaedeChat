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

`application_home` is security-sensitive. The SDK accepts only a canonical
HTTPS origin whose hostname exactly matches the domain in `application_ref`.
Enrollment and command sync disable redirects and environment proxies before
sending a control credential, so don't point either operation at a vanity,
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

A Gateway connection that's already open picks up changes too: it reloads the
current application, worker, token, and exact installation grants. A
suspension, revocation, or grant revision closes the connection right away and
requires a fresh identify.

## Resources and events

The wrapper exposes typed fetch and action methods, so you don't have to make
raw HTTP calls:

```python
guild = await bot.fetch_guild(kaede.EntityRef.parse("42@chat.example"), target="https://chat.example")
channels = await guild.channels()
members = await guild.members(limit=250)
roles = await guild.roles()

await channels[0].send("Deployment complete")
stickers = await guild.stickers()
await channels[0].send_sticker(stickers[0])
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

# Requires emojis.manage, attachments.write, and the live MANAGE_EMOJIS
# permission (which covers guild emoji and stickers).
ticket = await bot.upload_sticker(
    guild.ref,
    sticker_bytes,
    filename="release-party.png",
    content_type="image/png",
    crop={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
    remove_background=True,
    target=guild.target,
)
sticker = await bot.commit_sticker(
    guild.ref,
    ticket.ref,
    "release_party",
    description="The release is live",
    target=guild.target,
)
```

Sticker discovery only needs `guilds.read`; sending uses `messages.send` and
the same source-guild membership and external-sticker permission checks as a
human account. `Sticker.token` is federation-qualified, and
`GUILD_STICKER_CREATE` / `GUILD_STICKER_DELETE` are exposed as typed SDK
events. Sticker creation is scanned before commit, so production bots should
poll and retry `commit_sticker` when it returns an `Attachment` instead of a
`Sticker`.

Fetched guilds, channels, and roles keep their server `version`, and their
`edit()` methods send it as `If-Match` for you. A stale resource fails instead
of overwriting another moderator's update.

Forums and threads use the same `Channel` and `Message` resources as ordinary
chat. A forum post is created atomically with its starter message, while a
thread attached to an existing message uses the message-scoped route:

```python
forum = next(channel for channel in channels if channel.is_forum)
post = await forum.create_post(
    "Release 2.1 feedback",
    "Please keep one issue per reply.",
    applied_tag_ids=[forum.available_tags[0].id],
)

page = await forum.threads(tag_id=forum.available_tags[0].id, sort_order=0)
await page.threads[0].join()
members = await page.threads[0].members(limit=100, with_member=True)
creator = await page.threads[0].fetch_member(
    members[0].user_ref,
    with_member=True,
)

# Pagination cursors are opaque and already bind the sort/filter boundary.
if page.next_cursor:
    next_page = await forum.threads(
        tag_id=forum.available_tags[0].id,
        sort_order=0,
        cursor=page.next_cursor,
    )

thread = await message.start_thread("Investigate this report")
await thread.send("I can reproduce it.")
await thread.edit_thread(archived=True)
```

Creating a forum post uses `SEND_MESSAGES`; public and announcement threads in
ordinary channels use `CREATE_PUBLIC_THREADS`, and private threads use
`CREATE_PRIVATE_THREADS`. Those creation permissions do not imply or require
parent-channel `SEND_MESSAGES`. A supplied first message and later replies use
`SEND_MESSAGES_IN_THREADS`; moderating another member's thread uses
`MANAGE_THREADS`. The SDK exposes active/archived listing, join/leave, member
management, tags, pin/close state, and typed thread Gateway events without a
separate bot-only thread model.

Pass `ThreadPage.next_cursor` back unchanged. The older timestamp-based
`before` argument remains available for compatibility, but cannot be combined
with `cursor`.

Task tracker channels use typed board resources rather than message history.
Grant `tasks.read` and `tasks.write`, enable `guild_tasks`, and grant the bot's
role the corresponding tracker permissions:

```python
bot = kaede.Client(
    worker_state=kaede.WorkerState.load("/run/secrets/kaede-worker"),
    intents=kaede.Intents(guild_tasks=True),
)

tracker_channel = next(channel for channel in channels if channel.is_tracker)
board = await tracker_channel.tracker()
planned = next(lane for lane in board.lanes if lane.kind == "planned")

task = await board.create_task(
    planned.ref,
    "Publish release notes",
    priority="high",
    client_nonce="release-2026-08-notes",
)
await task.move(
    next(lane.ref for lane in board.lanes if lane.kind == "in_progress"),
    0,
)

@bot.event
async def on_tracker_task_update(task: kaede.TrackerTask) -> None:
    print(task.key, task.title, task.lane_ref)
```

Every existing-resource mutation sends the fetched resource's version through
`If-Match`. On a stale-write error, refetch the board before retrying. Task
creation's `client_nonce` is safe to reuse only for the identical request. See
[Task tracker channels](task-tracker.md) for permissions and route details.

Joining a thread can set the bot's own Kaede notification preference. Adding
another member never changes that member's preference; they control it through
their own `@me` membership operation. Member listing is capped at 100 per page;
continue with the last `user_ref` as `after`. `with_member=True` includes the
typed guild-member projection when the installation may read it.

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

The SDK uploads directly to the short-lived HTTPS storage URL and never sends
the bot token or DPoP headers to storage. Kaede accepts the attachment in a
message only from the installation that reserved its quota, and plaintext media
passes the normal scan/quarantine pipeline. E2EE uploads are stricter: they
need an already encrypted `kaede-file-v1` payload with opaque metadata, and the
SDK never falls back to plaintext. Bot attachment uploads in DMs aren't
supported yet.

Outbound DMs bind to an active installation that granted both `dm.send` and
`messages.send`. Use a fetched guild so the SDK carries that exact installation
ID into DM creation and later writes:

```python
dm = await guild.open_dm("alice@chat.example")
await dm.send("Your scheduled export is ready")
```

The resulting `Channel` keeps its `bot_installation_id`. If that installation
is revoked or loses scopes, another installation of the same application can't
take its place.

Supported listener aliases follow the event families:

- `on_ready`
- `on_message`, `on_message_edit`, `on_message_delete`
- `on_reaction_add`, `on_reaction_remove`
- `on_member_join`, `on_member_update`, `on_member_remove`
- `on_guild_join`, `on_guild_update`, `on_guild_remove`, plus channel and role
  create/update/delete listeners
- `on_thread_create`, `on_thread_update`, `on_thread_delete`,
  `on_thread_list_sync`, `on_thread_member_update`, and
  `on_thread_members_update`
- `on_tracker_board_update`, tracker lane create/update/delete, and tracker task
  create/update/delete listeners
- `on_presence`, `on_typing`, `on_voice_state`, and `on_interaction`

`Client.listen()` registers additional listeners, and `Client.wait_for()`
waits for a filtered event.

Reaction events use their own `message_reactions` intent, and typing events use
`guild_typing`, which is disabled by default. Gateway cursors are persisted
next to the owner-only worker key, so a restarted process resumes each guild
topic instead of silently starting from the live edge.

Moderation helpers (`edit_member`, `kick_member`, `ban_member`, `unban_member`,
and `bans`) need both the `moderation.members` scope and the matching live
guild permission. Deleting another author's message and bulk deletion use
`moderation.messages`; pin changes and removing another user's reaction use
`messages.manage`. Voice mute/deafen, disconnect, and move operations need
`voice.moderate`. Every one of these still enforces live permissions,
hierarchy, audit logs, and the authoritative guild/federation target.

Bots fail closed in E2EE-required forums and active E2EE threads. They cannot
create or reply to an encrypted post until Kaede has a verified bot-device MLS
participant protocol; plaintext fallback is never attempted. Forum/thread
titles, tags, counts, membership, and archive/lock state remain visible
metadata when the installation otherwise has access.
