# Release hardening

Updated: 2026-07-21

## Abuse controls

Client-facing rate limits use atomic Dragonfly token buckets keyed by
route and immutable local user identity or verified source address. Message
sends, typing, DM opens, reactions, invite creation/acceptance/preview, guild
creation, every upload-ticket route, and remote-media cache misses return stable
`X-RateLimit-Bucket`, limit, remaining, and reset headers. A rejection includes
both `Retry-After` and a millisecond retry value. Dragonfly's clock and integer
milli-tokens avoid API-worker clock drift and floating-point races. Typing is
admitted before database locking; remote-media cache misses also use bounded
process-local admission, while cache hits do not spend that fetch budget.

Gateway identify admission is a global and per-source token bucket. A missing
Dragonfly snapshot sentinel closes admission with gateway code 4008 and a bounded
retry reason. One elected gateway warms the durable user/membership query set;
other workers wait for the ready fence, and a background guard repeats the gate
if Dragonfly is flushed at runtime. New resumable sessions are atomically capped
at eight per user, including concurrent handshakes. Dragonfly snapshots remain
scheduled every five minutes. Gateway readiness is false until warmup completes.

The release smoke creates real shared-stream fanout through Dragonfly and verifies
that twenty subscribers receive each event while only one stream entry is
written. It also queues one signed envelope for twenty federation destinations
and verifies that envelope storage is constant while only slim outbox pointers
amplify. Federation batches remain capped at 100 events and 1 MiB.

## Security controls

The regression suite covers permission generation stability on message writes,
author/signing-origin
binding, DNS/private-address/rebinding SSRF rejection, generated opcode and
permission drift, resume from shared streams, bounded request bodies, federation
hop limits, refresh-token reuse, media scan gating, LiveKit generation fencing,
and strict environment validation. Dependency locks are audited in CI. Kaede's
application runtime services use read-only root filesystems, drop capabilities,
and set `no-new-privileges`; third-party infrastructure images retain only the
filesystem access and runtime behavior required by their upstream images. The
topology also uses internal networks, bounded logs, non-floating image tags,
externally supplied secrets, and loopback-only host bindings for the reverse
proxy, API diagnostics, and Grafana. Rendered-Compose policy checks keep these
application-service, network, port, and credential-isolation invariants from
drifting.

## Observability and operations

`/metrics` now exports API health, connected gateway sessions, pending and failed
federation outbox rows, delivery failures, and per-task run/failure/duration
metrics. Prometheus alert rules and a provisioned Grafana overview dashboard ship
with the optional observability profile. The operator guide covers
backup/restore drills, signing-key overlap and retirement, allowlists and domain
blocklist import/export, alert access, and deployment rollback. The
`kaedechat.com` template is documented separately and contains no live secrets.

`make release-check` starts only disposable PostgreSQL and Dragonfly services,
publishes no host ports, runs migrations and bootstrap, exercises warmup, rate
limits, shared fanout, and federation amplification, then removes all state.
