# Kaede Chat architecture

This guide covers data ownership and the server/client design. For wire
formats, see the [federation contract](kaede-fed-v1.md) and the
[bot API reference](bots-and-automations.md).

## Product boundary

Kaede Chat is a self-hosted chat system. Identities are immutable
`username@domain` handles, and a user's home instance owns authentication and
identity. Both participants' instances retain a direct message. A guild has
exactly one home instance, which orders its changes and replicates them to
instances with participating members.

The v1 client is a SvelteKit static SPA. FastAPI serves REST, federation, media
authorization, and LiveKit webhooks. A separate FastAPI gateway service owns
long-lived WebSockets. PostgreSQL holds durable state, and Dragonfly holds
ephemeral state and carries tasks. Media lives in an S3-compatible store
(Garage by default), and LiveKit runs media sessions. The Android and iOS
clients use the same home-instance API and gateway as web and desktop. They
never call peer federation endpoints directly.

The v1 production scope covers identity and authentication, single-instance
chat, federation, media and webhooks, voice/video/screen sharing, mobile
clients, task tracker channels, message search, group DMs, and the operational
controls for them. Public, private, and announcement threads and forum posts
share the channel and message APIs. Compressed gateway encoding isn't
implemented. MLS 1.0 messaging, encrypted attachments, recovery, and LiveKit
frame encryption ship enabled by default.

## Binding conventions

- Snowflakes use 10 leased worker bits and 12 sequence bits after a
  2026-01-01 UTC epoch. PostgreSQL `BIGINT` is signed, so v1 reserves the sign
  bit and allows 41 positive timestamp bits. Generation fails closed before
  that range runs out; using a 42nd bit would need a new storage and API
  version. JSON encodes identifiers as decimal strings.
- Every federated row is identified by `(id, origin_domain)`. References carry
  both parts, and Dragonfly keys include the domain.
- `instances.is_self` has a unique partial index. Internal `is_local` values
  bind to it through composite foreign keys, so migrations enforce locality
  without hard-coding a deployment domain.
- Authentication tokens are opaque and prefix-scannable (`kc1_at_`,
  `kc1_rt_`, `kc1_mfa_`), never JWTs. LiveKit grants use LiveKit's own
  room-scoped HS256 JWTs and aren't Kaede credentials.
- Python owns gateway opcodes, close codes, event names, and permissions. The
  generated TypeScript is committed and checked for drift.

## Runtime topology

Host nginx owns ports 80/443 and terminates TLS. It forwards the main virtual
host, plus the media host when Garage is selected, to a Caddy edge on a
loopback-only high port. The setup wizard can render the host nginx
configuration.

The edge sends REST, media authorization, federation, and well-known requests
to the API, and `/gateway` to gateway replicas. Everything else goes to an
atomically switched SPA release. The previous release is kept so loaded pages
can still fetch their hashed chunks during a rollout. `/livekit` returns 404
unless `KAEDE_VOICE_ENABLED=true`, in which case the edge proxies it to the
host-networked LiveKit service.

Garage objects are reachable only through presigned requests on
`media.<domain>`. Before a read reaches Garage, the edge asks the API to
recheck the object against current safety state. It returns 404 for any
`evidence/` path and marks reads `private, no-store`. External S3 mode
redirects to signed URLs at the provider's HTTPS origin, so those reads skip
the edge check. Kubernetes NetworkPolicy keeps PostgreSQL and
password-protected Dragonfly private and limits outbound traffic to the
services that need it.

The SPA carries a SvelteKit-generated hash CSP for its inline bootstrap. The
edge adds header-only protections such as `frame-ancestors` but no second
`script-src` that would override the hash. The frontend check hashes every
inline script in the built fallback and rejects CSP drift.

### Worker ID leases

API processes lease worker IDs from Dragonfly instead of using a static ID.
A compare-and-expire Lua script renews the process-specific lease on every
mint and on each heartbeat. A newly unclaimed slot waits in a non-minting
quarantine for one full 60-second lease lifetime, so a Dragonfly flush can't
put old and new generators on the same slot. The cost is a cold start of about
60 seconds and one Dragonfly round trip per mint. A process that loses its
lease, or can't prove it, stops minting. The production health check uses
readiness with a quarantine-aware start period, and Caddy waits for it.
Dragonfly snapshotting keeps a total session loss from causing an identify
stampede.

### Process secrets

Gateway processes get a separate, non-authoritative configuration key instead
of `KAEDE_SECRET_KEY`, so a gateway compromise can't decrypt the federation
signing key or TOTP material. In production Kubernetes the gateway also has no
administrator credential. Only the email worker and the configuration
preflight receive email delivery credentials. The scheduler gets just the
Dragonfly broker URL and log level.

### Load bounds

Host nginx limits concurrent and burst upgrades per source address on
`/gateway`. Each gateway process also admits at most 128 unauthenticated
connections, checked before the WebSocket accept. A slot is released once a
valid IDENTIFY or RESUME passes Dragonfly admission, so slow handshakes can't
exhaust tasks or file descriptors.

Expensive client mutations pass through per-user, domain-qualified Dragonfly
token buckets with consistent retry headers. Gateway IDENTIFY has separate
global and per-source buckets. After a Dragonfly flush, IDENTIFY stays closed
until an elected worker finishes the durable cache warmup. A sentinel in the
five-minute Dragonfly snapshots lets a runtime guard detect a later flush and
repeat warmup.

Each API process runs at most one Argon2id hash or verification at a time, off
the event loop through an AnyIO capacity limiter, so password work can't
exhaust memory or stall other requests.

### Background work

Taskiq workers over Redis Streams handle email, federation draining, media
scanning and derivation, and periodic cleanup: account and federation
retention, unverified accounts, partition creation, message cursor and mention
projections, upload orphans, remote-cache eviction, media retention, and
deletion purge. Two safety jobs run every minute, one copying buffered
child-safety evidence into storage and one retrying confirmed removals. A daily
job disposes of evidence for closed cases past retention with no active hold.
Gateway processes run cache warmup at startup and whenever the cache stops
being ready.

A leader-elected API coordinator runs the fast loops: voice permission
enforcement every 5 seconds, federation heartbeats every 30 seconds, and
LiveKit reconciliation every 60 seconds.

One-time-token email goes through a PostgreSQL outbox. The token and the
AES-GCM-encrypted recipient, subject, and body commit together. The Redis task
is only a wake signal and never carries the address or link. Workers claim due
rows with `FOR UPDATE SKIP LOCKED`, reclaim abandoned claims after ten minutes,
and retry with bounded backoff until the token expires. A per-minute sweep
covers lost wake-ups. Delivery metadata is kept at most seven days. Delivery is
at-least-once, so a crash after the provider accepts a message can send a
duplicate, but the link still works only once.

### Observability

The internal metrics endpoint works without a Docker socket. It derives gateway
leases and task counters from Dragonfly and federation queues from PostgreSQL.
Prometheus rules and the Grafana dashboard stay on internal or loopback
interfaces. The release smoke gate checks shared-stream fanout and the
one-envelope federation amplification model against real disposable services.

## Data ownership

Users, guilds, members, roles, channels, overwrites, messages, attachments,
reactions, pins, emojis, and DM conversations can be replicated. Federation
carries profiles, structural guild snapshots, the guild mutation stream, and
direct and group DM conversations and messages. Media federation shares
attachment metadata and authenticated access; objects stay at their origin.
Voice federation adds call and occupancy state.

Auth, settings, per-user relationship rows, audit, federation delivery state,
remote media cache, instance blocks, and storage accounting stay local. Signed
relationship events sync the two independently owned friendship rows, and no
remote instance keeps friendship privileges after a local removal or block.

Task tracker boards are local guild-authority data that cascade with their
type-17 channel. Replicas serve a bounded cache built from signed snapshots,
and human writes route to the guild home. See
[Task tracker channels](task-tracker.md).

Messages use monthly range partitions keyed by snowflake. History reads include
a lower ID floor so PostgreSQL can prune partitions without a cursor.
Presence, typing, and live voice occupancy never enter SQL; only server
mute/deafen flags persist.

## Federation runtime

Discovery and key retrieval use fixed `kaede-fed/1` paths. All outbound
federation and remote-media traffic goes through one DNS-pinning SSRF guard:
public addresses only, port 443, and the original TLS server name. Private,
loopback, link-local, reserved, multicast, and metadata ranges are rejected.
Development-only `.localhost` overrides let the Alpha/Beta topology use
internal service names.

Every server-to-server mutation has an HTTP signature and a separately signed
event envelope. The inbox authenticates transport before parsing, checks that
the actor belongs to the sending origin, and deduplicates on
`(origin,event_id)`. Per-destination drainers batch up to 100 events and
1 MiB, back off with jitter, open a circuit after 24 hours, and survive
restarts. A pooled, signed `kaede-fed.1` WebSocket carries the same batches.
It reconnects to reauthenticate and falls back to the HTTP inbox on failure.

The lower participant domain mints a two-party DM. A group DM's creator home
mints it and stays authoritative (see [group DMs](#group-direct-messages)). A
DM recipient applies its privacy policy and rechecks it on ingestion.

Guild homes mint message snowflakes and assign event sequences. Replicas apply
signed structural snapshots and ordered guild, channel, role, member,
moderation, message, reaction, and pin events. Permission-sensitive changes
fence on a filtered snapshot, and hidden channel changes use signed
redactions. On a sequence gap, a replica pulls the retained event log before
continuing. Offline remote writes stay visibly queued until committed or
rejected.

## Identity and sessions

Email and password credentials stay on the home instance. Email-enabled
deployments offer verification and password recovery. Email-disabled ones use
username and password only and can't send recovery links. Passwords use
Argon2id with 64 MiB memory, three iterations, and one lane.

Browser sessions use HttpOnly `kc_access` and `kc_refresh` cookies, and
cookie-based writes require `X-Kaede-Client: web`. Native clients get tokens in
the response body and send `Authorization: Bearer`, which wins over cookies.
Refresh tokens rotate. Reusing a superseded token after the grace window
(60 seconds by default) revokes that session's access tokens. MFA uses TOTP
and single-use recovery codes; changing it requires reauthentication and
revokes other sessions. The console email backend is for development only.

## Chat and permissions

Creating a guild also creates the owner membership, `@everyone`, and `general`.
Permission checks apply ownership, roles, administrator access,
everyone/role/member channel overrides, implicit channel restrictions, and
timeouts, in that order. Role and moderation actions respect hierarchy and
protect the owner. Ownership can transfer only to a member homed on the
guild's instance.

Messages support client nonces for retries, cursor-paged history, soft
deletion, reactions, and pins. Pins are newest-first, capped at 250 per
channel, in pages of 50. Message writes queue batched unread and mention
projections. Permission cache keys use the guild generation and member
version, and message creation doesn't invalidate them.

Human gateway sessions resume from shared per-topic streams. Each connection
has a bounded queue, and overflow invalidates the session rather than dropping
events. The bot gateway reconnects with a fresh Identify and saved topic
cursors; see [the bot reference](bots-and-automations.md#bot-gateway).

Forums hold child threads with starter messages, tags, sorting, pagination, and
archive/lock controls. Encrypted child threads have their own MLS groups, and a
parent's encryption doesn't admit a device to its children.

### Read positions

Web, desktop, Android, and iOS open an unread channel or DM around the saved
read cursor, or at the first retained unread message if there is none.
**Jump to where you left off** returns to the cursor as it was when the visit
began, where the new-message divider stays. **Jump to latest** (**Jump to
present** on mobile) loads the newest page directly, and mobile's **Load newer
messages** pages forward. In the message area, Shift+PageUp jumps to the saved
position and Ctrl+End to latest. Jumps keep the composer draft, and a deleted
saved message falls back to the nearest retained history.

Only messages actually on screen acknowledge: the document must be visible
and focused (after a 250 ms scroll debounce), or on mobile the chat screen
must be resumed and visible. Background apps, covered screens, loading
history, and open inbox dialogs don't count. The bottom of an older history
page never acknowledges unloaded newer messages. The cursor never moves
backward from a stale device, and a partial acknowledgement keeps later
mentions unread.

**Mark unread** in the message menu moves the cursor to the previous message,
increments `read_version`, and pauses automatic acknowledgement until the user
navigates or jumps. Each automatic acknowledgement carries the version it was
queued under, and a stale one gets 409 and a state refresh instead of undoing
the rewind. **Mark as read** works per conversation, server, or globally, and
acknowledges a snapshot of each conversation's latest message; anything newer
stays unread.

The inbox (server rail on web, conversation browser on mobile) lists unread
conversations and recent mentions, with server and mention-type filters. Muted
servers are hidden from Unreads unless **Show muted servers** is on. Mention
dismissal is stored per account, separate from the cursor. Mentions are
paginated and limited to seven days. Inbox metadata never contains decrypted
message bodies, and opening an item goes through the normal authorized history
and decryption path.

### Group direct messages

Group DMs contain 3–10 people. Any member may rename the group or add an
existing friend, and both homes must confirm the friendship. Only the owner may
remove another member. Leaving transfers ownership to the earliest remaining
member, and the conversation closes when the last member leaves. Removal ends
access immediately but doesn't erase messages already delivered.

The creator's home owns the group identity, membership, name, and a monotonic
state version. Other homes route mutations there and accept only newer signed
full-state updates; equal-version conflicts are rejected. Messages and calls
travel directly between members' homes, which verify current membership.
History and attachments stay requester-bound. One active call may ring the
current members, and call actions and media tokens recheck membership. Bots
can't start or join group-DM calls.

The client routes use composite channel and user references:

```text
POST   /api/v1/users/@me/channels/group
PATCH  /api/v1/users/@me/channels/{channel_ref}/group
POST   /api/v1/users/@me/channels/{channel_ref}/group/recipients
DELETE /api/v1/users/@me/channels/{channel_ref}/group/recipients/{user_ref}
POST   /api/v1/users/@me/channels/{channel_ref}/group/leave
```

Creation takes 2–9 unique friend handles and an optional name. Group channels
have `conversation_type: "group"`, `owner_id`, `owner_domain`, and `recipients`
(the other current members). Compare both owner fields. The usual message,
search, attachment, reaction, and call APIs work on the group channel.

## Message search

PostgreSQL stays authoritative for messages and permissions. A durable SQL
queue projects eligible plaintext messages into a private Meilisearch index on
the deployment network. Message, attachment, reaction, pin, and
channel-encryption changes enqueue replacement or deletion. Meilisearch is
treated as an untrusted, rebuildable candidate source: every hit is loaded from
SQL and its channel access rechecked before it reaches a client. Search
outages never block delivery. Rebuild a replaced index online with
`make search-rebuild`.

Federated search never shares a Meilisearch cluster or key. The user's home
sends a signed, bounded query to the authoritative guild or DM instance, but
only if it advertises `message-search/1`. The authority applies its own
permissions and returns a minimal result set, which the home re-authorizes
against local channel state. Unreachable authorities are reported, not papered
over with cached results.

A DM search scoped to one channel goes to that conversation's authority.
Account-wide DM search fans out to the user's active conversation authorities
concurrently, each with its own signed authorization and timeout, and merges
their pagination deterministically. A short-lived server-side cursor bound to
the actor and query keeps the client token small. Fan-out is capped at 256
authorities and fails if that is exceeded.

`e2ee` channels are excluded at indexing, query, SQL hydration, and federation.
The UI explains why search is off. Encrypted DMs offer on-device search instead
([E2EE](e2ee.md#product-disclosures)). Server plaintext indexing is never
re-enabled for encrypted rooms.

## Optional end-to-end encryption

Every message path, including local, gateway, federation, retained history, and
DM replication, accepts a bounded, versioned `e2ee` envelope in place of
plaintext. A message has one or the other, and deletion discards both. Version
2 is the Kaede MLS 1.0 envelope. Version 1 is legacy opaque transport and is
never presented as MLS. Instances advertise `e2ee-mls/1` and `e2ee-media/1`.
Federation's instance Ed25519 signatures authenticate servers only; clients
verify MLS credentials and authenticated data themselves.

Encryption must never be negotiated from an unauthenticated boolean or from
the mere presence of an envelope. Encrypted rooms give up server search, link
previews, content moderation, and plaintext mention extraction. Operators can
block new activations with `KAEDE_E2EE_ACTIVATION_ENABLED=false`. The
[E2EE protocol](e2ee.md) covers identities, room policy, the envelope's
bindings, files, calls, and what metadata servers still see.

## Media storage and processing

Three private S3 buckets hold attachments, derivatives, and remote cache.
Garage creates them in bundled deployments; external storage needs the bucket
and CORS setup in the [setup guide](deployment-wizard.md). Clients get scoped
presigned URLs, never object-store credentials.

An upload ticket reserves quota and yields a 15-minute PUT signed for the exact
length and media type. Committing a message or asset verifies the object and
consumes the single-use ticket. Workers ignore the declared MIME type, check
file signatures, scan plaintext with ClamAV, and produce WebP image variants,
blurhash/phash metadata, and video posters. Only the exact scanned bytes are
promoted to a clean, content-addressed key. A still-valid upload URL can
overwrite its staging key but not the clean object. Staging cleanup waits more
than 16 minutes past PUT expiry, so third-party upload clients must also cap
requests at 15 minutes. Infected and deleted objects are removed and their
quota released.

Optional PhotoDNA checks plaintext images before promotion. On a match,
delivery is blocked, the scanned bytes are encrypted and verified under
`evidence/` in the attachments bucket, and staff confirm removal or dismiss the
finding. If storage is down, the encrypted bytes wait in PostgreSQL for the
per-minute evidence job. Dismissal sends held content through normal
processing. Tombstones and digest revocation start only on confirmation, and a
confirmed removal that can't finish is retried every minute. Decoder or local
policy rejections remove the upload without blocking its digest. E2EE
attachments are ciphertext and skip scanning, and clients must make that clear
before encryption is turned on.

Message media reads recheck channel access, then return a `private, no-store`
redirect to a 60-second signed link. Profile, guild, and emoji assets are
addressed by content hash, and their redirects may be cached publicly for
60 seconds and point to a five-minute link. Remote reads use one fixed signed
federation path, are authorized by the origin, and are rescanned locally before
going into a bounded S3 cache. LRU/TTL jobs and origin-signed `media.delete`
tombstones evict cached copies. Message deletion queues cleanup and quota
release, and storage failures stay retryable. The [operator guide](operator.md)
covers limits and storage repair.

Incoming webhook secrets are random `kwh_` tokens, shown only at creation or
rotation and stored as SHA-256 digests. Rotation or revocation invalidates
them. Execution is home-only and rate-limited. Messages show the webhook as
sender in local and federated payloads; the creator is kept only as the audit
principal. See [bot recipes](bot-api-quickstart.md#sdk-recipes).

### Attachment spoilers

Clients mark spoilers with a `SPOILER_` filename prefix and must hide the media
and download controls until revealed, including in embeds and forwards. Forum
thumbnails omit spoiler images. This is presentation, not access control.
Older clients don't hide spoilers, so deploy backend and client updates
together. Encrypted files keep the prefix in the private manifest filename;
their public upload name stays `encrypted-file`.

`PATCH /api/v1/attachments/{id}/spoiler` accepts `{"spoiler": true}` or `false`
for the uploader's queued plaintext message attachment, before finalization or
binding. It locks against finalization and rejects encrypted uploads and
non-message assets. Clients must block Send while the update is pending or
failed. Mobile applies the prefix before uploading on Send.

## Security invariants

- Bootstrap creates one durable self identity, and the configured domain must
  always match it. The Ed25519 private key is AES-256-GCM encrypted with the
  instance secret.
- The home instance rechecks permissions for every federated write.
- A verified federation origin may act only for its own users.
- Key rotation keeps old verification keys, and envelopes are signed
  separately, so stored events stay auditable.
- Originals are private and uploads are exact-length. No plaintext bytes,
  local or remote, are served before local magic validation and malware
  scanning. E2EE ciphertext is served unscanned.
