# Task tracker channels

Task tracker channels are Kaede channel type `17`. They provide an ordered,
lane-based board for work that belongs to a guild: a stable task key, title,
description, priority, due date, creator, and optional assignee. The same
resource model is used by the responsive web client, desktop shell, Flutter
client, human REST API, and bot API.

Tracker channels are deliberately separate from message channels. Opening one
does not fetch message history, typing state, pins, or a composer. Its board is
loaded from the tracker API and updated from tracker Gateway events.

Task titles and descriptions are ordinary guild data, not message content, and
tracker channels do not currently support E2EE. Operators and guild members
should not place secrets in tasks. An installation granted `tasks.read` can
read that content wherever its live channel permissions allow it.

## Board model

Creating a tracker channel creates one board and four ordered lanes in the same
transaction:

| Lane | Kind | Completion lane |
| --- | --- | --- |
| Backlog | `backlog` | No |
| Planned | `planned` | No |
| In progress | `in_progress` | No |
| Done | `completed` | Yes |

Guild managers may rename, recolor, reorder, add, and remove lanes. A lane
cannot be removed while it contains tasks, and the final lane cannot be
removed. Moving a task into a completion lane records `completed_at`; moving it
out clears that timestamp.

Each board has a two-to-ten character ASCII key prefix. It is normalized to
uppercase, begins with a letter, and contains only letters and digits. If no
`tracker_key_prefix` is supplied during channel creation, Kaede derives one
from the channel name. Task numbers are monotonically allocated and are never
reused, producing keys such as `OPS-42`.

The storage boundary is one board, at most 50 lanes, and at most 5,000 tasks.
Titles are limited to 200 characters, descriptions to 10,000 characters, and
lane names to 100 characters. Priorities are `none`, `low`, `medium`, `high`,
or `urgent`. Due dates must carry an explicit timezone offset.

Tasks have a single optional assignee. The assignee must currently be a member
of the guild. Removing a member atomically clears their assignments, advances
each affected board version, and queues full-refresh notifications; a database
foreign key also prevents stale assignees if a maintenance or replica path
deletes membership directly. Task creators remain durable historical
attribution even after leaving. Creation accepts an optional 1–64 character
`client_nonce` for safe bot and client retries. Reusing a nonce with the same
request returns the original task; reusing it for different input fails with a
conflict.

## Permissions

Tracker authorization is evaluated from the actor's live guild roles plus the
channel's permission overwrites. Application scopes never replace these
checks.

| Permission | Capability |
| --- | --- |
| `VIEW_CHANNEL` | Read the board and receive its events |
| `CREATE_TRACKER_TASKS` | Create tasks |
| `EDIT_OWN_TRACKER_TASKS` | Edit, move, or remove a task the actor created or is assigned to |
| `MANAGE_TRACKER_TASKS` | Edit, move, or remove any task |
| `ASSIGN_TRACKER_TASKS` | Assign or unassign other guild members |
| `MANAGE_TRACKER` | Change the key prefix and manage lanes |

An actor may assign or unassign themselves without
`ASSIGN_TRACKER_TASKS`. Assigning another member while creating a task still
requires that permission. Administrators retain the normal permission bypass;
all other decisions remain channel-scoped.

New guilds grant `CREATE_TRACKER_TASKS` and `EDIT_OWN_TRACKER_TASKS` in the
ordinary member baseline. During upgrade, roles and channel overwrites that
already allowed or denied `SEND_MESSAGES` receive the matching allow or deny
for those two collaborative tracker grants. Management and assignment grants
remain opt-in.

## Human REST API

All identifiers are composite entity references, for example
`20@chat.example`. Snowflakes, task counters, and permission masks are encoded
as decimal strings in response JSON.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/channels/{channel}/tracker` | Fetch the complete ordered board |
| `PATCH` | `/api/v1/channels/{channel}/tracker` | Change `key_prefix` |
| `POST` | `/api/v1/channels/{channel}/tracker/lanes` | Create a lane |
| `PATCH` / `DELETE` | `/api/v1/channels/{channel}/tracker/lanes/{lane}` | Edit or remove a lane |
| `POST` | `/api/v1/channels/{channel}/tracker/lanes/{lane}/move` | Move a lane to `position` |
| `POST` | `/api/v1/channels/{channel}/tracker/tasks` | Create a task |
| `PATCH` / `DELETE` | `/api/v1/channels/{channel}/tracker/tasks/{task}` | Edit or remove a task |
| `POST` | `/api/v1/channels/{channel}/tracker/tasks/{task}/move` | Move a task to `lane_id` and `position` |

Board, lane, and task mutations that target an existing resource require its
current `version` in `If-Match`. Missing preconditions return `428`; stale
versions return `412` with the current version. Clients should reload the board
and let the user reapply their change. Creation uses row locks and bounded
positions; task numbering and nonce idempotency are serialized at the board.

## Bots and applications

The bot API mirrors every route under `/api/v1/bots/channels/...`. A bot's
effective access is the intersection of its worker ceiling, installation
grant, optional channel restriction, and current managed-role permissions.

| Scope | Access |
| --- | --- |
| `tasks.read` | Fetch boards and receive tracker events |
| `tasks.write` | Create and mutate tasks, subject to live task permissions |
| `tasks.manage` | Change board settings and lanes, subject to `MANAGE_TRACKER` |

Because changing board settings returns the refreshed complete board, that
operation also requires `tasks.read`; `tasks.manage` alone never discloses task
content.

Subscribe to the `guild_tasks` intent to receive these Gateway dispatches:

- `TRACKER_BOARD_UPDATE`
- `TRACKER_LANE_CREATE`, `TRACKER_LANE_UPDATE`, `TRACKER_LANE_DELETE`
- `TRACKER_TASK_CREATE`, `TRACKER_TASK_UPDATE`, `TRACKER_TASK_DELETE`

Create and update events contain the complete lane or task resource. Delete
events contain its composite reference and the parent channel reference. Every
lane and task event includes `board_version`, allowing consumers to reject an
older incremental projection. Moves use the corresponding update event.
Delivery is at least once; handlers should key state by the full composite
reference and may refetch the board after a sequence gap.

Tracker mutations and their gateway events are committed atomically to a SQL
outbox. Delivery is attempted immediately and swept at least once per minute,
so a Redis/Dragonfly or task-queue outage does not lose a committed change.
Rows are acknowledged only after the event has entered the resumable gateway
stream. A process failure between stream insertion and acknowledgement can
replay the event; consumers must therefore remain idempotent. Stream retention
is bounded, so clients whose resume cursor is older than the retained window
must refetch the board. The board snapshot and resource `version` fields are
authoritative; event order must never overwrite a newer version.

`TRACKER_BOARD_UPDATE` is also an explicit invalidation event. When
`full_refresh` is true—for example, after a lane changes completion semantics
for every task or an insertion/reorder changes sibling resource versions—clients
must refetch the board instead of trying to infer all affected projections from
one event. Its optional `reason` is diagnostic, not an authorization or branching
input.

The Python SDK exposes `Channel.is_tracker`, `Channel.tracker()`, typed
`TrackerBoard`, `TrackerLane`, and `TrackerTask` resources, convenience CRUD
methods, typed delete and board-update events, and the `guild_tasks` intent.

## Authority and federation

The guild home remains authoritative for tracker content. Writes against a
non-authoritative replica fail explicitly with
`409 FEDERATED_WRITE_UNSUPPORTED`; they are never accepted locally and replayed
later. Clients must present that state rather than implying that a task was
saved.

Reads work through a bounded replica cache. After the requesting human or bot
passes the replica's live `VIEW_CHANNEL` and tracker-read checks, a missing or
invalidated board is fetched from the guild home through a signed, paginated
tracker snapshot endpoint. The home serves a peer only while that origin has
an active guild member and at least one member from the origin can view the
channel. Snapshot cursors are authenticated and bound to the board version;
any concurrent mutation returns a restart response. The replica verifies the
final task count, contiguous lane/task ordering, resource identities,
timestamps, creator profiles, and current assignee memberships before replacing
the cache atomically and applying replica storage quotas. A creator profile is
included even if that creator is no longer a guild member.

Every authoritative tracker mutation emits an ordered
`guild.tracker.board.invalidate` event in the same transaction. Replicas
serialize that event with hydration, discard the stale board contents, and
durably queue a local `TRACKER_BOARD_UPDATE` full-refresh dispatch. Tracker
invalidations use their own board version and do not restart unrelated
multi-page guild snapshots. A full guild resynchronization purges tracker
caches, and channel or guild removal cascades them; the next eligible read
hydrates current state again. If the home cannot supply a coherent snapshot,
the read fails with the existing temporary federation-unavailable response
instead of returning a mixed or stale board.

Deleting the channel cascades its board, lanes, tasks, and task nonce metadata. The
schema downgrade refuses to proceed while tracker channels exist, preventing a
rollback from silently discarding task data.

## PostgreSQL storage verification

The normal unit suite does not require a database. Operators and CI can run the
opt-in PostgreSQL constraint test against a disposable database after applying
the current migrations:

```console
KAEDE_TRACKER_TEST_DATABASE_URL=postgresql+asyncpg://kaede:kaede@127.0.0.1:5432/kaede \
  pytest -q tests/test_tracker_postgres.py
```

The test exercises deferred lane and task swaps, creator-scoped nonce
uniqueness, membership `ON DELETE SET NULL`, channel cascades, and committed
tracker gateway outbox persistence on PostgreSQL 16.
