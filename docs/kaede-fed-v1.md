# kaede-fed/1

This document is the versioned protocol contract between Kaede instances. It
covers:

- discovery and versioning
- signed HTTP and hot-link transport
- durable DMs
- guild synchronization and remote writes
- authenticated remote media
- voice token brokering, occupancy, and call signaling

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
    "e2ee-transport/1",
    "group-dm/1",
    "guild-audit-log/1",
    "guild-history-sync/1",
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
layout is intentionally not a raw Discord API v10 mask: in particular, Kaede
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
- `e2ee-transport/1` advertises only bounded opaque encrypted-message storage
  and relay as described in section 6.1. It does not claim that the instance or
  its clients implement key management, MLS, device verification, or an
  end-to-end encryption user experience.
- `dm-history-page/1` advertises signed, participant-authorized, bounded DM
  history paging. A non-authoritative home MUST NOT evict older remote-authored
  cache rows until the conversation authority advertises this capability. Media
  referenced by an on-demand page uses the same capability: every signed media
  authorization and byte request carries the exact conversation and message
  composite references, and the attachment origin verifies that its durable
  attachment row belongs to that message in that conversation. Authorization
  based only on a peer participating in some other DM is forbidden.
- `group-dm/1` advertises authority-owned group conversations, friend-confirmed
  invitation authorization, monotonic full-state membership updates, and calls
  with up to 10 participants. A peer that does not advertise it cannot be added
  to a group conversation. Group state is signed by the conversation authority.
  Its actor may belong to another participating instance when that participant
  sent the mutation to the authority; this exception applies only to
  `dm.group.state`. Receivers still require that actor in the prior or resulting
  participant set, and a newly added local participant must already be the
  actor's accepted friend.
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
  document cannot silently remove it and downgrade that relationship.

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
object. The authorization header identifies `origin`, `key`, and base64 signature.
The destination rejects timestamps outside ±300 seconds, mismatched destinations,
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

| Input                       | Exact value                                                        |
| --------------------------- | ------------------------------------------------------------------ |
| Ed25519 private seed (hex)  | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| Ed25519 public key (base64) | `A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=`                     |
| Exact HTTP body             | `{"events":[]}`                                                    |
| Body SHA-256                | `24de1c4a19c43ad41b013f13dcd858c17b0daa7f33a53f19913e5b11366d1c2e` |
| Method                      | `POST`                                                             |
| Unsorted input query        | `z=2&a=hello%20world`                                              |
| Canonical request target    | `/_kaede/v1/inbox?a=hello+world&z=2`                               |
| Origin / destination        | `alpha.example` / `beta.example`                                   |
| Timestamp                   | `1783886400`                                                       |

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

### Durable event registry

Unregistered durable event types are rejected. The HTTP inbox and hot link
accept the following exact names:

| Event type                                                             | Authority and purpose                                                                                                                                                            |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `relationship.request`                                                 | A user's home sends a versioned actor profile and an unguessable request correlation ID to the target user's home.                                                               |
| `relationship.accept`                                                  | The target user's home accepts only the exact still-pending correlation ID. Late acceptance cannot recreate cancelled or blocked state.                                          |
| `relationship.remove`                                                  | A user's home removes friendship or pending state at the peer. The sender never reveals whether the local reason was removal or blocking.                                        |
| `relationship.profile`                                                 | A user's home sends a versioned profile update to an accepted remote friend. The receiver applies it only while the exact friendship is still active.                            |
| `dm.open.request`                                                      | A participant asks the deterministic DM authority to open a conversation asynchronously.                                                                                         |
| `dm.conversation.create`                                               | The deterministic authority announces the converged conversation and its two participants.                                                                                       |
| `dm.open.rejected`                                                     | The authority rejects a previously queued open request with a stable code.                                                                                                       |
| `dm.message.create`                                                    | A participant's home replicates one DM message to the other participant's instance.                                                                                              |
| `dm.message.update`, `dm.message.delete`                               | The deterministic conversation authority relays an author's validated edit or terminal tombstone to every participant home.                                                      |
| `dm.reaction.add`, `dm.reaction.remove`                                | The deterministic conversation authority relays one participant's canonical reaction state change.                                                                               |
| `dm.poll.vote.add`, `dm.poll.vote.remove`, `dm.poll.finalize`          | The deterministic conversation authority relays opaque poll vote state or finalization; encrypted question and answer text never enters the event.                               |
| `dm.pin.add`, `dm.pin.remove`                                          | The deterministic conversation authority relays the shared conversation pin state to every participant home.                                                                     |
| `guild.member.add`                                                     | The guild home announces a remote invite join.                                                                                                                                   |
| `guild.member.profile`                                                 | A user's home sends its exact versioned profile either to a remote guild authority or, when it is also the guild authority, directly to the guild's current remote member homes. |
| `guild.update`                                                         | The guild home replaces mutable guild metadata.                                                                                                                                  |
| `guild.channel.create`, `guild.channel.update`, `guild.channel.delete` | The guild home creates, replaces, or removes channel state.                                                                                                                      |
| `guild.role.create`, `guild.role.update`, `guild.role.delete`          | The guild home creates, replaces, or removes role state.                                                                                                                         |
| `guild.emoji.create`, `guild.emoji.delete`                             | The guild home creates or removes a content-addressed custom emoji.                                                                                                              |
| `guild.overwrite.upsert`                                               | The guild home replaces a channel permission overwrite.                                                                                                                          |
| `guild.member.update`, `guild.member.remove`                           | The guild home updates or removes membership state.                                                                                                                              |
| `guild.member.profile.relay`                                           | The guild home sequences an exact user-home-signed `guild.member.profile` source for current member homes; it cannot rewrite the nested profile.                                 |
| `guild.members.origin.remove`                                          | The guild home atomically removes every member homed on one federated origin after an instance-wide sanction.                                                                    |
| `guild.member.role.add`, `guild.member.role.remove`                    | The guild home changes one member-role assignment.                                                                                                                               |
| `guild.ban.add`, `guild.ban.remove`                                    | The guild home changes its moderation ban set; an add may carry an absolute expiry.                                                                                              |
| `guild.access.revoked`                                                 | The guild home directly removes one user at the target user's origin; it remains valid when that origin has no member left and can no longer request snapshots.                  |
| `guild.instance_access.revoked`                                        | The guild home directly removes all of the target origin's local members and instructs that origin to purge its cached guild data after an instance-wide ban.                    |
| `guild.resync.required`                                                | A revision-bound guild-home marker replaces expired delivery rows and requires background gap-fill/full snapshot recovery.                                                       |
| `guild.message.create`                                                 | The guild home announces a message authored on the home instance.                                                                                                                |
| `guild.message.update`, `guild.message.delete`, `guild.message.purge`  | The guild home edits, tombstones, or author/time-range purges messages.                                                                                                          |
| `guild.reaction.add`, `guild.reaction.remove`                          | The guild home changes one message reaction.                                                                                                                                     |
| `guild.poll.vote.add`, `guild.poll.vote.remove`, `guild.poll.finalize` | The guild home changes one poll vote or closes a poll; member homes apply the mutation in guild sequence order.                                                                  |
| `guild.pin.add`, `guild.pin.remove`                                    | The guild home changes one channel pin.                                                                                                                                          |
| `guild.proxy.message.create`                                           | A replica durably queues a remote member's write for the guild home.                                                                                                             |
| `guild.message.committed`                                              | The guild home announces the authoritative result of a proxied write.                                                                                                            |
| `guild.event.redacted`                                                 | Signed placeholder that advances a peer past a channel event none of its members may inspect.                                                                                    |
| `message.send_rejected`                                                | The guild home rejects a previously queued proxy write.                                                                                                                          |
| `media.delete`                                                         | The attachment origin invalidates every cached variant of one origin-owned attachment.                                                                                           |

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

Peers that advertise `presence/1` may send a signed
`POST /_kaede/v1/presence` projection.
The subject domain must equal the signing origin, the recipient must already
know the subject through a shared guild, and the projection expires within 90
seconds unless refreshed. Receivers reject stale timestamps and publish the
state only to local subscribers of shared guilds. Media bytes are fetched on
demand; only deletion is durable.
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

Every S2S route applies a per-origin token bucket. Invite resolution and guild
join also have independent 30/minute and 10/minute origin buckets. `429` includes a retry delay.
Inbox depth, repeated signature failure, and sustained throttling are observable.

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
has applied successfully. The departure record intentionally survives deletion of
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
`disabled` or `full_retained`; every text or announcement channel stores
`inherit`, `disabled`, or `full_retained`. The channel override wins. Policy is
only one gate: the requesting member MUST currently have both `VIEW_CHANNEL` and
the existing `READ_MESSAGE_HISTORY` permission in that channel. Administrator
expansion follows the ordinary permission algorithm. Voice, category, unavailable,
and omitted channels are never eligible.

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
`guild-history-sync/2-recent-first` send each channel newest-to-oldest, so recent
conversation becomes available before deep history. The response declares
`order=recent_first` and advances an exclusive, strictly decreasing `next_before`
cursor. A cursor that is unchanged, increasing, or inconsistent with the final
message makes the receiver reject the transfer. Version 1 remains oldest-first for
compatibility. Pages contain only non-deleted messages at or below the channel
high-water mark, stable attachment metadata, reactions, pins, and bounded author
profiles. The guild home remains the message authority; payload-provided URLs are
still forbidden.

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
This purge is intentionally best effort: federation cannot force a malicious,
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
Cross-instance read receipts and group DMs are not part of v1.

### 6.1 Opaque encrypted-message transport

An instance advertising `e2ee-transport/1` can store and relay an `e2ee` object in
place of plaintext message content across local message APIs, gateway events,
guild and DM durable events, proxy commits, and retained-history transfer. The
object requires an integer `version` from 1 through 2,147,483,647 and is limited to
64 KiB, 16 nested levels, 4,096 JSON values, 256 members per object, 1,024 items per
array, and 256 UTF-8 bytes per object key. The transport treats all remaining
members as opaque and never selects or validates a cipher suite. Plaintext and an
`e2ee` object are mutually exclusive. A sender MUST NOT activate an encrypted room
across an instance that does not advertise this capability.

This capability alone is plumbing, not proof of end-to-end encryption. Kaede's
`e2ee-mls/1` and `e2ee-media/1` extensions add device credentials, one-use key
packages, MLS groups and welcomes, recovery UX, and encrypted attachments. The
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
resistance are not provided by `e2ee-transport/1`.

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

| Method and path                                                        | Purpose                                                                   | Availability           |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------- | ---------------------- |
| `GET /keys`                                                            | Current and historical verification keys                                  | v1                     |
| `POST /inbox`                                                          | Batched durable event delivery                                            | v1                     |
| `GET /users/lookup?handle=…`                                           | Resolve a local user profile for a remote peer                            | v1                     |
| `GET /users/profile?user_id=…&user_domain=…`                           | Return a home-signed public profile proof for an exact composite identity | `profile-by-ref/1`     |
| `POST /dm/authorize`                                                   | Ask the non-authority recipient instance to enforce DM privacy            | v1                     |
| `POST /dm/open`                                                        | Authoritative direct-message open                                         | v1                     |
| `POST /invites/resolve`                                                | Resolve a remote guild invite                                             | v1                     |
| `POST /guilds/{id}/join`                                               | Consume an invite and obtain the authoritative guild identity             | v1                     |
| `GET /guilds/{id}/snapshot`                                            | Paged structural initial/full synchronization                             | v1                     |
| `GET /guilds/{id}/events`                                              | Sequence gap fill for registered guild events                             | v1                     |
| `POST /guilds/{id}/management`                                         | Replay-bounded signed guild-management authority RPC                      | v1                     |
| `POST /guilds/{id}/audit-logs`                                         | Private requester-bound audit-log page with live authority recheck        | `guild-audit-log/1`    |
| `POST /guilds/{id}/history-exports`                                    | Create or resume a permission-bound history grant                         | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}`                            | Read the bound manifest                                                   | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}/channels/{channel}?after=…` | Read one bounded oldest-first page                                        | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}/delta?after_seq=…`          | Reconcile retained mutations after the baseline                           | `guild-history-sync/1` |
| `POST /guilds/{id}/history-exports/{export}/complete`                  | Idempotently acknowledge a merged export                                  | `guild-history-sync/1` |
| `POST /guilds/{id}/proxy`                                              | Idempotent remote guild message write                                     | v1                     |
| `POST /guilds/{id}/proxy-pin`                                          | Permission-checked remote guild pin mutation                              | v1                     |
| `POST /guilds/{id}/pins`                                               | Permission-checked authoritative guild pin page                           | v1                     |
| `POST /dms/{id}/pins`                                                  | Participant-bound authoritative DM pin page                               | v1                     |
| `POST /search/messages`                                                | Bounded permission-checked federated message search                       | `message-search/1`     |
| `POST /application-directory/search`                                   | Signed, policy-filtered reviewed application search                       | v1                     |
| `POST /application-directory/bot-profile`                              | Resolve one bot authority's active application and install template       | v1                     |
| `POST /application-directory/detail`                                   | Read one exact reviewed application product projection                    | v1                     |
| `GET /link`                                                            | Signed `kaede-fed.1` hot-link WebSocket upgrade                           | v1                     |
| `POST /voice/token`                                                    | Home-SFU guild token broker                                               | v1                     |
| `POST /voice/dm-token`                                                 | Caller-SFU DM call token broker                                           | v1                     |
| `POST /voice/state`                                                    | Droppable guild occupancy snapshot/heartbeat                              | v1                     |
| `POST /calls`                                                          | Droppable two-party DM call signaling                                     | v1                     |
| `GET /media/{attachment}/{variant}`                                    | Signed remote-media stream                                                | v1                     |

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
substitutes arbitrary or unscanned content. For compatibility with older clean
image attachments that predate derivative generation, an authority may return the
already-scanned bounded original image instead. A `media.delete` envelope must name the
signing origin in `content.origin_domain`; receivers purge all variants for
`content.attachment_id` and cannot use the event to delete another origin's data.

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
return `KAED_VOICE_HOME_UNREACHABLE` when the home cannot be reached.

Droppable `guild.voice_state.*` frames are represented by signed
`POST /voice/state` snapshots. The payload contains the guild ID, canonical room,
Unix `generated_at`, and bounded participant list. The authenticated origin is the
guild home. Receivers atomically ignore a timestamp older than their current
heartbeat. A home sends the full snapshot every 30 seconds; after 75 seconds by
default a client renders occupancy unknown, never asserted current.

### Federated soundboard

Instances advertising `guild-soundboard/1` expose three private signed routes:

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
suspend policy applies to all three routes. Human and bot public APIs use this
path transparently for remote-authority guilds; create/edit/delete remain owned by
the guild home.

DM calls use `dm.call.create`, `ring`, `accept`, `decline`, and `end` semantics on
signed `POST /calls` requests. The payload binds call, channel, authority, actor,
action, and creation time. Actor domain must equal the authenticated origin; the
receiver rechecks DM participation. Rooms are `d.<channel_id>.<call_id>` on the
caller's instance. State is TTL-bounded and non-durable. A non-caller must accept
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

Implemented transport and durable-delivery failures are registered below. HTTP
errors contain top-level `code`, a safe `message`, `trace_id`, and optional
`retry_after_ms`. Per-event failures appear in an inbox result instead of changing
the batch's HTTP status.

| Code                                           | Surface                                         | Meaning                                                                                                                                                                                                                                                                                            |
| ---------------------------------------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `KAED_FED_SIGNATURE_REQUIRED`                  | HTTP 401                                        | The request has no parseable Kaede authorization header.                                                                                                                                                                                                                                           |
| `KAED_FED_BAD_SIGNATURE`                       | HTTP 401                                        | The request signature, body hash, key syntax, or signature length is invalid.                                                                                                                                                                                                                      |
| `KAED_FED_CLOCK_SKEW`                          | HTTP 401                                        | The signed timestamp is outside the configured window.                                                                                                                                                                                                                                             |
| `KAED_FED_UNKNOWN_KEY`                         | HTTP 401 or per-event retry                     | Discovery/rotation did not yet yield the signing key, or a pooled link delivered an event immediately after rotation. Unknown event keys receive one rate-limited refresh before retry/rejection.                                                                                                  |
| `KAED_FED_KEY_REFRESH_RATE_LIMITED`            | HTTP 429                                        | Unknown-key discovery exceeded the pre-authentication per-origin refresh quota.                                                                                                                                                                                                                    |
| `KAED_FED_UNSUPPORTED_VERSION`                 | HTTP 400                                        | `X-Kaede-Version` is neither `1` nor `2`.                                                                                                                                                                                                                                                          |
| `KAED_FED_BAD_NONCE`                           | HTTP 400                                        | A version 2 request nonce is missing, malformed, or unexpectedly attached to version 1.                                                                                                                                                                                                            |
| `KAED_FED_NONCE_REQUIRED`                      | HTTP 401                                        | A peer previously advertised replay-protected requests but attempted to downgrade to version 1.                                                                                                                                                                                                    |
| `KAED_FED_REPLAYED_REQUEST`                    | HTTP 409                                        | A valid signed version 2 request reused an already consumed nonce.                                                                                                                                                                                                                                 |
| `KAED_FED_HOP_LIMIT`                           | HTTP 400/508                                    | The hop header is malformed or outside zero through five.                                                                                                                                                                                                                                          |
| `KAED_FED_NOT_ALLOWLISTED`                     | HTTP 403                                        | Local allowlist policy has not approved the origin.                                                                                                                                                                                                                                                |
| `KAED_FED_INSTANCE_SILENCED`                   | HTTP 403                                        | A local silence rejects this guild snapshot, event, or proxy surface while leaving DM federation permitted.                                                                                                                                                                                        |
| `KAED_FED_INSTANCE_SUSPENDED`                  | HTTP 403                                        | A local suspend block rejects exchange with the origin.                                                                                                                                                                                                                                            |
| `KAED_RATE_LIMITED`                            | HTTP 429                                        | The per-origin token bucket is exhausted.                                                                                                                                                                                                                                                          |
| `KAED_FED_BATCH_TOO_LARGE`                     | HTTP 413                                        | The signed request body exceeds 1 MiB.                                                                                                                                                                                                                                                             |
| `KAED_FED_INVALID_CONTENT_LENGTH`              | HTTP 400                                        | `Content-Length` is not a valid non-negative decimal length.                                                                                                                                                                                                                                       |
| `KAED_FED_INVALID_BATCH`                       | HTTP 400                                        | The inbox body is not a JSON object containing an event list.                                                                                                                                                                                                                                      |
| `KAED_FED_INVALID_JSON`                        | HTTP 400                                        | A signed request contains ambiguous or non-interoperable JSON.                                                                                                                                                                                                                                     |
| `KAED_FED_INVALID_BATCH_SIZE`                  | HTTP 400                                        | The inbox event count is outside one through 100.                                                                                                                                                                                                                                                  |
| `KAED_FED_INVALID_EVENT`                       | Per event                                       | The envelope does not satisfy the registered structural bounds.                                                                                                                                                                                                                                    |
| `KAED_FED_BAD_EVENT_SIGNATURE`                 | Per event                                       | No retained origin key verifies the envelope.                                                                                                                                                                                                                                                      |
| `KAED_FED_AUTHOR_ORIGIN_MISMATCH`              | HTTP 403 or per event                           | The authenticated origin does not own the asserted actor.                                                                                                                                                                                                                                          |
| `KAED_FED_EVENT_ID_CONFLICT`                   | Per event                                       | A global event ID already names different signed content.                                                                                                                                                                                                                                          |
| `KAED_FED_EVENT_REJECTED`                      | Per event                                       | A registered event failed its authority, identity, privacy, permission, or state checks.                                                                                                                                                                                                           |
| `KAED_FED_EVENT_RETRY`                         | Per-event retry                                 | The receiver cannot yet prove a terminal inbox or commit state; the sender retries the same event ID.                                                                                                                                                                                              |
| `KAED_FED_INBOX_QUOTA_EXCEEDED`                | Per-event retry                                 | A per-origin or global retained inbox row/accepted-envelope byte budget is full. No idempotency claim is stored; retry the same signed event later.                                                                                                                                                |
| `KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED`     | HTTP 507, per-event rejection, or replica retry | The receiver cannot retain another federated account identity. DM opens and authoritative proxy writes receive an explicit application rejection; a remote-guild replica pauses without advancing its sequence and retries after capacity is available. Exact identity counts are never disclosed. |
| `KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED`     | HTTP 507, per-event rejection, or replica retry | The receiver cannot retain another remote server namespace. It follows the same DM/proxy rejection and guild-replica pause rules as identity capacity, without disclosing the cached server count.                                                                                                 |
| `KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED` | Per-event terminal rejection                    | A pending relationship-request allowance is full. The code deliberately does not identify whether the recipient, origin, or pair allowance was reached. The sender must remove only the request whose correlation ID was rejected; a later request, friendship, or block is not changed.           |
| `KAED_FED_REPLICA_QUOTA_EXCEEDED`              | Per-event/history retry                         | Applying remote-guild state would exceed a per-guild or per-origin durable replica row/estimated-byte high-water mark. The mutation and sequence advance are rolled back, and the replica remains paused until capacity is released or reconfigured.                                               |
| `KAED_FED_EVENT_TIMESTAMP_INVALID`             | Per event                                       | The durable envelope is too far in the future or older than the receiver's retention window.                                                                                                                                                                                                       |
| `KAED_FED_INVALID_SNAPSHOT_CURSOR`             | HTTP 400                                        | A guild member continuation cursor is incomplete or malformed.                                                                                                                                                                                                                                     |
| `KAED_FED_SNAPSHOT_CHANGED`                    | HTTP 409                                        | A paged guild snapshot changed and must be restarted.                                                                                                                                                                                                                                              |
| `KAED_FED_SNAPSHOT_BUSY`                       | HTTP 429                                        | Another snapshot/visibility computation for this origin and guild is already in progress.                                                                                                                                                                                                          |
| `KAED_FED_SNAPSHOT_WORK_LIMIT`                 | HTTP 429                                        | The requested visibility graph exceeds a bounded snapshot work or record budget.                                                                                                                                                                                                                   |
| `KAED_FED_FULL_RESYNC`                         | HTTP 410                                        | Retained guild events cannot fill the requested sequence gap.                                                                                                                                                                                                                                      |
| `KAED_FED_HISTORY_NOT_FOUND`                   | HTTP 404                                        | The export does not exist for the authenticated requesting origin.                                                                                                                                                                                                                                 |
| `KAED_FED_HISTORY_FORBIDDEN`                   | HTTP 403                                        | The origin tried to request history for a user it does not own.                                                                                                                                                                                                                                    |
| `KAED_FED_HISTORY_CAPACITY`                    | HTTP 429                                        | The authority's per-origin or global active export/channel-grant budget is full; retry after grants expire.                                                                                                                                                                                        |
| `KAED_FED_HISTORY_EXPIRED`                     | HTTP 410                                        | The short-lived export grant expired.                                                                                                                                                                                                                                                              |
| `KAED_FED_HISTORY_REVOKED`                     | HTTP 410                                        | Membership or channel authorization was revoked.                                                                                                                                                                                                                                                   |
| `KAED_FED_HISTORY_GRANT_STALE`                 | HTTP 409                                        | A bound membership, permission, or policy generation changed; restart negotiation.                                                                                                                                                                                                                 |
| `KAED_FED_HISTORY_CURSOR_INVALID`              | HTTP 400                                        | A page or delta cursor is outside its grant.                                                                                                                                                                                                                                                       |
| `KAED_FED_RESYNC_RETRY`                        | Per-event retry                                 | A valid resync marker was retained but its callback gap-fill/snapshot could not yet complete.                                                                                                                                                                                                      |
| `KAED_FED_DELIVERY_EXPIRED`                    | Local delivery state                            | A destination pointer exceeded its delivery window; guild peers gap-fill and affected pending DM projections become failed.                                                                                                                                                                        |
| `KAED_DM_WRONG_AUTHORITY`                      | HTTP 409                                        | The receiver is not the deterministic DM authority.                                                                                                                                                                                                                                                |
| `KAED_DM_INVALID_PARTICIPANTS`                 | HTTP 400                                        | DM authorization does not contain the authenticated remote and local recipient.                                                                                                                                                                                                                    |
| `KAED_DM_OPEN_REJECTED`                        | `dm.open.rejected` content                      | The DM authority rejected an open request without a more specific stable application code.                                                                                                                                                                                                         |
| `KAED_GUILD_INVALID_MENTION`                   | HTTP 400                                        | A proxied message mentions an identity outside the guild.                                                                                                                                                                                                                                          |
| `KAED_GUILD_NONCE_STATE_CONFLICT`              | HTTP 409 or queued write rejection              | A known proxy nonce has no matching authoritative guild event, or was reused for different immutable message semantics.                                                                                                                                                                            |
| `KAED_VOICE_INVALID_ROOM`                      | HTTP 400                                        | An occupancy payload does not bind its declared guild to a canonical guild room.                                                                                                                                                                                                                   |
| `KAED_VOICE_INVALID_STATE`                     | HTTP 400                                        | Occupancy contains a malformed participant or mismatched room/identity.                                                                                                                                                                                                                            |
| `VOICE_HOME_UNREACHABLE`                       | HTTP 503                                        | A new guild/call join cannot reach or validate the authoritative SFU broker; clients retry after the supplied delay.                                                                                                                                                                               |
| `CALL_HOME_UNREACHABLE`                        | HTTP 503                                        | A call action cannot reach its caller-instance authority.                                                                                                                                                                                                                                          |

Route-specific application failures such as `USER_NOT_FOUND`, `INVITE_NOT_FOUND`,
`BANNED_FROM_GUILD`, `NOT_A_GUILD_MEMBER`, `CANNOT_DM_USER`, and permission/channel
errors retain their API meanings. Voice and call failures never authorize local
SFU fallback: the declared guild/call authority remains binding.
