# Kaede Chat architecture

Status: normative for `v1` · Updated: 2026-07-20

## Product boundary

Kaede Chat is a self-hosted chat system. Identities are immutable
`username@domain` handles, and a user's home instance owns authentication and
user identity. Direct messages are retained by both participants' instances.
A guild has exactly one home instance; that home orders its changes and
replicates them to instances with participating members.

The v1 client is a SvelteKit static SPA. FastAPI serves REST, federation, media
authorization, and LiveKit webhooks, while a separate FastAPI gateway service
owns long-lived WebSockets. PostgreSQL holds durable state. Dragonfly holds
ephemeral state and carries tasks. Media lives in an S3-compatible store
(Garage by default), and LiveKit provides media sessions.

## Binding conventions

- Snowflakes use the specified 10 leased worker bits and 12 sequence bits after
  a 2026-01-01 UTC epoch. PostgreSQL `BIGINT` is signed, so v1 deliberately
  reserves the sign bit and permits 41 positive timestamp bits. Generation fails
  closed before that range is exhausted. JSON always encodes identifiers as
  decimal strings. Using the nominal 42nd timestamp bit would require a future
  storage and API version rather than producing negative identifiers.
- Every federated row is identified by `(id, origin_domain)`. References carry
  both components, and Dragonfly keys include the domain.
- `instances.is_self` has a unique partial index. Internal `is_local` values are
  bound to it with composite foreign keys, so reusable migrations can enforce
  locality without embedding a deployment domain in a `CHECK` constraint.
- Application authentication tokens are opaque and prefix-scannable (`kc1_at_`,
  `kc1_rt_`, `kc1_mfa_`), never JWTs. LiveKit connection grants use LiveKit's
  own room-scoped HS256 JWT format; they are not Kaede authentication
  credentials.
- Python owns gateway opcodes, close codes, event names, and permissions.
  Generated TypeScript is committed and checked for drift.

## Runtime topology

Host nginx owns public ports 80/443 and terminates TLS. It forwards the main
virtual host — plus the media virtual host when Garage is selected — to an
internal edge on a loopback-only high port. Caddy provides that internal
routing layer. The setup wizard can separately render the TLS proxy
configuration for the host-level nginx service.

The internal edge routes REST, media authorization, federation, and well-known
requests to the API, and `/gateway` to gateway replicas. Everything else goes
to an atomically switched static-SPA release. `/livekit` returns 404 unless the
operator sets `KAEDE_VOICE_ENABLED=true`. The `voice` profile also refuses to
start unless that switch is true; once it is, the edge proxies the route
through the Docker host gateway to the host-networked SFU. The immediately
previous SPA release is retained so already-loaded pages can still fetch their
hashed chunks during a rollout. Garage objects are reachable only through
presigned requests on `media.<domain>`; external S3 mode redirects to signed
URLs at the configured HTTPS provider origin. Edge and data bridges are
internal. PostgreSQL and password-protected Dragonfly stay on the data bridge,
and only services that need outbound federation or update traffic join the
egress bridge.

The static frontend carries a SvelteKit-generated hash CSP for its
build-specific inline bootstrap. The edge keeps header-only protections such as
`frame-ancestors` without imposing a second `script-src` policy that would
override the generated hash. The frontend check hashes every inline script in
the built fallback and rejects CSP drift.

API processes lease worker IDs from Dragonfly rather than receiving a static
ID. Lease ownership is process-specific. It is renewed by a compare-and-expire
Lua script on every mint and by the heartbeat. A newly unclaimed worker slot
sits in a non-minting quarantine for one full 60-second former-lease lifetime
before activation, so a Dragonfly flush cannot overlap old and replacement
generators. This has two costs: a cold API process can take about 60 seconds to
become ready, and each mint incurs one Dragonfly round trip. Losing the lease,
or being unable to prove it, stops further ID minting. Dragonfly snapshotting
prevents a total session/cache loss from causing an identify stampede. The
production API health check therefore uses readiness with a quarantine-aware
start period, and Caddy does not enter service until that readiness check
passes.

Secrets are split by process. Gateway processes receive a distinct,
non-authoritative configuration key rather than `KAEDE_SECRET_KEY`, so a
gateway-only compromise cannot decrypt the instance federation signing key or
encrypted TOTP material. Gateway, API, scheduler, and migration processes get
no email delivery credentials; only the email-capable worker and the
configuration preflight do. The scheduler receives only the Dragonfly
task-broker URL and log level — not the master key, database URL, or proxy
secret. In the production Compose topology the gateway also receives no
administrator credential.

Incomplete WebSocket handshakes are bounded twice. Host nginx limits concurrent
and burst upgrades per source address on the exact `/gateway` route, and each
gateway process admits at most 128 pre-authentication connections. The
process-local admission is fail-fast, happens before the WebSocket accept/HELLO,
and is released as soon as a structurally valid IDENTIFY or RESUME passes the
existing Dragonfly admission check. Slow handshakes therefore cannot consume an
unbounded number of gateway tasks or file descriptors, while established
authenticated sockets stay outside the pre-authentication pool.

Argon2id retains its 64 MiB memory cost, but API routes execute it off the
event loop through a dedicated AnyIO capacity limiter. Each API process runs at
most one password hash or verification at once. That bounds Argon2 memory
amplification without letting password work stall unrelated async requests in
the process. Synchronous primitives remain available to offline verification
tools.

Background work runs on Taskiq workers over Redis Streams. They handle email
delivery, federation draining, and media scanning/derivation, plus the periodic
cleanup jobs: account and federation retention, unverified-account cleanup,
partition creation, message cursor and mention projections, upload-orphan
cleanup, remote-cache eviction, media retention, deletion purge, and cache
warmup. A leader-elected API coordinator runs the fast loops: voice permission
enforcement every five seconds, federation heartbeats every 30 seconds, and
LiveKit reconciliation every 60 seconds. Scheduled Taskiq jobs remain separate
from workers.

One-time-token email uses a transactional PostgreSQL outbox. Token creation and
an AES-GCM-encrypted recipient/subject/body commit together; the Redis task
carries only a wake signal, never the address or bearer link. Workers claim due
rows with `FOR UPDATE SKIP LOCKED`, reclaim abandoned claims after ten minutes,
and retry with bounded backoff until the associated token expires. A minute
scheduler sweep guarantees eventual attempts when the post-commit Redis wake
fails. Terminal delivery metadata is kept no longer than seven days, and can
disappear earlier when its expired one-time-token row is pruned. Provider
delivery is at-least-once: if a process fails after an SMTP/API provider
accepts a message but before the SQL success update, a duplicate can go out,
because those providers do not share the database transaction. Links remain
one-time credentials, so a duplicate does not create a second usable token.

## Federation runtime

Federation discovery and key retrieval use fixed `kaede-fed/1` paths.
Production DNS results are checked for private, loopback, link-local, reserved,
multicast, and metadata ranges. The HTTP transport connects to the checked
address while retaining the original TLS server name. Development-only
`.localhost` overrides let the isolated Alpha/Beta topology use internal
service names.

Every implemented server-to-server mutation has a canonical HTTP signature and
an independently signed event envelope. The inbox authenticates the transport
before parsing events, enforces actor-origin ownership, and deduplicates on
`(origin,event_id)`. Per-destination outbox drainers batch up to 100 events and
1 MiB, use bounded jittered backoff, open a circuit after 24 hours, and retain
write state across restarts. A pooled, signed `kaede-fed.1` WebSocket carries
the same batches with correlated results. It reconnects for periodic
reauthentication and falls back to the signed HTTP inbox on connection or
protocol failure.

The lower participant domain mints a two-party DM conversation. The creator's
home mints a group DM and stays authoritative for its name, owner, membership,
and monotonic state version. Every invitee's home confirms an accepted
friendship before the authority adds that person. Signed full-state updates
let every participating home deliver messages and calls directly while
rejecting stale or conflicting membership changes. A direct-message recipient
authorizes its privacy policy and rechecks it on message ingestion.
Guild homes mint message snowflakes and assign transactional event sequences
for the registered guild mutation set. Replicas apply signed structural
snapshots and ordered guild/channel/role/member/moderation/message/reaction/pin
events. Permission-sensitive changes fence on a filtered snapshot, and hidden
channel changes use signed redactions. On a sequence discontinuity, a replica
pulls the retained guild event log before live application continues. Offline
remote message writes stay visibly queued and receive a later commit or a
stable rejection.

## Media runtime

The selected S3-compatible backend owns three private buckets. A transactional
upload ticket reserves quota and yields a 15-minute PUT whose signature
includes the exact content length and media type. Message or asset commit
verifies object metadata and moves the reservation from pending to used bytes.
Workers distrust declared MIME. They scan magic-validated bytes with ClamAV and
produce safe WebP image variants, blurhash/phash metadata, and video posters.
Originals stay unavailable until clean; infected and deleted objects are
removed and their accounting released.

Local message reads authorize against current channel access before issuing a
short-cache private redirect. Immutable profile/guild/emoji assets are
addressed by clean content hash. Remote reads reconstruct one fixed federation
path through the DNS-pinning client, authorize the requesting peer at the
origin, and re-scan at the consuming instance before a bounded local S3 cache
insert. LRU/TTL jobs and origin-authenticated `media.delete` tombstones bound
and invalidate that cache.

Incoming webhook secrets are random `kwh_` tokens stored only as SHA-256
digests. Execution is home-only and rate-limited, and the webhook is attributed
in both local and federated message payloads. The creator identity remains an
audit/FK principal, not the displayed sender.

## Release hardening runtime

Expensive client mutations pass through domain-qualified, per-user Dragonfly
token buckets and expose consistent retry headers. Gateway IDENTIFY has
separate global and source buckets. After a Dragonfly flush it stays closed
until one elected worker has completed the durable cache warmup fence. A
persistent sentinel is included in five-minute Dragonfly snapshots, and a
runtime guard detects a later flush and repeats warmup.

Operational state is observable without a Docker socket. The internal metrics
endpoint derives active gateway leases from Dragonfly, federation queues from
PostgreSQL, and delivery/task counters from Dragonfly. Prometheus rules and the
provisioned Grafana dashboard remain on internal or loopback-only interfaces.
The release smoke gate verifies shared-stream fanout and the
one-envelope/slim-outbox federation amplification model against disposable real
services.

## Data ownership

The v1 ownership model permits replicated users, guilds, guild members, roles,
channels, overwrites, messages, attachments, reactions, pins, emojis, and DM
conversations. Federation replicates profiles, structural guild snapshots, the
registered granular guild mutation stream, and direct/group DM conversations
and messages. Media federation replicates attachment metadata and authenticated
access while objects stay at their origin. Voice federation adds call and
occupancy state.

Some data never replicates. Auth, settings, authoritative per-user relationship
rows, audit, federation delivery state, remote media cache, instance blocks,
and storage accounting remain local. Signed relationship events synchronize the
two independently owned rows; no remote instance can retain local friendship
privileges after local removal or blocking.

Task tracker boards, lanes, tasks, and creation nonces are normalized local
guild-authority data. They cascade with their type-17 channel and are not
silently accepted by a replica; remote mutations fail closed until a tracker
snapshot/event capability is added to Kaede Fed. The local API and storage
contract is documented in [Task tracker channels](task-tracker.md).

Messages use monthly range partitions keyed by the timestamp-bearing snowflake.
History reads include a lower ID floor so PostgreSQL can prune partitions even
without a cursor. Presence, typing, and live voice occupancy never enter SQL;
only server mute/deafen flags persist.

## Message search

PostgreSQL remains authoritative for messages and permissions. A durable SQL
desired-state queue projects only eligible plaintext messages into a private
Meilisearch index on the same deployment network. Message, attachment, pin, and
channel-encryption changes enqueue replacement or deletion work. Search treats
the engine as an untrusted, rebuildable candidate generator: every hit is
loaded from SQL, and its current channel access is checked again before it
reaches a client. Search outages never block message delivery. A replaced index
can be repopulated online with `make search-rebuild`.

Federated search never joins Meilisearch clusters or shares a master key. A
user home sends a signed, bounded query to the authoritative guild or DM
instance, and only when that instance advertises `message-search/1`. The
authority applies its own permissions and returns a minimal bounded result
projection. The user home validates and re-authorizes each result against local
channel state before showing it. Cached local matches remain available when a
peer is offline and are labeled as incomplete.

A channel-scoped DM search contacts that conversation's deterministic
authority. Account-wide DM search stays within the user's bounded recent
replica cache instead of broadcasting sensitive terms to every instance
the account has ever contacted. Clients label that coverage and offer complete
authority search when the user opens a specific conversation.

Channels marked `e2ee` are excluded at indexing, query, SQL hydration, and
federation boundaries. The UI explains why search is disabled. A later E2EE
extension may define a separate opt-in client-side or privacy-preserving search
design, but server plaintext indexing is not silently re-enabled for encrypted
rooms.

## Optional end-to-end encryption

Message storage and every local, gateway, federation, retained-history, and DM
replication path accept a bounded, versioned `e2ee` envelope instead of
plaintext. Plaintext and encrypted bodies are mutually exclusive, and deleted
messages discard either representation. Version 2 is the Kaede MLS 1.0
application envelope; version 1 remains legacy opaque transport and is never
presented as MLS encryption. Instances advertise storage and relay support as
`e2ee-transport/1`, while device and encrypted-media capabilities are negotiated
separately.

Each device has a proof-of-possession identity and one-use MLS KeyPackages. MLS
authenticated data canonically binds: the composite channel reference and
group, the policy generation and epoch, the sender device, the operation and
edit target, and the encrypted attachment manifest digest. The MLS credential
binds the author account. Federation's instance-level Ed25519 signatures
authenticate transport between servers; clients still verify MLS credentials
and authenticated data.

Enabling encryption creates a monotonic room-policy generation, and membership
and device changes pause writes until a fresh group excludes removed devices.
Clients reject plaintext or stale epochs after the policy is enabled, and
authorities and replicas must reject writes that do not match the current
policy. Optional encryption must never be negotiated from an unauthenticated
boolean or from the presence of an opaque envelope alone.

Encrypted rooms trade away server-side content search, link previews, content
moderation, and plaintext mention extraction, unless clients provide a
separately designed privacy-preserving projection. None of those server
features is a prerequisite for accepting an opaque envelope, so the present
architecture does not force future encrypted rooms to reveal content.
Notification text, mention recipients, link metadata, thumbnails, filenames,
and attachment dimensions are projections too. Encrypted-room clients omit or
encrypt them unless the user chooses to disclose selected content. Servers
still learn routing metadata: the participating instances, composite room and
sender references, timestamps, ciphertext sizes, and delivery activity. Padding
and traffic analysis resistance are outside the current transport capability.

## Security invariants

- Bootstrap creates one durable self identity. The configured domain must
  always match it. The Ed25519 private key is AES-256-GCM encrypted with the
  instance secret.
- The home instance authoritatively rechecks permissions for every federated
  write.
- A verified federation origin may assert actions only for users on that
  origin.
- All outbound federation and remote-media traffic uses one DNS-pinning SSRF
  guard, fixed protocol paths, public addresses, and port 443 (with
  `.localhost` dev override).
- Key rotation retains old verification keys, and event envelopes are
  independently signed, so stored events remain auditable.
- Originals are private, uploads are exact-length scoped, and neither local nor
  remote bytes are served before local magic validation and malware scanning.

## Version 1 scope

The production scope covers identity and authentication, single-instance chat,
federation, media and webhooks, voice/video/screen sharing, Android and iOS
clients, task tracker channels, message search, group DMs, and the associated
operational controls.
Threads and compressed gateway encoding are not currently implemented. MLS 1.0
messaging, encrypted attachments, recovery, and LiveKit frame encryption ship
behind the operator activation gate documented in `docs/e2ee.md`.
The mobile clients use the same home-instance API and gateway boundary as the
web and desktop clients; they never call peer federation endpoints directly.
