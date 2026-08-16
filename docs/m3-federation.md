# Federation implementation

This document describes the implemented `kaede-fed/1` server-to-server behavior.
The normative wire contract is in [kaede-fed-v1.md](kaede-fed-v1.md).

## Protocol coverage

- Well-known discovery, version negotiation, and current plus historical
  Ed25519 keys.
- Canonical HTTP and WebSocket signatures, independently signed event
  envelopes, key rotation, clock/body/destination checks, and actor-origin
  binding.
- A single outbound resolver with DNS pinning. It rejects private, loopback,
  link-local, metadata, unsafe-port, redirect, and mixed-address targets.
  Explicit `*.localhost` overrides and custom trust roots are
  test/development-only.
- Durable inbox/outbox delivery with ordered per-destination drains, bounded at
  100 events and 1 MiB. Batches are idempotent with per-event results; retries
  use jittered backoff with circuit state, queue retention and caps, policy
  holds, and delivery-state gateway updates.
- A reusable signed `kaede-fed.1` WebSocket hot link carries the same durable
  batches. Frames are bounded and results correlated. Connections hold leases,
  heartbeat, and reauthenticate hourly by reconnect; policy is checked live,
  and fallback to the HTTP inbox is automatic.
- Remote profile lookup with five-minute freshness and one-minute negative
  caching. Stale entries revalidate asynchronously, refreshes coalesce, and
  requester and target-domain amplification limits are independent.
- Versioned federated display profiles, plus cross-instance friend request,
  acceptance, removal, and privacy-safe blocking. Correlation IDs fence stale
  acceptance; each home remains authoritative for its user's DM policy.
- Deterministic two-party DM authority: convergent opening, privacy
  enforcement, replicated messages, reverse-direction delivery, duplicate
  suppression, and local live gateway fanout.
- Signed remote invite resolution and authoritative joins, with paged
  structural snapshots, permission-filtered sequencing and gap fill, and
  full-resync signaling.
- Per-user and origin-wide direct access revocation, timed or permanent
  remote-user and federated-instance join sanctions, and authoritative queued
  or synchronous remote message writes.
- Signed remote leave requests let a member leave at the guild home. The home
  removes authoritative membership and sends a direct access revocation; losing
  the final local member makes the replica unavailable and schedules
  cooperative message and media-cache removal. Ownership transfer remains a
  guild-home-only operation and may target only a member homed on that same
  instance.
- Capability-negotiated, permission-bound retained guild-history transfer.
  Guild policy is disabled by default, with channel overrides. Imports are
  durable and leased: recent-first byte-bounded pages, sequenced delta
  reconciliation, bounded replica merge, independent acknowledgement retry,
  provenance, and best-effort revocation purge.
- The guild mutation registry: guild/channel/role/overwrite/member,
  membership-role, moderation, message edit/delete/purge, reaction, and pin
  events. Permission-sensitive changes fence application on a fresh filtered
  snapshot; inaccessible channel events become signed sequence-only redactions.
  Omitted channels are tombstoned, and their message/object cache is
  cooperatively purged once no local member at that replica retains access.
- Open/allowlist federation plus subdomain-aware silence/suspend
  administration, route-specific invite/join limits, and Mastodon-compatible
  block CSV exchange.

## Security invariants

Every server-to-server operation is authenticated before untrusted payload data
can mutate state. The verified origin owns the actor. Exact bytes are hashed,
queries are canonicalized, timestamps and hop counts are bounded, and the event
signature remains verifiable independently of transport. Federation never
accepts payload-provided URLs.

Remote entities retain composite `(id, origin_domain)` identity. Guild homes
are the permission, sequencing, and mutation authority. DM conversation
snowflakes are minted only by the deterministic authority. Permission changes
cannot leak private channel metadata: receivers either obtain a current
filtered snapshot or advance through an opaque signed redaction.

## Validation

`make federation-check` creates disposable Alpha/Beta PostgreSQL and Dragonfly
instances on internal Compose networks with no published host ports. It
verifies:

- signed lookup and stale-safe identity replication;
- convergent DMs, live gateway delivery, duplicate suppression, retry through a
  simulated policy outage, healing, and reverse delivery;
- cross-instance friendship convergence and immediate local enforcement of
  friend-only DM privacy after removal;
- remote guild join and permission-filtered snapshot persistence;
- authoritative remote message writes, composite replies, sequence gaps, and
  background gap recovery;
- granular guild, channel, role, member-role, member, message edit/delete,
  reaction, and pin replication; and
- role/channel removal and final member access revocation.

The ordinary gate uses internal cleartext peer overrides. `make
federation-tls-check` repeats the same scenario through an isolated TLS nginx
edge and separate per-instance Caddy hops, including `wss` hot-link upgrades,
with an ephemeral private test CA. It publishes no host ports and removes its
containers, networks, certificates, and data volumes on success or failure.

Both gates prefer the hot link for outbox delivery and exercise signed HTTP as
the fallback path during reconnect/outage behavior. CI runs the same gates.

## Limitations and deployment notes

Media upload, malware scanning, derivatives, quotas, remote caching, and
webhooks are documented in [m4-media.md](m4-media.md). LiveKit voice and call
federation are documented in [m5-voice.md](m5-voice.md). Permission-bound guild
history is available through the optional
`guild-history-sync/2-recent-first` capability, with version 1 rolling-upgrade
compatibility. Cross-instance read receipts and group DMs remain outside
`kaede-fed/1`. Presence, typing, and live occupancy remain droppable,
non-durable surfaces rather than PostgreSQL state.

Deployments must migrate to Alembic head before running federation services.
The TLS gate uses a custom CA only in `development`/`test`; production settings
forbid peer URL overrides and custom federation trust roots.
