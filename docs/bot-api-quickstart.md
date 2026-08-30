# Python bot API quickstart

The `kaede-bot` package connects your worker directly to each Kaede instance
that owns a resource. Guild operations go to the guild authority; DM messages,
calls, voice, and Gateway events go to the deterministic conversation
authority. The application home manages identity and configuration and
coordinates DM capability setup, but it never proxies runtime traffic.

## Create the application

Open **User settings → Developer Portal** and create an application. Pick the scopes and Gateway intents it needs. Then create an invite template and use its link to install the bot in a guild. Remote guilds go through the same consent page.

Message content is a separate scope and intent. Don't request it for a command-only bot.

Kaede also follows Discord's user-install model. A user install exposes an
application's commands in its approved `guild`, `bot_dm`, and
`private_channel` contexts, but it does not add the bot user to a guild or
conversation. Its grant is intentionally limited to `applications.commands`,
`interactions.respond`, and optional interaction attachment read/write scopes;
it delivers explicit interactions over the Gateway but does not grant ambient
channel/DM events, ordinary messages, outbound DMs, calls, or voice. Use a
guild installation for those runtime features. Users manage this Discord-like
grant under **User settings → Authorized apps**.

An install template is either `disabled` for E2EE or `participant` capable.
Participant capability grants no room access by itself. The worker must enroll
a verified MLS device, keep signed KeyPackages available, and be explicitly
admitted to each encrypted room. Encrypted commands use that same admission;
there is no callback-only bypass.

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

The SDK handles the connection plumbing for you. It reads `Retry-After`,
obtains short-lived target tokens, and signs every request with the enrolled
worker key. After Gateway `Hello`, it sends heartbeats and identifies with its
last per-topic sequence cursors. A cursor is saved only after all handlers for
that event finish, so a process failure replays uncompleted work. Delivery is
therefore at least once and persistent bots should make handlers idempotent.

Every reconnect is a new Identify with the retained cursor map; Kaede does not
implement Discord's session-ID/Resume opcode. The SDK retries with exponential
backoff from one to thirty seconds plus jitter. The authority replays its
bounded per-topic backlog and emits `GAP` when a saved cursor is too old.

Kaede never forwards bot tokens between instances. To cut off a worker, revoke
it in the Developer Portal; new token issuance stops and connected Gateway
sessions close promptly at every target.

A Gateway connection that's already open picks up changes too: it reloads the
current application, worker, token, and exact installation or DM-capability
grant. A suspension, revocation, or authorization revision closes the
connection with code `4009`. The SDK refreshes affected opaque DM grants and
then reconnects with a fresh token, proof, Identify, and retained topic
cursors. An unchanged lease keeps the same cursor namespace; a real
authorization revision starts a separately fenced capability namespace.

Discord-compatible poll support intentionally excludes app voting. Bots can
create polls, read voters, receive vote events, and end their own polls, but a
bot call to `Message.vote()` or `Message.remove_vote()` is rejected with
`BOT_POLL_VOTE_UNSUPPORTED`.

### Interaction and webhook message ownership

The SDK keeps the credential path that created a `Message`; it does not choose
an edit route from the channel-message ID alone. A public original interaction
response has a private, write-once binding to its interaction and `@original`.
A public follow-up also retains its durable response ID. `Message.edit()`,
`Message.delete()`, and `Message.end_poll()` therefore use the exact interaction
response routes, including for commands-only user installs:

- original edit/delete:
  `/api/v1/bots/interactions/{interaction}/responses/@original`
- follow-up edit/delete:
  `/api/v1/bots/interactions/{interaction}/followups/{response_id}`
- original or follow-up poll finalization:
  `/api/v1/bots/interactions/{interaction}/responses/{@original|response_id}/polls/expire`

That lifecycle binding is attached only by trusted interaction helpers such as
`respond()`, `original_response()`, `send_followup()`, and `fetch_followup()`;
it is never recovered from an unrelated message payload. An ephemeral response
is not a channel `Message` and remains an isolated dictionary. Use
`Interaction.edit_original_response()`, `edit_followup()`,
`delete_original_response()`, `delete_followup()`, `end_original_poll()`, and
`end_followup_poll()` for that private lifecycle.

Token-authenticated webhook execution with `wait=True`, webhook-message fetch,
and webhook-message edit return a `Message` with a separate private binding to
the webhook ID, token, target, and optional thread. Its `edit()` and `delete()`
methods stay on
`/api/v1/webhooks/{webhook}/{token}/messages/{message}` and preserve the thread
parameter. The token is omitted from resource representations and is never sent
to object storage. Other generic channel actions, including `end_poll()`, fail
locally because Kaede has no token-scoped webhook poll-finalization route; the
SDK never borrows an unrelated bot installation for them.

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
page = await channels[0].pin_page(limit=50)
if pins:
    await pins[0].unpin(reason="No longer operationally relevant")
occupancy = await channels[0].voice_occupancy()
```

Pins use Discord's modern newest-first page shape: each typed `MessagePin`
contains `pinned_at` and its `Message`, `has_more` advances with an aware
`before` timestamp, and `Channel.pins()` safely follows all five possible
pages under the 250-pin channel cap. `Message.pin()` and `Message.unpin()` use
the modern `/messages/pins/{message}` paths, accept an optional audit reason,
and work unchanged when the channel authority is federated.

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

Publish and manage announcement followers with the same qualified references
used elsewhere:

```python
follow = await bot.follow_announcement_channel(announcements.ref, updates.ref)
published = await bot.crosspost_message(announcements.ref, release_message.ref)

for follower in await bot.announcement_follows(announcements.ref):
    print(follower["id"], follower["target_channel_domain"])

await bot.delete_announcement_follow(
    announcements.ref,
    kaede.EntityRef.parse(follow["ref"]),
)
```

Grant the source installation `channels.read` and channel visibility. Grant the
target installation `webhooks.manage` and `MANAGE_WEBHOOKS`. Kaede obtains and
binds separate worker intents automatically when the application, source and
target authorities differ; do not proxy worker tokens between instances.

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

The SDK uploads directly to the short-lived, authority-bound HTTPS storage URL
and never sends the bot token or DPoP headers to storage. Kaede accepts the
attachment in a message only from the installation that reserved its quota,
and plaintext media passes the normal scan/quarantine pipeline. E2EE uploads
are stricter: they need an already encrypted `kaede-file-v1` payload with
opaque metadata, and the SDK never falls back to plaintext. A DM capability
retains its exact source installation, so DM upload quota and revocation remain
bound to one consent record.

DM access starts from an active installation that granted `dm.send` and the
scope for each requested operation. Use a fetched guild so the SDK selects its
qualified installation for creation; the returned capability carries that
source lineage through history, messages, reactions, polls, pins, typing, and
attachments:

```python
dm = await guild.open_dm("alice@chat.example")
await dm.send("Your scheduled export is ready")
```

The resulting `Channel` retains its opaque `dm_capability_id`, authorization
revision, qualified `installation_ref`, and `installation_type`. Every resource
created from it inherits that immutable runtime context. If the source
installation is revoked or loses scopes, another installation of the same
application cannot silently take its place.

Federated DMs use three authorities. The SDK asks application home A to open or
refresh the conversation; A obtains installation authority B's original signed
proof for the qualified source installation; then the returned `Channel`
targets conversation authority C. The protocol preserves both guild and user
source lineage, but the current commands-only user-install grant cannot supply
`dm.send`; public outbound DM opening therefore uses a guild installation. All
later REST, Gateway, call, and voice traffic goes directly to C with the exact
capability headers.

The capability lease is at most ten minutes and refreshes automatically. Its
refresh route accepts only the opaque grant ID; the caller cannot replace the
source installation, target user, conversation, or authority. A normal refresh
keeps its stable grant ID and authorization revision, so it does not interrupt
an active call. A real source grant, scope, intent, restriction, E2EE-mode,
suspension, or revocation change advances the revision and makes C reject the
superseded REST, Gateway, call, and media admission immediately.

For a worker enrolled with `dm.send`, `Client.start()` pages on restart
`GET /api/v1/bots/dm-capabilities?limit=100&after={opaque_cursor}` at A before
opening Gateways. It validates each opaque `kbdg_` grant, force-refreshes it,
and starts a separate capability-scoped Gateway and refresh loop at C. The
worker state does not store or reconstruct B's signed proof. Terminal
`401`/`403`/`404` refresh failures and expired leases are discarded; transient
failures are retried only while the current lease remains valid. A commands-only
worker skips this endpoint and starts its ordinary interaction targets directly.

DM channel payloads use Discord's public channel types: `1` for `DM` and `3`
for `GROUP_DM`. Kaede may persist both conversation shapes with an internal DM
channel row, but the wire model and `Channel.is_group_dm` expose type `3` for a
group conversation. User-installed commands may be invoked in that private
context; applications cannot start or join group-DM calls, so the SDK's group
DM call and voice helpers fail before network I/O.

### Enroll a participant device

Participant devices belong to workers, not human sessions. At application home
the lifecycle is challenge, proof-bearing registration, signed KeyPackage
upload, inventory replenishment, and explicit revocation:

- `POST /api/v1/bots/e2ee/devices/challenge`
- `POST /api/v1/bots/e2ee/devices`
- `GET /api/v1/bots/e2ee/devices`
- `POST /api/v1/bots/e2ee/devices/{kbe_id}/key-packages`
- `DELETE /api/v1/bots/e2ee/devices/{kbe_id}`

The SDK exposes the same lifecycle without raw requests:

```python
credential = kaede.bot_mls_credential(
    bot.worker_state.application_ref,
    bot.worker_state.worker_id,
)
provider = kaede.NativeOpenMLSProvider.generate(credential)

# Registers this worker's device when needed and keeps 20–50 packages ready.
device = await bot.replenish_e2ee_key_packages(provider)
inventory = await bot.e2ee_devices()

status = await bot.e2ee_participation(
    dm.ref,
    target=dm.target,
    dm_capability_id=dm.dm_capability_id,
)

# This immediately revokes the device and starts room rekeying.
await bot.revoke_e2ee_device(device.protocol_id)
```

For explicit control, use `create_e2ee_device_challenge()`,
`complete_e2ee_device_registration()` (or the combined
`register_e2ee_device()`), and `upload_e2ee_key_packages()`. Persist
`provider.export_state()` securely and restore it for the next process; a new
provider identity is a different device.

Use a real MLS provider and persist its private state in the same protected
deployment boundary as the worker key. The server receives the public identity,
credential, signatures, and public KeyPackages, never the MLS private state.
Room access remains pending until a guild administrator admits the app under
**Guild settings → Integrations**, or every human in a private conversation
consents, and an authorized client commits the resulting MLS membership change.
`GET /api/v1/bots/channels/{channel}/e2ee/participation` reports the exact
runtime status at the channel authority.

### Encrypted bot voice

Install the optional transport with `kaede-bot[voice]`. A participant-mode bot
joins encrypted voice only with a verified bot-device MLS context; the worker
API signing key is never used as a media key:

```python
group_id = verified_voice_group_id  # Exactly 32 bytes from verified room state.
voice_e2ee = kaede.VoiceE2EEContext(
    provider=provider,              # A real MLS provider for the approved device.
    device_id=approved_device_id,
    channel_ref=voice_channel.ref,
    group_id=group_id,
    epoch=provider.group_epoch(group_id),
)

voice = await bot.connect_voice(
    voice_channel.ref,
    target=voice_channel.target,
    listen=True,
    speak=True,
    e2ee_context=voice_e2ee,
)
```

The target still evaluates the exact guild installation or DM capability,
scopes, live channel permissions, restrictions, and authoritative occupancy.
Its short-lived grant names the current policy generation, MLS epoch, protocol,
suite, and a media session bound to the composite channel and local group ID.
The SDK verifies all of them, rechecks the provider epoch around MLS export,
and places the exporter key in LiveKit's native E2EE provider before connecting.
It never joins with missing, stale, or mismatched state and never retries as
plaintext.

An approved-device revocation calls `voice_e2ee.revoke()`; other control-log
changes call `voice_e2ee.invalidate(reason)`. Active clients also monitor the
provider epoch. Any of these conditions disconnects LiveKit and clears the key.
After an MLS commit, build a new `VoiceE2EEContext` at the new epoch and request
a fresh voice connection. Do not mutate or reuse the old context, media grant,
or room across epochs.

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
next to the owner-only worker key. A restarted process identifies with those
cursors for every ordinary target and for each unchanged DM-capability
revision, rather than silently starting from the live edge.

Moderation helpers (`edit_member`, `kick_member`, `ban_member`, `unban_member`,
and `bans`) need both the `moderation.members` scope and the matching live
guild permission. Deleting another author's message and bulk deletion use
`moderation.messages`; pin changes and removing another user's reaction use
`messages.manage`. Voice mute/deafen, disconnect, and move operations need
`voice.moderate`. Every one of these still enforces live permissions,
hierarchy, audit logs, and the authoritative guild/federation target.

Bots fail closed in E2EE-required forums and active E2EE threads unless the
current worker's verified device is active in that exact child room. A required
forum post is created as a metadata-only child shell with a nonce-bound starter
reservation. Activate the child room, then claim that reservation with the
first rich-v2 encrypted message; no plaintext starter is accepted. A thread
created from a message in an encrypted parent is also an independent child MLS
room: the source remains under the parent keys and child replies begin only
after activation. Forum/thread titles, tags, counts, membership, and
archive/lock state remain visible metadata when the installation otherwise has
access.
