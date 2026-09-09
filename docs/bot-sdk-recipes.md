# Bot SDK recipes

Start with the [bot quickstart](bot-api-quickstart.md) to install the SDK,
enroll a worker, and run `/ping`. This page contains examples to adapt after
that works. The [API reference](bots-and-automations.md) lists the full routes
and authorization rules.

Unless a block defines a complete program, put it inside an async function
or handler with the quickstart's `bot` client and `import kaede_bot as kaede`.
Replace example references with your own. Variables such as `guild`, `channel`,
and `message` are fetched resources or event arguments, not SDK globals.
Run examples independently; some delete resources created earlier in the block.

## Choose access for the feature

Add scopes to the application, installation approval, and worker enrollment.
Add event intents to those grants and to `kaede.Intents`. For a worker with
too few scopes, enroll a replacement into a new state directory with the
required scopes/intents, switch the bot to it, and revoke the old worker.
Updating only Python code cannot expand a grant. Channel visibility, role permissions,
installation channel restrictions, and instance policy still apply.

| Feature | Additional scopes | Event intents when needed |
| --- | --- | --- |
| Read guild/channel metadata | `guilds.read`, `channels.read` | `guilds` |
| Read message content/history | `messages.metadata`, `messages.content`, `messages.history` | `guild_messages`, `message_content` |
| Send/edit/delete own messages | `messages.send`, `messages.edit.own`, `messages.delete.own` | None for REST |
| Reactions | `reactions.read`, `reactions.write` | `message_reactions` |
| Pins and message management | `messages.manage` | `guild_messages` |
| Files | `attachments.write`, `attachments.read` | None for REST |
| Polls | `polls.write`, `polls.read` | `guild_message_polls` |
| Members and roles | `members.read`, `roles.read`, `roles.manage` | `guild_members` |
| Channels and threads | `channels.read`, `channels.manage`, `messages.send` as needed | `guilds` |
| Tasks | `tasks.read`, `tasks.write`; `tasks.manage` for board/lane management | `guild_tasks` |
| Outbound DMs | `dm.send` plus each operation's scope | `direct_messages` |
| Voice/video | `voice.connect`, plus `voice.listen`, `voice.speak`, `voice.stream` as used | `voice_states` for occupancy changes |
| Scheduled events | `events.read`, `events.manage` | `guild_scheduled_events` |
| Webhooks and invites | `webhooks.read`, `webhooks.manage`, `invites.read`, `invites.manage` as used | `guild_webhooks`, `guild_invites` |

For the voice examples, jump to [audio](#play-audio-in-a-voice-channel),
[receiving media](#receive-audio-and-video), [video](#publish-camera-or-screen-video),
or [DM calls](#make-a-two-person-dm-call).

## Send, edit, react, and search

Fetch a known text channel; do not assume the first channel is writable.
These examples need the scopes listed above and matching live channel permissions.
Use `allowed_mentions` when relaying text so copied mentions do not notify people.

```python
channel = await bot.fetch_channel(kaede.EntityRef.parse("100@chat.example"))
message = await channel.send("Build started", allowed_mentions={"parse": []})
message = await message.edit(content="Build passed")
await message.add_reaction("✅")
await message.pin(reason="Current build")
await message.unpin(reason="Build superseded")
await message.delete()

page = await bot.search_guild_messages(
    guild.ref, "release", channels=[channel.ref], pinned=True, limit=10,
)
for result in page.results:
    print(result.message.content)
```

Search uses `messages.history` and `messages.content` as well as channel access.
Encrypted content is not indexed. Pass `page.next_cursor` unchanged to the same
search to continue. For a reply handler, enable `guild_messages` and
`message_content`, and grant message metadata/content and send scopes:

```python
@bot.event
async def on_message(message: kaede.Message) -> None:
    if message.author is None or message.author.bot:
        return
    if message.content == "hello":
        await message.reply("Hello!", mention_author=False)
```

## Commands, private replies, files, and buttons

Add these commands alongside `/ping`, then run `sync_commands.py` again.
The first command needs only the quickstart grants. `ephemeral=True` keeps
its response private to the invoking user. Defer before doing slow work;
response visibility is fixed when you acknowledge the interaction.

```python
@bot.command(name="status", description="Show a private status")
async def status(interaction: kaede.Interaction) -> None:
    await interaction.defer(ephemeral=True)
    await interaction.edit_original_response(content="All systems ready.")
    await interaction.send_followup("You can close this message.", ephemeral=True)
```

Add `attachments.write` to send a generated file:

```python
@bot.command(name="report", description="Download a sample report")
async def report(interaction: kaede.Interaction) -> None:
    await interaction.defer(ephemeral=True)
    attachment = await interaction.upload_attachment(
        b"status,count\nready,3\n", filename="report.csv", content_type="text/csv",
    )
    await interaction.edit_original_response(
        content="Your report is ready.", attachment_ids=[attachment.ref.id],
    )
```

On an edit, `attachment_ids` is the full set to retain: omit it to keep the
current files, or pass `[]` to remove them. Attachment command options need
`attachments.read`; access them through `interaction.input_attachments` and
`read_input_attachment(ref, max_bytes=...)` rather than downloading arbitrary URLs.

A button can update its own message. The SDK routes the component interaction
back to its registered callback and disables this View after its timeout.

```python
from kaede_bot.ui import ActionRow, Button, View

@bot.command(name="check", description="Try a button")
async def check(interaction: kaede.Interaction) -> None:
    async def clicked(click: kaede.Interaction) -> None:
        await click.update_message(content="Checked!", view=View())

    view = View(
        rows=[ActionRow([Button(label="Check", custom_id="check")])],
        callbacks={"check": clicked},
        timeout=60,
    )
    await interaction.respond("Ready to check?", view=view)
```

The UI module also provides string/entity selects, checkboxes, text inputs,
and modals. Persistent Views need stable custom IDs and registration after
restart; do not expect Python callbacks to survive a stopped process.

## Embeds, polls, and forwarding

Embeds use ordinary send permissions. Polls also need `polls.write`; reading
voters needs `polls.read`. Bots cannot cast or remove votes.

```python
await channel.send(embeds=[kaede.Embed(
    title="Release ready", description="The build and checks passed.", color=0x48A868,
)])
poll_message = await channel.send(poll=kaede.Poll(
    question=kaede.PollMedia(text="When should we deploy?"),
    answers=[
        kaede.PollAnswer(kaede.PollMedia(text="Today")),
        kaede.PollAnswer(kaede.PollMedia(text="Tomorrow")),
    ],
    duration=24,
))
# End a poll created by this bot when voting is complete.
await poll_message.end_poll()

# source_message is a fetched message the bot may read.
await channel.send(forward=source_message)
```

Forwarding obtains a proof from the source authority instead of trusting copied
metadata. Source and destination permissions are checked separately. Encrypted
forwarding requires the relevant MLS state and attachment re-encryption; see
[E2EE](e2ee.md). Polls, calls, and legacy encrypted envelopes cannot be forwarded.

## Work across two instances

Suppose the application lives at `apps.example` and is installed in guilds on
`chat.example` and `community.example`. Approve each installation first and
allow both domains in the application/worker target policies. The SDK connects
to each resource's authority directly:

```python
local_channel = await bot.fetch_channel(kaede.EntityRef.parse("100@chat.example"))
remote_channel = await bot.fetch_channel(
    kaede.EntityRef.parse("200@community.example"),
)
await local_channel.send("Deployment started")
await remote_channel.send("Deployment started")
```

For explicit Gateway targets, replace the quickstart's `await bot.start()` with:

```python
await bot.start("https://chat.example", "https://community.example", auto_discover=False)
```

The app home still handles application identity and DM capability setup.
Each target issues its own token; never copy a token from one instance to
another. A remote installation is still subject to that guild's channel and
role permissions. Preserve `id@domain` references even when numeric IDs match.
Tracker replication between human home servers is not currently supported;
SDK tracker operations contact the owning guild directly.

## Invites, webhooks, and scheduled events

These recipes require the corresponding management scopes and live guild
permissions. Create resources in a test guild first.

```python
invite = await guild.create_invite(
    channel_id=channel.ref.id, max_uses=5, max_age_seconds=3600,
)
print(invite.code)
await invite.revoke(reason="Test finished")

webhook = await channel.create_webhook("Build notifications")
posted = await webhook.send("Build passed", wait=True, allowed_mentions={"parse": []})
if posted is not None:
    await posted.edit(content="Build passed and deployed")
    await posted.delete()
await webhook.delete()
```

The newly created webhook carries its secret token; keep it private.
Messages returned by webhook helpers retain their own edit/delete route.
Do not try to turn an unrelated channel message into a webhook message.

Schedule a voice event with a timezone-aware start time:

```python
from datetime import datetime, timedelta, timezone

voice_channel = next(c for c in await guild.channels() if c.is_voice)
event = await guild.create_scheduled_event(
    "Community call", datetime.now(timezone.utc) + timedelta(days=1),
    entity_type=2, channel=voice_channel.ref,
)
# External events use entity_type=3, a location, and an end time.
external = await guild.create_scheduled_event(
    "Meetup", datetime.now(timezone.utc) + timedelta(days=2),
    entity_type=3, location="Community hall",
    scheduled_end_time=datetime.now(timezone.utc) + timedelta(days=2, hours=2),
)
for scheduled in await guild.scheduled_events():
    print(scheduled.name)
```

## Play audio in a voice channel

Install `kaede-bot[voice]` in the worker environment. File decoding also needs
FFmpeg installed on that machine and available as `ffmpeg`. LiveKit must be
enabled on the guild authority, with working RTC/TURN networking.
Grant `voice.connect` and `voice.speak`, plus live `CONNECT` and `SPEAK`
permissions. Fetching the channel needs `channels.read`.

This is a complete one-shot program after enrollment with those scopes. Save it
as `play_audio.py` alongside `bot.py`, replace the channel reference, put a
local `announcement.ogg` beside it, and run `python play_audio.py`.
It does not need a Gateway loop for this REST/media operation.

```python
import asyncio
import kaede_bot as kaede
from bot import bot

async def main() -> None:
    try:
        voice_channel = await bot.fetch_channel(
            kaede.EntityRef.parse("300@chat.example"),
        )
        async with await voice_channel.connect_voice(speak=True) as voice:
            await voice.play_file("announcement.ogg", volume=0.5)
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
```

`play_file()` accepts local files or encoded bytes, not network URLs.
For decoded audio, use signed 16-bit little-endian interleaved PCM:

```python
from pathlib import Path

await voice.play(kaede.PCM16AudioSource(
    Path("audio.s16le").read_bytes(), sample_rate=48000, channels=2,
))
```

Only one source can play at once. To control playback while other handlers run,
start `voice.play_file()` in an `asyncio` task; use `voice.pause()`,
`voice.resume()`, and `voice.stop()`, then await that task before disconnecting.
For guild soundboard playback, add `soundboard.read` and `soundboard.use` and
the corresponding live permission:

```python
sounds = await guild.soundboard_sounds()
if sounds:
    await voice.play_soundboard(sounds[0], volume=0.5)
```

## Receive audio and video

Add `voice.listen` and join with `listen=True`. This sample prints frame
metadata for 30 seconds; it does not record participants' media. Keep callbacks
short so processing cannot delay incoming frames.

```python
async with await voice_channel.connect_voice(listen=True) as voice:
    @voice.listen
    async def audio_received(frame: kaede.AudioFrame, participant: str) -> None:
        print(participant, frame.sample_rate, frame.samples_per_channel)

    @voice.listen_video
    async def video_received(frame: kaede.VideoFrame, participant: str) -> None:
        print(participant, frame.source, frame.width, frame.height)

    await asyncio.sleep(30)
```

The video callback receives decoded frames for camera and screen tracks.
`listen=True` controls receiving; it does not grant publishing permission.

## Publish camera or screen video

Add `voice.stream` and live `STREAM`, then join with `stream=True`.
This sends a blue test frame at 10 FPS for five seconds. Run it inside an async
function with a fetched voice channel:

```python
async with await voice_channel.connect_voice(stream=True) as voice:
    frame = kaede.VideoFrame(
        data=bytes([30, 90, 180, 255]) * (640 * 360),
        width=640, height=360, pixel_format="rgba", source="camera",
    )
    for _ in range(50):
        await voice.publish_video(frame)
        await asyncio.sleep(0.1)
    await voice.stop_video("camera")
```

For a screen track, set `source="screen_share"` and stop it with
`await voice.stop_video("screen_share")`. The SDK publishes frames you supply;
it does not open a webcam or capture your desktop. Feed packed frames from
your capture source, keep the format/dimensions consistent, and pace them at
your intended frame rate. An RGBA frame needs exactly `width * height * 4`
bytes. Stopping a video track leaves the room and audio connection open;
exiting the context disconnects everything.

These examples target plaintext rooms. For encrypted audio or video, pass a
verified `VoiceE2EEContext` as shown in [encrypted bot voice](#encrypted-bot-voice).
Encryption is not enabled by a `stream` flag or by HTTPS alone.

## Make a two-person DM call

Use a guild installation with `dm.send` and the voice scopes for the requested
media. Recipient privacy and the exact source installation still apply.
The bot must wait for the person to accept; it cannot accept its own ringing
call. This bounded example rings for up to 30 seconds, plays a local clip after
acceptance, and ends the call on exit:

```python
dm = await guild.open_dm("alice@community.example")
call = await dm.start_call()
try:
    for _ in range(30):
        active = await dm.active_call()
        if active.call is None or active.call.ref != call.ref:
            break
        if active.call.state == "active":
            async with await dm.connect_voice(call=active.call, speak=True) as voice:
                await voice.play_file("hello.ogg")
            break
        await asyncio.sleep(1)
finally:
    await call.act("end")
```

Use the returned `dm` and `call` resources so their capability binding is
preserved across federation. Bot group-DM calls are unsupported, even though
human clients support them. Commands-only user installations cannot open calls.

## Use the REST API directly

Use `Client.request()` when you need a raw JSON result. It still handles bot
authentication and request signing. This guild example needs `guilds.read`:

```python
raw = await bot.request(
    "GET", f"/api/v1/bots/guilds/{guild.ref}", target=guild.target,
)
print(raw["id"])
```

Prefer resource helpers for DMs, interactions, and webhooks: they preserve the
exact capability or response credentials required by those routes. If you are
implementing another SDK, follow [direct target authentication](bots-and-automations.md#direct-target-authentication)
and the [REST route tables](bots-and-automations.md#rest-api). A control token
cannot be used as a runtime bearer token.

## IDs and usernames

Kaede resources use composite references such as `987654321@chat.example`. A snowflake is an opaque database and ordering identifier; it can't be decoded into a username. Fetch the user once and use the returned handle:

```python
ref = kaede.EntityRef.parse("987654321@chat.example")
user = await bot.fetch_user(ref, target="https://chat.example")
print(user.handle)   # regular username@instance formatting
print(user.mention)  # <@987654321@chat.example>
```

Always keep the full composite reference. Two instances may issue the same numeric snowflake.

## Reconnects and message ownership

The SDK handles short-lived tokens, request signing, `Retry-After`, heartbeats,
and reconnect backoff. It saves a topic cursor after all event handlers finish.
A crash can replay unfinished work, so use stable nonces or stored event IDs
for durable actions. Old cursors can produce `GAP`; the backlog is bounded.

Interaction and webhook message objects retain the credentials and route used
to create them. Their `edit()` and `delete()` methods use that binding. Private
interaction responses are dictionaries: use the `Interaction` original-response
and follow-up helpers. Webhook-token messages cannot end polls. Details are in
[the API reference](bots-and-automations.md).

## Guilds, channels, members, roles, and stickers

The wrapper exposes typed fetch and action methods, so you don't have to make
raw HTTP calls:

```python
guild = await bot.fetch_guild(kaede.EntityRef.parse("42@chat.example"), target="https://chat.example")
channels = await guild.channels()
channel = next(c for c in channels if c.ref == kaede.EntityRef.parse("100@chat.example"))
members = await guild.members(limit=250)
roles = await guild.roles()

await channel.send("Deployment complete")
stickers = await guild.stickers()
if stickers:
    await channel.send_sticker(stickers[0])
await channel.trigger_typing()
pins = await channel.pins()
page = await channel.pin_page(limit=50)
for pinned in pins:
    print(pinned.ref, pinned.content)
voice_channel = next(channel for channel in channels if channel.is_voice)
occupancy = await voice_channel.voice_occupancy()
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
# Select the intended member explicitly; never assign by list position.
member = next(m for m in members if m.user.handle == "alice@chat.example")
await member.add_role(release_role.ref)

# Requires emojis.manage, attachments.write, and the live MANAGE_EMOJIS
# permission (which covers guild emoji and stickers).
from pathlib import Path

sticker_bytes = Path("release-party.png").read_bytes()
ticket = await bot.upload_sticker(
    guild.ref,
    sticker_bytes,
    filename="release-party.png",
    content_type="image/png",
    crop={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
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

## Forums and threads

Forums and threads use the same `Channel` and `Message` resources as ordinary
chat. A forum post is created atomically with its starter message, while a
thread attached to an existing message uses the message-scoped route:

```python
forum = next(channel for channel in channels if channel.is_forum)
# This example assumes the forum has at least one configured tag.
tag = forum.available_tags[0]
post = await forum.create_post(
    "Release 2.1 feedback",
    "Please keep one issue per reply.",
    applied_tag_ids=[tag.id],
)

page = await forum.threads(tag_id=tag.id, sort_order=0)
await post.join()
thread_members = await post.members(limit=100, with_member=True)
for thread_member in thread_members:
    print(thread_member.user_ref)

# Pagination cursors are opaque and already bind the sort/filter boundary.
if page.next_cursor:
    next_page = await forum.threads(
        tag_id=tag.id,
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

Joining a thread can set the bot's own Kaede notification preference. Adding
another member never changes that member's preference; they control it through
their own `@me` membership operation. Member listing is capped at 100 per page;
continue with the last `user_ref` as `after`. `with_member=True` includes the
typed guild-member projection when the installation may read it.

Encrypted forum posts and threads have independent MLS rooms. Admit the bot's
verified device to the child before sending encrypted replies. A required-E2EE
forum starter needs a reserved child shell, room activation, and an encrypted
starter claim. See [the encryption contract](e2ee.md); the plaintext forum
example above does not perform that sequence.

## Federated announcements

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

## Task tracker automation

Task tracker channels use typed board resources rather than message history.
Grant `tasks.read` and `tasks.write`, enable `guild_tasks`, and grant the bot's
role the corresponding tracker permissions:

```python
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

## Upload and download files

File uploads need `attachments.write`; downloads need `attachments.read`.
Usage is charged to the installation that authorized the upload:

```python
upload = await channel.upload(
    b"release notes",
    filename="release.txt",
    content_type="text/plain",
)
message = await channel.send("Artifacts", attachment_ids=[upload.ref.id])

# attachments.read is independent from messages.content.
# After scanning completes, download with a size bound.
body = await message.attachments[0].read(max_bytes=1_000_000)
```

If a download is still pending a scan, retry after it becomes available; a
rejected upload must not be treated as clean. The SDK uploads directly to the
short-lived, authority-bound HTTPS storage URL
and never sends the bot token or DPoP headers to storage. Kaede accepts the
attachment in a message only from the installation that reserved its quota,
and plaintext media passes the normal scan/quarantine pipeline. E2EE uploads
are stricter: they need an already encrypted `kaede-file-v1` payload with
opaque metadata, and the SDK never falls back to plaintext. A DM capability
retains its exact source installation, so DM upload quota and revocation remain
bound to one consent record.

## Direct messages

DM access starts from an active installation that granted `dm.send` and the
scope for each requested operation. Use a fetched guild so the SDK selects its
qualified installation for creation; the returned capability carries that
source lineage through history, messages, reactions, polls, pins, typing, and
attachments:

```python
dm = await guild.open_dm("alice@chat.example")
await dm.send("Your scheduled export is ready")
```

Keep the returned DM resource: it retains the exact installation and opaque
capability used to open the conversation. The application home arranges the
grant, then runtime requests go directly to the conversation authority. Leases
refresh automatically, and workers with `dm.send` restore active capabilities
on startup. Revoking the source installation removes that access; another
installation cannot replace it. See the
[DM routing contract](bots-and-automations.md#federated-dm-capability-routing).

## Enroll a participant device

Encrypted examples require a participant-mode installation, a working native
OpenMLS provider, and explicit admission to the room. Device enrollment alone
does not grant access. These are integration fragments, not a standalone E2EE
bot: your worker must process verified room state and MLS membership changes.
See the [device protocol](bots-and-automations.md#participant-device-lifecycle).

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
```

For explicit control, use `create_e2ee_device_challenge()`,
`complete_e2ee_device_registration()` (or the combined
`register_e2ee_device()`), and `upload_e2ee_key_packages()`. Persist
`provider.export_state()` securely and restore it for the next process; a new
provider identity is a different device. When retiring a device, call
`await bot.revoke_e2ee_device(device.protocol_id)`; that revokes access and
starts room rekeying, so keep it out of enrollment/startup code.

Use a real MLS provider and persist its private state in the same protected
deployment boundary as the worker key. The server receives the public identity,
credential, signatures, and public KeyPackages, never the MLS private state.
Room access remains pending until a guild administrator admits the app under
**Guild settings → Integrations**, or every human in a private conversation
consents, and an authorized client commits the resulting MLS membership change.
`GET /api/v1/bots/channels/{channel}/e2ee/participation` reports the exact
runtime status at the channel authority.

## Encrypted bot voice

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

The SDK checks the grant, room, policy, and MLS epoch before installing the
media key in LiveKit. Missing or stale state rejects the join without a
plaintext fallback. Revocation or an epoch change disconnects the room and
clears its key. After a commit, create a new context and request a fresh
connection; never reuse an old context across epochs. Disconnect with
`await voice.disconnect()` when finished.

## Gateway events

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
revision, rather than starting from the live edge.

## Moderation

Member edits and kicks need `moderation.members`; ban management uses
`moderation.bans`. Both also require matching live guild permissions. Deleting another author's message and bulk deletion use
`moderation.messages`; pin changes and removing another user's reaction use
`messages.manage`. Voice mute/deafen, disconnect, and move operations need
`voice.moderate`. Every one of these still enforces live permissions,
hierarchy, audit logs, and the authoritative guild/federation target.


For example, temporarily time out a selected test member and then remove the
timeout. This needs `moderation.members` and live moderation permission:

```python
from datetime import datetime, timedelta, timezone

member = next(m for m in await guild.members() if m.user.handle == "alice@chat.example")
await member.timeout(
    until=datetime.now(timezone.utc) + timedelta(minutes=5),
    reason="Moderation test",
)
await member.remove_timeout(reason="Test finished")
```

Use `moderation.bans` for ban management; legacy member-moderation grants are
accepted only on routes that explicitly allow them. Voice moderation uses
`voice.moderate` and the matching mute/deafen/move permissions:

```python
await member.set_voice_moderation(server_mute=True, reason="Moderator action")
await member.set_voice_moderation(server_mute=False, reason="Mute lifted")
```
