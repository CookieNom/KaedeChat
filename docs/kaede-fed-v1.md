# kaede-fed/1

This document is the versioned protocol contract between Kaede instances. It
covers:

- discovery and versioning
- signed HTTP and hot-link transport
- durable DMs
- guild synchronization and remote writes
- authenticated remote media
- voice token brokering, occupancy, and call signaling

[Discovery](#1-discovery-and-versions) · [Signatures](#2-http-request-signatures) ·
[Delivery](#4-durable-delivery) · [Guilds](#5-guild-authority-and-synchronization) ·
[DMs](#6-direct-messages) · [Routes](#7-federation-routes) ·
[Calls](#8-voice-and-calls) · [Errors](#9-blocking-and-failure-codes)

## 1. Discovery and versions

`GET https://<domain>/.well-known/kaede/server` returns:

```json
{
  "server": "federation.example.com",
  "versions": ["1", "2"],
  "permission_schema": "kaede-permissions-v1",
  "capabilities": [
    "permission-schema/kaede-permissions-v1",
    "dm-history-page/1",
    "e2ee-mls/1",
    "e2ee-media/1",
    "bot-direct-auth/1",
    "guild-audit-log/1",
    "guild-soundboard/1",
    "guild-history-sync/1",
    "guild-history-sync/2",
    "member-self-moderation/1",
    "message-search/1",
    "profile-by-ref/1",
    "request-nonce/1"
  ]
}
```

The server value is a public hostname, not a URL. Clients connect only to port
443 after resolving and pinning a public IP. Version 1 requests include
`X-Kaede-Version: 1`; peers advertising `request-nonce/1` use the
replay-protected version 2 signing form below. When there is no mutually
supported version, the failure code is `KAED_FED_UNSUPPORTED_VERSION`.

`permission_schema` and the matching
`permission-schema/kaede-permissions-v1` capability are mandatory. Every
decimal permission field in discovery, signed federation requests, event
envelopes, and imported snapshots uses Kaede's published bit layout. That
layout is not a raw Discord API v10 mask: in particular, Kaede
retains its published `STREAM = 1 << 31` and
`USE_APPLICATION_COMMANDS = 1 << 32` assignments. Implementations MUST reject
a missing or different schema instead of guessing by software name or silently
reinterpreting bits. A Discord-mask bridge needs its own explicitly versioned
translation at the bridge boundary.

Capability entries are bounded strings. The permission-schema entry is
mandatory; the remaining capabilities are optional extensions:

- `permission-schema/kaede-permissions-v1` is the mandatory permission-mask
  negotiation fence described above. A peer that omits it or advertises a
  different `permission_schema` cannot exchange authenticated federation data.

- `guild-history-sync/1` advertises the permission-bound retained-history
  transfer in section 5.1. A peer MUST NOT call that extension when it is
  absent.
- `e2ee-mls/1` advertises the MLS extension in section 6.1. Room activation
  requires this capability from every member’s instance, in addition to device
  admission and cryptographic checks.
- `e2ee-media/1` advertises encrypted-attachment support. It is informational:
  no peer operation is gated on it. Devices MAY also list `e2ee-media/1` among
  their published device capabilities.
- `guild-history-sync/2` adds recent-first history export as described in section
  5.1.
- `bot-direct-auth/1` and `guild-soundboard/1` identify the direct bot
  authentication and soundboard extensions described later in this document.
  Presence, report forwarding, and group DMs do not currently negotiate separate
  capabilities; their former discovery strings are no longer advertised.
- `dm-history-page/1` advertises signed, participant-authorized, bounded DM
  history paging. A non-authoritative home MUST NOT evict older remote-authored
  cache rows until the conversation authority advertises this capability. Media
  referenced by an on-demand page uses the same capability: every signed media
  authorization and byte request carries the exact conversation and message
  composite references, and the attachment origin verifies that its durable
  attachment row belongs to that message in that conversation. Authorization
  based only on a peer participating in some other DM is forbidden.
- Group conversations use authority-owned state, friend-confirmed invitation
  authorization, monotonic full-state membership updates, and calls with up to
  10 participants. The conversation authority signs group state, committed
  messages, and call creation. It may attest a participant-home actor for
  `dm.group.state`, `dm.group.message.committed`, and `dm.group.call.create`.
  State receivers require that actor in the prior or resulting participant set,
  and a newly added local participant must already be the actor's accepted
  friend. Messages and calls require a current participant actor.
- `guild-audit-log/1` advertises private, requester-bound audit-log paging at
  the guild home. Audit rows never appear in guild snapshots or events; the
  home rechecks current membership and `VIEW_AUDIT_LOG` for every page.
- `profile-by-ref/1` advertises exact composite-ID public profile proofs. It
  lets a replica resolve an opaque historical author from that user's own home
  without trusting mutable profile data relayed by a guild authority. Absence
  of the capability, an unavailable home, or an unknown user leaves a
  non-blocking generic identity and never invalidates the surrounding guild
  snapshot or history import.
- `member-self-moderation/1` advertises the affected-user-only timeout status
  lookup in section 5. Its response is never part of a guild replica or
  broadcast.
- `message-search/1` advertises the signed, permission-bound search endpoint in
  section 7. A user home never receives or shares the authority's search-engine
  credentials. Encrypted channels are excluded, and a missing capability
  degrades to explicitly partial local-cache results.
- `request-nonce/1`, once observed for a peer, is pinned: a later discovery
  document cannot remove it and downgrade that relationship.

Rendered same-origin media paths carry a 15-minute HMAC. After expiry, the
user's home MAY renew that exact signed tuple only after authenticating the
user, confirming current conversation participation, and repeating the
origin's exact conversation/message/attachment authorization. An expired or
tampered path is never itself sufficient to fetch bytes.

`GET /_kaede/v1/keys` returns `verify_keys` and `old_verify_keys`, keyed by key ID.
Rotation adds the former current key to `old_verify_keys`. Senders MUST continue
advertising old keys while retained events may reference them. Consumers refresh
cached sets at least hourly and mark a previously known key expired when it is
omitted from a later authoritative response.

The keys response is an unsigned JSON object with these members:

```json
{
  "server_name": "federation.example.com",
  "display_name": "Example",
  "software_version": "0.1.0",
  "current_key_id": "ed25519:20260712",
  "verify_keys": { "ed25519:20260712": "<base64 32-byte Ed25519 public key>" },
  "old_verify_keys": { "ed25519:20260101": "<base64>" }
}
```

`verify_keys` holds only the current key. A server that would advertise more than
64 old keys refuses with HTTP 503 `KAED_FED_KEY_HISTORY_OVERFLOW` rather than
publish a partial trust document.

A consumer fetches discovery and keys in one bounded exchange: at most 64 KiB for
the discovery document, at most 256 KiB for the keys document, five seconds per
request, and 12 seconds overall. Redirects are not followed. The consumer MUST
reject the peer when:

- the discovery body is not a JSON object, or `versions` is not a list containing
  `"1"`;
- `capabilities` is not a list of at most 64 strings of 1 through 64 characters
  each (duplicates are collapsed);
- `permission_schema` or the matching capability is missing or different;
- the normalized `server` value differs from the queried domain (delegated
  federation hosts are not supported);
- `server_name`, when present, differs from the queried domain;
- either key map is not an object of at most 64 entries, a key ID does not match
  `^[A-Za-z0-9._:-]{1,64}$`, or a key is not base64 for exactly 32 bytes;
- the same key ID appears in both maps, or in the consumer's retained history,
  with different key material (key IDs are immutable; rotation allocates a new
  ID);
- `current_key_id` is missing, malformed, or absent from `verify_keys`;
- accepting the document would exceed the consumer's retained per-peer key
  history (default 512 keys); or
- a previously observed pinned capability (`request-nonce/1`) has been removed.

Every check runs before any trust state changes. Accepted keys and the capability
set are then replaced atomically, and previously active keys omitted from both
maps are retired. `message-search/1` is omitted from discovery while the
operator has disabled search.

## 2. HTTP request signatures

The signing object is UTF-8 JSON with keys sorted lexicographically, no insignificant
whitespace, no NaN/Infinity, and the exact members below:

```json
{
  "content_sha256": "<lowercase hex>",
  "destination": "beta.example",
  "method": "POST",
  "origin": "alpha.example",
  "request_target": "/_kaede/v1/inbox?a=1&z=2",
  "ts": 1783886400
}
```

`request_target` contains the path and a query encoded as sorted key/value pairs.
`content_sha256` hashes the exact HTTP body bytes. Ed25519 signs the canonical
object. The request MUST carry `Authorization: Kaede origin="<domain>",key="<key
id>",sig="<base64 signature>"`, `X-Kaede-Timestamp` with the decimal Unix-seconds
value of `ts`, and `X-Kaede-Version`. `X-Kaede-Hop` is optional, defaults to `0`,
and MUST lie in 0 through 5. The destination rejects timestamps outside the
configured clock-skew window (default ±300 seconds), mismatched destinations,
unknown/expired keys, invalid body hashes, and invalid signatures.

When both peers support `request-nonce/1`, the sender uses `X-Kaede-Version: 2`,
adds an unpredictable 22–64 character base64url `X-Kaede-Nonce`, and includes
`"nonce":"<exact header value>"` in the signing object. The receiver records a
successfully verified nonce for longer than the accepted clock-skew window and
rejects reuse. Changing, removing, or replaying the nonce therefore cannot reuse
the signature. Version 1 remains accepted only for peers that have never
advertised this capability, preserving rolling compatibility.

WebSocket upgrades use the same signature inputs. TLS remains mandatory even though
requests and envelopes are signed.

### Fixed request-signing vector

This vector is normative. The private seed is published only to make independent
implementations reproducible and must never be used as an operational key.

| Input | Exact value |
| --- | --- |
| Ed25519 private seed (hex) | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| Ed25519 public key (base64) | `A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=` |
| Exact HTTP body | `{"events":[]}` |
| Body SHA-256 | `24de1c4a19c43ad41b013f13dcd858c17b0daa7f33a53f19913e5b11366d1c2e` |
| Method | `POST` |
| Unsorted input query | `z=2&a=hello%20world` |
| Canonical request target | `/_kaede/v1/inbox?a=hello+world&z=2` |
| Origin / destination | `alpha.example` / `beta.example` |
| Timestamp | `1783886400` |

The exact canonical UTF-8 bytes, shown as text, are:

```json
{
  "content_sha256": "24de1c4a19c43ad41b013f13dcd858c17b0daa7f33a53f19913e5b11366d1c2e",
  "destination": "beta.example",
  "method": "POST",
  "origin": "alpha.example",
  "request_target": "/_kaede/v1/inbox?a=hello+world&z=2",
  "ts": 1783886400
}
```

The resulting Ed25519 signature is:

```text
Gj8RsqP7pnJVuSpHhbXZLERH24yA3RyZzsoDHoH9AWdxv6z60PbS4ztNg4BzPb6n4H5oVB/41ZFjaDJKevzPDQ==
```

## 3. Event envelopes

```json
{
  "event_id": "kcfe_opaque-id",
  "origin": "alpha.example",
  "type": "message.create",
  "ts": 1783886400000,
  "actor": { "id": "123", "domain": "alpha.example" },
  "context": { "guild_id": "456", "guild_domain": "beta.example", "seq": "91" },
  "content": {},
  "signatures": { "alpha.example": { "ed25519:20260712": "<base64>" } }
}
```

The origin signs the envelope without `signatures`. A transport signer may only
send actors whose domain equals its verified origin. Violations are rejected as
`KAED_FED_AUTHOR_ORIGIN_MISMATCH`. Snowflakes and permission fields are decimal
strings. Envelopes from the future beyond the negotiated HTTP clock skew or older
than the receiver's configured event-retention window are rejected, preventing an
idempotency record that has aged out from enabling an indefinite replay. Unknown
optional content keys are ignored; missing required keys reject only that event.

Envelope members are bounded as follows. `event_id` matches
`^kcfe_[A-Za-z0-9_-]{16,59}$`. `type` is 1 through 100 characters. `ts` is a
non-negative integer of Unix milliseconds. `actor.id` is a canonical decimal
snowflake and `actor.domain` a normalized federation domain. `signatures` names
one through eight signer domains, each with one through eight entries whose key
IDs match `^[A-Za-z0-9._:-]{1,64}$` and whose signatures are at most 128
characters. Unknown members of the envelope and of `actor` are retained in the
canonical signing input rather than dropped, so they cannot be altered in transit.
`context` and `content` default to empty objects. The receiver's event-retention
window defaults to 30 days and cannot be configured below seven.

The actor rule has a closed set of exceptions. In each, the envelope `origin`
MUST still equal the authenticated transport origin (except for a relayed
`media.delete`, below), the signature MUST verify with the origin's key, and the
receiver independently validates the attributed actor. A foreign actor domain is
accepted only for:

- guild-authority attestation of the closed guild-control family, whose context
  or content names the signing origin as guild authority: every sequenced guild
  mutation in the registry below, `guild.member.add`, `guild.message.committed`,
  `guild.event.redacted`, `guild.access.revoked`,
  `guild.instance_access.revoked`, `guild.resync.required`,
  `message.send_rejected`, `announcement.crosspost.sync`, all seven
  `guild.announcement.follow.*` names, `guild.audit-log.page`, and the three
  `guild.soundboard.*` response envelopes; plus the exact
  `guild.media.delete.request` shape and authority-attested guild message and
  crosspost references;
- group-conversation authority attestation of `dm.group.*` events, including
  the terminal deleted-group `dm.group.state`;
- three exact authority-minted `dm.message.create` shapes that keep a remote
  participant as semantic author: an MLS room-activation or rekey control
  message, a pin system notice, and a poll-result message;
- direct-conversation authority relays of `dm.poll.*`, `dm.message.update`,
  `dm.message.delete`, `dm.reaction.*`, and `dm.pin.*`;
- an authenticated room-policy change (`e2ee.room-policy.changed`);
- `bot.application.target.changed` signed by a target instance for an
  application-home actor, `developer.team.snapshot`,
  `bot.dm.installation-capability`, and `bot.interaction.response`, each in its
  exact authority-attested shape;
- the exact terminal `guild.instance_access.revoked` shape; and
- `media.delete` in the exact removal-only shape, where the actor is attribution
  only.

A `media.delete` envelope MAY also be relayed by a third instance: the transport
origin then differs from the envelope origin, the receiver verifies the envelope
against the attachment origin's keys (refreshing them if necessary), and applies
its block policy for the attachment origin. Signed request and response proofs
verified outside the inbox use the same predicate family, extended with
`message.forward.source.authorized` and `expression.use.authorized`.

Some envelopes are exempt from the lower age bound, because they are permanent
invalidations or latest-state projections that a peer recovering from a long
outage MUST still accept. They are `media.delete`; the latest-state set
(`developer.team.snapshot`, `bot.application.runtime.changed`,
`bot.application.target.changed`, `bot.dm.installation-capability`,
`e2ee.device-list.changed`, `e2ee.room-policy.changed`, and the five
`guild.announcement.follow.*` lifecycle events); the exact terminal-room shapes
(`guild.instance_access.revoked` with an authority-attested terminal actor, and a
`dm.group.state` whose conversation has `deleted: true` and an empty participant
list); and `guild.media.delete.request`. The future-skew bound, signature, and
idempotency checks still apply to them.

Before signature canonicalization, a decoded envelope is also limited to 24 nested
levels, 16,384 JSON values, 1,024 members per object, 4,096 items per array, and
256 UTF-8 bytes per object key. Strings remain bounded by the 1 MiB frame/request
limit. NUL, floating-point numbers, non-JSON values, and integers outside the
interoperable range `-(2^53-1)` through `2^53-1` are rejected; larger identifiers
and counters use decimal strings. Requiring integer or string numeric projections
avoids Python/JavaScript number serialization differences in signed bytes. The
same structural limits apply to unknown signed extension members, which keeps
extension data from becoming a parser, stack, or canonicalization
resource-exhaustion surface.

JSON object names must be unique at every level. Receivers reject duplicate-key,
non-finite, floating-point, out-of-range integer, or pathologically nested request
JSON before route validation, so independent implementations cannot interpret the
same signed bytes differently.

Floating-point numbers have exactly two exceptions, and neither applies to a
durable envelope. The signed request body of
`POST /_kaede/v1/channels/{channel}/interactions` MAY contain finite floats, which
carry Discord-compatible `NUMBER` command options. Response bodies of the guild,
application, and developer management RPCs and of DM-bot E2EE management MAY
also contain finite floats inside their bounded public API objects. Their signed
requests still pass the ordinary transport parse and therefore cannot. Non-finite
values are always rejected.

### Durable event registry

Unregistered durable event types are rejected. The HTTP inbox and hot link
accept the following exact names:

| Event type | Authority and purpose |
| --- | --- |
| `relationship.request` | A user's home sends a versioned actor profile and an unguessable request correlation ID to the target user's home. |
| `relationship.accept` | The target user's home accepts only the exact still-pending correlation ID. Late acceptance cannot recreate cancelled or blocked state. |
| `relationship.remove` | A user's home removes friendship or pending state at the peer. The sender never reveals whether the local reason was removal or blocking. |
| `relationship.profile` | A user's home sends a versioned profile update to an accepted remote friend. The receiver applies it only while the exact friendship is still active. |
| `dm.open.request` | A participant asks the deterministic DM authority to open a conversation asynchronously. |
| `dm.conversation.create` | The deterministic authority announces the converged conversation and its two participants. |
| `dm.open.rejected` | The authority rejects a previously queued open request with a stable code. |
| `dm.message.create` | A participant's home replicates one DM message to the other participant's instance. |
| `dm.message.update`, `dm.message.delete` | The deterministic conversation authority relays an author's validated edit or terminal tombstone to every participant home. |
| `dm.reaction.add`, `dm.reaction.remove` | The deterministic conversation authority relays one participant's canonical reaction state change. |
| `dm.poll.vote.add`, `dm.poll.vote.remove`, `dm.poll.finalize` | The deterministic conversation authority relays opaque poll vote state or finalization; encrypted question and answer text never enters the event. |
| `dm.pin.add`, `dm.pin.remove` | The deterministic conversation authority relays the shared conversation pin state to every participant home. |
| `dm.group.state` | The group conversation authority sends monotonic full conversation and participant state. A `deleted: true` conversation with no participants is a terminal room deletion. |
| `dm.group.message.proposed` | A participant's home proposes one group message to the group conversation authority. |
| `dm.group.message.committed` | The group conversation authority distributes an accepted group message to every participant home. |
| `dm.group.call.create` | The group conversation authority announces a new group call. |
| `e2ee.device-list.changed` | A user's home publishes the latest device-list state for E2EE admission. Latest-state projection. |
| `e2ee.room-policy.changed` | The room authority publishes an authenticated, monotonic room encryption-policy change. Latest-state projection. |
| `bot.application.runtime.changed` | The application home publishes the roster-free runtime authorization snapshot (status, manifest, revocation, and worker generations) for one target instance. Latest-state projection. |
| `bot.application.target.changed` | A target instance publishes its roster-free installation-presence aggregate for one application to the application home; this is the only bot event a target may sign for an application-home actor. Latest-state projection. |
| `bot.dm.installation-capability` | The installation home publishes the latest DM-installation capability grant for a user. Latest-state projection. |
| `bot.interaction.response` | The channel authority delivers one interaction response (callback types 4, 7, 8, or 9) to the invoker's home. It expires with the interaction response window. |
| `developer.team.snapshot` | The developer team's home publishes the latest team and application projection. Latest-state projection. |
| `guild.member.add` | The guild home announces a remote invite join. |
| `guild.leave.request` | A member's home durably asks the guild home to remove that member; the user domain must equal the signing origin. |
| `guild.member.profile` | A user's home sends its exact versioned profile either to a remote guild authority or, when it is also the guild authority, directly to the guild's current remote member homes. |
| `guild.update` | The guild home replaces mutable guild metadata. |
| `guild.channel.create`, `guild.channel.update`, `guild.channel.delete` | The guild home creates, replaces, or removes channel state. |
| `guild.forum.cursor.update` | The guild home moves a forum channel's latest-post cursor. |
| `guild.thread.member.upsert`, `guild.thread.member.delete` | The guild home adds, updates, or removes one thread membership. |
| `guild.tracker.board.invalidate` | The guild home tells replicas that a tracker board changed; replicas re-read it through the tracker snapshot route. |
| `guild.role.create`, `guild.role.update`, `guild.role.delete` | The guild home creates, replaces, or removes role state. |
| `guild.emoji.create`, `guild.emoji.update`, `guild.emoji.delete` | The guild home creates, renames, or removes a content-addressed custom emoji. |
| `guild.sticker.create`, `guild.sticker.update`, `guild.sticker.delete` | The guild home creates, replaces, or removes a guild sticker. |
| `guild.soundboard.sound.create`, `guild.soundboard.sound.update`, `guild.soundboard.sound.delete` | The guild home creates, replaces, or removes one soundboard sound. |
| `guild.soundboard.sounds.update` | The guild home replaces the guild's complete soundboard sound list. |
| `guild.stage.instance.create`, `guild.stage.instance.update`, `guild.stage.instance.delete` | The guild home starts, edits, or ends a Stage instance. |
| `guild.scheduled_event.create`, `guild.scheduled_event.update`, `guild.scheduled_event.delete` | The guild home creates, replaces, or removes a scheduled event. |
| `guild.scheduled_event.user.add`, `guild.scheduled_event.user.remove` | The guild home changes one scheduled-event subscription. |
| `guild.voice_channel_status.update`, `guild.voice_channel_start_time.update` | The guild home changes a voice channel's status text or session start time. |
| `guild.automod.rule.create`, `guild.automod.rule.update`, `guild.automod.rule.delete` | The guild home creates, replaces, or removes an AutoMod rule. |
| `guild.automod.execution` | The guild home reports one AutoMod action execution. |
| `guild.overwrite.upsert`, `guild.overwrite.delete` | The guild home replaces or removes a channel permission overwrite. |
| `guild.member.update`, `guild.member.remove` | The guild home updates or removes membership state. |
| `guild.member.profile.relay` | The guild home sequences an exact user-home-signed `guild.member.profile` source for current member homes; it cannot rewrite the nested profile. |
| `guild.members.origin.remove` | The guild home atomically removes every member homed on one federated origin after an instance-wide sanction. |
| `guild.member.role.add`, `guild.member.role.remove` | The guild home changes one member-role assignment. |
| `guild.ban.add`, `guild.ban.remove` | The guild home changes its moderation ban set; an add may carry an absolute expiry. |
| `guild.access.revoked` | The guild home directly removes one user at the target user's origin; it remains valid when that origin has no member left and can no longer request snapshots. |
| `guild.instance_access.revoked` | The guild home directly removes all of the target origin's local members and instructs that origin to purge its cached guild data after an instance-wide ban. |
| `guild.resync.required` | A revision-bound guild-home marker replaces expired delivery rows and requires background gap-fill/full snapshot recovery. |
| `guild.message.create` | The guild home announces a message authored on the home instance. |
| `guild.message.update`, `guild.message.delete`, `guild.message.purge` | The guild home edits, tombstones, or author/time-range purges messages. |
| `guild.message.bulk_delete` | The guild home tombstones an explicit set of messages in one channel. |
| `guild.reaction.add`, `guild.reaction.remove` | The guild home changes one message reaction. |
| `guild.reaction.clear` | The guild home removes all reactions, or all reactions of one emoji, from a message. |
| `guild.poll.vote.add`, `guild.poll.vote.remove`, `guild.poll.finalize` | The guild home changes one poll vote or closes a poll; member homes apply the mutation in guild sequence order. |
| `guild.pin.add`, `guild.pin.remove` | The guild home changes one channel pin. |
| `guild.proxy.message.create` | A replica durably queues a remote member's write for the guild home. |
| `guild.message.committed` | The guild home announces the authoritative result of a proxied write. |
| `guild.event.redacted` | Signed placeholder that advances a peer past a channel event none of its members may inspect. |
| `message.send_rejected` | The guild home rejects a previously queued proxy write. |
| `guild.announcement.follow.accepted`, `guild.announcement.follow.finalized`, `guild.announcement.follow.rejected`, `guild.announcement.follow.revoked`, `guild.announcement.follow.updated` | The source or target guild authority advances a cross-authority announcement-follow saga (section 6.1). Latest-state projections. |
| `announcement.crosspost.sync` | The source guild authority queues a crosspost copy edit or deletion against the immutable delivery receipt of one follow. |
| `guild.media.delete.request` | A guild home asks an attachment's origin to delete origin-owned media from a deleted message after that origin has left the guild. It is a minimal removal-only control addressed directly to the attachment home. |
| `media.delete` | The attachment origin invalidates every cached variant of one origin-owned attachment. |

The guild-mutation rows above, `guild.member.add`, `guild.message.create`,
`guild.message.committed`, and `guild.event.redacted` are the only names that
`GET /guilds/{id}/events` gap fill may return.

The following signed envelope types are exchanged only as request or response
proofs on the HTTP routes in section 7. They are not durable events, and an
inbox or hot link that receives one rejects it as unregistered:

| Envelope type | Carried by |
| --- | --- |
| `user.profile` | `GET /users/profile` response |
| `guild.audit-log.page` | `POST /guilds/{id}/audit-logs` response |
| `guild.soundboard.query`, `guild.soundboard.play`, `guild.soundboard.source-capability` | Soundboard query, play, and capability responses |
| `guild.announcement.follow.source_authorized`, `guild.announcement.follow.authorized` | Announcement-follow authorization exchanges |
| `message.forward.source.authorized` | `POST /channels/{channel}/forward-authorize` response |
| `expression.use.authorized` | `POST /expressions/authorize` response |
| `bot.application.manifest`, `bot.application.runtime-manifest` | Application manifest routes |
| `bot.worker.authorization` | Worker authorization route |
| `bot.e2ee.device-snapshot` | `POST /bot-e2ee/applications/{application}/devices` response |
| `federation.actor.intent` | Nested actor-home intent inside a resource-authority request (section 6.1) |

The following content schemas complete the group, bot, and developer entries
above. `D+` denotes a canonical decimal string from 1 through 2^63−1; `D0`
also permits `"0"`. `Ref` denotes a qualified `<id>@<domain>` reference, and
`Domain` denotes a canonical federation domain. JSON numbers MUST NOT replace
these decimal strings. `Profile` is the user profile object: required `id: D0`,
`origin_domain: Domain`, and `username: string` (`[a-z0-9_.]{2,32}`); `account_type` is `human`
(default) or `bot`; nullable `display_name` (100 characters), `avatar_hash` and
`banner_hash` (128 each), `bio` (500), and `custom_status` (128) default to null.
`profile_version` is an integer defaulting to 1, from 1 through 2^31−1;
`e2ee_device_generation` is an integer defaulting to 0, from 0 through 2^63−1. The message
objects below use the ordinary message payload and the encrypted-message rules
in section 6.1; embedding them does not relax attachment, author, or room checks.

| Group event | `content` and `context` |
| --- | --- |
| `dm.group.state` | `content` contains `conversation` and `participants: Profile[]`, with optional `notice: {message, author: Profile, target: Profile}`. `conversation` contains `id: D+`, `origin_domain: Domain`, `pair_key: string`, `type: "group"`, `authority_domain: Domain`, `owner: {id: D+, origin_domain: Domain}`, `name: string or null`, `state_version: D+`, `deleted: boolean`, and `encryption_policy`. `context` is empty. |
| `dm.group.message.proposed`, `dm.group.message.committed` | `content` requires `message` and `author: Profile`; optional fields are `forward_source_nsfw: boolean`, the forwarding authorization `forward_source_proof`, `encryption_policy`, and `e2ee_control`. `context` contains `conversation_id: D+`, `conversation_domain: Domain`, and `state_version: D+`. The proposed form travels from the author's home to the conversation authority; the committed form travels from that authority to participant homes. |
| `dm.group.call.create` | `content` contains only `call`, whose fields are `id: D+`, `channel_id: D+`, `channel_domain: Domain`, `authority_domain: Domain`, `room: string`, `state: "ringing"`, `created_at: integer` (Unix milliseconds, nonnegative), `ended_at: null`, `caller: Ref`, and `participants: Ref[]`. `context` has the same three conversation fields as a group message. |

In group state, both conversation domains equal the signing authority, and
`pair_key` is the lowercase SHA-256 hex digest of
`"group\n" + authority_domain + "\n" + id`. A live snapshot has one through ten
unique participants, includes its owner, and has a null name or a trimmed name
of one through 100 characters. A membership `notice` describes exactly the
accepted addition, departure, or removal; its message type is respectively 3,
4, or 5, its flags are 4, and its author is the envelope actor. It carries no
attachments, encryption body, references, nonce, edits, deletion, or mentions.
Terminal state instead has `deleted: true`, no participants or notice, and the
`_terminal_generation` field and exact deletion shape specified in section 7.
The encryption policy uses the existing `mode`, `state`, `generation`,
`protocol`, `suite`, `group_id`, and `epoch` fields.

A group message's channel reference MUST match its context; its author profile
and message author MUST match the envelope actor, who must be a participant.
A receiver behind the required `state_version` retries instead of applying the
message. For a committed message whose actor is remote from the authority, the
current authority-attestation parser accepts only `message`, `author`,
`forward_source_nsfw`, `encryption_policy`, and `e2ee_control`; a nested
`forward_source_proof` is not currently accepted on that path. A group call has
two through ten distinct participants, includes its caller, and names the
envelope actor as caller. Its channel and authority domains equal the group
authority, and its room is exactly `d.<conversation_id>.<call_id>`.

`bot.application.runtime.changed` and `bot.application.target.changed` have
empty `context` and the following closed content objects; unknown fields are
rejected. The application and bot domains MUST match.

| Event | Required content fields |
| --- | --- |
| `bot.application.runtime.changed` | `application_id: D+`, `application_domain: Domain`, `bot_user_id: D+`, `bot_user_domain: Domain`, `target_domain: Domain`, `manifest_generation: D+`, `revocation_generation: D+`, `access_revocation_generation: D0`, `status`, `target_allowed: boolean`, and `workers`. `status` is `draft`, `active`, `review_required`, `suspended`, `deleting`, or `deleted`. `workers` has at most 100 distinct entries `{id: D+, generation: D+, revoked: boolean, target_allowed: boolean}`. |
| `bot.application.target.changed` | `application_id: D+`, `application_domain: Domain`, `bot_user_id: D+`, `bot_user_domain: Domain`, `target_domain: Domain`, `generation: D+`, `guild_installations: D0`, and `user_installations: D0`. Both counts are aggregates, never installation or member rosters. |

The runtime event is signed by the application home for its bot actor and one
exact target. The target event is signed by `target_domain` for that same bot
actor; the target is active if either installation count is nonzero.

`bot.dm.installation-capability` has empty `context` and a closed content
object. `version` defaults to 1; `guild_ref` and `installing_user_ref` default
to null. All other fields below are required.

| Fields | Types and constraints |
| --- | --- |
| `version`, `grant_id`, `source_kind` | `1`; `kbdg_` followed by 43 base64url characters; `guild` or `user`. |
| `installation_ref`, `application_ref`, `bot_user_ref`, `target_user_ref` | `Ref`. The application and bot share their home domain. |
| `guild_ref`, `installing_user_ref` | Nullable `Ref`. A guild source requires only `guild_ref`; a user source requires only `installing_user_ref`, equal to `target_user_ref`. The installation and its guild or installing user share a home domain. |
| `pair_key`, `authority_domain` | A 64-character lowercase hex digest for the bot/user pair; the DM conversation authority's `Domain`. |
| `scopes`, `intents`, `channel_restrictions` | Sorted, unique arrays of supported scope strings (at most 64, including `dm.send`), supported intent strings (at most 32, including `direct_messages`), and `Ref` values (at most 500), respectively. |
| `e2ee_mode` | `disabled` or `participant`. |
| `installation_revision`, `runtime_manifest_generation`, `runtime_revocation_generation`, `revision`, `expires_at_ms` | `D+`; the expiry is Unix milliseconds. |
| `target_access_revocation_generation`, `runtime_snapshot_fingerprint` | `D0`; the 64-character lowercase SHA-256 hex digest of the bound runtime snapshot. |
| `status` | `active`, `suspended`, or `revoked`. |

The installation home signs for the bot actor; `authority_domain` identifies
the conversation authority and need not be the signing origin. The `grant_id`
MUST equal `"kbdg_"` plus the unpadded base64url SHA-256 digest of the UTF-8
string `"kaede-bot-dm-capability-v1\n" + source_kind + "\n" + installation_ref
+ "\n" + application_ref + "\n" + bot_user_ref + "\n" + pair_key + "\n"
+ authority_domain`. A live grant also binds the accepted runtime
revision and snapshot fingerprint; the expiry alone does not confer authority.

`bot.interaction.response` has empty `context` and exactly the following
content fields, including fields whose value is null:

| Fields | Types and constraints |
| --- | --- |
| `authority_domain`, `interaction_id`, `response_id` | `Domain`, `D+`, `D+`. The authority is the channel/interaction authority. |
| `interaction_ref`, `response_ref`, `channel_ref`, `application_ref`, `user_ref`, `invoker_ref` | `Ref`. The first three belong to `authority_domain`; `interaction_ref` and `response_ref` contain the corresponding IDs. `user_ref` equals `invoker_ref` and the envelope actor. |
| `response_grant_id`, `sequence`, `revision` | A 43-character base64url encoding of 32 bytes; an integer from 0 through 2^63−1; `D+`. |
| `callback_type`, `ephemeral`, `data` | Integer 4, 7, 8, or 9; boolean; callback payload object. Encrypted responses retain only `e2ee`, `attachments`, and any `view_version`, `view_expires_at`, and `view_persistent` replay fields. |
| `message_ref`, `autocomplete_generation` | Nullable `Ref` belonging to the channel authority; nullable `D+`. Ephemeral responses and callback types 8 and 9 require a null message reference. Only callback type 8 has a non-null autocomplete generation. |
| `operation`, `expires_at`, `deleted_at` | `CREATE`, `UPDATE`, or `DELETE`; an ISO 8601 timestamp with timezone; nullable ISO 8601 timestamp. |

`CREATE` has revision 1; `UPDATE` and `DELETE` have a greater revision.
Only `DELETE` has a non-null `deleted_at`, and its `data` MUST be empty.
The response's authority attests the human invoker, delivers only to that
invoker's home, and retains the event only within the response window.

`developer.team.snapshot` has empty `context` and closed content
`{team_id: D+, team_domain: Domain, team_name: string, personal: boolean,
revision: D+, member_id: D+, member_domain: Domain, member_role, applications}`.
The team name has one through 100 characters. `member_role` is null or one of
`owner`, `administrator`, `developer`, `security`, `analyst`, and `support`.
Null means removal and requires an empty application list. `applications`
contains at most 75 distinct application projections with distinct bot
identities, each belonging to this team. The team home signs a member-specific
projection for that member as envelope actor; this is not a full team roster.

Each application projection is a closed object with the following fields.
Nullable metadata fields default to null; other fields are required.

| Fields | Types and constraints |
| --- | --- |
| `id`, `origin_domain`, `team_id`, `team_domain`, `bot_user` | `D+`, `Domain`, `D+`, `Domain`, `Profile`. Both domains and the bot's domain match; the bot profile has `id: D+` and `account_type: "bot"`. |
| `name`, `description` | String of one through 100 characters; nullable string of at most 1,000 characters. |
| `icon_hash`, `banner_hash`, `support_url`, `privacy_url`, `terms_url` | Nullable lowercase SHA-256 hex image hashes (64 characters) and canonical HTTPS URLs (at most 2,048 characters). |
| `directory_enabled`, `directory_approved`, `directory_summary`, `directory_category` | Two booleans; nullable string of at most 200 characters; null or `entertainment`, `games`, `moderation`, `productivity`, `social`, `utilities`. |
| `directory_tags`, `directory_collections` | At most five unique tags matching `[a-z0-9][a-z0-9_-]{0,31}`; at most three unique values from `featured`, `staff-picks`, `new-and-noteworthy`. |
| `directory_media` | At most five unique objects: `{type: "image", asset_id: D+}` or `{type: "youtube", video_id: string}` with an 11-character `[A-Za-z0-9_-]` video ID. |
| `directory_external_links` | At most five `{name: string, url: string}` objects, with nonblank names up to 100 characters and canonical HTTPS URLs up to 2,048; names (case-insensitive) and URLs are unique. |
| `directory_supported_locales`, `directory_description_localizations` | Unique supported command-locale strings and a map from those locale strings to nonblank descriptions of at most 1,000 characters. Every localized description must name a supported locale. |
| `status`, `custody_mode`, `target_policy` | The runtime status enum above; `managed` or `external`; `open`, `allowlist`, `blocklist`, or `local_only`. |
| `default_scopes`, `default_intents`, `default_permissions` | At most 64 supported scope strings; at most 32 supported intent strings; `D0` containing only known permission bits. |
| `supported_install_types`, `user_install_scopes`, `user_install_contexts`, `e2ee_modes` | One or two distinct values from `guild_install`, `user_install`; two through four supported user-install scopes; one through three distinct values from `guild`, `bot_dm`, `private_channel`; zero or one `participant` value. The application install contract applies to all four lists. |
| `manifest_generation`, `command_generation`, `revocation_generation` | `D+`. |

An approved directory projection additionally requires enabled discovery,
nonblank name, description and summary, a category, at least one tag, and all
three support/privacy/terms URLs. Installation scopes and intents MUST be
supported and unique, including the required user-install scopes.

The bot HTTP proof envelopes also have empty `context` and closed content
objects. Their nested manifest objects use the application install and command
contracts; signed proof responses are not accepted as durable inbox events.

| Proof type | Content |
| --- | --- |
| `bot.application.manifest` | `{application: ManifestApplication, template: ManifestTemplate, workers: ManifestWorker[], commands: ManifestCommand[], emojis: ManifestEmoji[]}`. Limits are 100 workers, 130 commands, and 2,000 emojis. `emojis` defaults to an empty list for older homes. |
| `bot.application.runtime-manifest` | `{target_domain: Domain, application: ManifestApplication, revocation_generation: D+, workers: ManifestWorker[]}`; at most 100 workers with distinct IDs. |
| `bot.worker.authorization` | `{application_id: D+, application_domain: Domain, bot_user_id: D+, worker: ManifestWorker, manifest_generation: D+, command_generation: D+ or null, revocation_generation: D+, dm_capability: Delegation or null}`. `command_generation` and `dm_capability` default to null; new homes send the command generation. |

`ManifestApplication` contains `id`, `origin_domain`, `team_id`, `team_domain`,
`name`, `description`, `icon_hash`, `support_url`, `privacy_url`, `target_policy`,
`default_scopes`, `default_intents`, `default_permissions`,
`supported_install_types`, `user_install_scopes`, `user_install_contexts`,
`e2ee_modes`, `manifest_generation`, `command_generation`, and `bot_user`, with
the types and bounds of the developer application projection above, plus
`status`, which MUST be `active`. It has no directory, banner, terms, custody,
or revocation fields. Nullable metadata defaults to null.
`supported_install_types` defaults to `["guild_install"]`,
`user_install_scopes` to `["applications.commands", "interactions.respond"]`,
and `user_install_contexts` to `["guild", "bot_dm", "private_channel"]`.

| Nested object | Fields |
| --- | --- |
| `ManifestTemplate` | `id: D+`, `slug: string` matching `[a-z0-9][a-z0-9_-]{1,63}`, `name: string` (1–100 characters), `description: string or null` (at most 500, default null), `scopes: string[]` (at most 64), `intents: string[]` (at most 32), `permissions: D0`, `contexts: ["guild"]`, `e2ee_mode: "disabled" or "participant"`, and `generation: D+`. Scopes/intents are unique and supported; permissions contain only known bits. |
| `ManifestWorker` | `id: D+`, `name: string` (1–100 characters), `public_key: string` (43–44 characters), `scopes: string[]` (at most 64), `intents: string[]` (at most 32), `target_domains: Domain[]` (at most 100), `generation: D+`, and `expires_at: string or null` (ISO 8601 with timezone, default null). Scope, intent, and target arrays are unique; scopes and intents are supported. |
| `ManifestCommand` | `id: D+`, `name: string` (1–32 characters), `type: "chat_input", "user", or "message"` (default `chat_input`), `description: string` (at most 100, default empty), `name_localizations` and `description_localizations` (maps of at most 32 locale/string entries each, default empty), `default_member_permissions` (at most 64 permission names or `"0"`, default empty list), `nsfw: boolean` (default false), `contexts` (1–3 distinct `guild`, `bot_dm`, or `private_channel` values), `integration_types` (1–2 distinct `guild_install` or `user_install` values), and `options` (at most 25 command-option objects, default empty). |
| `ManifestEmoji` | `id` and `version` are positive decimal strings of 1–19 digits; `name` matches `[A-Za-z0-9_]{2,32}`, `media_hash` is lowercase SHA-256 hex, `animated: boolean` defaults to false, and `available: boolean` defaults to true. |
| `Delegation` | `grant_id` matching `kbdg_[A-Za-z0-9_-]{43}`, `revision: D+`, `authority_domain: Domain`, `conversation_ref: Ref` with that domain, and `expires_at_ms: D+` (future Unix milliseconds). |

Template and worker scopes/intents MUST remain within the application grant;
template E2EE participation MUST be supported by the application. Applications
without guild installation support have empty template grants. Command IDs and
names within each command type are unique, and integration types MUST be
supported by the application. Chat-input commands require a description;
user/message commands have no description, description localizations, or
options. Command options and their nested choices/subcommands retain the same
validation as local command registration, including the 8,000-character total
command budget and the prohibition on mixing subcommand containers with scalar
options.

`bot.e2ee.device-snapshot` has closed content
`{application_id, application_domain, bot_user_id, bot_user_domain, generation,
devices}`. Its IDs and generation are positive decimal strings of 1–19 digits;
its two domains are strings of 1–253 characters matching the signed application
home. `devices` contains at most 16 closed descriptors with distinct protocol
IDs. A descriptor has:

| Fields | Types and constraints |
| --- | --- |
| `source_id`, `worker_id`, `generation` | Positive decimal strings of 1–19 digits. |
| `source_domain`, `protocol_id` | Canonical `Domain` (1–253 characters), equal to both snapshot domains; `kbe_` followed by 43 base64url characters. |
| `identity_key`, `credential` | Base64url strings: exactly 43 characters decoding to 32 bytes; 2–22,000 characters decoding to at most 16,384 bytes. |
| `capabilities` | One through eight distinct strings from `e2ee-mls/1` and `e2ee-media/1`, requiring `e2ee-mls/1`; receivers normalize the order. |
| `trust_state` | `trusted` (default). |

`federation.actor.intent` is an actor-home-signed human intent envelope, not a
durable event. Its `context` is empty and its content is a closed object:

| Field | Type and constraints |
| --- | --- |
| `version` | Integer 1 (default). |
| `action` | One through 100 characters matching `[a-z0-9._-]+`. |
| `audience`, `actor_ref` | Receiving authority `Domain` (at most 253 characters); `Ref` (3–320 characters) matching the envelope actor. |
| `resources` | Map of one through 16 string keys to string values. Keys have one through 64 lowercase-letter, digit, `.`, `_`, or `-` characters; values have one through 512 characters. |
| `issued_at`, `expires_at` | Positive integer Unix seconds; `issued_at < expires_at <= issued_at + 120`. |
| `nonce` | 16–128 characters matching `[A-Za-z0-9_-]+`. |

The receiver MUST match the action, audience, actor, and entire resource map to
the requested operation, reject expired proofs and issue times over 60 seconds
in the future, and require the envelope timestamp to agree with `issued_at`
within one second. Worker-signed bot actor intents are a separate proof object,
not a `federation.actor.intent` envelope. They use the same action, audience,
resources, time, and nonce fields plus `application_ref`, `actor_ref`,
`worker_id`, `worker_generation`, `runtime_target`,
`runtime_manifest_generation`, `runtime_revocation_generation`,
`runtime_access_revocation_generation`, and `signature`. IDs and generations
are decimal strings; only the access-revocation generation may be zero.
The signature is a base64url Ed25519 signature over
`"kaede-federated-actor-intent-v1\n"` followed by the canonical JSON object with
`signature` removed. The receiver also verifies current worker and runtime
authorization for the exact named target.

Typing, presence, voice-state, and occupancy are not durable events. A typing
actor's home sends a signed, ten-second `POST /_kaede/v1/typing/publish`
projection to the exact channel authority. The authority rechecks current room
membership, channel visibility, member-interaction policy, and bot installation
or DM-capability lineage before sending direct
`POST /_kaede/v1/typing/relay` requests. Each relay carries a sorted, bounded
batch of exact destination-home user references selected under the authority's
current channel ACL. The receiver intersects that audience with its own current
membership and channel ACL, rejects a removed actor or an empty intersection,
and publishes only ephemeral per-user Gateway events. Typing never enters the
SQL inbox/outbox or a resumable Redis stream; stale, duplicate, delayed, and
out-of-order generations are discarded. Token webhooks have no typing operation.

Presence is not capability-negotiated. A user's home sends a best-effort signed
`POST /_kaede/v1/presence` projection to every instance hosting an accepted
friend of that user, and to every instance that shares a guild with the user,
either as the guild home or as another member's home. A guild-only peer that the
sender has locally silenced is skipped; a friend's instance is still reached.
Delivery uses a three-second timeout in batches of eight destinations and never
enters the durable outbox.

The body contains exactly `user_id`, `user_domain`, `status` (`online`, `idle`,
`dnd`, or `offline`), at most 16 normalized `activities`, optional `since`,
`afk`, `observed_at` (Unix microseconds), and `expires_at` (Unix seconds). The
subject domain must equal the signing origin, and `expires_at` must lie in the
future and no more than 95 seconds ahead. The sender sets it 90 seconds ahead,
so the projection expires unless refreshed. `observed_at` must be within 60
seconds of the receiver's clock and acts as a monotonic generation: an older
generation never replaces a newer one. The receiver requires the subject to share
an available guild with a local member, or to be an accepted friend of a local
user. It then publishes the state to the subscribers of those shared guilds and
to each such local friend. While the sender is locally silenced, only the
friend audience is used. A stale, unknown, or audience-less projection returns
HTTP 409 `KAED_PRESENCE_STALE_OR_UNKNOWN`. Media bytes are fetched on demand;
only deletion is durable.
Future durable names require an explicit registry revision.

### Message pins

Pin state has one room authority and mirrors Discord's current contract. A
channel may contain at most 250 pins. `GET /channels/{channel}/messages/pins`
uses an aware ISO-8601 `before` cursor and a `limit` from 1 through 50 (default
50), returning newest-first `{items: [{pinned_at, message}], has_more}` pages.
The deprecated `/channels/{channel}/pins` projection remains a first-page
compatibility view. Guild mutations require `VIEW_CHANNEL`,
`READ_MESSAGE_HISTORY`, and `PIN_MESSAGES`; any current DM participant may
manage the shared DM pin set. Ordinary messages, replies, application-command
results, and context-command responses are pinnable; system messages are not.

A successful new pin writes audit action 74 for guilds, then creates a
contentless type-6 `CHANNEL_PINNED_MESSAGE` referencing the source message.
Unpin writes action 75 and creates no system message. Both mutations publish
`CHANNEL_PINS_UPDATE` with `channel_id`, optional `guild_id`, and the current
`last_pin_timestamp`; Kaede's qualified message/domain fields are additive
extensions. Deleting a pinned message removes its pin without emitting that
event, matching Discord's Gateway behavior. Remote reads and writes are sent
directly to the guild or DM authority, which rechecks current membership and
permissions. Replicas accept only authority-attested pin notices and bounded,
cursor-consistent pin pages, so bots, websites, and apps see the same result on
every participant home.

## 4. Durable delivery

`POST /_kaede/v1/inbox` accepts at most 100 events and 1 MiB. It returns a result
per event (`accepted`, `duplicate`, `rejected`, or `retry`) rather than failing an
otherwise valid batch. `(origin,event_id)` is the idempotency key.

Each destination outbox is a strict total-order stream. A retry whose backoff has
not elapsed is a head-of-line barrier: later due events cannot overtake it. An
inbox also stops before the unprocessed suffix after returning `retry` for an
earlier event. The sender retries that omitted suffix behind the same barrier.
This ordering is what makes unsequenced authority-owned DM edit/delete,
reaction, poll, and pin deltas converge under outages without retaining
plaintext message content at any intermediary.

Receivers also bound cumulative retained protocol state. The default admission
budgets are 5,000,000 inbox/idempotency rows and 16 GiB of accepted canonical event
envelopes per origin, with global limits of 50,000,000 rows and 160 GiB. Operators may
change those four row/byte limits, provided each global limit remains at least its
per-origin limit. The corresponding settings are
`KAEDE_FEDERATION_INBOX_MAX_EVENTS_PER_ORIGIN`,
`KAEDE_FEDERATION_INBOX_MAX_BYTES_PER_ORIGIN`,
`KAEDE_FEDERATION_INBOX_MAX_EVENTS_TOTAL`, and
`KAEDE_FEDERATION_INBOX_MAX_BYTES_TOTAL`. Admission locks the singleton global
ledger before the applicable origin ledger, making both ceilings exact across API
replicas. When
any budget is full, the receiver returns per-event `retry` with
`KAED_FED_INBOX_QUOTA_EXCEEDED` and stores no idempotency claim for that attempt;
the sender therefore retries the identical signed event later. The daily retention
sweep deletes expired records and reconciles persisted per-origin counters from the
retained rows, repairing drift after an interrupted migration or operator
maintenance.

The sender stores an envelope once and queues slim destination pointers. Drainers
coalesce per destination. Retry delays are 5s, 30s, 2m, 10m, 30m, then 1h, with
jitter. After 24h a circuit opens and performs hourly key probes. Destination
queues are capped at 50,000 events or seven days; tombstones force gap fill. DMs
are never discarded within seven days.

The following refines those rules:

- Each delay is multiplied by a uniformly random factor from 0.85 through 1.15. A
  `429` with a positive `Retry-After` replaces the next delay, capped at one
  hour. A failed batch also marks that destination's guild replicas unavailable.
- A pointer older than 24 hours moves to the `circuit` state and is retried
  hourly. Before each circuit attempt the sender forces a fresh discovery and
  key fetch for the destination.
- A drain batch is bounded to 1 MiB of canonical `{"events":[…]}` JSON. A single
  envelope that cannot fit alone fails locally with `KAED_FED_EVENT_TOO_LARGE`.
- The 50,000 cap counts `pending`, `retry`, and `circuit` pointers. Enqueueing
  past it fails with `FEDERATION_OUTBOX_CAPACITY_EXCEEDED` locally, or
  `KAED_FED_OUTBOX_CAPACITY_EXCEEDED` when the enqueue happens inside a federation
  request. The queue depth is never disclosed to peers. The security-critical
  events `guild.access.revoked`, `guild.instance_access.revoked`,
  `guild.leave.request`, `guild.media.delete.request`, `guild.resync.required`,
  `media.delete`, `relationship.remove`, the latest-state projections, and exact
  terminal-room deletions bypass the cap.
- The seven-day expiry marks a pointer `expired` with `KAED_FED_DELIVERY_EXPIRED`
  and, for a guild event, queues a `guild.resync.required` marker for that peer.
  It never applies to the latest-state projections, `media.delete`,
  `guild.media.delete.request`, a `guild.instance_access.revoked` whose reason is
  `guild_deleted`, a deleted-group `dm.group.state`, or a pointer held by a local
  block.
- A local block holds matching pointers in `circuit` indefinitely instead of
  sending them, except `media.delete`, `guild.media.delete.request`, and exact
  terminal-room deletions, which are always delivered. A silence holds only
  guild-scoped events; a suspend holds everything else.
- The latest-state projections, `media.delete`, terminal-room deletions, and
  `guild.media.delete.request` remain retryable after a peer rejects them, so
  that they converge across rolling upgrades. During the first 24 hours a generic
  `KAED_FED_EVENT_REJECTED` for `dm.group.state`, `dm.group.message.proposed`,
  `dm.group.message.committed`, or `dm.group.call.create` is also retried.

`GET /_kaede/v1/link` upgrades with subprotocol `kaede-fed.1` using a signed GET
with an empty body. A successful connection starts with a `hello` frame declaring
version, batch, frame, and heartbeat limits. The sender transmits
`{"op":"events","id":"…","events":[…]}` and receives a correlated
`{"op":"results","id":"…","results":[…]}`. Frames are at most 1 MiB,
batches contain one through 100 events, and each origin may hold at most four
inbound leases. Connections heartbeat, recheck block/rate policy for every batch,
and reconnect within 55 minutes for fresh request authentication. Any connection,
protocol, or timeout failure discards the pooled link and falls back to the signed
HTTP inbox without weakening durable ordering or idempotency.

The receiver's `hello` frame has exactly these members:

```json
{"op":"hello","version":"1","max_batch":100,"max_frame_bytes":1048576,"heartbeat_interval_ms":30000}
```

The sender may send `{"op":"ping"}`; the receiver answers
`{"op":"pong","ts":<Unix milliseconds>}`. An `events` frame's `id` is a string of
1 through 64 characters and is echoed in the matching `results` frame. A
structurally invalid envelope receives a per-event `KAED_FED_INVALID_EVENT`
result. Processing stops after the first `retry` result, including a block-policy
retry. The receiver closes the socket with:

| Close code | Reason |
| --- | --- |
| `1000` | The 55-minute link lifetime elapsed. |
| `4400` | `kaede-fed.1` was not offered, or a frame is not valid JSON or a valid `events` frame. |
| `4401` | Upgrade authentication failed. |
| `4409` | A frame exceeds 1 MiB. |
| `4429` | The origin already holds four links, or a frame or byte budget is exhausted. |

Each connection allows a burst of 30 frames refilled at 10 per second, and 2 MiB
refilled at 512 KiB per second. Across all of an origin's links the receiver also
allows 120 frames refilled at 50 per second and 8 MiB refilled at about 2 MiB per
second. Every event delivered on either transport is charged to the origin's
event bucket.

Every S2S route applies a per-origin token bucket. Invite resolution and guild
join also have independent 30/minute and 10/minute origin buckets. `429` includes a retry delay.
Inbox depth, repeated signature failure, and sustained throttling are observable.

All buckets are token buckets keyed by the authenticated origin unless noted.
The shared buckets are:

| Bucket | Capacity | Refill | Scope |
| --- | --- | --- | --- |
| Pre-authentication | 60 | 60 per second | Source IP, before signature verification |
| Unknown-key refresh | 5 and 2 | 5 and 2 per minute | Source IP and claimed origin; failure is `KAED_FED_KEY_REFRESH_RATE_LIMITED` with a 30-second retry |
| Request | 100 | 100 per second | Every authenticated request, charged before the nonce is stored |
| Event | 200 | 200 per second | Each inbox or hot-link event, valid or not |

Exhausting the request, event, or pre-authentication bucket returns
`KAED_RATE_LIMITED` with `retry_after_ms: 1000` and `Retry-After: 1`. Most routes then
charge an additional route bucket. Exhausting one returns `KAED_RATE_LIMITED`
with `retry_after_ms: 60000` and `Retry-After: 60`. Unless two figures are given,
capacity equals the per-minute refill:

| Route bucket | Per minute | Route |
| --- | --- | --- |
| `user-lookup`, `profile-by-ref` | 120 each | `GET /users/lookup`, `GET /users/profile` |
| `dm-open`, `dm-authorize` | 120 each | `POST /dm/open`, `POST /dm/authorize` |
| `dm-group-authorize`, `dm-group-mutate` | 120, 180 | `POST /dm/groups/authorize`, `POST /dm/groups/mutate` |
| `dm-history-page`, `dm-history-media-authorize` | 3,000 each | `GET /dms/{id}/messages`, `GET /media/{a}/{v}/authorize` |
| `dm-message-operation`, `dm-forward-resolve` | 3,000, 1,200 | DM message operations and forward resolution |
| `media-get` | 1,200 | `GET /media/{attachment}/{variant}` |
| `presence` | 300 | `POST /presence` |
| `typing-publish`, `typing-relay` | 600, 1,200 | Typing routes |
| `invite-resolve`, `guild-join`, `guild-leave` | 30, 10, 20 | Invite resolution, join, and leave |
| `guild-snapshot-start`, `guild-snapshot-page` | 10, 300 | Snapshot without and with a continuation cursor |
| `guild-events`, `guild-tracker-snapshot` | 300 each | Gap fill and tracker snapshots |
| `member-self-moderation` | 1,200 | Timeout status lookup |
| `guild-history-create`, `-manifest`, `-page`, `-delta`, `-complete` | 10, 60, 300, 120, 30 | History export routes |
| `guild-message-create`, `guild-message-operation`, `guild-reaction-mutation` | 3,000 each | Proxy writes, message operations, and reactions |
| `guild-pin-mutation`, `channel-pins-page` | 600 each | Pin mutation and pin pages |
| `guild-poll-vote`, `guild-poll-finalize`, `guild-poll-voters` | 300, 120, 600 | Poll proxy routes |
| `guild-forward-resolve`, `forward-source-authorize` | 600 each | Forwarding authorization |
| `thread-mutation` | 600 | `POST /guilds/{id}/proxy-thread` |
| `guild-management`, `application-management` | 240 each | Management RPCs |
| `developer-management` | 120 | Developer-team management RPC |
| `guild-audit-log` | 120 | Audit-log pages |
| `message-search` | 600 | Federated search |
| `e2ee-key-package-claim` | 1,000 | Key-package claims |
| `e2ee-room-proxy` | 120 | Room propose, activate, rekey, and status |
| `e2ee-consent:<actor>` | 60 | DM encryption consent, keyed per origin and actor |
| `bot-e2ee-device-snapshot`, `dm-bot-e2ee-management` | 240, 120 | Bot E2EE routes |
| `expression-use-authorize` | 1,200 | Expression authorization |
| `announcement-follow-*` | 120 (`-list` 300) | Announcement-follow routes |
| `announcement-crosspost-deliver`, `announcement-crosspost-resolve` | 600, 1,200 | Crosspost routes |
| `application-directory-search`, `-bot-profile`, `-detail` | 120, 240, 240 | Application directory |
| `bot-manifest`, `bot-runtime-manifest`, `bot-worker-authorization` | 300, 300, 600 | Bot authority routes |
| `bot-install`, `bot-uninstall` | capacity 60, refill 30 | Bot install and uninstall |
| `bot-installation-list`, `bot-restrictions`, `bot-guild-command-put` | capacity 120, refill 60 | Installation list, channel restrictions, guild command upload |
| `bot-command-list`, `bot-command-create` | 600, 300 | Command list and interaction creation |
| `bot-command-permission-list`, `-get`, `-put` | capacity 120/240/60, refill 60/120/30 | Command permission routes |
| `interaction-input-media`, `interaction-private-media` | capacity 240, refill 120 | Interaction attachment routes |
| `interaction-private-poll`, `interaction-private-poll-list` | capacity 180, refill 90 | Private interaction poll routes |
| `bot-dm-capability-validate`, `-apply`, `-attest` | 240, 240, 120 | Bot DM capability routes |
| `voice-token`, `dm-voice-token` | 60 each | Voice token brokers |
| `voice-state`, `dm-voice-state`, `voice-self-state`, `dm-voice-self-state` | 180 each | Occupancy and self-state |
| `voice-move` | 30 | `POST /voice/move` |
| `rtc-probes` | 60 | `POST /voice/rtc` |
| `dm-call`, `dm-call-state` | 120 each | Call signaling and call state |
| `guild-soundboard-query`, `-source-capability`, `-play` | 120, 120, 60 | Soundboard authority routes |
| `voice-soundboard-effect` | 180 | Soundboard effect delivery |
| `abuse-reports` | capacity 30, refill 5 | `POST /reports` |
| `abuse-report-evidence` | capacity 20, refill 2 | Report evidence routes |

## 5. Guild authority and synchronization

The guild origin is its sole ordering authority. For every registered mutation it
allocates canonical snowflakes where applicable and a
monotonically increasing per-guild `seq` in the same transaction as the mutation
and its `guild_events` record.

A remote join resolves a signed invite, obtains a join grant, then downloads:

1. guild metadata, roles, channels, and custom emoji metadata;
2. members in pages of at most 1,000;
3. permission overwrites and membership/role assignments needed by the replica.

The snapshot carries `snapshot_seq`; live application begins after it. Member pages
use a `(origin_domain,id)` keyset cursor plus an initial membership-time watermark,
so concurrent joins cannot shift an offset and duplicate or skip already-paged
members. New peers also return and echo `snapshot_generation`, a structural,
membership, and permission watermark. Ordinary message/reaction/pin events may
advance the live sequence during a large snapshot without invalidating its pages;
the requester gap-fills those events after applying the original `snapshot_seq`.
A changed generation returns `KAED_FED_SNAPSHOT_CHANGED` and the requester restarts.
For rolling compatibility, continuations that omit `snapshot_generation` retain
the earlier strict `snapshot_seq` behavior.

Snapshot visibility is bulk-evaluated from bounded role, membership, assignment,
and overwrite sets, cached by permission/structural generation, and protected by
per-origin/guild concurrency and work admission. A gap schedules bounded background
recovery and calls
`GET /_kaede/v1/guilds/{id}/events?after_seq=<n>`. Gaps older than
retained events return HTTP 410 `KAED_FED_FULL_RESYNC` with
`snapshot_required=true` and the current `latest_seq`. Gap fill enforces aggregate
page, event, byte, duration, and cursor-progress budgets. A signed event that fails
semantic application quarantines the incremental stream and triggers a fresh
signed snapshot instead of being retried forever.

A guild home may relay a third-party member profile only by preserving the exact
inner `guild.member.profile` envelope signed by that user's home inside a
sequenced `guild.member.profile.relay`. Every receiver verifies both the outer
guild-authority signature and the inner user-home signature, requires the subject
to remain a current guild member, and applies profile versions monotonically. If
the user's home is also the guild home, that exact home-signed source is delivered
directly to each current remote member home; its monotonic profile version makes
guild sequencing unnecessary and avoids locking every guild during a single user
profile edit. Receivers recheck that both the subject and at least one local user
remain members before accepting either form. A local user is always rendered from
local storage, so replaying an older valid source cannot regress their profile.
The guild authority also emits a full `GUILD_MEMBER_UPDATE` locally. A guild
snapshot remains the recovery path after a sequenced relay gap.

Unsequenced profile-shaped member data remains authoritative only for users homed
on the sending instance. For an unknown third-party user, the replica retains
only the exact composite reference and a non-authoritative placeholder; a guild
authority cannot create or mutate that third party's profile. A peer advertising `profile-by-ref/1`
may then request `GET /users/profile?user_id=…&user_domain=…` directly from that
user's home. The home returns a signed `user.profile` envelope whose actor,
subject, and embedded profile must all match the requested composite reference.
The replica verifies the home signature and profile version before upgrading the
placeholder. Resolution is bounded, deduplicated, retried in the background, and
never blocks snapshot application. Peers missing the capability keep the generic
identity for rolling compatibility; their discovery document is rechecked slowly
so upgrading later converges without user action.

Guild, channel, role, emoji, overwrite, member, moderation, message mutation,
reaction, and pin changes are granular sequenced events. Permission-sensitive changes carry `snapshot_required`; a
replica fetches a current permission-filtered snapshot before accepting the retried
event. Peers without channel visibility receive a signed sequence-only redaction.

Plaintext message content uses origin-qualified tokens for mutable display
objects. A user mention is `<@user_id@user_origin>`, a role mention is
`<@&role_id@guild_origin>`, and a custom emoji is
`<:name:emoji_id@emoji_origin>` (`<a:...>` when animated). Unicode emoji remain
ordinary Unicode text. The receiving client resolves names and media from its
replicated guild state, so renames do not change the referenced identity. For a
role mention, only the guild authority may expand the token into
`mention_user_refs`; it validates that the role belongs to the guild, is
mentionable (or that the actor has the override permission), and resolves current
assignments. Replicas may forward explicit user mentions but cannot assert role
recipients. Committed message events carry the authority-derived user references,
which keeps notification fanout deterministic across instances.

An ownership transfer is represented by an authority-signed `guild.update` and
may name an active human member from any canonical user home. The transfer event
is authorized against the pre-update owner; snapshots require the resulting
owner to remain in the signed member collection. When the current owner is
remote, the guild home may authority-attest that exact owner as the envelope
actor for the closed guild-control event family. The signed content retains the
semantic remote member or message author, and receivers independently validate
that identity and current membership; the guild home never signs an envelope as
an arbitrary remote member. A remote member leaves through its home instance,
which durably delivers a signed `guild.leave.request`; the direct
`DELETE /_kaede/v1/guilds/{id}/members/@me` federation route remains the
equivalent request/response form. In either form, the user domain must match the
signing origin. The guild home removes membership before emitting the sequenced
member removal and target-specific access revocation. Guild owners cannot leave
through this operation.

The member's home instance revokes local access and durably records the composite
`(guild_id,guild_origin,user_id,user_origin)` departure before delivering the
leave request. A delayed authority-signed member-add event or snapshot is still
consumed for sequence convergence, but cannot restore that local membership.
Only a new explicit local invite/join flow may transition the record to pending;
the record is removed only after an authoritative snapshot containing that member
has applied successfully. The departure record survives deletion of
the cached guild replica.

Kicks and user bans additionally send a direct target-specific access revocation.
An instance ban sends a direct origin-wide revocation even after every member from
that origin has been removed from the authoritative membership set. It also emits
the sequenced `guild.members.origin.remove` mutation to remaining replicas.

User bans and federated-instance bans may be permanent or carry an absolute expiry.
Join authorization always evaluates expiry directly; it does not depend on the
background cleanup task running on time. Member timeouts may likewise be finite or
explicitly indefinite. These sanctions are owned and evaluated by the guild home.

Remote writes include `(origin,client_nonce)`. The remote instance performs an
advisory check and proxies to the home for an authoritative check. A response
within 10 seconds is relayed. Unreachable writes return `202 {"status":"queued"}`.
Later rejection emits `message.send_rejected` with channel, nonce, and stable code. A
`MEMBER_TIMED_OUT` rejection also carries the timeout expiry or indefinite marker and the
user-visible moderation reason so the sender's home instance can explain the denial.
No server-side optimistic echo is generated.

Timeout reasons are private to the affected member. They MUST NOT appear in guild
snapshots, ordinary member-list responses, member chunks, or guild-topic dispatches.
The guild home advertises `member-self-moderation/1` when it supports the scoped
lookup below:

```text
GET /_kaede/v1/guilds/{guild_id}/members/{user_id}/moderation-status
```

The request uses normal signed federation authentication and a fixed empty body.
The guild home treats `{user_id}@{signing_origin}` as the requested identity; the
caller cannot supply a different user domain. It returns the state only when that
exact identity is a current guild member. The bounded response is:

```json
{
  "guild_id": "123",
  "guild_domain": "guild.example",
  "timed_out": true,
  "timeout_until": "2026-08-13T12:00:00Z",
  "timeout_indefinite": false,
  "reason": "User-visible moderation reason",
  "details_available": true
}
```

An active timeout has exactly one duration mode. An inactive response has null
expiry and reason and a false indefinite marker. The user home exposes this only
through its authenticated `members/@me/moderation-status` API and MUST NOT persist
the reason in its guild replica or publish it to guild subscribers. It may fetch
the response on demand after an ordinary member-state invalidation. Existing
replicas continue to provide the non-private timeout timing during rolling
upgrades. When the capability is absent or temporarily unreachable, clients show
that timing with an unavailable reason and `details_available: false`;
authoritative enforcement remains at the guild home. A user home may perform
deduplicated domain-only capability rediscovery, and clients automatically retry
that private lookup without a user click.

Remote human audit-log reads use the following private extension:

```text
POST /_kaede/v1/guilds/{guild_id}/audit-logs
```

The signed request binds the canonical guild, requester, requesting instance,
exact normalized filters and cursor, a random one-use request ID, issue time, and
a deadline no more than 15 seconds later. The guild home verifies that binding,
consumes the request ID once, and rechecks live membership and
`VIEW_AUDIT_LOG`; replica permissions are never authoritative. The home returns
a bounded `guild.audit-log.page` envelope that echoes the request. The caller
verifies the home signature, authority, freshness, request and requester
bindings, schema, filters, cursors, ordering, and response size before exposing
the page. Audit pages MUST NOT be persisted in a shared cache or redistributed.

When home is unavailable, replicas stay mounted and readable and emit availability
and peer-status updates. Writes remain visibly pending. Unavailability never means
guild deletion.

### 5.1 Permission-bound retained-history transfer

Historical export is disabled by default. The guild stores a default policy of
`disabled` or `full_retained`; every channel stores `inherit`, `disabled`, or
`full_retained`. The channel override wins, and `inherit` resolves to the guild
default, not to a parent channel. Policy is only one gate: the requesting member
MUST currently have both `VIEW_CHANNEL` and the existing `READ_MESSAGE_HISTORY`
permission in that channel. Administrator expansion follows the ordinary
permission algorithm.

Only message-capable guild channels are eligible: text (type 0), the embedded
text chat of a voice channel (2), announcement (5), announcement, public, and
private threads (10, 11, 12), and the embedded text chat of a Stage channel (13).
Each thread is evaluated as its own channel. Categories, forum and media parents,
other channel types, unavailable channels, and omitted channels are never
eligible.

A replica advertising and observing `guild-history-sync/1` requests an export for
one of its local guild members after join and again when replicated policy,
membership version, roles, or permission generation later make new history
eligible. The home issues a short-lived grant bound to:

- the authenticated requesting instance and its exact user identity;
- guild identity, member version, permission generation, and history-policy generation;
- a guild sequence baseline; and
- an immutable per-channel high-water message ID.

Every manifest and page request rechecks the grant and current authorization.
Changing any bound generation or membership invalidates the grant. Pages are
cursor-bound and limited by both peers' configured page, byte, duration, reaction,
database-work, and total-message caps. Peers that advertise
`guild-history-sync/2` send each channel newest-to-oldest, so recent conversation
becomes available before deep history. The response declares `order=recent_first`
and advances a strictly decreasing `next_before` cursor equal to the final message
ID minus one. The `before` query parameter is an inclusive upper bound. A cursor that is unchanged, increasing, or inconsistent with the final
message makes the receiver reject the transfer. Version 1 remains oldest-first for
compatibility. Pages contain only non-deleted messages at or below the channel
high-water mark, stable attachment metadata, reactions, pins, and bounded author
profiles. The guild home remains the message authority; payload-provided URLs are
still forbidden.

Each exported message carries, in addition to the rendered message:

- `author` and `history_author`, both a strict remote-user profile of the author;
- `reactions`, a list of `{user_id, user_domain, emoji, created_at}`;
- `poll_votes`, a list of `{answer_id, user_id, user_domain, created_at}`. Receivers
  reject more than 10,000 votes, duplicate votes, or votes on a message without a
  poll; and
- `pin`, either `null` or `{pinned_by_id, pinned_by_domain, pinned_at}`.

By default a page holds at most 100 messages and 512 KiB of canonical message
JSON. A single message larger than the page byte limit fails with HTTP 413
`KAED_FED_HISTORY_MESSAGE_TOO_LARGE`. The delta route returns only these
retained mutation types: `guild.message.update`, `guild.message.delete`,
`guild.message.bulk_delete`, `guild.message.purge`, `guild.reaction.add`,
`guild.reaction.remove`, `guild.reaction.clear`, `guild.poll.vote.add`,
`guild.poll.vote.remove`, `guild.poll.finalize`, `guild.pin.add`, and
`guild.pin.remove`. It returns at most 500 events per page and is bounded to
1,000 requests per import. An export grant lives for 24 hours by default. An
export that is no longer active or completed returns HTTP 409
`KAED_FED_HISTORY_UNAVAILABLE`.

The receiver durably records one import per `(guild,user,grant generation)`, with a
leased worker and a per-channel cursor, and stages pages without exposing them to
clients. It never holds a database lock across a peer request. It then reads
sequenced edits, deletions, purges, reaction changes, and pin changes after the
baseline. Under a replica guild lock it performs a final delta read and merges the
staged set in bounded database chunks. Ordinary live events are still authoritative and safely
converge if they arrived before or after the merge. Transfers resume from staged
cursors after transient failure. Local completion is durable independently of the
idempotent acknowledgement to the home, so acknowledgement outages do not repeat
the import.

An authority MUST bound active export objects and their per-channel grant rows
both per requesting origin and globally. Admission is serialized before rows are
created; exhausting either temporary budget returns HTTP 429
`KAED_FED_HISTORY_CAPACITY` with retry guidance. Expired grants no longer consume
admission capacity and are removed by retention. This prevents a peer with many
otherwise valid joined users from multiplying short-lived grants by the guild's
channel count.

Replicas tag messages whose initial copy came from an export. On replicated policy,
role, overwrite, or membership revocation, a cooperative replica re-evaluates all
local members and deletes imported-only messages for channels no local member may
still read. A periodic reconciliation sweep repairs missed live notifications.
For ordinary live cache as well as imported history, a permission-filtered snapshot
tombstones a channel and deletes its local messages once no local member on that
replica may view it. Losing the final local guild membership, or receiving an
origin-wide access revocation, applies the same purge to every replicated channel.
Attachment cache entries are expired in the same transaction and their object bytes
are removed asynchronously by retryable media garbage collection.
This purge is best effort: federation cannot force a malicious,
modified, backed-up, or offline remote server to delete data it already received.
Administrative clients MUST show that warning before enabling export. Local API
authorization continues to apply independently of cached history.

## 6. Direct messages

Relationship state is stored independently at each user's home. Cross-instance
request, acceptance, and removal events synchronize those local projections, but
the local user's row is authoritative for local privacy. Removing or blocking a
remote user takes effect before delivery is attempted and therefore cannot be
vetoed by remote code. Acceptances carry an unguessable request ID and are applied
only to the matching current `pending_out` row; stale events are harmless.
Blocking sends only `relationship.remove`, preserves the local block on inbound
removal, and does not disclose block state to the peer.

The conversation key is SHA-256 of the two normalized handles sorted
lexicographically and joined with LF. The lexicographically lower participant
domain alone mints the conversation snowflake. `POST /_kaede/v1/dm/open` converges
concurrent opens on that key. Same-instance pairs mint locally.

DM events are stored on both participants' instances. The sending instance persists
outbox state and publishes pending/delivered/failed gateway updates; message history
reconstructs that sender-side state from the retained event/outbox records after reconnect. The recipient
instance enforces `everyone`, `shared_guild`, or `friends` privacy during ingest. Accepted
friends are allowed by every mode; `shared_guild` additionally permits users who share an
accessible guild, while `friends` permits no other users.
Cross-instance read receipts are not part of v1. Group conversations are not
capability-negotiated; their rules are in section 1 and the `dm.group.*` rows of
the durable event registry.

### 6.1 Opaque encrypted-message transport

An instance can store and relay an `e2ee` object in
place of plaintext message content across local message APIs, gateway events,
guild and DM durable events, proxy commits, and retained-history transfer. The
object requires an integer `version` from 1 through 2,147,483,647 and is limited to
64 KiB, 16 nested levels, 4,096 JSON values, 256 members per object, 1,024 items per
array, and 256 UTF-8 bytes per object key. The transport treats all remaining
members as opaque and never selects or validates a cipher suite. Plaintext and an
`e2ee` object are mutually exclusive. Activating an encrypted room requires
`e2ee-mls/1` in the current discovery document of every remote participant's
instance. The activating home checks this both when it proposes and when it
commits the activation, and fails with HTTP 409 `E2EE_INSTANCE_UNSUPPORTED`, or
`E2EE_INSTANCE_UNAVAILABLE` when discovery fails, naming the affected domain.

This transport alone is plumbing, not proof of end-to-end encryption. Kaede's
`e2ee-mls/1` extension adds device credentials, one-use key packages, MLS groups
and welcomes, recovery UX, and encrypted attachments. The informational
`e2ee-media/1` capability advertises encrypted-attachment support. The
Ed25519 signatures in sections 2 and 3 authenticate one instance to another;
they do not prove which participant device created a ciphertext.

The version-2 MLS extension defines a canonical authenticated-data encoding
shared by web, mobile, desktop, and the Python SDK. It binds:

- the encryption protocol and cipher-suite versions;
- the composite guild/channel or DM reference (numeric ID and origin domain);
- the authenticated sender device (the MLS credential binds the user account);
- an authenticated room-policy generation and cryptographic epoch;
- the create/edit operation, monotonic message revision, and exact edit target;
- the author, application/installation lineage, reply reference, delivery
  flags, and resolved mention-routing projection;
- authenticated attachment manifests, sticker refs, and custom-emoji refs;
- sanitized component and opaque poll routing contracts; and
- canonical rich-body and author-free forward-projection digests.

The encrypted body has plaintext-message feature parity where cryptography
permits it: content, embeds, Components V1/V2, polls, stickers, TTS and voice
markers, flags, and file manifests. Federation carries the exact opaque MLS
envelope and the authenticated public routing projection; it MUST NOT project
questions, poll labels, component display text, embed text, filenames, or other
decrypted rich data. Peers preserve application attribution, reply references,
view/poll revisions, attachment references, and opaque vote counts exactly.

The extension also enforces downgrade protection. Enabling encryption cannot be
an unauthenticated boolean: it must advance an authenticated, monotonic room-policy
generation, and membership or trusted-device changes must advance the cryptographic
epoch. Clients must reject plaintext, stale policies, and stale epochs after
encryption is enabled. Authorities and replicas must reject writes that conflict
with the current policy. The mere presence or absence of `e2ee`, or a peer's
capability advertisement by itself, is not a secure negotiation mechanism.

Encrypted bodies cannot participate in server-side full-text search, link
previews, or plaintext moderation. Mention delivery uses a consented,
authenticated routing projection: clients commit visible user/role/everyone
intent and allowed-mention policy, the authority rechecks permission and
mentionability and binds the exact resolved recipients, and recipients compare
that projection with decrypted tokens before rendering. Notification previews
remain generic. Routing still exposes participant instances, composite room and
sender references, timestamps, ciphertext sizes, approved expression/attachment
references, poll counts, and delivery activity; padding and traffic-analysis
resistance are not provided by this transport.

Client-mediated forwarding uses the source-authority event
`message.forward.source.authorized`. Its signed content is requester,
application/device, source message/channel, destination channel/mode, nonce,
expiry, age context, source type/flags/timestamps, attachment refs, and the
rich-v2 forward-projection digest. The proof additionally binds the sorted
canonical sticker items and custom-emoji routing tokens approved at the source;
the destination rechecks external-expression permissions without receiving any
encrypted text. It contains an authoritative author-free
snapshot only for a plaintext source and never decrypted E2EE content. The
destination authority verifies the source signature and exact bindings before
admission. A client that is an MLS participant in both encrypted rooms decrypts
locally and re-encrypts the depth-one snapshot and each attachment for the
destination; mixed plaintext/E2EE directions use the same proof and require an
explicit disclosure for E2EE-to-plaintext. Attachment semantics include the
authenticated plaintext SHA-256 while all destination transport bindings are
fresh. A delayed queued guild/DM proposal is accepted only when its signed
outer event was issued before proof expiry. Polls, calls, activities, legacy
E2EE envelopes without the v2 commitment, and unauthorized history floors fail
closed.

Announcement follow/crosspost is not treated as forwarding because its
unattended fan-out has no endpoint that can consent to and perform cross-group
MLS re-encryption. E2EE activation/follow configuration is rejected at the
boundary unless a future bridge is explicitly admitted to every group; peers
MUST NOT copy MLS ciphertext or downgrade to plaintext.

For plaintext announcement channels, a cross-authority follow is a durable,
generation-fenced saga. The source guild signs an exact, short-lived
authorization for the source channel, target channel, creator, generation and
stable authorization ID. The target persists a pending type-2 follower webhook
and signs `authorized`; the source persists `accepted`; the target alone
creates the deletable type-12 notice and emits `finalized`; only then do both
sides expose the follow as active. Rejection, expiry and revocation are retained
as tombstones, and every transition is safe to retry through the federation
outbox. A new follow after revocation must use the next generation.

When the authenticated actor home, source authority and target authority are
three different instances, the actor home supplies one receiver-keyed nested
intent per resource authority. Human intents are actor-home instance-signed;
bot intents are worker-signed and bind the exact live worker generation plus
runtime manifest, revocation and access-revocation revisions. Both forms bind
the action, audience, qualified actor and exact resource map and consume a
short-lived replay nonce. An outer instance signature never substitutes for
the actor-home proof.

The `bot-direct-auth/1` install and runtime manifest contract requires the
owning developer team's exact `team_id` and `team_domain`; a peer rejects a
manifest that omits either field. The team domain must equal the application
authority, and an existing application must retain the same team and bot
identities across refreshes. This makes install-first and
developer-snapshot-first discovery converge on the same rows instead of
inventing an order-dependent team ID.

Application, command, template, worker, and emoji projections are
generation-idempotent. A canonically equivalent replay at the stored
generation is a no-op; a different projection at that generation is rejected
as equivocation, and a lower generation is rejected as rollback. Only a
strictly greater governing generation may replace a projection. A missing
child is treated as an authoritative deletion only while applying such a
strict parent-generation advance, never during an equal-generation replay.

Publishing commits only a source receipt and one asynchronous delivery job per
follower, so an unavailable target cannot delay healthy followers or the
source response. Delivery is idempotent by follow generation and source
message. Later edits and source deletion are queued for every delivered copy,
including retained copies whose follow was subsequently revoked. Destination
copies keep their target-owned follower name/avatar attribution. Interactive
components are rebound only to an exact active installation of the same
qualified application in the target guild; without one, link/display content
remains visible but dispatchable component lineage is removed.

## 7. Federation routes

Except for discovery at `/.well-known/kaede/server`, protocol routes are under
`/_kaede/v1`:

Guild-management results echo the exact request ID, closed operation name, and
qualified guild identity from the signed request. Callers reject a mismatch in
any of those fields before using the body. A closed per-operation result
contract additionally binds top-level guild, channel, and resource identities
where the public response shape carries them; nested creator, actor, member,
and subscriber identities may remain legitimately foreign.

| Method and path | Purpose | Availability |
| --- | --- | --- |
| `GET /keys` | Current and historical verification keys | v1 |
| `POST /inbox` | Batched durable event delivery | v1 |
| `GET /users/lookup?handle=…` | Resolve a local user profile for a remote peer | v1 |
| `GET /users/profile?user_id=…&user_domain=…` | Return a home-signed public profile proof for an exact composite identity | `profile-by-ref/1` |
| `POST /presence` | Expiring presence projection from the user's home | v1 |
| `POST /typing/publish` | Typing projection from the actor's home to the channel authority | v1 |
| `POST /typing/relay` | Authority-selected typing fan-out to destination homes | v1 |
| `POST /dm/authorize` | Ask the non-authority recipient instance to enforce DM privacy | v1 |
| `POST /dm/open` | Authoritative direct-message open | v1 |
| `POST /dm/groups/authorize` | Ask an invitee's home to confirm a friend-authorized group invitation | v1 |
| `POST /dm/groups/mutate` | Send a participant's group mutation to the group authority | v1 |
| `GET /dms/{id}/messages` | Participant-authorized, bounded DM history page | `dm-history-page/1` |
| `POST /dms/{id}/message-operation` | Participant message operation at the DM authority | v1 |
| `POST /dms/{id}/forward-resolve` | Resolve a DM source message for client-mediated forwarding | v1 |
| `POST /invites/resolve` | Resolve a remote guild invite | v1 |
| `POST /guilds/{id}/join` | Consume an invite and obtain the authoritative guild identity | v1 |
| `DELETE /guilds/{id}/members/@me` | Request/response form of a remote member's leave | v1 |
| `GET /guilds/{id}/members/{user}/moderation-status` | Affected-user-only timeout status | `member-self-moderation/1` |
| `GET /guilds/{id}/snapshot` | Paged structural initial/full synchronization | v1 |
| `GET /guilds/{id}/events` | Sequence gap fill for registered guild events | v1 |
| `GET /guilds/{id}/trackers/{channel}/snapshot` | Permission-checked tracker board snapshot after an invalidation | v1 |
| `POST /guilds/{id}/management` | Replay-bounded signed guild-management authority RPC | v1 |
| `POST /applications/{application}/management` | Replay-bounded signed application-management authority RPC | v1 |
| `POST /developer-teams/{team}/management` | Replay-bounded signed developer-team management authority RPC | v1 |
| `POST /guilds/{id}/audit-logs` | Private requester-bound audit-log page with live authority recheck | `guild-audit-log/1` |
| `POST /guilds/{id}/history-exports` | Create or resume a permission-bound history grant | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}` | Read the bound manifest | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}/channels/{channel}?after=…` or `?before=…` | Read one bounded page, oldest-first or recent-first | `guild-history-sync/1`, `guild-history-sync/2` |
| `GET /guilds/{id}/history-exports/{export}/delta?after_seq=…` | Reconcile retained mutations after the baseline | `guild-history-sync/1` |
| `POST /guilds/{id}/history-exports/{export}/complete` | Idempotently acknowledge a merged export | `guild-history-sync/1` |
| `POST /guilds/{id}/proxy` | Idempotent remote guild message write | v1 |
| `POST /guilds/{id}/message-operation` | Remote member message edit or delete at the guild home | v1 |
| `POST /guilds/{id}/proxy-reaction` | Permission-checked remote guild reaction mutation | v1 |
| `POST /guilds/{id}/proxy-poll-vote` | Remote guild poll vote add or remove | v1 |
| `POST /guilds/{id}/proxy-poll-finalize` | Remote guild poll finalization | v1 |
| `POST /guilds/{id}/proxy-poll-voters` | Permission-checked poll voter page | v1 |
| `POST /guilds/{id}/proxy-thread` | Remote thread and forum mutation | v1 |
| `POST /guilds/{id}/proxy-forward-resolve` | Resolve a guild source message for client-mediated forwarding | v1 |
| `POST /guilds/{id}/proxy-pin` | Permission-checked remote guild pin mutation | v1 |
| `POST /guilds/{id}/pins` | Permission-checked authoritative guild pin page | v1 |
| `POST /dms/{id}/pins` | Participant-bound authoritative DM pin page | v1 |
| `POST /channels/{source}/forward-authorize` | Source-authority forwarding proof (`message.forward.source.authorized`) | v1 |
| `POST /channels/{source}/announcement-follow-source-authorize` | Source authority signs a follow authorization | v1 |
| `POST /channels/{target}/announcement-follow-authorize` | Target authority records the pending follow and signs `authorized` | v1 |
| `POST /channels/{source}/announcement-follow-accept` | Source authority accepts an authorized follow | v1 |
| `POST /channels/{source}/announcement-follow-create` | Create a follow on behalf of a remote actor at the source authority | v1 |
| `POST /channels/{source}/announcement-follow-list` | List follows of a source channel | v1 |
| `POST /channels/{target}/announcement-follow-revoke` | Target authority revokes a follow | v1 |
| `POST /channels/{source}/announcement-follow-deactivate` | Source authority deactivates a follow | v1 |
| `POST /channels/{source}/announcement-follow-delete/{follow}` | Delete one follow at the source authority | v1 |
| `POST /channels/{target}/announcement-crossposts` | Deliver one crosspost to a follower target | v1 |
| `POST /channels/{source}/announcement-crossposts/resolve` | Resolve a delivered crosspost's source | v1 |
| `POST /search/messages` | Bounded permission-checked federated message search | `message-search/1` |
| `POST /reports` | Forward a user's message report to the channel authority | v1 |
| `POST /reports/{report}/attachment-evidence` | Create a reporter-disclosed evidence upload ticket for an encrypted attachment | v1 |
| `PUT /reports/{report}/attachment-evidence` | Commit that evidence upload | v1 |
| `POST /expressions/authorize` | Authorize use of an external custom emoji or sticker (`expression.use.authorized`) | v1 |
| `POST /application-directory/search` | Signed, policy-filtered reviewed application search | v1 |
| `POST /application-directory/bot-profile` | Resolve one bot authority's active application and install template | v1 |
| `POST /application-directory/detail` | Read one exact reviewed application product projection | v1 |
| `GET /applications/{application}/manifest` | Signed application install manifest | `bot-direct-auth/1` |
| `GET /applications/{application}/runtime-manifest` | Signed application runtime manifest | `bot-direct-auth/1` |
| `GET /applications/{application}/workers/{worker}/authorization` | Signed worker authorization | `bot-direct-auth/1` |
| `POST /guilds/{id}/bot-install` | Install a bot into a guild at the guild authority | v1 |
| `PATCH /guilds/{id}/bot-install` | Update an installed bot's channel restrictions | v1 |
| `DELETE /guilds/{id}/bot-install` | Uninstall a bot from a guild | v1 |
| `GET /guilds/{id}/bot-installations` | List a guild's bot installations | v1 |
| `GET /guilds/{id}/application-commands` | List the application commands available in a guild | v1 |
| `PUT /applications/{application}/guilds/{id}/commands` | Upload an application's guild commands to the guild authority | v1 |
| `GET /applications/{application}/guilds/{id}/commands/permissions` | List command permissions in a guild | v1 |
| `GET /applications/{application}/guilds/{id}/commands/{command}/permissions` | Read one command's permissions | v1 |
| `PUT /applications/{application}/guilds/{id}/commands/{command}/permissions` | Replace one command's permissions | v1 |
| `POST /channels/{channel}/interactions` | Create an interaction at the channel authority | v1 |
| `GET /interactions/attachments/{attachment}/{variant}` | Stream an interaction input attachment | v1 |
| `GET /interactions/{interaction}/responses/{response}/attachments/{attachment}/{variant}` | Stream a private interaction-response attachment | v1 |
| `GET /interactions/{interaction}/responses/{response}/polls/answers/{answer}` | List voters on a private interaction-response poll answer | v1 |
| `PUT /interactions/{interaction}/responses/{response}/polls/answers/{answer}/@me` | Add the requester's vote on a private interaction-response poll | v1 |
| `DELETE /interactions/{interaction}/responses/{response}/polls/answers/{answer}/@me` | Remove that vote | v1 |
| `POST /bot-dm/capabilities/validate` | Validate a bot DM installation capability | v1 |
| `POST /bot-dm/capabilities/apply` | Apply a refreshed bot DM capability | v1 |
| `POST /bot-dm/capabilities/attest` | Attest a bot DM capability | v1 |
| `POST /bot-e2ee/applications/{application}/devices` | Signed bot E2EE device snapshot (`bot.e2ee.device-snapshot`) | `e2ee-mls/1` |
| `POST /e2ee/dm-bots/manage` | Replay-bounded DM-bot E2EE management | `e2ee-mls/1` |
| `POST /e2ee/key-packages/claim` | Claim one-use MLS key packages | `e2ee-mls/1` |
| `POST /e2ee/rooms/consent` | Record a participant's DM encryption consent | `e2ee-mls/1` |
| `POST /e2ee/rooms/propose` | Propose room activation to the room authority | `e2ee-mls/1` |
| `POST /e2ee/rooms/activate` | Commit room activation | `e2ee-mls/1` |
| `POST /e2ee/rooms/rekey/propose` | Propose a room rekey | `e2ee-mls/1` |
| `POST /e2ee/rooms/rekey/activate` | Commit a room rekey | `e2ee-mls/1` |
| `POST /e2ee/rooms/operations/status` | Read a room operation's status | `e2ee-mls/1` |
| `GET /link` | Signed `kaede-fed.1` hot-link WebSocket upgrade | v1 |
| `POST /voice/token` | Home-SFU guild token broker | v1 |
| `POST /voice/dm-token` | Conversation-authority SFU DM call token broker | v1 |
| `POST /voice/state` | Droppable guild occupancy snapshot/heartbeat | v1 |
| `POST /voice/self-state` | Occupant-home guild self-mute and self-deafen update | v1 |
| `POST /voice/dm-state` | Droppable DM call occupancy snapshot | v1 |
| `POST /voice/dm-self-state` | Occupant-home DM call self-mute and self-deafen update | v1 |
| `POST /voice/move` | Guild-authority voice move of a remote occupant | v1 |
| `POST /voice/rtc` | RTC routing and probe targets from the guild or call authority for a remote human user | v1 |
| `POST /guilds/{id}/soundboard/capability` | Source-authority soundboard media capability | `guild-soundboard/1` |
| `POST /guilds/{id}/soundboard/query` | Bounded soundboard list/get | `guild-soundboard/1` |
| `POST /guilds/{id}/soundboard/play` | Authoritative soundboard playback grant | `guild-soundboard/1` |
| `POST /voice/soundboard-effect` | Occupant-home soundboard effect delivery | `guild-soundboard/1` |
| `POST /calls` | Droppable DM call signaling | v1 |
| `POST /calls/state` | Droppable DM call state replication | v1 |
| `GET /media/{attachment}/{variant}` | Signed remote-media stream | v1 |
| `GET /media/{attachment}/{variant}/authorize` | Confirm that an attachment belongs to an exact DM message without streaming it | `dm-history-page/1` |

The push relay routes (`POST /_kaede/push/v1/wakes` and
`DELETE /_kaede/push/v1/subscriptions/{subscription}`) are outside this prefix
and are not part of kaede-fed/1; [Mobile push](mobile-push.md) describes the
relay.

Application-directory responses use a strict, bounded schema and expose only
approved public metadata, an ordered carousel of at most five owned images or
allowlisted YouTube IDs, at most five named HTTPS links, bounded supported
locales/localized descriptions, popular commands, similar reviewed apps, and an
active install template. Peers never supply arbitrary image or video URLs;
images resolve through the reviewed application-asset contract and clients
construct a canonical YouTube embed from the validated ID. The requester binds
list results to the exact queried origin, filters, selected collection, limit,
stable ordering, and cursor; detail results are additionally bound to the exact
qualified application reference. The application home applies target policy
for the authenticated requesting origin, and an active peer silence or
suspension rule denies both routes.

The bot-profile projection is independently useful for Discord-style **Add
App** from chat and member profiles, so it does not require a public Directory
listing. Its strict response is bound to the exact requested qualified bot,
requires an active bot and application at that bot's authority, and returns
only the same bounded active-template projection used by reviewed listings.
The requester still applies instance policy and rejects cross-origin bot,
application, or template lineage.

A foreign target may retain a signed user-installation grant only as a bounded
authority mirror. The grant carries an aware expiry no more than 20 minutes in
the future; missing, naive, expired, or excessively future expiry fails closed.
All authorization consumers share one effective-installation predicate that
requires either a user-home-owned row or an unexpired foreign lease. Equal
authority revisions may only extend an otherwise identical lease, while lower,
conflicting, or shortened replays cannot restore access. Expiry denies access
immediately, and a minute-bounded sweep revokes E2EE admission and publishes
zero-target snapshots. Application runtime generation remains a separate
authority fence and cannot change the user home's installation revision.
Lease expiry is absolute and anchored by the signed sender request before any
network work: interaction grants cannot outlive the response window plus five
minutes, and E2EE management grants cannot outlive twenty minutes from their
signed issue time. A delayed pre-revocation request therefore never receives a
fresh lease window merely because it arrived later.

On a successful `POST /guilds/{id}/proxy`, the guild home returns the rendered
message, its guild sequence, and the complete signed
`guild.message.committed` envelope. The requester must verify that envelope and
require its guild identity, sequence, and message to match the outer response
before applying the authoritative result. Idempotent nonce replays return the
original stored envelope rather than synthesizing an unsigned result. The
authority binds that receipt to a versioned canonical `proxy_request_fingerprint`
inside the signed event. It covers the immutable message, attachment, mention,
reference, application, and installation semantics, while excluding mutable
actor profile fields and replaceable short-lived authorization proofs. An exact
HTTP or queued replay is therefore resolved before live permission, membership,
mention, proof-expiry, slowmode, or AutoMod checks; a changed proposal using the
same nonce fails closed instead of creating a second message. A queued replay
re-delivers the retained event to the original actor home and does not repeat
message counters, attachment replication, mention work, or gateway fanout.

`POST /guilds/{id}/management` is the internal signed instance-to-instance
authority route used when website and native-app clients act through a user's
home instance. Public bot access tokens and DPoP proofs never traverse this
RPC: bot clients resolve qualified references and call the resource authority
directly. A signed federation request may nevertheless carry a bot actor; that
defense-in-depth path rebinds the actor to an exact active installation before
dispatch. The closed operation set covers guild profile/lifecycle, channels
and overwrites, roles, members, user and instance bans, invites, and the
existing AutoMod, expression, media, webhook, scheduled-event, stage,
soundboard, voice, tracker, and bot-E2EE settings surfaces. The signed request
binds the exact qualified guild and human-or-bot actor to the authenticated
requesting instance, one operation, bounded payload, random one-use request
ID, issue time, and a deadline no more than 15 seconds later. The authority
consumes the request ID once and rechecks live membership, resource ownership,
operation-specific permissions, and—when the actor is a bot—the exact active
installation, scopes, and installed permission ceiling; a user or bot home
replica is never sufficient authorization. Responses echo the request ID,
operation, and qualified guild and are validated against the operation's exact
allowed status and body kind (object, list, or empty) plus applicable resource
identity bindings before a client receives them. Malformed, mismatched,
replayed, expired, or otherwise operation-incompatible responses fail closed.

Payload URLs are forbidden. Media and federation URLs are reconstructed from the
validated origin and fixed paths. Federation HTTP clients reject redirects;
operators must publish the final canonical endpoint directly.

The media path accepts only `original`, `thumbnail_128`, `thumbnail_512`,
`thumbnail_1024`, or `poster`. The authenticated peer receives bytes only when it
has a DM participant on that origin or at least one guild member with visibility
for the attachment's channel. Origins expose only locally owned, clean,
message-bound media.

The media routes accept optional query parameters. `conversation_id`,
`conversation_domain`, `message_id`, and `message_domain` are all present or all
absent. When present, they bind the request to that exact DM conversation and
message, as `dm-history-page/1` requires. `requester_id` and `requester_domain`
are likewise paired, and the domain MUST equal the signing origin. The origin
serves only attachments whose scan state is `clean` or `encrypted` and whose
message is not deleted. It rechecks that state after the first authorization
read. An E2EE attachment has only the `original` variant. A silenced peer is
refused for guild-channel media. Every authorization failure, including an
unknown variant, returns the same non-disclosing HTTP 404
`KAED_MEDIA_NOT_FOUND`. A storage failure returns HTTP 503
`KAED_MEDIA_UNAVAILABLE`. A byte response carries
`Cache-Control: private, max-age=86400, immutable`,
`X-Content-Type-Options: nosniff`, and an exact `Content-Length`. The
`/authorize` form requires the DM scope and returns HTTP 204 with
`X-Kaede-Media-Encryption` and `X-Kaede-Media-Uploader` (`<id>@<domain>`)
headers instead of bytes.

Consumers bound the response by their configured attachment
limit, sniff its bytes, reject active/executable content, scan it through local
ClamAV, and only then write the private remote cache. Cache entries default to a
30-day TTL and a 100 GiB LRU ceiling. Downloads are streamed through bounded spool
files rather than retained as complete byte arrays. Per-process concurrency plus
atomic per-origin and instance-wide in-flight byte reservations prevent a group of
slow or large peers from exhausting memory or download capacity. Cache admission
is serialized against eviction and refuses writes over the configured byte
ceiling.

A requested derivative that does not exist returns 404; it never silently
substitutes the original image, including for older clean attachments that
predate derivative generation. A `media.delete` envelope must name the
signing origin in `content.origin_domain`; receivers purge all variants for
`content.attachment_id` and cannot use the event to delete another origin's data.

New senders emit `media.delete` with an empty `context` and a `content` of exactly
`attachment_id`, `origin_domain`, and `generation`, signed by the attachment
origin. `generation` is a canonical decimal string from 1 through 2^63−1; each new
proof for the same attachment uses the previous generation plus one. A remote
actor is admitted only on that exact shape with exactly one signature. A proof without `generation` is treated as generation 0 for
compatibility. Receivers keep one selected proof per attachment, ordered by
`(generation, ts, event_id)`, highest first, never by arrival order. For an
incoming proof:

- a lower generation than the selected proof is acknowledged as `duplicate`;
- the same generation with a different `event_id` is rejected with
  `KAED_FED_MEDIA_DELETE_GENERATION_CONFLICT`; and
- a higher generation replaces the selected proof, and superseded proofs are
  compacted.

A receiver records a tombstone even when the attachment has not arrived yet, so a
delete that overtakes its create still wins. Tombstones without a known source
are bounded per attachment origin (default 100,000). When that budget is full,
the event receives `retry` with `KAED_FED_REMOTE_MEDIA_TOMBSTONE_QUOTA_EXCEEDED`
and no idempotency claim is kept. Acknowledgement is transitive. A receiver that
previously disclosed the attachment to further peers commits its own tombstone
and relays the same origin-signed proof to them. Until every one of those
downstream routes has accepted the selected generation, it answers `retry` with
`KAED_FED_MEDIA_DELETE_CASCADE_PENDING`; once they all have, a repeat arrives as
`duplicate`.

`guild.media.delete.request` covers the case where the attachment origin has
already left the guild, so it no longer receives the sequenced message deletion.
The guild home then addresses this exact authority-signed removal-only control
to the attachment home. The recipient acts only when the attachment is its own
and the request matches a retained guild, message, and media route. Otherwise it
rejects with `KAED_FED_INVALID_EVENT`, or acknowledges as `duplicate` when it
already holds a tombstone.

Remote profiles carry the mutable display name, avatar, banner, biography,
custom status, and a monotonically increasing profile version. Equal-version
conflicts are rejected and older versions never overwrite newer cached state.
Remote profile caches are fresh for five minutes. A fresh hit performs no
network request; a stale hit returns immediately and coalesces an asynchronous
authoritative refresh for that handle. Missing profiles are cached for one
minute. Cache misses are bounded independently to 30 lookups per requester and
120 per target domain per minute; ordinary stale-handle refreshes consume that
same lookup budget.

Opaque history identities use a separate, bounded 120-per-target-domain exact-ID
refresh budget, so interactive handle lookups cannot starve no-click resolution.
Rate-limited candidates rotate behind the remaining oldest-first sweep. Newly
created placeholders are eligible in the next minute sweep. Each `(user, home)`
refresh is coalesced for 15 minutes, unsupported or failed homes back off, and
legacy homes are rediscovered at most hourly per domain. A successful proof emits
`USER_UPDATE` only to bounded local guild, DM, and relationship audiences that
already hold the public profile. Clients replace matching message authors,
members, recipients, and relationship rows in place; no reopen or refresh click is
required. Until then clients render `Remote user · <home>` rather than exposing the
internal randomized `history_<opaque>` storage handle. The alias is local-only,
is never derived from the composite reference, and must never be displayed or
treated as an authoritative handle; `profile_resolved=false` is the sole marker.

### Federated abuse reports

Report forwarding is not capability-negotiated. When a user reports a message
whose channel authority is another instance, the reporter's home signs
`POST /_kaede/v1/reports` to that authority with the ordinary report body plus
`reporter_ref` and `source_report_ref`. The home MUST NOT forward a report whose
category is `csam`; those reports stay with the reporter's home. A successful
forward leaves the local copy in `awaiting_remote` and records the authority and
remote report ID.

The authority accepts only `message` targets. Both the reporter and the source
report MUST belong to the signing origin. The reporter MUST be an existing human
account, and the message MUST be undeleted and in a channel the authority owns.
`target_ref` MUST equal the message's composite reference, and any focused
attachment MUST belong to that message. The authority rechecks that the reporter
can read the channel. Replaying the same `source_report_ref` returns the original
report rather than creating another. The route returns HTTP 201 with the
reporter-visible report projection.

For an encrypted attachment, the reporter's client may disclose decrypted
evidence. The home first calls `POST /reports/{report}/attachment-evidence` with
an explicit disclosure acknowledgement and plaintext encryption mode to obtain
one upload ticket, then calls `PUT` on the same path to commit the uploaded
attachment. The commit returns HTTP 202, and the evidence is scanned like any
other upload. Only one evidence attachment is allowed per report. Report intake
is permitted while the origin is silenced but not while it is suspended.

### Federated message search

`POST /_kaede/v1/search/messages` accepts:

- a structured query (maximum 1,024 characters);
- exactly one `channel`, `guild`, or account-DM scope;
- bounded channel, author/author-type (including negation), mention,
  role/everyone, reply, content-kind, embed, link, attachment, snowflake/date,
  pin, and NSFW filters;
- a sort order and an opaque cursor; and
- an `actor_ref`.

The actor origin must equal the authenticated signing origin. The authority rechecks current membership and `VIEW_CHANNEL` plus
`READ_MESSAGE_HISTORY` for every candidate. Requests and responses have fixed
size, result-count, cursor, string, and JSON limits and consume a per-origin
search rate budget.

The response contains at most 25 minimal result projections: composite message,
channel, guild, and author references, a bounded one-line snippet, and a
timestamp. It never contains attachment URLs or an arbitrary full
client payload. The receiving home validates every reference and timestamp,
rechecks local channel access, and reconstructs client payloads from local state.
It refuses peer-supplied bodies for locally authored messages and fails closed
on malformed, duplicate, or cross-scope peer results. A peer failure or missing
capability does not fail chat; clients identify unavailable authorities and
partial coverage explicitly.

Channel-scoped DM search contacts the deterministic conversation authority.
Account-wide DM search snapshots the requesting user's active conversation
authorities, then performs a bounded concurrent fan-out with independently
signed authorization, timeouts, and per-authority cursors. The home merges
results deterministically, stores the multi-authority cursor server-side under
an actor-and-query-bound opaque token, and never advances an unavailable
authority. Fan-out is capped at 256 authorities; exceeding that bound fails
explicitly rather than presenting incomplete history as complete.

An authority MUST NOT index or return a channel whose authenticated encryption
policy is `e2ee`, nor a message carrying an opaque `e2ee` envelope. Guild-wide
responses list excluded encrypted channel references so clients can explain the
coverage. Search queries are operationally sensitive and must not be placed in
URLs, access logs, federation event retention, analytics, or notification text.

## 8. Voice and calls

The guild home LiveKit instance owns room `g.<guild_id>.<channel_id>`. A remote
user's instance verifies cached `CONNECT`, then calls `POST /voice/token`; the home
rechecks permissions and signs a 15-minute token. Embedded generation values make
stale grants rejectable. A relayed federated grant URL must use `wss`, name the
authenticated guild authority exactly, use only the default/443 port, and contain
no userinfo, query, or fragment; downgrade or alternate-endpoint URLs are rejected.
Existing sessions may survive an S2S outage, but new joins
return `VOICE_HOME_UNREACHABLE` when the home cannot be reached.

Droppable `guild.voice_state.*` frames are represented by signed
`POST /voice/state` snapshots. The payload contains the guild ID, canonical room,
Unix `generated_at`, and bounded participant list. The authenticated origin is the
guild home. Receivers atomically ignore a snapshot whose `snapshot_version` is
lower than the one they hold. The elected voice coordinator schedules full
snapshots for active rooms every 30 seconds, with a five-second polling loop.
After 75 seconds by default, occupancy is stale and a client renders it unknown,
never asserted current. The stale threshold is configurable from 60 to 300 seconds.

The payload has exactly `guild_id`, `room` (`g.<guild_id>.<channel_id>`),
`generated_at`, `snapshot_version`, and at most 10,000 `participants`.
`snapshot_version` is an integer of at least 1, drawn from a per-room counter that
the home increments for every snapshot it builds. Ordering is decided by it, not
by `generated_at`. A receiver atomically replaces its occupancy for the room
unless it already holds a higher `snapshot_version`, so a delayed snapshot can
never overwrite a newer one. `generated_at` MUST be within the federation
clock-skew window of the receiver's clock (`KAED_VOICE_INVALID_STATE` otherwise).
The receiver also requires at least one local member of that guild
(`KAED_VOICE_NOT_SUBSCRIBED`). Each participant carries the peer-only
`generation`, a 43-character `connection_id`, and an optional `move_session_id`.
DM call occupancy uses `POST /voice/dm-state` with `call_id`, `channel_id`, a
`d.<channel_id>.<call_id>` room, the same `generated_at` and `snapshot_version`
rules, and at most 10 participants.

### Federated soundboard

Instances advertising `guild-soundboard/1` expose four private signed routes:

- `POST /_kaede/v1/guilds/{guild}/soundboard/capability` for a source-authority
  media capability;
- `POST /_kaede/v1/guilds/{guild}/soundboard/query` for bounded list/get
  operations;
- `POST /_kaede/v1/guilds/{guild}/soundboard/play` for an authoritative playback
  grant; and
- `POST /_kaede/v1/voice/soundboard-effect` for occupant-home delivery.

Each query/play request binds the requesting instance, human or bot identity,
optional bot application, exact guild/channel/sound, operation, random request
ID, issue time, and a deadline no more than ten seconds later. The guild home
consumes that request ID once and rechecks current guild/channel permissions. For
a bot it also rechecks the active installation, current bot membership, granted
`soundboard.read` or `soundboard.use` scope, and channel restrictions; the caller
instance's cached grant is never sufficient authority. Play additionally requires
an authoritative current room occupant with speaking enabled and consumes the
normal per-actor soundboard rate budget.

The authority response is a signed, request-echoing envelope. Query responses
contain at most 48 strictly bounded sound projections and no storage object key.
Play responses contain one exact sound plus a media URL valid for at most 60
seconds. Production receivers accept only HTTPS URLs on the exact
`media.<guild-authority>` host, with the default HTTPS port and without credentials
or a fragment, and validate the signed guild/channel/sound/actor context before
exposing the capability. They never dereference an arbitrary host selected by a
peer. Operators using external object storage must front federated soundboard
objects with that authority-bound media hostname (for example through a CNAME or
reverse proxy); an unrelated provider origin fails closed.

The authority sends `VOICE_CHANNEL_EFFECT_SEND` only to users currently in the
authoritative room. It delivers the same signed envelope to each occupied remote
home, which rechecks its replicated room and publishes only to its own current
occupants. A random delivery ID is consumed once for 120 seconds, so the initiating
HTTP response and asynchronous fanout cannot double-play an effect. Silence and
suspend policy applies to all four routes. Human and bot public APIs use this
path transparently for remote-authority guilds; create/edit/delete remain owned by
the guild home.

Signed `POST /calls` bodies carry `call_id`, `channel_id`, `channel_domain`,
`authority_domain`, `actor_id`, `actor_domain`, `action` (`create`, `accept`,
`decline`, or `end`), `created_at`, optional `state_version`, and boolean `ring`. An
`action` of `ring` is rejected. Actor domain must equal the authenticated origin,
except that a group `create` may be signed by the conversation authority; the
receiver rechecks DM participation. Rooms are `d.<channel_id>.<call_id>` on the
deterministic conversation authority. State is TTL-bounded and non-durable. A non-caller must accept
before `POST /voice/dm-token` can mint a grant.

## 9. Blocking and failure codes

Federation can be `open` or `allowlist`. A local silence denies inbound and
outbound guild snapshots, guild events, and remote-write proxy surfaces for the
blocked instance while still permitting DM federation. User lookup and profile
resolution remain available because DMs and user-installed applications depend
on stable identity resolution; invite resolution and public application-directory
search remain blocked because they discover or admit guild-scoped state. Global
security reports and the operator-pinned push relay are also unaffected by
silence. A local suspend rejects all federation exchange. Both policies include
subdomains when configured. CSV exchange uses Mastodon-compatible domain block
fields.

Suspension has one exception: removal-only controls, which must converge so
that already-disclosed media and deleted rooms are not stranded on downstream
replicas. A suspended peer may still authenticate an HTTP `POST /inbox` whose
events are all `media.delete`, exact `guild.media.delete.request` controls, or
exact terminal-room deletions, and it may hold a hot link. On either transport,
every event other than `media.delete`,
terminal-room deletions, and `guild.media.delete.request` then receives `retry`
with `KAED_FED_INSTANCE_SUSPENDED`. A silenced peer's guild-scoped events
likewise receive `retry` with `KAED_FED_INSTANCE_SILENCED`, and processing of the
batch stops there. Request authentication, replay protection, rate limits,
signatures, and semantic validation still apply to the exempt events. In the
other direction, a local block never holds outbound `media.delete`,
`guild.media.delete.request`, or terminal-room deletions. While a peer is
silenced, presence still reaches its instance when it hosts an accepted friend,
and federated abuse reports are still accepted from it.

Implemented transport and durable-delivery failures are registered below. HTTP
errors contain top-level `code`, a safe `message`, `trace_id`, and optional
`retry_after_ms`. Per-event failures appear in an inbox result instead of changing
the batch's HTTP status.

| Code | Surface | Meaning |
| --- | --- | --- |
| `KAED_FED_SIGNATURE_REQUIRED` | HTTP 401 | The request has no parseable Kaede authorization header. |
| `KAED_FED_BAD_SIGNATURE` | HTTP 401 | The request signature, body hash, key syntax, or signature length is invalid. |
| `KAED_FED_CLOCK_SKEW` | HTTP 401 | The signed timestamp is outside the configured window. |
| `KAED_FED_KEY_HISTORY_OVERFLOW` | HTTP 503 | `GET /keys` would advertise more than 64 old keys; the operator must retire key history. |
| `KAED_FED_PERMISSION_SCHEMA_REQUIRED` | HTTP 426 | The authenticated peer has not negotiated `kaede-permissions-v1`. |
| `KAED_FED_UNKNOWN_KEY` | HTTP 401 or per-event retry | Discovery/rotation did not yet yield the signing key, or a pooled link delivered an event immediately after rotation. Unknown event keys receive one rate-limited refresh before retry/rejection. |
| `KAED_FED_KEY_REFRESH_RATE_LIMITED` | HTTP 429 | Unknown-key discovery exceeded the pre-authentication per-origin refresh quota. |
| `KAED_FED_UNSUPPORTED_VERSION` | HTTP 400 | `X-Kaede-Version` is neither `1` nor `2`. |
| `KAED_FED_BAD_NONCE` | HTTP 400 | A version 2 request nonce is missing, malformed, or unexpectedly attached to version 1. |
| `KAED_FED_NONCE_REQUIRED` | HTTP 401 | A peer previously advertised replay-protected requests but attempted to downgrade to version 1. |
| `KAED_FED_REPLAYED_REQUEST` | HTTP 409 | A valid signed version 2 request reused an already consumed nonce. |
| `KAED_FED_HOP_LIMIT` | HTTP 400/508 | The hop header is malformed or outside zero through five. |
| `KAED_FED_NOT_ALLOWLISTED` | HTTP 403 | Local allowlist policy has not approved the origin. |
| `KAED_FED_INSTANCE_SILENCED` | HTTP 403 or per-event retry | A local silence rejects this guild snapshot, event, or proxy surface while leaving DM federation permitted. |
| `KAED_FED_INSTANCE_SUSPENDED` | HTTP 403 or per-event retry | A local suspend block rejects exchange with the origin, except for the removal-only controls described above. |
| `KAED_RATE_LIMITED` | HTTP 429 | A pre-authentication, request, event, hot-link, or route token bucket is exhausted (section 4). |
| `KAED_FED_BATCH_TOO_LARGE` | HTTP 413 | The signed request body exceeds 1 MiB. |
| `KAED_FED_INVALID_CONTENT_LENGTH` | HTTP 400 | `Content-Length` is not a valid non-negative decimal length. |
| `KAED_FED_INVALID_BATCH` | HTTP 400 | The inbox body is not a JSON object containing an event list. |
| `KAED_FED_INVALID_JSON` | HTTP 400 | A signed request contains ambiguous or non-interoperable JSON. |
| `KAED_FED_INVALID_BATCH_SIZE` | HTTP 400 | The inbox event count is outside one through 100. |
| `KAED_FED_INVALID_EVENT` | Per event | The envelope does not satisfy the registered structural bounds, or a removal-only control has invalid identifiers, generation, or attachment ownership. |
| `KAED_FED_EVENT_INVALID` | Per event, legacy alias | Older peers emit this spelling for invalid removal-only controls. Receivers MUST accept it with the same rejection and durable-retry semantics as `KAED_FED_INVALID_EVENT`; new responses use `KAED_FED_INVALID_EVENT`. |
| `KAED_FED_BAD_EVENT_SIGNATURE` | Per event | No retained origin key verifies the envelope. |
| `KAED_FED_AUTHOR_ORIGIN_MISMATCH` | HTTP 403 or per event | The authenticated origin does not own the asserted actor. |
| `KAED_FED_EVENT_ID_CONFLICT` | Per event | A global event ID already names different signed content. |
| `KAED_FED_EVENT_REJECTED` | Per event | A registered event failed its authority, identity, privacy, permission, or state checks. |
| `KAED_FED_EVENT_RETRY` | Per-event retry | The receiver cannot yet prove a terminal inbox or commit state; the sender retries the same event ID. |
| `KAED_FED_INBOX_QUOTA_EXCEEDED` | Per-event retry | A per-origin or global retained inbox row/accepted-envelope byte budget is full. No idempotency claim is stored; retry the same signed event later. |
| `KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED` | HTTP 507, per-event rejection, or replica retry | The receiver cannot retain another federated account identity. DM opens and authoritative proxy writes receive an explicit application rejection; a remote-guild replica pauses without advancing its sequence and retries after capacity is available. Exact identity counts are never disclosed. |
| `KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED` | HTTP 507, per-event rejection, or replica retry | The receiver cannot retain another remote server namespace. It follows the same DM/proxy rejection and guild-replica pause rules as identity capacity, without disclosing the cached server count. |
| `KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED` | Per-event terminal rejection | A pending relationship-request allowance is full. The code does not identify whether the recipient, origin, or pair allowance was reached. The sender must remove only the request whose correlation ID was rejected; a later request, friendship, or block is not changed. |
| `KAED_FED_REPLICA_QUOTA_EXCEEDED` | Per-event/history retry | Applying remote-guild state would exceed a per-guild or per-origin durable replica row/estimated-byte high-water mark. The mutation and sequence advance are rolled back, and the replica remains paused until capacity is released or reconfigured. |
| `KAED_FED_EVENT_TIMESTAMP_INVALID` | Per event | The durable envelope is too far in the future or older than the receiver's retention window. |
| `KAED_FED_INVALID_SNAPSHOT_CURSOR` | HTTP 400 | A guild member continuation cursor is incomplete or malformed. |
| `KAED_FED_SNAPSHOT_CHANGED` | HTTP 409 | A paged guild snapshot changed and must be restarted. |
| `KAED_FED_SNAPSHOT_BUSY` | HTTP 429 | Another snapshot/visibility computation for this origin and guild is already in progress. |
| `KAED_FED_SNAPSHOT_WORK_LIMIT` | HTTP 429 | The requested visibility graph exceeds a bounded snapshot work or record budget. |
| `KAED_FED_FULL_RESYNC` | HTTP 410 | Retained guild events cannot fill the requested sequence gap. |
| `KAED_FED_HISTORY_NOT_FOUND` | HTTP 404 | The export does not exist for the authenticated requesting origin. |
| `KAED_FED_HISTORY_FORBIDDEN` | HTTP 403 | The origin tried to request history for a user it does not own. |
| `KAED_FED_HISTORY_CAPACITY` | HTTP 429 | The authority's per-origin or global active export/channel-grant budget is full; retry after grants expire. |
| `KAED_FED_HISTORY_EXPIRED` | HTTP 410 | The short-lived export grant expired. |
| `KAED_FED_HISTORY_REVOKED` | HTTP 410 | Membership or channel authorization was revoked. |
| `KAED_FED_HISTORY_GRANT_STALE` | HTTP 409 | A bound membership, permission, or policy generation changed; restart negotiation. |
| `KAED_FED_HISTORY_CURSOR_INVALID` | HTTP 400 | A page or delta cursor is outside its grant. |
| `KAED_FED_HISTORY_MESSAGE_TOO_LARGE` | HTTP 413 | One exported message exceeds the page byte limit. |
| `KAED_FED_HISTORY_UNAVAILABLE` | HTTP 409 | The export is neither active nor completed. |
| `KAED_FED_DM_HISTORY_NOT_FOUND` | HTTP 404 | The receiver is not the authority for the requested DM conversation, or does not know it. |
| `KAED_FED_DM_HISTORY_FORBIDDEN` | HTTP 403 | The requester is not a current participant of the DM. |
| `KAED_FED_INVALID_TRACKER_CURSOR` | HTTP 400 | A tracker snapshot cursor is malformed. |
| `KAED_FED_TRACKER_SNAPSHOT_CHANGED` | HTTP 409 | The tracker board changed during a paged snapshot; restart it. |
| `KAED_FED_GUILD_MANAGEMENT_REQUEST_EXPIRED`, `KAED_FED_APPLICATION_MANAGEMENT_REQUEST_EXPIRED`, `KAED_FED_DEVELOPER_MANAGEMENT_REQUEST_EXPIRED`, `KAED_FED_DM_BOT_E2EE_REQUEST_EXPIRED` | HTTP 401 | A management RPC's `issued_at` is in the future beyond clock skew, or its deadline has passed. |
| `KAED_FED_GUILD_MANAGEMENT_REQUEST_REPLAYED`, `KAED_FED_APPLICATION_MANAGEMENT_REQUEST_REPLAYED`, `KAED_FED_DEVELOPER_MANAGEMENT_REQUEST_REPLAYED`, `KAED_FED_DM_BOT_E2EE_REQUEST_REPLAYED` | HTTP 409 | A management request ID was already consumed. |
| `KAED_FED_GUILD_MANAGEMENT_CALLER_MISMATCH`, `KAED_FED_APPLICATION_MANAGEMENT_CALLER_MISMATCH`, `KAED_FED_DEVELOPER_MANAGEMENT_CALLER_MISMATCH` | HTTP 403 | The signed actor or requesting instance does not match the authenticated caller. |
| `KAED_FED_DEVELOPER_MANAGEMENT_TEAM_MISMATCH`, `KAED_FED_DEVELOPER_MANAGEMENT_MEMBER_MISMATCH` | HTTP 403 | The request names a team or member that does not match the route or current membership. |
| `KAED_FED_GUILD_MANAGEMENT_OPERATION_UNSUPPORTED`, `KAED_FED_APPLICATION_MANAGEMENT_OPERATION_UNSUPPORTED`, `KAED_FED_DEVELOPER_MANAGEMENT_OPERATION_UNSUPPORTED` | HTTP 400 | The operation is outside the receiver's closed operation set. |
| `KAED_FED_BAD_REQUEST` | HTTP 400 | An application-management request names an unsupported resource. |
| `KAED_FED_AUDIT_LOG_REQUEST_EXPIRED` | HTTP 401 | An audit-log request is outside its issue/deadline window. |
| `KAED_FED_AUDIT_LOG_REQUEST_REPLAYED` | HTTP 409 | An audit-log request ID was already consumed. |
| `KAED_FED_AUDIT_LOG_REQUESTER_MISMATCH` | HTTP 403 | The signed requester does not belong to the signing origin. |
| `KAED_FED_AUDIT_LOG_SIGNER_UNAVAILABLE` | HTTP 503 | The guild home cannot currently sign the response page. |
| `KAED_FED_SOUNDBOARD_REQUEST_EXPIRED` | HTTP 401 | A soundboard request is outside its ten-second window. |
| `KAED_FED_SOUNDBOARD_REQUEST_REPLAYED` | HTTP 409 | A soundboard request ID was already consumed. |
| `KAED_FED_SOUNDBOARD_CALLER_MISMATCH`, `KAED_FED_SOUNDBOARD_SOURCE_CALLER_MISMATCH`, `KAED_FED_SOUNDBOARD_CALLER_KIND_MISMATCH` | HTTP 403 | The signed human or bot caller, or its kind, does not match the authenticated origin or request. |
| `KAED_FED_SOUNDBOARD_ACTOR_INTENT_INVALID` | HTTP 403 | The nested actor-home intent does not verify or bind the request. |
| `KAED_FED_SOUNDBOARD_EFFECT_INVALID` | HTTP 400 | A soundboard effect delivery is malformed or does not match its signed grant. |
| `KAED_FED_SOUNDBOARD_SIGNER_UNAVAILABLE` | HTTP 503 | The guild home cannot currently sign the soundboard response. |
| `KAED_FED_SEARCH_UNSUPPORTED` | HTTP 404 | Search is disabled at this authority. |
| `KAED_FED_SEARCH_ACTOR_INVALID` | HTTP 403 | The search `actor_ref` does not belong to the signing origin. |
| `KAED_FED_SEARCH_UNAVAILABLE` | HTTP 503 | The authority's search backend is unavailable. |
| `KAED_FED_REPORT_TARGET_INVALID` | HTTP 422 | A forwarded report does not target a message. |
| `KAED_FED_REPORTER_ORIGIN_MISMATCH` | HTTP 403 | The reporter or source report does not belong to the signing origin. |
| `KAED_FED_REPORTER_NOT_FOUND` | HTTP 404 | The reporter is not a known remote human account of the signing origin. |
| `KAED_FED_REPORT_MESSAGE_NOT_FOUND` | HTTP 404 | The reported message is missing, deleted, or not in a channel this instance owns. |
| `KAED_FED_REPORT_TARGET_MISMATCH` | HTTP 422 | `target_ref` differs from the message's composite reference. |
| `KAED_FED_REPORT_MEDIA_NOT_FOUND` | HTTP 404 | The focused attachment does not belong to the reported message. |
| `KAED_FED_REPORT_NOT_FOUND` | HTTP 404 | No eligible forwarded encrypted-attachment report exists for evidence upload. |
| `KAED_MEDIA_NOT_FOUND` | HTTP 404 | Non-disclosing failure for every media authorization, scope, variant, or lookup error. |
| `KAED_MEDIA_UNAVAILABLE` | HTTP 503 | The origin's object storage could not supply the bytes. |
| `KAED_PRESENCE_STALE_OR_UNKNOWN` | HTTP 409 | A presence projection is expired, too far ahead, outside clock skew, older than the current generation, or has no local audience. |
| `KAED_TYPING_SCOPE_INVALID` | HTTP 409 | A typing publish has a foreign actor or user, a channel this instance does not own, or an expired projection; or a relay's audience does not validate. |
| `KAED_TYPING_STALE` | HTTP 409 | A typing projection was not published because it is stale, duplicate, out of order, or has no remaining audience. |
| `KAED_THREAD_MUTATION_INVALID` | HTTP 422 | A proxied thread or forum mutation is malformed. |
| `KAED_GROUP_DM_INVITEE_NOT_LOCAL` | HTTP 400 | A group invitation authorization reached an instance that does not host the invitee. |
| `KAED_GROUP_DM_INVITE_NOT_FRIEND` | HTTP 403 | The invitee is not the inviter's accepted friend. |
| `KAED_GROUP_DM_INVITEE_HOME_UNREACHABLE` | HTTP 503 | The invitee's home could not confirm the invitation. |
| `KAED_GROUP_DM_WRONG_AUTHORITY` | HTTP 409 | A group mutation reached an instance that is not the group authority. |
| `KAED_E2EE_AUTHORITY_MISMATCH` | HTTP 403 | An E2EE request's actor, channel, operation, or target domain does not match the signing origin and this receiver's role. |
| `KAED_E2EE_TARGET_NOT_FOUND` | HTTP 404 | A key-package claim names an unknown channel, user, or device. |
| `E2EE_INSTANCE_UNSUPPORTED`, `E2EE_INSTANCE_UNAVAILABLE` | Local API HTTP 409 | The activating home's own client API found a participant instance without `e2ee-mls/1`, or could not discover it. Peers never return these codes. |
| `KAED_CALL_INVALID_TIMESTAMP` | HTTP 400 | A `create` call signal's `created_at` is outside the federation clock-skew window. |
| `KAED_VOICE_NOT_SUBSCRIBED` | HTTP 403 | The receiver has no local member of the guild or call the occupancy describes. |
| `KAED_VOICE_MOVE_NOT_EXPECTED`, `KAED_VOICE_MOVE_STALE` | HTTP 409 | A voice move has no matching pending session, or names a stale generation or connection. |
| `KAED_FED_RESYNC_RETRY` | Per-event retry | A valid resync marker was retained but its callback gap-fill/snapshot could not yet complete. |
| `KAED_FED_DM_STORAGE_QUOTA_EXCEEDED` | Per-event retry or rejection | The receiver cannot retain more federated DM conversations, messages, or bytes. A queued open is rejected with an explicit `dm.open.rejected`. |
| `KAED_FED_REMOTE_MEDIA_TOMBSTONE_QUOTA_EXCEEDED` | Per-event retry | The per-origin budget of tombstones without a known source is full. No idempotency claim is kept. |
| `KAED_FED_MEDIA_DELETE_CASCADE_PENDING` | Per-event retry | The tombstone is committed, but downstream peers have not all accepted the selected generation. |
| `KAED_FED_MEDIA_DELETE_GENERATION_CONFLICT` | Per event | A different `media.delete` proof already holds the same generation. |
| `KAED_FED_EVENT_TOO_LARGE` | Local delivery state | One envelope alone exceeds the 1 MiB batch bound and cannot be sent. |
| `KAED_FED_OUTBOX_CAPACITY_EXCEEDED` | HTTP 507 or per-event retry | Handling the request or event would enqueue past a destination's 50,000-pointer cap. An inbox event keeps no idempotency claim. The local API uses `FEDERATION_OUTBOX_CAPACITY_EXCEEDED`. |
| `KAED_FED_DELIVERY_EXPIRED` | Local delivery state | A destination pointer exceeded its delivery window; guild peers gap-fill and affected pending DM projections become failed. |
| `KAED_DM_WRONG_AUTHORITY` | HTTP 409 | The receiver is not the deterministic DM authority. |
| `KAED_DM_INVALID_PARTICIPANTS` | HTTP 400 | DM authorization does not contain the authenticated remote and local recipient. |
| `KAED_DM_OPEN_REJECTED` | `dm.open.rejected` content | The DM authority rejected an open request without a more specific stable application code. |
| `KAED_GUILD_INVALID_MENTION` | HTTP 400 | A proxied message mentions an identity outside the guild. |
| `KAED_GUILD_NONCE_STATE_CONFLICT` | HTTP 409 or queued write rejection | A known proxy nonce has no matching authoritative guild event, or was reused for different immutable message semantics. |
| `KAED_VOICE_INVALID_ROOM` | HTTP 400 | An occupancy payload does not bind its declared guild to a canonical guild room. |
| `KAED_VOICE_INVALID_STATE` | HTTP 400 | Occupancy contains a malformed participant or mismatched room/identity. |
| `VOICE_HOME_UNREACHABLE` | HTTP 503 | A new guild/call join cannot reach or validate the authoritative SFU broker; clients retry after the supplied delay. |
| `CALL_HOME_UNREACHABLE` | HTTP 503 | A call action cannot reach its conversation authority. |

Route-specific application failures such as `USER_NOT_FOUND`, `INVITE_NOT_FOUND`,
`BANNED_FROM_GUILD`, `NOT_A_GUILD_MEMBER`, `CANNOT_DM_USER`, and permission/channel
errors retain their API meanings. Voice and call failures never authorize local
SFU fallback: the declared guild/call authority remains binding.
