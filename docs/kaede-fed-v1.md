# kaede-fed/1

This is the versioned protocol contract for discovery, signed HTTP and hot-link
transport, durable DMs, guild synchronization, remote writes, authenticated
remote media, voice token brokering, occupancy, and call signaling.

## 1. Discovery and versions

`GET https://<domain>/.well-known/kaede/server` returns:

```json
{"server":"federation.example.com","versions":["1"],"capabilities":["guild-history-sync/1"]}
```

The server value is a public hostname, not a URL. Clients connect only to port
443 after resolving and pinning a public IP. Requests include
`X-Kaede-Version: 1`. No mutually supported version is `KAED_FED_UNSUPPORTED_VERSION`.
Capabilities are optional, bounded strings. `guild-history-sync/1` advertises the
permission-bound retained-history transfer in section 5.1. A peer MUST NOT call
that extension when it is absent.

`GET /_kaede/v1/keys` returns `verify_keys` and `old_verify_keys`, keyed by key ID.
Rotation adds the former current key to `old_verify_keys`. Senders MUST continue
advertising old keys while retained events may reference them; consumers refresh
cached sets at least hourly and mark a previously known key expired when it is
omitted from a later authoritative response.

## 2. HTTP request signatures

The signing object is UTF-8 JSON with keys sorted lexicographically, no insignificant
whitespace, no NaN/Infinity, and the exact members below:

```json
{"content_sha256":"<lowercase hex>","destination":"beta.example","method":"POST","origin":"alpha.example","request_target":"/_kaede/v1/inbox?a=1&z=2","ts":1783886400}
```

`request_target` contains the path and a query encoded as sorted key/value pairs.
`content_sha256` hashes the exact HTTP body bytes. Ed25519 signs the canonical
object. The authorization header identifies `origin`, `key`, and base64 signature.
The destination rejects timestamps outside ±300 seconds, mismatched destinations,
unknown/expired keys, invalid body hashes, and invalid signatures.

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
{"content_sha256":"24de1c4a19c43ad41b013f13dcd858c17b0daa7f33a53f19913e5b11366d1c2e","destination":"beta.example","method":"POST","origin":"alpha.example","request_target":"/_kaede/v1/inbox?a=hello+world&z=2","ts":1783886400}
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
  "actor": {"id": "123", "domain": "alpha.example"},
  "context": {"guild_id": "456", "guild_domain": "beta.example", "seq": "91"},
  "content": {},
  "signatures": {"alpha.example": {"ed25519:20260712": "<base64>"}}
}
```

The origin signs the envelope without `signatures`. A transport signer may only
send actors whose domain equals its verified origin. Violations are rejected as
`KAED_FED_AUTHOR_ORIGIN_MISMATCH`. Snowflakes and permission fields are decimal
strings. Envelopes from the future beyond the negotiated HTTP clock skew or older
than the receiver's configured event-retention window are rejected, preventing an
idempotency record that has aged out from enabling an indefinite replay. Unknown
optional content keys are ignored; missing required keys reject only that event.

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
| `guild.member.add` | The guild home announces a remote invite join. |
| `guild.update` | The guild home replaces mutable guild metadata. |
| `guild.channel.create`, `guild.channel.update`, `guild.channel.delete` | The guild home creates, replaces, or removes channel state. |
| `guild.role.create`, `guild.role.update`, `guild.role.delete` | The guild home creates, replaces, or removes role state. |
| `guild.overwrite.upsert` | The guild home replaces a channel permission overwrite. |
| `guild.member.update`, `guild.member.remove` | The guild home updates or removes membership state. |
| `guild.members.origin.remove` | The guild home atomically removes every member homed on one federated origin after an instance-wide sanction. |
| `guild.member.role.add`, `guild.member.role.remove` | The guild home changes one member-role assignment. |
| `guild.ban.add`, `guild.ban.remove` | The guild home changes its moderation ban set; an add may carry an absolute expiry. |
| `guild.access.revoked` | The guild home directly removes one user at the target user's origin; it remains valid when that origin has no member left and can no longer request snapshots. |
| `guild.instance_access.revoked` | The guild home directly removes all of the target origin's local members and instructs that origin to purge its cached guild data after an instance-wide ban. |
| `guild.resync.required` | A revision-bound guild-home marker replaces expired delivery rows and requires background gap-fill/full snapshot recovery. |
| `guild.message.create` | The guild home announces a message authored on the home instance. |
| `guild.message.update`, `guild.message.delete`, `guild.message.purge` | The guild home edits, tombstones, or author/time-range purges messages. |
| `guild.reaction.add`, `guild.reaction.remove` | The guild home changes one message reaction. |
| `guild.pin.add`, `guild.pin.remove` | The guild home changes one channel pin. |
| `guild.proxy.message.create` | A replica durably queues a remote member's write for the guild home. |
| `guild.message.committed` | The guild home announces the authoritative result of a proxied write. |
| `guild.event.redacted` | Signed placeholder that advances a peer past a channel event none of its members may inspect. |
| `message.send_rejected` | The guild home rejects a previously queued proxy write. |
| `media.delete` | The attachment origin invalidates every cached variant of one origin-owned attachment. |

Typing, presence, voice-state, and occupancy are not durable events. Peers that
advertise `presence/1` may send a signed `POST /_kaede/v1/presence` projection.
The subject domain must equal the signing origin, the recipient must already
know the subject through a shared guild, and the projection expires within 90
seconds unless refreshed. Receivers reject stale timestamps and publish the
state only to local subscribers of shared guilds. Media bytes are fetched on
demand; only deletion is durable.
Future durable names require an explicit registry revision.

## 4. Durable delivery

`POST /_kaede/v1/inbox` accepts at most 100 events and 1 MiB. It returns a result
per event (`accepted`, `duplicate`, `rejected`, or `retry`) rather than failing an
otherwise valid batch. `(origin,event_id)` is the idempotency key.

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

1. guild metadata, roles, and channels;
2. members in pages of at most 1,000;
3. permission overwrites and membership/role assignments needed by the replica.

The snapshot carries `snapshot_seq`; live application begins after it. Member pages
use a `(origin_domain,id)` keyset cursor plus an initial membership-time watermark,
so concurrent joins cannot shift an offset and duplicate or skip already-paged
members. Every continuation also carries the initial sequence and watermark; a
changed sequence returns `KAED_FED_SNAPSHOT_CHANGED` and the requester restarts.
A gap schedules bounded background recovery and calls
`GET /_kaede/v1/guilds/{id}/events?after_seq=<n>`. Gaps older than
retained events return HTTP 410 `KAED_FED_FULL_RESYNC` with
`snapshot_required=true` and the current `latest_seq`.

A guild home may relay profile-shaped member data, but it is authoritative only
for users homed on that same instance. A local user is always resolved from local
storage. A third-party user must already have been resolved from that user's own
origin; the relayed copy can identify the cached immutable handle but cannot create
or mutate the third party's profile. Nested user-signed profile proofs are reserved
for a future compatible extension.

Guild, channel, role, overwrite, member, moderation, message mutation, reaction,
and pin changes are granular sequenced events. Permission-sensitive changes carry `snapshot_required`; a
replica fetches a current permission-filtered snapshot before accepting the retried
event. Peers without channel visibility receive a signed sequence-only redaction.
An ownership transfer is represented by an authority-signed `guild.update` and
may name only an existing member homed on the guild-home instance. A remote
member leaves by sending an authenticated
`DELETE /_kaede/v1/guilds/{id}/members/@me` request whose user domain must match
the signing origin. The guild home removes membership before emitting the
sequenced member removal and target-specific access revocation. Guild owners
cannot leave through this operation.
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

## 7. Federation routes

Except for discovery at `/.well-known/kaede/server`, protocol routes are under
`/_kaede/v1`:

| Method and path | Purpose | Availability |
| --- | --- | --- |
| `GET /keys` | Current and historical verification keys | v1 |
| `POST /inbox` | Batched durable event delivery | v1 |
| `GET /users/lookup?handle=…` | Resolve a local user profile for a remote peer | v1 |
| `POST /dm/authorize` | Ask the non-authority recipient instance to enforce DM privacy | v1 |
| `POST /dm/open` | Authoritative direct-message open | v1 |
| `POST /invites/resolve` | Resolve a remote guild invite | v1 |
| `POST /guilds/{id}/join` | Consume an invite and obtain the authoritative guild identity | v1 |
| `GET /guilds/{id}/snapshot` | Paged structural initial/full synchronization | v1 |
| `GET /guilds/{id}/events` | Sequence gap fill for registered guild events | v1 |
| `POST /guilds/{id}/history-exports` | Create or resume a permission-bound history grant | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}` | Read the bound manifest | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}/channels/{channel}?after=…` | Read one bounded oldest-first page | `guild-history-sync/1` |
| `GET /guilds/{id}/history-exports/{export}/delta?after_seq=…` | Reconcile retained mutations after the baseline | `guild-history-sync/1` |
| `POST /guilds/{id}/history-exports/{export}/complete` | Idempotently acknowledge a merged export | `guild-history-sync/1` |
| `POST /guilds/{id}/proxy` | Idempotent remote guild message write | v1 |
| `GET /link` | Signed `kaede-fed.1` hot-link WebSocket upgrade | v1 |
| `POST /voice/token` | Home-SFU guild token broker | v1 |
| `POST /voice/dm-token` | Caller-SFU DM call token broker | v1 |
| `POST /voice/state` | Droppable guild occupancy snapshot/heartbeat | v1 |
| `POST /calls` | Droppable two-party DM call signaling | v1 |
| `GET /media/{attachment}/{variant}` | Signed remote-media stream | v1 |

On a successful `POST /guilds/{id}/proxy`, the guild home returns the rendered
message, its guild sequence, and the complete signed
`guild.message.committed` envelope. The requester must verify that envelope and
require its guild identity, sequence, and message to match the outer response
before applying the authoritative result. Idempotent nonce replays return the
original stored envelope rather than synthesizing an unsigned result.

Payload URLs are forbidden. Media and federation URLs are reconstructed from the
validated origin and fixed paths. Federation HTTP clients reject redirects;
operators must publish the final canonical endpoint directly.

The media path accepts only `original`, `thumbnail_128`, `thumbnail_512`,
`thumbnail_1024`, or `poster`. The authenticated peer receives bytes only when it
has a DM participant on that origin or at least one guild member with visibility
for the attachment's channel. Origins expose only locally owned, clean,
message-bound media. Consumers bound the response by their configured attachment
limit, sniff its bytes, reject active/executable content, scan it through local
ClamAV, and only then write the private remote cache. Cache entries default to a
30-day TTL and a 20 GiB LRU ceiling. A `media.delete` envelope must name the
signing origin in `content.origin_domain`; receivers purge all variants for
`content.attachment_id` and cannot use the event to delete another origin's data.

Remote profiles carry the mutable display name, avatar, banner, biography,
custom status, and a monotonically increasing profile version. Equal-version
conflicts are rejected and older versions never overwrite newer cached state.
Remote profile caches are fresh for five minutes. A fresh hit performs no
network request; a stale hit returns immediately and coalesces an asynchronous
authoritative refresh for that handle. Missing profiles are cached for one
minute. Cache misses are bounded independently to 30 lookups per requester and
120 per target domain per minute; background refreshes also consume the target
domain budget.

## 8. Voice and calls

The guild home LiveKit instance owns room `g.<guild_id>.<channel_id>`. A remote
user's instance verifies cached `CONNECT`, then calls `POST /voice/token`; the home
rechecks permissions and signs a 15-minute token. Embedded generation values make
stale grants rejectable. Existing sessions may survive an S2S outage, but new joins
return `KAED_VOICE_HOME_UNREACHABLE` when the home cannot be reached.

Droppable `guild.voice_state.*` frames are represented by signed
`POST /voice/state` snapshots. The payload contains the guild ID, canonical room,
Unix `generated_at`, and bounded participant list. The authenticated origin is the
guild home. Receivers atomically ignore a timestamp older than their current
heartbeat. A home sends the full snapshot every 30 seconds; after 75 seconds by
default a client renders occupancy unknown, never asserted current.

DM calls use `dm.call.create`, `ring`, `accept`, `decline`, and `end` semantics on
signed `POST /calls` requests. The payload binds call, channel, authority, actor,
action, and creation time. Actor domain must equal the authenticated origin; the
receiver rechecks DM participation. Rooms are `d.<channel_id>.<call_id>` on the
caller's instance. State is TTL-bounded and non-durable. A non-caller must accept
before `POST /voice/dm-token` can mint a grant.

## 9. Blocking and failure codes

Federation can be `open` or `allowlist`. A local silence denies inbound and
outbound guild snapshots, guild events, and remote-write proxy surfaces for the
blocked instance while still permitting DM federation. A local suspend rejects
all federation exchange. Both policies include subdomains when configured. CSV
exchange uses Mastodon-compatible domain block fields.

Implemented transport and durable-delivery failures are registered below. HTTP
errors contain top-level `code`, a safe `message`, `trace_id`, and optional
`retry_after_ms`. Per-event failures appear in an inbox result instead of changing
the batch's HTTP status.

| Code | Surface | Meaning |
| --- | --- | --- |
| `KAED_FED_SIGNATURE_REQUIRED` | HTTP 401 | The request has no parseable Kaede authorization header. |
| `KAED_FED_BAD_SIGNATURE` | HTTP 401 | The request signature, body hash, key syntax, or signature length is invalid. |
| `KAED_FED_CLOCK_SKEW` | HTTP 401 | The signed timestamp is outside the configured window. |
| `KAED_FED_UNKNOWN_KEY` | HTTP 401 | Discovery/rotation did not yield the signing key, or the key is expired. |
| `KAED_FED_KEY_REFRESH_RATE_LIMITED` | HTTP 429 | Unknown-key discovery exceeded the pre-authentication per-origin refresh quota. |
| `KAED_FED_UNSUPPORTED_VERSION` | HTTP 400 | `X-Kaede-Version` is not `1`. |
| `KAED_FED_HOP_LIMIT` | HTTP 400/508 | The hop header is malformed or outside zero through five. |
| `KAED_FED_NOT_ALLOWLISTED` | HTTP 403 | Local allowlist policy has not approved the origin. |
| `KAED_FED_INSTANCE_SILENCED` | HTTP 403 | A local silence rejects this guild snapshot, event, or proxy surface while leaving DM federation permitted. |
| `KAED_FED_INSTANCE_SUSPENDED` | HTTP 403 | A local suspend block rejects exchange with the origin. |
| `KAED_RATE_LIMITED` | HTTP 429 | The per-origin token bucket is exhausted. |
| `KAED_FED_BATCH_TOO_LARGE` | HTTP 413 | The signed request body exceeds 1 MiB. |
| `KAED_FED_INVALID_CONTENT_LENGTH` | HTTP 400 | `Content-Length` is not a valid non-negative decimal length. |
| `KAED_FED_INVALID_BATCH` | HTTP 400 | The inbox body is not a JSON object containing an event list. |
| `KAED_FED_INVALID_BATCH_SIZE` | HTTP 400 | The inbox event count is outside one through 100. |
| `KAED_FED_INVALID_EVENT` | Per event | The envelope does not satisfy the registered structural bounds. |
| `KAED_FED_BAD_EVENT_SIGNATURE` | Per event | No retained origin key verifies the envelope. |
| `KAED_FED_AUTHOR_ORIGIN_MISMATCH` | HTTP 403 or per event | The authenticated origin does not own the asserted actor. |
| `KAED_FED_EVENT_ID_CONFLICT` | Per event | A global event ID already names different signed content. |
| `KAED_FED_EVENT_REJECTED` | Per event | A registered event failed its authority, identity, privacy, permission, or state checks. |
| `KAED_FED_EVENT_RETRY` | Per-event retry | The receiver cannot yet prove a terminal inbox or commit state; the sender retries the same event ID. |
| `KAED_FED_EVENT_TIMESTAMP_INVALID` | Per event | The durable envelope is too far in the future or older than the receiver's retention window. |
| `KAED_FED_INVALID_SNAPSHOT_CURSOR` | HTTP 400 | A guild member continuation cursor is incomplete or malformed. |
| `KAED_FED_SNAPSHOT_CHANGED` | HTTP 409 | A paged guild snapshot changed and must be restarted. |
| `KAED_FED_FULL_RESYNC` | HTTP 410 | Retained guild events cannot fill the requested sequence gap. |
| `KAED_FED_HISTORY_NOT_FOUND` | HTTP 404 | The export does not exist for the authenticated requesting origin. |
| `KAED_FED_HISTORY_FORBIDDEN` | HTTP 403 | The origin tried to request history for a user it does not own. |
| `KAED_FED_HISTORY_EXPIRED` | HTTP 410 | The short-lived export grant expired. |
| `KAED_FED_HISTORY_REVOKED` | HTTP 410 | Membership or channel authorization was revoked. |
| `KAED_FED_HISTORY_GRANT_STALE` | HTTP 409 | A bound membership, permission, or policy generation changed; restart negotiation. |
| `KAED_FED_HISTORY_CURSOR_INVALID` | HTTP 400 | A page or delta cursor is outside its grant. |
| `KAED_FED_RESYNC_RETRY` | Per-event retry | A valid resync marker was retained but its callback gap-fill/snapshot could not yet complete. |
| `KAED_FED_DELIVERY_EXPIRED` | Local delivery state | A destination pointer exceeded its delivery window; guild peers gap-fill and affected pending DM projections become failed. |
| `KAED_DM_WRONG_AUTHORITY` | HTTP 409 | The receiver is not the deterministic DM authority. |
| `KAED_DM_INVALID_PARTICIPANTS` | HTTP 400 | DM authorization does not contain the authenticated remote and local recipient. |
| `KAED_DM_OPEN_REJECTED` | `dm.open.rejected` content | The DM authority rejected an open request without a more specific stable application code. |
| `KAED_GUILD_INVALID_MENTION` | HTTP 400 | A proxied message mentions an identity outside the guild. |
| `KAED_GUILD_NONCE_STATE_CONFLICT` | HTTP 409 | A known proxy nonce has no matching authoritative guild event. |
| `KAED_VOICE_INVALID_ROOM` | HTTP 400 | An occupancy payload does not bind its declared guild to a canonical guild room. |
| `KAED_VOICE_INVALID_STATE` | HTTP 400 | Occupancy contains a malformed participant or mismatched room/identity. |
| `VOICE_HOME_UNREACHABLE` | HTTP 503 | A new guild/call join cannot reach or validate the authoritative SFU broker; clients retry after the supplied delay. |
| `CALL_HOME_UNREACHABLE` | HTTP 503 | A call action cannot reach its caller-instance authority. |

Route-specific application failures such as `USER_NOT_FOUND`, `INVITE_NOT_FOUND`,
`BANNED_FROM_GUILD`, `NOT_A_GUILD_MEMBER`, `CANNOT_DM_USER`, and permission/channel
errors retain their API meanings. Voice and call failures never authorize local
SFU fallback: the declared guild/call authority remains binding.
