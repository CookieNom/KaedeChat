# Bot SDK guide

Start with a working `/ping` command, then add features using the SDK recipes below.
For wire formats and authorization rules, see the [Bot API reference](bots-and-automations.md).

[First bot](#1-install-the-sdk) · [Troubleshooting](#troubleshooting) ·
[SDK recipes](#sdk-recipes) · [Voice](#play-audio-in-a-voice-channel) ·
[Encryption](#enroll-a-participant-device)

You need Python 3.11 or newer, an account on a running Kaede instance, and a
guild where you can install applications. You don't need to host Kaede to write
a bot. To host the server too, follow the [root README](../README.md#setup).

A bot has three parts: an **application** in the Developer Portal, an
**installation** approved by a guild or user, and a **worker** (your Python
process) with its own signing key. The reference's
[platform model](bots-and-automations.md#platform-model) defines these terms.
The control token is only for setup and command publishing; it can't act as the
bot at runtime.

## 1. Install the SDK

In a terminal:

```sh
mkdir my-kaede-bot
cd my-kaede-bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install kaede-bot
```

On Windows, use `py -3 -m venv .venv` and activate with
`.venv\Scripts\Activate.ps1` in PowerShell. If you already use uv,
`uv add kaede-bot` installs the same package; run scripts with `uv run python`.
The package is named `kaede-bot`, but Python imports `kaede_bot`.

To use this checkout's SDK instead, run this from the repository root in an
activated virtual environment:

```sh
python -m pip install -e ./sdk/python
```

## 2. Create and install the application

1. Sign in to your home instance and open **User settings → Developer Portal**
   (or `/developers`). Create an application and copy its full reference,
   such as `123@chat.example.com`.
2. In **Permissions & installs**, select the scopes `applications.commands`
   and `interactions.respond` and the Gateway intent `interactions`, then
   **Save changes**. This first bot only answers commands, so it doesn't need
   message content.
3. Under **Invite links & servers → Bot invite links**, enter a slug and name,
   then **Create invite link**. The link copies the saved grants. Open it,
   select your test guild, and approve the installation. A remote guild uses
   the same flow. Encrypted participation stays off unless the application has
   the `participant` mode, which you can only turn on from the mobile portal or
   the API.
4. Create a control credential on the application page. This only works once
   the application has an invite link, because drafts reject control tokens.
   Copy the token when it's shown; you'll paste it at a hidden prompt later.

Use a normal text channel for the first test. The guild's command permissions
must allow your account to invoke the application there.

## 3. Enroll the worker once

Save this as `enroll.py`. Replace the two example values with your application's
HTTPS origin and full reference. Their domains must match exactly.

```python
import asyncio
from getpass import getpass

from kaede_bot import WorkerState

async def main() -> None:
    await WorkerState.enroll(
        application_home="https://chat.example.com",
        application_ref="123@chat.example.com",
        control_token=getpass("Control token: "),
        directory=".kaede-worker",
        name="my-first-bot",
        scopes=["applications.commands", "interactions.respond"],
        intents=["interactions"],
        target_domains=[],
    )

if __name__ == "__main__":
    asyncio.run(main())
```

Run it from your bot folder:

```sh
python enroll.py
```

Enrollment sends the public key to Kaede and saves the private worker state
locally with owner-only permissions. Keep `.kaede-worker/` out of Git and back
it up securely. The directory must stay writable because the SDK saves event
replay cursors there. Reuse it on restart; do not enroll a new worker each time.

To cap Gateway connections per target, pass `session_limit` (1 to 16) to
`WorkerState.enroll()`; otherwise the server default applies. Guild and DM
Gateways at the same target share that budget, as described under
[Bot Gateway](bots-and-automations.md#bot-gateway). The limit is set at
enrollment. A worker enrolled with a limit of 1 can't run both kinds at once, so
enroll a replacement, switch your deployment to it, and revoke the old one.

An empty `target_domains` adds no worker-specific domain restriction; the
application and installation policies still apply.

## 4. Write the bot

Save this as `bot.py` in the same folder:

```python
import asyncio

import kaede_bot as kaede

bot = kaede.Client(
    worker_state=kaede.WorkerState.load(".kaede-worker"),
    intents=kaede.Intents(
        guilds=False,
        guild_messages=False,
        direct_messages=False,
        direct_message_reactions=False,
        message_reactions=False,
        interactions=True,
    ),
)

@bot.command(name="ping", description="Check whether the bot is awake",
             contexts=["guild"], integration_types=["guild_install"])
async def ping(interaction: kaede.Interaction) -> None:
    await interaction.respond("Pong!")

async def main() -> None:
    try:
        await bot.start()
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
```

To reply in the user's language, read `interaction.locale`. It's `None` when
the user keeps their language private or an older event omits it, so fall back
with `locale = interaction.locale or "en-US"`. It can differ between the user's
devices, so don't store it as their permanent language.

`start()` finds approved target instances and keeps their Gateway connections
open. Keep this process running, or commands won't get replies.

## 5. Publish the command, then run

Save this as `sync_commands.py`, replacing the home URL:

```python
import asyncio
from getpass import getpass

from bot import bot

async def main() -> None:
    try:
        await bot.sync_commands(
            application_home="https://chat.example.com",
            control_token=getpass("Control token: "),
        )
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
```

Then run:

```sh
python sync_commands.py
python bot.py
```

In the test guild, type `/ping` and pick the application's command. You should
get **Pong!** back. Stop the process with Ctrl+C. Run the sync script again
whenever you change command definitions; it publishes the commands registered
in `bot.py`. Normal startup needs only the worker state, not the control token.

## 6. Add features

The [SDK recipes](#sdk-recipes) below cover messaging, guild management,
tasks, federation, DMs, voice and video, and encrypted participants. Each
feature needs its own scopes and, for events, intents. Grant them before you
add the handler; see [Choose access for the feature](#choose-access-for-the-feature).

For a complete bot you can deploy with Docker, see the
[Discord bridge](../official-bots/discord-bridge/README.md).

Command-only apps can also use a **user installation**. It offers commands in
approved guild, bot-DM, or private-channel contexts without adding a bot member,
and users manage it under **User settings → Authorized apps**. It can't send
ordinary messages, open DMs, or join calls or voice, so the other recipes need a
guild installation. See [Installation](bots-and-automations.md#installation).

## Troubleshooting

| What you see | What to check |
| --- | --- |
| Import fails | Activate the virtual environment used to install `kaede-bot`. |
| Worker state is missing | Run enrollment once, then start from the same folder. Use an absolute state path in a service. |
| Enrollment or sync fails | Use the exact HTTPS application home and matching full reference. Redirect URLs are rejected. Check the control credential. |
| `/ping` is missing | Run command sync; check installation, approved contexts, and the invoking user's command permissions. |
| Command appears but never replies | Keep `bot.py` running; check worker revocation, target connectivity, and the `interactions` intent. |
| REST returns 403 | Check application, installation, worker scopes, channel restrictions, and live role permissions. All must allow the action. |
| Message content is empty | Grant `messages.content` and enable `message_content` along with the message event intent. |
| Voice join fails | Enable LiveKit on the channel's authority and grant the requested voice scopes and live permissions. |
| Encrypted room rejects the bot | Participant mode also requires an approved MLS device in that exact room. See the encrypted recipes. |

## SDK recipes

Unless a block is a complete program, run it inside an async function or
handler that has the quickstart's `bot` client and `import kaede_bot as kaede`.
Replace the example references with your own. Names such as `guild`, `channel`,
and `message` stand for fetched resources or event arguments, not SDK globals.
Run each example on its own, since some delete what they created.

## Choose access for the feature

Add scopes to the application, the installation approval, and the worker
enrollment. Add event intents to those grants and to `kaede.Intents`. Changing
your Python code alone can't expand a grant.

Installations and workers can't be widened in place. To expand an installation,
a guild manager uninstalls the bot and approves an invite link that requests the
new access. For a worker, enroll a replacement into a new state directory with
the scopes and intents you need, switch the bot to it, and revoke the old one.
Channel visibility, role permissions, channel restrictions, and instance policy
still apply. The full scope list is under
[Authorization controls](bots-and-automations.md#authorization-controls).

| Feature | Additional scopes | Event intents when needed |
| --- | --- | --- |
| Read guild/channel metadata | `guilds.read`, `channels.read` | `guilds` |
| Read message content/history | `messages.metadata`, `messages.content`, `messages.history` | `guild_messages`, `message_content` |
| Send/edit/delete own messages | `messages.send`, `messages.edit.own`, `messages.delete.own` | None for REST |
| Reactions | `reactions.read`, `reactions.write` | `message_reactions` |
| Pins and message management | `messages.manage` | `guild_messages` |
| Files | `attachments.write`, `attachments.read` | None for REST |
| Polls | `polls.write`, `polls.read` | `guild_message_polls` |
| Members and roles | `members.read`, `roles.read`, `roles.manage` | `guild_members` (also required for `guild.members()` over REST) |
| Channels and threads | `channels.read`, `channels.manage`, `messages.send` as needed | `guilds` |
| Tasks | `tasks.read`, `tasks.write`; `tasks.manage` for board/lane management | `guild_tasks` |
| Outbound DMs | `dm.send` plus each operation's scope | `direct_messages` |
| Voice/video | `voice.connect`, plus `voice.listen`, `voice.speak`, `voice.stream` as used; `voice.states.read` for `voice_occupancy()` | `voice_states` for occupancy changes |
| Scheduled events | `events.read`, `events.manage` | `guild_scheduled_events` |
| Webhooks and invites | `webhooks.read`, `webhooks.manage`, `invites.read`, `invites.manage` as used | `guild_webhooks`, `guild_invites` |

For the voice examples, jump to [audio](#play-audio-in-a-voice-channel),
[receiving media](#receive-audio-and-video), [video](#publish-camera-or-screen-video),
or [DM calls](#make-a-two-person-dm-call).

## Send, edit, react, and search

Fetch a known text channel instead of assuming the first one is writable. You
need the scopes above and matching channel permissions. Pass `allowed_mentions`
when relaying text so copied mentions don't notify anyone.

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

Search needs `messages.history`, `messages.content`, and channel access.
Encrypted content isn't indexed. To continue, pass `page.next_cursor` unchanged
to the same search. A reply handler needs the `guild_messages` and
`message_content` intents plus the message metadata, content, and send scopes:

```python
@bot.event
async def on_message(message: kaede.Message) -> None:
    if message.author is None or message.author.bot:
        return
    if message.content == "hello":
        await message.reply("Hello!", mention_author=False)
```

## Commands, private replies, files, and buttons

Add these commands next to `/ping`, then run `sync_commands.py` again. The
first needs only the quickstart grants. `ephemeral=True` shows the response only
to the user who ran the command. Respond or defer within 3 seconds, and defer
before slow work. Visibility is fixed once you acknowledge the interaction.

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

On an edit, `attachment_ids` is the full set to keep. Omit it to keep the
current files, or pass `[]` to remove them. Attachment command options need
`attachments.read`. Read them through `interaction.input_attachments` and
`read_input_attachment(ref, max_bytes=...)` instead of downloading URLs.

A button can update its own message. The SDK routes the click to the registered
callback and disables the View when it times out.

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

The UI module also has string and entity selects, checkboxes, text inputs, and
modals. Python callbacks don't survive a restart, so persistent Views need
stable custom IDs and must be registered again at startup.

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

Forwarding gets a proof from the source authority instead of trusting copied
metadata, and checks source and destination permissions separately. Encrypted
forwarding needs MLS state and attachment re-encryption; see [E2EE](e2ee.md).
Polls, calls, and legacy encrypted envelopes can't be forwarded.

## Work across two instances

Say the application lives at `apps.example` and is installed in guilds on
`chat.example` and `community.example`. Approve each installation and allow both
domains in the application and worker target policies. The SDK talks to each
resource's authority directly:

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

The app home still handles application identity and DM setup. Each target
issues its own token, so never copy a token between instances. A remote guild's
channel and role permissions still apply. Keep full `id@domain` references even
when the numeric IDs match. Tracker calls from the SDK always go to the owning
guild, not to a replica.

## Invites, webhooks, and scheduled events

These need the matching management scopes and guild permissions. Try them in a
test guild first.

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

A new webhook object includes its secret token, so keep it private. Messages
returned by webhook helpers keep their own edit and delete route; an unrelated
channel message can't be turned into a webhook message.

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

Install `kaede-bot[voice]` in the worker environment. Decoding files also needs
FFmpeg on that machine, available as `ffmpeg`. The guild authority must have
LiveKit enabled with working RTC/TURN networking. Grant `voice.connect` and
`voice.speak`, plus the `CONNECT` and `SPEAK` permissions. Fetching the channel
needs `channels.read`.

This is a complete one-shot program. Save it as `play_audio.py` next to
`bot.py`, replace the channel reference, put an `announcement.ogg` beside it,
and run `python play_audio.py`. It doesn't need a Gateway loop.

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

Only one source plays at a time. To control playback while other handlers run,
start `voice.play_file()` in an `asyncio` task and use `voice.pause()`,
`voice.resume()`, and `voice.stop()`. Await the task before disconnecting.
For guild soundboard sounds, add `soundboard.read`, `soundboard.use`, and the
matching permission:

```python
sounds = await guild.soundboard_sounds()
if sounds:
    await voice.play_soundboard(sounds[0], volume=0.5)
```

## Receive audio and video

Add `voice.listen` and join with `listen=True`. This sample prints frame
metadata for 30 seconds and doesn't record anything. Keep callbacks short so
they don't hold up incoming frames.

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

The video callback gets decoded frames from camera and screen tracks.
`listen=True` only controls receiving, not publishing.

## Publish camera or screen video

Add `voice.stream` and the `STREAM` permission, then join with `stream=True`.
This sends a blue test frame at 10 FPS for five seconds:

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
`await voice.stop_video("screen_share")`. The SDK only publishes frames you give
it; it doesn't open a webcam or capture the desktop. Keep the format and size
consistent and pace frames at your target rate. An RGBA frame needs exactly
`width * height * 4` bytes. Stopping a video track leaves the room and audio
open; leaving the context disconnects everything.

These examples are for plaintext rooms. For encrypted audio or video, pass a
verified `VoiceE2EEContext` as shown in [encrypted bot voice](#encrypted-bot-voice).
Neither the `stream` flag nor HTTPS turns on encryption.

## Make a two-person DM call

Use a guild installation with `dm.send` and the voice scopes for the media you
need. The recipient's privacy settings still apply. The bot has to wait for the
person to accept; it can't accept its own call. This example rings for up to 30
seconds, plays a clip once the call is accepted, and ends the call on exit:

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

Use the returned `dm` and `call` objects, which carry the DM capability across
federation. Bots can't make group-DM calls, and user installations can't open
calls at all.

## Use the REST API directly

Use `Client.request()` when you need raw JSON. It still handles bot
authentication and request signing. This example needs `guilds.read`:

```python
raw = await bot.request(
    "GET", f"/api/v1/bots/guilds/{guild.ref}", target=guild.target,
)
print(raw["id"])
```

For DMs, interactions, and webhooks, use the resource helpers instead: they keep
the capability or response credentials those routes need. If you're writing
another SDK, follow [direct target authentication](bots-and-automations.md#direct-target-authentication)
and the [REST route tables](bots-and-automations.md#rest-api).

## IDs and usernames

Kaede resources use composite references such as `987654321@chat.example`. The
numeric snowflake is an opaque ID and can't be decoded into a username. Fetch
the user and use the returned handle:

```python
ref = kaede.EntityRef.parse("987654321@chat.example")
user = await bot.fetch_user(ref, target="https://chat.example")
print(user.handle)   # regular username@instance formatting
print(user.mention)  # <@987654321@chat.example>
```

`fetch_user()` needs `members.read`, and the user must share a guild with the
bot where its installation grants that scope. Otherwise the lookup returns
`USER_NOT_FOUND`. Always keep the full reference, because two instances can
issue the same snowflake.

## Reconnects and message ownership

The SDK handles short-lived tokens, request signing, `Retry-After`, heartbeats,
and reconnect backoff. It saves each topic cursor only after your handlers
finish, so a crash can replay unfinished work. Use stable nonces or stored event
IDs for anything durable. A very old cursor gets `GATEWAY_GAP`, because the
backlog is bounded. See [Bot Gateway](bots-and-automations.md#bot-gateway).

Interaction and webhook messages remember the credentials and route that
created them, and their `edit()` and `delete()` use it. Private interaction
responses are dictionaries, so use the `Interaction` original-response and
follow-up helpers for them. Webhook-token messages can't end polls. The rules
are under [Commands and interactions](bots-and-automations.md#commands-and-interactions).

## Guilds, channels, members, roles, and stickers

Typed fetch and action methods cover these, so you don't need raw HTTP:

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

Pin pages are newest first, like Discord's. Each `MessagePin` has `pinned_at`
and its `Message`, and you page with `has_more` and a timezone-aware `before`
timestamp. `Channel.pins()` follows all pages, at most five under the 250-pin
channel cap. `Message.pin()` and `Message.unpin()` use the
`/messages/pins/{message}` paths, take an optional audit reason, and work the
same on federated channels.

Management actions use narrow scopes instead of one administrator grant:

```python
# Requires channels.manage and the bot's live MANAGE_CHANNELS permission.
created = await guild.create_channel("build-status", topic="Release automation")
created = await created.edit(topic="Current release automation")

# Requires roles.manage and the relevant live role permissions/hierarchy.
release_role = await guild.create_role("release-manager", permissions=0)
# Select the intended member explicitly; never assign by list position.
member = next(m for m in members if m.user.handle == "alice@chat.example")
await member.add_role(release_role.ref)

# Requires emojis.manage or expressions.manage, attachments.write, and the live
# CREATE_GUILD_EXPRESSIONS permission.
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

Listing stickers needs only `guilds.read`. Sending one needs `messages.send`
and passes the same source-guild membership and external-sticker checks as a
human. `Sticker.token` is federation-qualified, and `GUILD_STICKER_CREATE` and
`GUILD_STICKER_DELETE` arrive as typed events. New stickers are scanned before
commit, so when `commit_sticker` returns an `Attachment` instead of a `Sticker`,
wait and retry.

Fetched guilds, channels, and roles keep their server `version`, and `edit()`
sends it as `If-Match`. Editing a stale copy fails instead of overwriting
someone else's change.

## Forums and threads

Forums and threads use the same `Channel` and `Message` objects as ordinary
chat. A forum post is created together with its starter message. A thread on an
existing message starts from that message:

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

Permissions:

- Forum post: `SEND_MESSAGES`.
- Public or announcement thread in an ordinary channel: `CREATE_PUBLIC_THREADS`.
  Private thread: `CREATE_PRIVATE_THREADS`. Neither needs `SEND_MESSAGES` in
  the parent channel.
- First message and replies in a thread: `SEND_MESSAGES_IN_THREADS`.
- Moderating another member's thread: `MANAGE_THREADS`.

The SDK also covers active and archived listing, join and leave, member
management, tags, pin and close state, and typed thread events.

Pass `ThreadPage.next_cursor` back unchanged. The older timestamp `before`
argument still works but can't be combined with `cursor`. Member lists return
at most 100 per page; continue with the last `user_ref` as `after`.
`with_member=True` adds the guild-member data when the installation may read
it. Joining a thread can set the bot's own notification preference, but adding
another member never changes theirs.

Encrypted forum posts and threads each have their own MLS room, so admit the
bot's device to the child room before sending encrypted replies. The plaintext
example above won't work in a forum that requires E2EE; see
[End-to-end encrypted conversations](bots-and-automations.md#end-to-end-encrypted-conversations)
for the starter sequence.

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

Grant the source installation `channels.read` and channel visibility, and the
target installation `webhooks.manage` and `MANAGE_WEBHOOKS`. When the
application, source, and target are on different instances, the SDK handles the
separate authorizations itself. Don't pass worker tokens between instances.

## Task tracker automation

Task tracker channels use typed board objects, not message history. Grant
`tasks.read` and `tasks.write`, enable `guild_tasks`, and give the bot's role
the matching tracker permissions:

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

Changes to existing items send the fetched version through `If-Match`. On a
stale-write error, refetch the board and try again. Reuse a `client_nonce` only
when repeating the identical request. Permissions and routes are in
[Task tracker channels](task-tracker.md).

To configure custom fields, grant `tasks.manage` and `tasks.read` and give the
bot's role `MANAGE_TRACKER`. Field IDs are stable keys local to the channel:

```python
board = await board.edit(custom_fields=[
    *board.custom_fields,
    {"id": "reviewers", "name": "Reviewers", "type": "users"},
    {"id": "related", "name": "Related channels", "type": "channels"},
    {"id": "evidence", "name": "Evidence", "type": "attachments"},
])
task = await board.create_task(
    planned.ref,
    "Review the release",
    custom_values={
        "reviewers": ["5@users.example"],
        "related": [str(tracker_channel.ref)],
    },
)
```

People fields hold extra people, such as reviewers, separate from the main
assignee. Assigning other people also needs `ASSIGN_TRACKER_TASKS`. All eleven
field types are in the exported `kaede.TrackerField` type. Saved data is in
`board.custom_fields` and `task.custom_values`, and task events include custom
values too. Passing `custom_fields` or `custom_values` replaces the whole
collection. Omit it to keep it, pass `[]` or `{}` to clear it, and copy the
other entries when you edit one.

Uploading task attachments also needs `attachments.write`. Reading private
media needs both `tasks.read` and `attachments.read`:

```python
upload = await bot.upload_tracker_attachment(
    tracker_channel.ref, image_bytes,
    filename="review.png", content_type="image/png",
)
evidence = await bot.commit_tracker_attachment(tracker_channel.ref, upload.ref)
task = await task.edit(custom_values={**task.custom_values, "evidence": [evidence]})
url = await bot.fetch_tracker_attachment_url(tracker_channel.ref, upload.ref)
```

The commit waits for the media scan. If it times out, retry with the same
upload reference. The result can go straight into an attachments field. Read
URLs are short-lived private links: don't log them, and don't send bot
credentials when fetching them. For an external link, use
`{"name": "Design", "url": "https://example.org/design.png", "type": "image"}`.

## Upload and download files

Uploads need `attachments.write` and downloads need `attachments.read`. Storage
counts against the installation that made the upload:

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

If a file is still being scanned, retry once it's available, and never treat a
rejected upload as clean. The SDK uploads straight to a short-lived storage URL
and never sends bot credentials to storage. Only the installation that
reserved the upload can attach it to a message. In encrypted rooms, uploads
must already be an encrypted `kaede-file-v1` payload with opaque metadata; the
SDK never falls back to plaintext. DM uploads count against the DM's source
installation.

## Direct messages

DMs start from an active guild installation that granted `dm.send` plus the
scope for each operation. Open the DM from a fetched guild so the SDK uses that
guild's installation:

```python
dm = await guild.open_dm("alice@chat.example")
await dm.send("Your scheduled export is ready")
```

Keep the returned DM object. It holds the installation and capability that
opened the conversation, and every later DM call uses them. The SDK refreshes
the capability and restores it on startup. If the source installation is
revoked, the DM access ends and another installation can't take over. See the
[DM routing contract](bots-and-automations.md#federated-dm-capability-routing).

## Enroll a participant device

Encrypted examples need a participant-mode installation, a working native
OpenMLS provider, and admission to the room; enrolling a device doesn't grant
access by itself. These snippets aren't a complete E2EE bot. Your worker still
has to process verified room state and MLS membership changes. See the
[device protocol](bots-and-automations.md#participant-device-lifecycle).

```python
# Creates the MLS identity and binds the bot credential to its public key.
provider = kaede.NativeOpenMLSProvider.generate_bot(
    bot.worker_state.application_ref,
    bot.worker_state.worker_id,
)

# Registers this worker's device when needed and keeps 20–50 packages ready.
device = await bot.replenish_e2ee_key_packages(provider)
inventory = await bot.e2ee_devices()

status = await bot.e2ee_participation(
    dm.ref,
    target=dm.target,
    dm_capability_id=dm.dm_capability_id,
)
```

Use `generate_bot()`, not `generate()`, for a bot device. The bot credential
and `kbe_` device ID come from the identity key, which the provider creates.
The registration helpers register the credential that `bot_mls_credential()`
derives from that key, so the provider's own MLS credential has to match it.
For step-by-step registration, use the methods listed under
[Participant-device lifecycle](bots-and-automations.md#participant-device-lifecycle).

Save `provider.export_state()` with the same protection as the worker key and
restore it in the next process. A new provider identity is a different device.
The server only ever receives public keys, credentials, signatures, and
KeyPackages. To retire a device, call
`await bot.revoke_e2ee_device(device.protocol_id)`. That revokes access and
rekeys rooms, so keep it out of enrollment and startup code.

Room access stays pending until a guild administrator admits the app under
**Guild settings → Integrations**, or every human in a private conversation
consents, and a client commits the MLS membership change. `e2ee_participation()`
reports the current status.

## Encrypted bot voice

For encrypted calls, use the opt-in voice
transport for Opus audio and VP8 video. The prebuilt library targets Ubuntu
24.04 on x86_64. Install the SDK and its matching LiveKit Python binding in
the bot's environment, then download the library from the same SDK release.

```sh
python -m pip install 'kaede-bot[voice]' 'livekit==1.1.18'
sdk_version=$(python -c 'from importlib.metadata import version; print(version("kaede-bot"))')
release="https://github.com/CookieNom/KaedeChat/releases/download/sdk-v$sdk_version"
bundle=kaede-bot-voice-linux-x86_64.tar.gz
mkdir -p kaede-voice
(
  set -e
  cd kaede-voice
  curl --fail-early --fail --location --remote-name-all "$release/$bundle" "$release/$bundle.sha256"
  sha256sum --check "$bundle.sha256"
  tar -xzf "$bundle"
) && export LIVEKIT_LIB_PATH="$PWD/kaede-voice/liblivekit_ffi.so"
```

Set `LIVEKIT_LIB_PATH` in the worker's environment **before starting
Python**, because LiveKit loads one native library per process. The opt-in
transport checks that the loaded library supports Kaede's frame keys. An
encrypted join also needs an approved bot device and its verified MLS context.

```python
group_id = verified_voice_group_id  # Exactly 32 bytes from verified room state.
voice_e2ee = kaede.VoiceE2EEContext(
    provider=provider,              # A real MLS provider for the approved device.
    device_id=approved_device_id,
    channel_ref=voice_channel.ref,
    group_id=group_id,
    epoch=voice_channel.encryption_epoch,  # Channel epoch from verified room state.
    mls_epoch=1,                    # A fresh room group has one Add commit.
)

voice = await bot.connect_voice(
    voice_channel.ref,
    target=voice_channel.target,
    listen=True,
    speak=True,
    transport=kaede.LiveKitTransport(kaede_e2ee=True),
    e2ee_context=voice_e2ee,
)
```

The SDK checks the grant, room, policy, and MLS epoch before handing the media
key to LiveKit. Missing or stale state rejects the join; there's no plaintext
fallback. Revocation or an epoch change disconnects the room and clears its
key. After a commit, create a new context and connect again. Never reuse a
context across epochs. Call `await voice.disconnect()` when you're done.

## Gateway events

Listener names follow the event families:

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

`Client.listen()` adds more listeners, and `Client.wait_for()` waits for a
filtered event. Reaction events need the `message_reactions` intent. Typing
events need `guild_typing`, which is off by default. After a restart the SDK
resumes from the cursors saved in the worker directory; see
[Reconnects and message ownership](#reconnects-and-message-ownership).

## Moderation

| Action | Scope |
| --- | --- |
| Edit, time out, or kick members | `moderation.members` |
| Manage bans | `moderation.bans` |
| Delete others' messages, bulk delete | `moderation.messages` |
| Change pins, remove others' reactions | `messages.manage` |
| Mute, deafen, disconnect, or move in voice | `voice.moderate` |

Each action also needs the matching guild permission and respects role
hierarchy, and each one is audit-logged. Legacy member-moderation grants work
only on routes that still accept them.

This example times out a test member for five minutes and then lifts it:

```python
from datetime import datetime, timedelta, timezone

member = next(m for m in await guild.members() if m.user.handle == "alice@chat.example")
await member.timeout(
    until=datetime.now(timezone.utc) + timedelta(minutes=5),
    reason="Moderation test",
)
await member.remove_timeout(reason="Test finished")
```

Server mute uses `voice.moderate` and the mute permission:

```python
await member.set_voice_moderation(server_mute=True, reason="Moderator action")
await member.set_voice_moderation(server_mute=False, reason="Mute lifted")
```
