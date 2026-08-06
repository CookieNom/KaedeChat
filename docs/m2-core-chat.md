# Core chat

Updated: 2026-07-20

This document describes the REST, persistence, permission, moderation,
direct-message, gateway, browser, and runtime-scaling behavior of the current
chat implementation.

## Features

- Guild creation with an owner membership, `@everyone` role, and `general`
  channel in one explicitly ordered transaction.
- Guild listing and detail payloads with channels and roles.
- Channel, role, and channel-overwrite creation with audit entries.
- Guild, channel, and role updates; safe deletion for empty channels and
  non-default roles.
- Permission resolution in the normative order: owner, base roles,
  `ADMINISTRATOR`, everyone overwrite, aggregate role overwrites, member
  overwrite, implicit channel masks, and timeout mask.
- Permission caching keyed by guild generation and member version. Message
  creation does not change the guild permission generation.
- Atomic, expiring, use-limited local invites with ban checks.
- Manager-only invite listing and durable revocation with retained audit records.
- Role assignment and removal with owner/self protection, position hierarchy,
  lower-snowflake tie-breaking, and permission-grant ceilings.
- Paginated member and ban lists, nickname changes, 28-day maximum timeouts,
  kick, ban, unban, optional recent-message deletion, and audit reasons.
- Permission-gated, newest-first audit-log reads.
- Partitioned message creation through SQLAlchemy Core, nonce reconciliation,
  history paging, edit, soft-delete, reactions, pins, read acknowledgements,
  typing events, and per-channel slow mode.
- Permission-gated bulk message soft deletion, self/moderator reaction removal,
  and pin listing/removal with gateway updates through the shared channel fanout.
- Local relationship listing, friend request/accept/remove flows, blocking, and
  recipient-controlled direct-message privacy (`everyone`, shared guild, or
  friends), with blocks taking precedence.
- Deterministic, order-independent local DM opening using the immutable handle
  pair key, participant-only history, the shared channel message handlers, and
  per-user gateway fanout.
- Durable background direct-mention accounting, channel acknowledgements,
  Dragonfly write-through latest-message state, batched persisted cursor
  projections, and an unread-state API used by guild, channel, and DM badges.
- One Redis dispatch path using a shared per-topic stream for resumable events,
  with generation-fenced presence kept ephemeral so it cannot evict chat history.
- Gateway `HELLO`, cookie or opaque-token `IDENTIFY`, `READY`, heartbeat and token
  liveness checks, session-revocation subscription, guild/user topic fanout,
  presence publication, member-op admission, and direct-message hydration in
  `READY`.
- Redis-backed stateless `RESUME` with per-topic shared-stream cursors and
  overrun rejection, atomic global/per-client identify admission control,
  per-connection client-op limits, SQL-backed heartbeat revalidation, expiring
  presence with a guarded leader-elected offline reaper, bounded lazy member
  chunks/ranges, and graceful `RECONNECT` draining.
- Normalized Svelte rune entity stores back the guild and DM chat routes. A shared
  measured virtual-list wrapper preserves the anchor while prepending history,
  follows the bottom only when appropriate, groups author/day/unread boundaries,
  and presents a new-message pill when the reader is away from the bottom.
- Markdown is parsed with `marked`, sanitized through a strict DOMPurify allowlist,
  then token-decorated without interpreting user text as HTML. The composer has
  keyboard-operable ARIA mention/channel/emoji completion, edit/cancel, failed-send
  retry, Enter/Shift+Enter, Escape, and Arrow-Up-to-edit behavior.
- Last-channel persistence, a `Ctrl`/`Cmd`+`K` channel switcher, `Alt+Arrow`
  history navigation, public invite landing (including federated invites), and
  permission-gated guild overview/channel/role/invite settings routes are present.
- Navigation completion and REST/gateway optimistic-send races have deterministic
  generation-fence and nonce-reconciliation regression tests.
- Gateway workers share one process-wide reference-counted Redis Pub/Sub reader.
  Each connection receives a bounded queue and a precomputed channel-visibility
  summary that refreshes on structural permission or membership events; overflow
  invalidates the resumable session instead of silently dropping a dispatch.
- Permission cache recomputation uses a guarded five-second single-flight lease.
  Waiters may briefly serve only a stale denial, never a stale positive grant,
  and otherwise wait for or safely recompute the exact generation/version key.
- Message requests commit a durable projection row but do not write channel cursors
  or mention counters. Taskiq batches up to 100 pending messages per channel into
  one cursor update, applies mention counts transactionally, writes through the
  Redis cursor, and has a scheduled sweep for lost wake signals.

## Validation

`make chat-check` validates the complete backend acceptance slice. It starts
disposable PostgreSQL, Dragonfly, the task worker, and their private media-service
dependencies in an isolated, per-run `kaede-chat-validation-*` Compose project.
It registers Alice, Bob, and Charlie; creates a guild and invites; identifies
Bob's gateway using the same HttpOnly
cookie mechanism as the browser; and delivers Alice's guild and direct messages
live. It also verifies lazy member ranges, presence publication, disconnect and
shared-stream replay through `RESUMED`, direct-mention counters, unread
state, friend request acceptance, blocking, DM pair idempotency, participant
isolation, DM hydration and history, acknowledgements, permission-generation
stability, invite listing/revocation permissions, reaction removal, pin
round-trips, bulk soft deletion, owner immunity, role hierarchy, lower-role
assignment, timeout masking, ban persistence, banned invite refusal,
unban/rejoin, kick, channel overwrites, owner bypass, and audit-log reads. The
target removes its containers, networks, and volumes on exit. `make check` also
validates the frontend unit suite, Svelte diagnostics, the static production
build, and CSP. `make migration-check` covers every migration up/down/up,
database invariants, guarded downgrade behavior, and metadata drift.

## Scope limits

Group DMs, message search, threads, forums, stages, vanity invites, MLS,
MessagePack, and native mobile clients are outside the current scope. Federation
behavior is documented in [m3-federation.md](m3-federation.md).
