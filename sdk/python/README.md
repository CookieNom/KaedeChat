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

Gateway recovery is cursor-based, not session-based. The SDK saves a topic
cursor only after event handlers finish. After every disconnect it backs off
from one to thirty seconds plus jitter, gets the required target token, and
sends a fresh Identify with the saved cursor map; Kaede has no opcode-6 Resume
or resumable Gateway session ID. Authorities replay their bounded topic backlog
and emit `GAP` when a cursor is no longer retained. Authorization-change close
code `4009` triggers exact DM-capability reconciliation before retry; heartbeat
timeout `4408` follows the normal reconnect path. Failures dispatch
`GATEWAY_ERROR` without stopping other target loops.

`Client.update_presence()`, `Client.update_voice_state()`, and
`Client.request_guild_members()` expose Discord's bot Gateway opcodes 3, 4,
and 8. Guild commands are authority-routed from the qualified `EntityRef`;
presence may be broadcast to all currently connected target authorities.
Member requests support either a username/nickname prefix or up to 100 exact
qualified user references, optional presence hydration with the privileged
intent, and a caller nonce of at most 32 UTF-8 bytes. Results arrive as typed
`GUILD_MEMBERS_CHUNK` events. The bot enum intentionally omits Discord's
undocumented private opcode 12.

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

`ScheduledEvent` supports Discord-compatible voice and external event CRUD,
strict lifecycle transitions, real subscriber counts and cursor-paged
`ScheduledEventUser` rows. Event create/update/delete and user add/remove
Gateway dispatches are typed, and event-linked invites expose the validated
event object instead of an unverified identifier.

Forum listings use `Channel.threads(include_archived=True)` for one globally
ordered active-and-archived feed. They expose an opaque `ThreadPage.next_cursor`;
pass it unchanged to `Channel.threads(cursor=..., include_archived=True)` to
continue the same pinned-and-timestamp-ordered listing. Thread member helpers
support `after`/`limit` pagination and optional typed guild-member envelopes.

Message pins use the modern Discord-compatible resource. `Channel.pin_page()`
returns typed `MessagePin` entries with aware `pinned_at` timestamps and
`has_more`; `Channel.pins()` follows at most five 50-item pages under the
250-pin cap. `Message.pin(reason=...)` and `Message.unpin(reason=...)` preserve
the bot's installation or DM-capability lineage and route directly to a remote
room authority when needed.

Scoped helpers cover channel and role CRUD, forum policy and post creation,
thread lifecycle and membership, member roles, and message and voice
moderation. Others manage invites, webhooks, and emojis, open participant-bound
DMs, and carry one exact opaque capability through DM history, messages,
reactions, poll reads/finalization, pins, typing, calls, voice, and safe
attachment upload/download. The application home obtains the selected guild
installation authority's signed DM proof; the returned `Channel` retains its
`dm_capability_id`, revision, qualified `installation_ref`, and
`installation_type`, then routes runtime traffic directly to the deterministic
conversation authority. Guild, channel, and role updates use the version
returned by Kaede, and object-storage redirects never receive bot credentials.
Each bot `Guild` also retains its qualified `channel_restrictions` and exact
installation revision, so workers can inspect the target-owned channel ceiling
that every REST, Gateway, voice, and federation path enforces.
See the repository quickstart for required scopes, installation-level media
quotas, the fail-closed E2EE boundary, and the full endpoint contract.

Invite reads, creation, and deletion stay bound to the qualified guild/code
authority. `Guild.fetch_invite()` returns one typed invite; deletion follows
Discord's REST result shape, so `Client.revoke_invite()` and `Invite.revoke()`
return the deleted typed `Invite` rather than discarding the HTTP 200 body.

When the enrolled worker has `dm.send`, `Client.start()` restores DM access
without persisting or reconstructing the installation proof. It pages opaque
active `kbdg_` grants from application home, force-refreshes each immutable
grant, and opens one capability-scoped Gateway and renewal loop at its
conversation authority. A normal lease renewal keeps the grant ID,
authorization revision, and cursor namespace. A real grant revision creates a
separately fenced Gateway/cursor namespace; a terminal authorization failure or
expiry drops the old context. Commands-only workers skip DM bootstrap.

Discord-style user installations are commands-only. They expose commands in
approved `guild`, `bot_dm`, and `private_channel` contexts with
`applications.commands`, `interactions.respond`, and optional interaction
attachment scopes, but do not add a bot member or grant ambient messages, DMs,
channel/DM event subscriptions, calls, or voice; explicit interactions still
arrive over the Gateway. Private-channel wire values also match Discord:
`Channel.type == 1` is `DM`, `Channel.type == 3` is `GROUP_DM`, and
`Channel.is_group_dm` recognizes the latter. Application call and voice helpers
reject group DMs locally. Users manage user-install grants in **User settings →
Authorized apps**, separately from guild integrations.

Forum starters accept the same authored embeds, Views, polls, replies,
authority-resolved mentions, attachments, and live forwards as ordinary bot
messages. Managed webhooks can be moved between eligible guild channels and
use the same scanned ticket/upload/commit lifecycle for avatar replacement;
token-authenticated webhook objects expose fetch, edit, delete, avatar,
execution, and message edit/delete helpers without leaking the token into media
uploads. Standard, Slack, and GitHub execution with `wait=True`, plus
token-authenticated fetch and edit, return a `Message` whose private write-once
binding retains the webhook ID, token, target, and optional thread. Its
`edit()`/`delete()` methods use
`/api/v1/webhooks/{webhook}/{token}/messages/{message}`; generic channel actions
fail locally rather than borrowing a bot grant. `Message.end_poll()` is
therefore unavailable for webhook-token messages because there is no
token-scoped webhook poll-expire route. Merely parsing webhook metadata from an
unrelated payload never grants the token binding, and the token is omitted from
the resource representation.

The administration surface also includes typed AutoMod rules, triggers and
actions; prune estimates and executions; bulk bans with per-user failures;
guild emoji role restrictions; sticker tags and availability; advanced invite
targets; and application-owned assets and emoji. A bot can inspect its own
Discord-style command permission scopes through `Guild.command_permissions()`
and `Guild.command_permission()`; permission edits remain user-authorized.
Application media exposes both
upload-ticket primitives and upload conveniences, so workers can choose whether
the SDK or their own storage client performs the presigned upload.

Rich messages support authored embeds, polls, live-reference forwarding,
announcement crossposts/follows, and complete reaction-group clearing. The UI
module provides buttons, string and entity selects, Kaede checkboxes, text
inputs, modals, and stateful `View` dispatch with author/application checks,
timeouts, persistent registrations, and stale-version protection. A finite
View with `disable_on_timeout=True` authoritatively disables its remote
components before it unregisters locally. Interaction
helpers cover defer, original response fetch/edit/delete, private responses,
follow-ups, attachments, autocomplete, and poll voter/end operations. As on
Discord, applications cannot vote: the message vote helpers receive
`BOT_POLL_VOTE_UNSUPPORTED` when used with bot authentication.

Announcement helpers route to qualified source/target authorities and mint a
separate receiver-bound worker intent for each authority when needed:

```python
follow = await client.follow_announcement_channel(source.ref, target.ref)
await client.crosspost_message(source.ref, message.ref)
await client.delete_announcement_follow(source.ref, EntityRef.parse(follow["ref"]))
```

The source grant is `channels.read` plus visibility; the destination grant is
`webhooks.manage` plus `MANAGE_WEBHOOKS`. Follower rename/avatar/channel
management is exposed through the destination guild's Integrations/Webhooks
surface, matching its target-owned type-2 webhook identity.

Ordinary bot messages follow Discord's mention defaults: visible user, role,
and everyone mentions are parsed unless `allowed_mentions` narrows them.
`Channel.send()`, `Client.send_message()`, and message edits accept that policy;
`Message.reply(..., mention_author=True)` opts into notifying the referenced
author (reply notifications otherwise default to off). The same policy is
authority-resolved for federated plaintext messages and authenticated inside
the encrypted payload for E2EE messages.

`Interaction` also exposes the Discord invocation snapshot: `version`,
`locale`, `guild_locale`, `app_permissions`,
`authorizing_integration_owners`, `attachment_size_limit`, and an optional
source `message`. Guild events provide `interaction.member` (including its
invocation-time `permissions`) while private events provide the top-level
`interaction.user`; `interaction.user` remains a convenience alias for the
member's user in guild handlers. Owner maps may contain both a guild and a user
installation even though the selected lifecycle authority remains singular.
Guild-installed apps used in bot DMs expose Discord's `"0"` guild-owner
sentinel. Ephemeral sources parse as `InteractionSourceMessage`, which is an
immutable snapshot without durable channel-message mutation methods.
Attachment staging rejects data larger than the event's advertised limit before
making a request. Low-level callbacks opt into Discord's `with_response=true`
object and transparently unwrap message resources, preserving the existing
`Message`/private-dictionary return contract for `respond()` and
`update_message()`.

`send_message(..., forward=source_message)` performs secure forwarding rather
than copying server-visible content. It obtains a short-lived signed proof from
the source authority, verifies the decrypted source's rich-v2 semantic
commitment, rebinds every attachment to a fresh destination upload/manifest,
and encrypts the author-free snapshot when the destination has a registered MLS
context. For an E2EE source and plaintext destination, callers must explicitly
provide the freshly uploaded plaintext `forward_attachments`; their authenticated
plaintext hashes and voice metadata must match the source manifests. Qualified
source and destination references route independently, so the same API covers
local and cross-authority installations without forwarding a bot token or
combining grants. Poll/call/activity messages and legacy encrypted envelopes
without a forward commitment fail closed.
Component handlers can acknowledge with `defer_update()` and later edit the
exact source, or use `update_message(...)` (`edit_message(...)`) for an
immediate type-7 update whose View remains registered against that source.
`await interaction.defer(ephemeral=True)` fixes response visibility at
acknowledgement time. A poll may then be supplied to the first
`edit_original_response(poll=...)`; later message and follow-up edits keep polls
immutable. Private original and follow-up polls can be ended with
`end_original_poll()` and `end_followup_poll(...)`.
Public interaction messages keep a separate private lifecycle binding to the
interaction, original/follow-up kind, durable follow-up response ID, target,
and guild- or user-install context. It is attached only by trusted response,
fetch, and edit helpers. `Message.edit()` and `delete()` route to
`.../responses/@original` or `.../followups/{response_id}`, and
`Message.end_poll()` routes to
`.../responses/{@original|response_id}/polls/expire`; none guesses from the
channel-message ID. This also preserves commands-only user-install response
lifecycle without granting ambient channel operations. Private or ephemeral
responses remain dictionaries and use the `Interaction` helpers directly.

Voice support is optional (`kaede-bot[voice]`) and joins through a short-lived
grant bound to the exact guild installation or DM capability. `VoiceClient`
can publish PCM/FFmpeg file audio, play a guild soundboard entry, receive audio
and camera/screen video frames, publish packed camera or screen-share frames
with `publish_video(VideoFrame(...))`, stop either track with `stop_video()`,
and leave cleanly. Request `stream=True` when joining; the authority and
LiveKit grant enforce the installation's video permission. The REST client
separately exposes soundboard CRUD, voice occupancy and moderation, and
voice-channel configuration. Use `voice_regions(guild_ref)` to load the guild authority's
configured catalog and set `rtc_region` on voice-channel create/update; `None`
keeps automatic region selection.

Encrypted participant voice requires a `VoiceE2EEContext` built from the
verified bot device's real MLS provider, device ID, channel group ID, and exact
current epoch. Pass that context to `connect_voice(e2ee_context=...)`. Before
LiveKit connects, the SDK validates the channel, group-bound media session,
policy generation, and provider epoch; derives the 32-byte media key with the
shared `kaede livekit v1` MLS exporter contract; and installs it through
LiveKit's native `E2EEOptions` key provider. Missing, stale, or mismatched state
fails before media can flow. Revocation, provider failure, or an epoch change
disconnects the room and clears its key. After an MLS commit, create a new
context at the new epoch and call `connect_voice` again so the server issues a
fresh grant and LiveKit room; never reuse a context or bearer grant across
epochs.

The general E2EE surface exports `NativeOpenMLSProvider`, the `E2EEProvider`
protocol, `bot_mls_credential`, `InteractionE2EEContext`, and the authenticated
interaction helpers. Device registration and KeyPackage inventory are
application-home operations; room participation is then checked at the channel
authority for every encrypted REST, Gateway, interaction, file, call, and voice
operation. Use `create_e2ee_device_challenge()` and
`complete_e2ee_device_registration()` for the explicit proof flow, or
`register_e2ee_device()` for both steps. `e2ee_devices()` lists devices and
inventory; `upload_e2ee_key_packages()` uploads one signed 1–50-package batch;
`replenish_e2ee_key_packages()` registers when needed and maintains a bounded
pool; `revoke_e2ee_device()` revokes a `kbe_` device; and
`e2ee_participation()` reads exact room status. Persist
`NativeOpenMLSProvider.export_state()` securely—the worker signing key is never
an MLS key.

Interaction responses use the same staged-media flow without exposing bot
credentials to object storage. Upload first, then include the returned ID in
the initial response, an edit, or a follow-up:

```python
attachment = await interaction.upload_attachment(
    report_bytes,
    filename="report.pdf",
    content_type="application/pdf",
)
await interaction.respond(
    "Your report is ready.",
    attachment_ids=[attachment.ref.id],
    ephemeral=True,
)
```

The installation needs `attachments.write` in addition to
`interactions.respond`. Interaction attachments are plaintext. On response
edits, `attachment_ids` is the complete set to retain: omit it to leave media
unchanged, pass existing and newly uploaded IDs to replace the set, or pass an
empty sequence to remove every attachment. `Interaction.edit_original_response`
and the follow-up helpers preserve the interaction's exact target instance.

Attachment-valued command options are authority-resolved in
`interaction.input_attachments`. The installation and worker need
`attachments.read`; use `await interaction.fetch_input_attachment(ref)` to
refresh scan state and `await interaction.read_input_attachment(ref,
max_bytes=...)` for a bounded private download. Input files are single-use and
expire with the interaction token.
