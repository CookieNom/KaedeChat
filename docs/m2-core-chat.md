# Core chat

Updated: 2026-08-07

This document records the current chat implementation: REST, persistence,
permissions, moderation, direct messages, the gateway, the browser client, and
runtime scaling.

## Features

- Guild creation produces the owner membership, `@everyone` role, and `general`
  channel in one ordered transaction.
- Guild listing and detail payloads include channels and roles.
- Channels, roles, and channel overwrites are created with audit entries.
- Guilds, channels, and roles can be updated. Deletion is safe: only empty
  channels and non-default roles.
- Permission resolution follows the normative order: owner, base roles,
  `ADMINISTRATOR`, everyone overwrite, aggregate role overwrites, member
  overwrite, implicit channel masks, and timeout mask.
- Permission caching is keyed by guild generation and member version. Message
  creation does not change the guild permission generation.
- Local invites are atomic, expiring, and use-limited, with ban checks.
- Invite listing is manager-only. Revocation is durable and retains the audit
  record.
- Role assignment and removal enforce owner immunity and permission-grant
  ceilings. The owner may manage their own roles; other role managers get
  hierarchy-bounded self-management, with lower-snowflake tie-breaking.
- Member and active-ban lists are paginated. Moderation covers nickname
  changes, finite or indefinite timeouts, kick, timed or permanent ban, unban,
  optional recent-message deletion, and audit reasons. Finite timeouts are
  capped at 28 days; a distinct indefinite mode avoids encoding permanence as a
  distant timestamp.
- Permission-aware member and message context actions expose timeout, kick, and
  ban without weakening the server-side hierarchy check.
- Guild members may leave directly. An owner must instead transfer ownership to
  another member homed on the guild instance, or permanently delete the guild.
  Ownership transfer uses optimistic concurrency and is recorded in the audit
  log.
- A dedicated critical `BAN_INSTANCES` permission controls timed or permanent
  guild bans for a complete federated origin. The action enforces role
  hierarchy, removes current members from that origin, rejects later joins
  while active, records an audit entry, and sends an authority-signed cache
  revocation to the affected instance.
- Audit-log reads are permission-gated and newest-first.
- Message creation is partitioned and runs through SQLAlchemy Core, with nonce
  reconciliation. History paging, edit, soft-delete, reactions, pins, read
  acknowledgements, typing events, and per-channel slow mode are all in place.
- Bulk message soft deletion is permission-gated. Reaction removal works for
  self and moderators, and pin listing/removal sends gateway updates through
  the shared channel fanout.
- Local relationships cover listing, friend request/accept/remove flows, and
  blocking. Direct-message privacy is recipient-controlled (`everyone`, shared
  guild, or friends), with blocks taking precedence.
- Local DM opening is deterministic and order-independent, keyed on the
  immutable handle pair. History is participant-only, and DMs reuse the shared
  channel message handlers and per-user gateway fanout.
- Direct-mention accounting is durable and runs in the background, alongside
  channel acknowledgements, Dragonfly write-through latest-message state, and
  batched persisted cursor projections. An unread-state API backs the guild,
  channel, and DM badges.
- There is one Redis dispatch path: a shared per-topic stream for resumable
  events. Presence is generation-fenced and kept ephemeral so it cannot evict
  chat history.
- The gateway implements `HELLO`, cookie or opaque-token `IDENTIFY`, `READY`
  with direct-message hydration, and heartbeat and token liveness checks. It
  also handles session-revocation subscription, guild/user topic fanout,
  presence publication, and member-op admission.
- `RESUME` is stateless and Redis-backed, using per-topic shared-stream cursors
  with overrun rejection. Identify admission control is atomic, both global and
  per-client, with per-connection client-op limits and SQL-backed heartbeat
  revalidation. Presence expires on its own under a guarded leader-elected
  offline reaper, member chunks and ranges are lazy and bounded, and
  `RECONNECT` drains gracefully.
- Normalized Svelte rune entity stores back the guild and DM chat routes.
- A shared measured virtual-list wrapper preserves the anchor while prepending
  history and follows the bottom only when appropriate. It groups
  author/day/unread boundaries and shows a new-message pill when the reader is
  away from the bottom.
- Markdown is parsed with `marked`, sanitized through a strict DOMPurify
  allowlist, then token-decorated without interpreting user text as HTML.
- The composer offers keyboard-operable ARIA user/role/channel/emoji
  completion, covering the full Unicode catalog and federated custom-emoji
  thumbnails. Role mentions and custom emoji use immutable federated tokens
  rather than display names. Edit/cancel, failed-send retry, Enter/Shift+Enter,
  Escape, and Arrow-Up-to-edit are also supported.
- The member roster groups active users under their highest role marked for
  separate display, then lists remaining online and offline members.
- Role mention fanout is resolved by the guild authority against current
  assignments and the role's mentionability setting; clients cannot supply
  arbitrary role recipients.
- Message, channel, and profile menus are portaled to the document layer before
  viewport placement, so transformed scroll panes cannot clip or offset them.
- Last-channel persistence, a `Ctrl`/`Cmd`+`K` channel switcher, and
  `Alt+Arrow` history navigation are present, as are public invite landing
  (including federated invites) and permission-gated guild
  overview/channel/role/invite settings routes.
- Navigation completion and REST/gateway optimistic-send races have
  deterministic generation-fence and nonce-reconciliation regression tests.
- Gateway workers share one process-wide reference-counted Redis Pub/Sub
  reader. Each connection receives a bounded queue and a precomputed
  channel-visibility summary that refreshes on structural permission or
  membership events; overflow invalidates the resumable session instead of
  silently dropping a dispatch.
- Permission cache recomputation uses a guarded five-second single-flight
  lease. Waiters may briefly serve only a stale denial, never a stale positive
  grant, and otherwise wait for or safely recompute the exact
  generation/version key.
- Message requests commit a durable projection row but do not write channel
  cursors or mention counters. Taskiq batches up to 100 pending messages per
  channel into one cursor update, applies mention counts transactionally,
  writes through the Redis cursor, and runs a scheduled sweep for lost wake
  signals.

## Validation

`make chat-check` validates the complete backend acceptance slice. It starts
disposable PostgreSQL, Dragonfly, the task worker, and their private
media-service dependencies in an isolated, per-run `kaede-chat-validation-*`
Compose project. The scenario registers Alice, Bob, and Charlie; creates a
guild and invites; identifies Bob's gateway using the same HttpOnly cookie
mechanism as the browser; and delivers Alice's guild and direct messages live.

From there it verifies, by area:

- Gateway: lazy member ranges, presence publication, disconnect and
  shared-stream replay through `RESUMED`, and permission-generation stability.
- Messaging: direct-mention counters, unread state, acknowledgements, reaction
  removal, pin round-trips, and bulk soft deletion.
- Relationships and DMs: friend request acceptance, blocking, DM pair
  idempotency, participant isolation, and DM hydration and history.
- Moderation and permissions: owner immunity, role hierarchy, lower-role
  assignment, timeout masking in both finite and indefinite modes, ban
  persistence, banned invite refusal, unban/rejoin, kick, channel overwrites,
  owner bypass, invite listing/revocation permissions, and audit-log reads.

The target removes its containers, networks, and volumes on exit. `make check`
also validates the frontend unit suite, Svelte diagnostics, the static
production build, and CSP. `make migration-check` covers every migration
up/down/up, database invariants, guarded downgrade behavior, and metadata
drift.

## Scope limits

Group DMs, message search, threads, forums, stages, vanity invites, MLS, and
MessagePack are outside the current scope. Native Android and iOS clients are
implemented in `mobile/` and consume the same core-chat contracts. Federation
behavior is documented in [m3-federation.md](m3-federation.md).
