# Kaede Chat architecture

Status: normative for `v1` · Updated: 2026-07-20

## Product boundary

Kaede Chat is a self-hosted chat system whose identities are immutable
`username@domain` handles. A user's home instance owns authentication and user
identity. Direct messages are retained by both participants' instances. A guild
has exactly one home instance, which orders its changes and replicates them to
instances with participating members.

The v1 client is a SvelteKit static SPA. FastAPI serves REST, federation, media
authorization, and LiveKit webhooks. A separate FastAPI gateway service owns
long-lived WebSockets. PostgreSQL is durable state, Dragonfly is ephemeral state
and task transport, an S3-compatible store (Garage by default) stores media, and
LiveKit provides media sessions.

## Binding conventions

- Snowflakes use the specified 10 leased worker bits and 12 sequence bits after a
  2026-01-01 UTC epoch. PostgreSQL `BIGINT` is signed, so v1 deliberately reserves
  its sign bit and permits 41 positive timestamp bits; generation fails closed
  before that range is exhausted. JSON always encodes identifiers as decimal
  strings. Using the nominal 42nd timestamp bit would require a future storage and
  API version rather than producing negative identifiers.
- Every federated row is identified by `(id, origin_domain)`. References carry
  both components and Dragonfly keys include the domain.
- `instances.is_self` has a unique partial index. Internal `is_local` values are
  bound to it with composite foreign keys, allowing reusable migrations to enforce
  locality without embedding a deployment domain in a `CHECK` constraint.
- Application authentication tokens are opaque and prefix-scannable (`kc1_at_`,
  `kc1_rt_`, `kc1_mfa_`), never JWTs. LiveKit connection grants necessarily use
  its room-scoped HS256 JWT format and are not Kaede authentication credentials.
- Python owns gateway opcodes, close codes, event names, and permissions. Generated
  TypeScript is committed and checked for drift.

## Runtime topology

Host nginx owns public ports 80/443 and terminates TLS. It forwards the main
virtual host, plus the media virtual host when Garage is selected, to an internal
edge on a loopback-only high port. Caddy provides this internal routing layer.
The setup wizard can separately render the TLS proxy configuration for the
host-level nginx service. The internal edge sends REST, media authorization, federation, and
well-known requests to the API; `/gateway` to gateway replicas; and everything
else to an atomically switched static-SPA
release. `/livekit` returns 404 unless the operator explicitly sets
`KAEDE_VOICE_ENABLED=true`; the `voice` profile additionally refuses to start
unless that switch is true, after which the edge proxies the route through the
Docker host gateway to the host-networked SFU. The immediately previous SPA
release is retained so already-loaded pages can still fetch their hashed chunks
during a rollout. Garage objects are reachable only through presigned requests
on `media.<domain>`; external S3 mode redirects to signed URLs at the configured
HTTPS provider origin. Edge and data bridges are internal, PostgreSQL and
password-protected Dragonfly remain on the
data bridge, and only services that require outbound federation/update traffic
join the egress bridge.

The static frontend carries a SvelteKit-generated hash CSP for its
build-specific inline bootstrap. The edge keeps header-only protections such as
`frame-ancestors` without imposing a second `script-src` policy that would
override the generated hash. The frontend check hashes every inline script in
the built fallback and rejects CSP drift.

API processes lease worker IDs from Dragonfly rather than receiving a static ID.
Lease ownership is process-specific and renewed by a compare-and-expire Lua script
on every mint as well as by the heartbeat. A newly unclaimed worker slot remains
in a non-minting quarantine for one full 60-second former-lease lifetime before
activation, so a Dragonfly flush cannot overlap old and replacement generators.
Consequently, a cold API process can take about 60 seconds to become ready and
each mint incurs one Dragonfly round trip. Losing or being unable to prove the
lease prevents further ID minting. Dragonfly snapshotting prevents a total
session/cache loss from causing an identify stampede.
The production API health check therefore uses readiness with a quarantine-aware
start period, and Caddy does not enter service until that readiness check passes.

Gateway processes receive a distinct, non-authoritative configuration key rather
than `KAEDE_SECRET_KEY`; they therefore cannot decrypt the instance federation
signing key or encrypted TOTP material after a gateway-only compromise. Gateway,
API, scheduler, and migration processes receive no email delivery credentials;
only the email-capable worker and configuration preflight receive them. The
scheduler receives only the Dragonfly task-broker URL and log level, not the
master key, database URL, or proxy secret. The gateway also receives no
administrator credential in the production Compose topology.

Incomplete WebSocket handshakes are bounded twice: host nginx limits concurrent
and burst upgrades per source address only on the exact `/gateway` route, and
each gateway process admits at most 128 pre-authentication connections. The
process-local admission is fail-fast, precedes the WebSocket accept/HELLO, and is
released as soon as a structurally valid IDENTIFY or RESUME passes the existing
Dragonfly admission check. This keeps slow handshakes from consuming an
unbounded number of gateway tasks or file descriptors while leaving established
authenticated sockets outside the pre-authentication pool.

Argon2id retains its 64 MiB memory cost, but API routes execute it off the event
loop through a dedicated AnyIO capacity limiter. Each API process runs at most
one password hash or verification at once, bounding Argon2 memory amplification
without allowing password work to stall unrelated async requests in that
process. Synchronous primitives remain available to offline verification tools.

Taskiq Redis Streams workers run email delivery, federation draining,
account/federation retention, unverified-account cleanup, partition creation,
message cursor/mention projections, media scan/derivation, upload-orphan cleanup,
remote-cache eviction, media retention, deletion purge, and cache warmup. A
leader-elected API coordinator performs five-second voice permission enforcement,
30-second federation heartbeats, and 60-second LiveKit reconciliation. Scheduled
Taskiq jobs remain separate from workers.

One-time-token email uses a transactional PostgreSQL outbox. Token creation and
an AES-GCM-encrypted recipient/subject/body commit together; the Redis task carries
only a wake signal, never the address or bearer link. Workers claim due rows with
`FOR UPDATE SKIP LOCKED`, reclaim abandoned claims after ten minutes, and retry
with bounded backoff until the associated token expires. A minute scheduler sweep
guarantees eventual attempts when the post-commit Redis wake fails. Terminal
delivery metadata is kept no longer than seven days and can disappear earlier
when its expired one-time-token row is pruned. Provider delivery is at-least-once:
a process failure after an SMTP/API provider accepts a message but before the SQL
success update can produce a duplicate, because those providers do not share the
database transaction. Links remain one-time credentials, so a duplicate does not
create a second usable token.

## Federation runtime

Federation discovery and key retrieval use fixed `kaede-fed/1` paths. Production
DNS results are checked for private, loopback, link-local, reserved, multicast,
and metadata ranges, and the HTTP transport connects to the checked address while
retaining the original TLS server name. Development-only `.localhost` overrides
allow the isolated Alpha/Beta topology to use internal service names.

Every implemented server-to-server mutation has a canonical HTTP signature and
independently signed event envelope. The inbox authenticates the transport before
parsing events, enforces actor-origin ownership, and deduplicates
`(origin,event_id)`. Per-destination
outbox drainers batch up to 100 events and 1 MiB, use bounded jittered backoff,
open a circuit after 24 hours, and retain write state across restarts. A pooled,
signed `kaede-fed.1` WebSocket carries the same batches with correlated results;
it reconnects for periodic reauthentication and falls back to the signed HTTP
inbox on connection or protocol failure.

The lower participant domain mints a two-party DM conversation, while the remote
recipient authorizes its privacy policy and rechecks it on message ingestion.
Guild homes mint message snowflakes and assign transactional event sequences for
the registered guild mutation set. Replicas apply signed structural
snapshots and ordered guild/channel/role/member/moderation/message/reaction/pin
events; permission-sensitive changes fence on a filtered snapshot and hidden
channel changes use signed redactions. A sequence discontinuity pulls the retained
guild event log before live application continues. Offline remote message writes
remain visibly queued and receive a later commit or stable rejection.

## Media runtime

The selected S3-compatible backend owns three private buckets. A transactional
upload ticket reserves quota and yields a 15-minute PUT whose signature includes
the exact content length and media type. Message or asset commit verifies object
metadata and moves the reservation from
pending to used bytes. Workers distrust declared MIME, scan magic-validated bytes
with ClamAV, and produce safe WebP image variants, blurhash/phash metadata, and
video posters. Originals remain unavailable until clean; infected and deleted
objects are removed and accounting is released.

Local message reads authorize against current channel access before issuing a
short-cache private redirect. Immutable profile/guild/emoji assets are addressed
by clean content hash. Remote reads reconstruct one fixed federation path through
the DNS-pinning client, authorize the requesting peer at the origin, and re-scan
at the consuming instance before a bounded local S3 cache insert. LRU/TTL jobs and
origin-authenticated `media.delete` tombstones bound and invalidate that cache.

Incoming webhook secrets are random `kwh_` tokens stored only as SHA-256 digests.
Execution is home-only, rate-limited, and explicitly attributed in local and
federated message payloads; the creator identity remains an audit/FK principal,
not the displayed sender.

## Release hardening runtime

Expensive client mutations pass through domain-qualified, per-user Dragonfly
token buckets and expose consistent retry headers. Gateway IDENTIFY has separate
global and source buckets and remains closed after a Dragonfly flush until one
elected worker has completed the durable cache warmup fence. A persistent
sentinel is included in five-minute Dragonfly snapshots, while a runtime guard
detects a later flush and repeats warmup.

Operational state is observable without a Docker socket: the internal metrics
endpoint derives active gateway leases from Dragonfly, federation queues from
PostgreSQL, and delivery/task counters from Dragonfly. Prometheus rules and the
provisioned Grafana dashboard remain on internal or loopback-only interfaces.
The release smoke gate verifies shared-stream fanout and the one-envelope/slim-outbox
federation amplification model against disposable real services.

## Data ownership

The v1 ownership model permits replicated users, guilds, guild members, roles,
channels, overwrites, messages, attachments, reactions, pins, emojis, and DM
conversations. Federation replicates profiles, structural guild snapshots, the
registered granular guild mutation stream, and two-party DM conversations and
messages. Media federation replicates attachment metadata and authenticated
access while retaining objects at their origin. Voice federation adds call and
occupancy state.
Auth, settings, authoritative per-user relationship rows, audit, federation
delivery state, remote media cache, instance blocks, and storage accounting
remain local. Signed relationship events synchronize the two independently owned
rows; no remote instance can retain local friendship privileges after local
removal or blocking.

Messages use monthly range partitions keyed by the timestamp-bearing snowflake.
History reads include a lower ID floor so PostgreSQL can prune partitions even
without a cursor. Presence, typing, and live voice occupancy never enter SQL;
only server mute/deafen flags persist.

## Future end-to-end encryption compatibility

Message storage and every local, gateway, federation, retained-history, and DM
replication path accept a bounded, versioned opaque `e2ee` envelope instead of
plaintext. Plaintext and encrypted bodies are mutually exclusive, deleted messages
discard either representation, and the server deliberately does not interpret or
select a cipher suite. This preserves room for a future MLS-style guild protocol and
per-device ratcheted DMs without another message-table rewrite or a federation
envelope fork. Instances advertise this storage-and-relay behavior as
`e2ee-transport/1`. That capability is deliberately narrow: it does not mean that
Kaede currently implements E2EE, MLS, device identity, key packages, recovery,
trust UX, membership epoch changes, or attachment encryption.

An eventual encrypted-room protocol must define authenticated device identities,
key/epoch state, and a canonical associated-data encoding. At minimum, ciphertext
must be bound to the protocol and cipher-suite versions, the composite guild/channel
or DM reference, the sender user and device, the room-policy generation and
cryptographic epoch, a stable message nonce/generation, the operation being
performed, and any encrypted attachment manifest. Federation's instance-level
Ed25519 signatures authenticate transport between servers; they do not authenticate
a participant device and cannot substitute for ciphertext authentication.

Downgrade resistance also belongs to that future protocol. Enabling encryption must
create an authenticated, monotonic room-policy generation; membership and device
changes must advance the cryptographic epoch. Clients must reject plaintext or stale
epochs after the policy is enabled, and authorities/replicas must reject writes that
do not match the current policy. Optional encryption must never be negotiated from
an unauthenticated boolean or from the presence of an opaque envelope alone.

Encrypted rooms will necessarily trade away server-side content search, link
previews, content moderation, and plaintext mention extraction unless clients
provide a separately designed privacy-preserving projection. None of those current
server features is treated as a prerequisite for accepting an opaque envelope, so
the present architecture does not force future encrypted rooms to reveal content.
Notification text, mention recipients, link metadata, thumbnails, filenames, and
attachment dimensions are projections too and must be omitted, encrypted, or
explicitly accepted as metadata leakage by the future protocol. Servers still learn
routing metadata such as the participating instances, composite room and sender
references, timestamps, ciphertext sizes, and delivery activity; padding and traffic
analysis resistance are outside the current transport capability.

## Security invariants

- Bootstrap creates one durable self identity. The configured domain must always
  match it. The Ed25519 private key is AES-256-GCM encrypted with the instance secret.
- The home instance authoritatively rechecks permissions for every federated write.
- A verified federation origin may assert actions only for users on that origin.
- All outbound federation and remote-media traffic uses one DNS-pinning SSRF guard,
  fixed protocol paths, public addresses, and port 443 (with `.localhost` dev override).
- Key rotation retains old verification keys, and event envelopes are independently
  signed so stored events remain auditable.
- Originals are private, uploads are exact-length scoped, and neither local nor
  remote bytes are served before local magic validation and malware scanning.

## Version 1 scope

Version 1 includes identity and authentication, single-instance chat,
federation, media and webhooks, voice/video/screen sharing, Android and iOS
clients, and the associated operational controls. Group DMs, search, threads,
MLS, and compressed gateway encoding are outside the version 1 scope. The
mobile clients use the same home-instance API and gateway boundary as the web
and desktop clients; they never call peer federation endpoints directly.
