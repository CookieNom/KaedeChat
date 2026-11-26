# Task tracker channels

Task tracker channels are Kaede channel type `17`. Each holds an ordered,
lane-based board of guild work. A task has a stable key, title, description,
priority, due date, creator, and optional assignee. Web, desktop, the Flutter
client, the human REST API, and the bot API share the same resource model.

A tracker channel has no message history, typing state, pins, or composer. Its
board loads from the tracker API and updates from tracker Gateway events.

Task titles and descriptions are ordinary guild data, not message content, and
tracker channels don't support E2EE. Don't put secrets in tasks. An
installation granted `tasks.read` can read them wherever its live channel
permissions allow.

## Board model

Creating a tracker channel creates one board and four ordered lanes in the same
transaction:

| Lane | Kind | Completion lane |
| --- | --- | --- |
| Backlog | `backlog` | No |
| Planned | `planned` | No |
| In progress | `in_progress` | No |
| Done | `completed` | Yes |

Guild managers can rename, recolor, reorder, add, and remove lanes. A lane
that still has tasks can't be removed, and neither can the last lane. Moving a
task into a completion lane sets `completed_at`, and moving it out clears it.

Each board has a 2–10 character ASCII key prefix: uppercase, starting with a
letter, letters and digits only. If channel creation omits
`tracker_key_prefix`, Kaede derives one from the channel name. Task numbers
only increase and are never reused, giving keys such as `OPS-42`.

| Limit | Value |
| --- | --- |
| Boards per channel | 1 |
| Lanes per board | 50 |
| Tasks per board | 5,000 |
| Title | 200 characters |
| Description | 10,000 characters |
| Lane name | 100 characters |

Priorities are `none`, `low`, `medium`, `high`, or `urgent`. Due dates must
include a timezone offset.

A task has at most one assignee, who must be a current guild member. Removing a
member clears their assignments in the same transaction, bumps each affected
board version, and queues full-refresh notifications. A foreign key also blocks
stale assignees if membership is deleted directly. Creators stay attributed
after they leave. Creation accepts an optional 1–64 character `client_nonce`
for safe retries. Repeating a nonce with the same request returns the original
task; repeating it with different input returns a conflict.

## Custom task fields

Open **Board settings → Add custom field** to add a field to every task in
the channel. Field types are short or long text, dropdown, multiple choice,
number, date, checkbox, link, people, channel, and attachment. People fields
hold extra assignments such as reviewers without replacing the main assignee.
Channel fields pick channels from the same guild.

On mobile, open **Tracker settings → Add field**. Custom fields are available
when creating or editing a task and in its read-only details. Tap an image or
video attachment to preview it; use **Search all members** to find an assignee
outside the currently loaded member list.

A channel can have up to 50 fields, shown in the task editor in field order.
Managers can rename fields, edit choices, and remove fields. Removing one asks
for confirmation and deletes its values from every task. A field's type can't
change, and choices that tasks still use can't be removed.

Edit custom values in the task details dialog. People fields follow the normal
assignment rules, so adding or removing someone else needs
`ASSIGN_TRACKER_TASKS`. Custom assignments don't grant edit rights, and
removing a member from the guild clears them.

Each attachment field takes up to ten uploaded files or external links, with
image and video previews. Uploads use the normal quotas and scanning. While a
task references an upload, only channel viewers can open it; the uploader can
also preview it before it's attached. External links keep the hosting site's
permissions. Unreferenced uploads fall under the configured media retention
cleanup.

Custom values are limited to 32 KB per task, and field definitions to 64 KB per
board.

## Permissions

Tracker access comes from the actor's live guild roles plus the channel's
permission overwrites. Application scopes never replace these checks.

| Permission | Capability |
| --- | --- |
| `VIEW_CHANNEL` | Read the board and receive its events |
| `CREATE_TRACKER_TASKS` | Create tasks |
| `EDIT_OWN_TRACKER_TASKS` | Edit, move, or remove a task the actor created or is assigned to |
| `MANAGE_TRACKER_TASKS` | Edit, move, or remove any task |
| `ASSIGN_TRACKER_TASKS` | Assign or unassign other guild members |
| `MANAGE_TRACKER` | Change the key prefix and manage lanes |

Anyone can assign or unassign themselves without `ASSIGN_TRACKER_TASKS`, but
assigning someone else at creation still needs it. Administrators keep the
usual bypass; everything else is channel-scoped.

New guilds give members `CREATE_TRACKER_TASKS` and `EDIT_OWN_TRACKER_TASKS` by
default. On upgrade, roles and overwrites that allowed or denied
`SEND_MESSAGES` get the same allow or deny for those two permissions.
Management and assignment permissions are opt-in.

## Human REST API

Identifiers are composite references such as `20@chat.example`. Snowflakes,
task counters, and permission masks are decimal strings in JSON.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/channels/{channel}/tracker` | Fetch the complete ordered board |
| `PATCH` | `/api/v1/channels/{channel}/tracker` | Change `key_prefix` or `custom_fields` |
| `POST` | `/api/v1/channels/{channel}/tracker/lanes` | Create a lane |
| `PATCH` / `DELETE` | `/api/v1/channels/{channel}/tracker/lanes/{lane}` | Edit or remove a lane |
| `POST` | `/api/v1/channels/{channel}/tracker/lanes/{lane}/move` | Move a lane to `position` |
| `POST` | `/api/v1/channels/{channel}/tracker/tasks` | Create a task |
| `PATCH` / `DELETE` | `/api/v1/channels/{channel}/tracker/tasks/{task}` | Edit or remove a task |
| `POST` | `/api/v1/channels/{channel}/tracker/tasks/{task}/move` | Move a task to `lane_id` and `position` |
| `POST` | `/api/v1/channels/{channel}/tracker/attachments/{action}` | Upload, commit, or read task field media (`ticket`, `commit`, `read`) |

Changes to an existing board, lane, or task need its current `version` in
`If-Match`. A missing header returns `428`, and a stale version returns `412`
with the current one. Clients should reload the board and let the user reapply
the change. Task numbering and nonce checks are serialized per board.

## Bots and applications

The bot API mirrors every route under `/api/v1/bots/channels/...`. A bot gets
the intersection of its worker ceiling, installation grant, optional channel
restriction, and current managed-role permissions.

| Scope | Access |
| --- | --- |
| `tasks.read` | Fetch boards and receive tracker events |
| `tasks.write` | Create and mutate tasks, subject to live task permissions |
| `tasks.manage` | Change board settings and lanes, subject to `MANAGE_TRACKER` |

Changing board settings returns the full board, so it also needs `tasks.read`.
`tasks.manage` alone never exposes task content.

Subscribe to the `guild_tasks` intent to receive these Gateway dispatches:

- `TRACKER_BOARD_UPDATE`
- `TRACKER_LANE_CREATE`, `TRACKER_LANE_UPDATE`, `TRACKER_LANE_DELETE`
- `TRACKER_TASK_CREATE`, `TRACKER_TASK_UPDATE`, `TRACKER_TASK_DELETE`

Create and update events carry the full lane or task. Delete events carry its
composite reference and the parent channel reference. Moves arrive as update
events. Every lane and task event includes `board_version`, so consumers can
drop older updates.

Delivery is at least once. Key state by the full composite reference, keep
handlers idempotent, and refetch the board after a sequence gap or when your
resume cursor is older than the retained stream. The board snapshot and each
resource's `version` are authoritative, so never let event order overwrite a
newer version. Mutations and their events commit together to a SQL outbox
that's retried at least once a minute, so a Dragonfly or task-queue outage
doesn't lose a committed change.

When `TRACKER_BOARD_UPDATE` has `full_refresh` set, refetch the board instead
of patching local state. This happens when one change affects many resources,
such as a lane's completion setting or a reorder. Its optional `reason` is for
diagnostics only; don't branch on it.

The Python SDK provides `Channel.is_tracker`, `Channel.tracker()`, typed
`TrackerBoard`, `TrackerLane`, and `TrackerTask` resources, CRUD helpers, typed
delete and board-update events, and the `guild_tasks` intent.

Bots configure fields with `PATCH /api/v1/bots/channels/{channel_ref}/tracker`
(`custom_fields`) and set values in task POST/PATCH requests
(`custom_values`), with the same schemas, version checks, membership rules,
and validation as human clients. Configuring fields needs `tasks.manage` and
`tasks.read`; task writes need `tasks.write`. Role permissions apply as well.

Bots reach task media through
`POST /api/v1/bots/channels/{channel_ref}/tracker/attachments/{action}`. Human
clients use the same actions without scopes:

| Action | Payload | Required bot scopes | Result |
| --- | --- | --- | --- |
| `ticket` | `filename`, `content_type`, `size` | `tasks.write`, `attachments.write` | Existing upload-ticket shape; PUT file bytes to `upload_url` |
| `commit` | Qualified `attachment_id` | `tasks.write`, `attachments.write` | Pending attachment scan status, or `{id, name, type}` ready for a task field |
| `read` | Qualified `attachment_id` | `tasks.read`, `attachments.read` | Short-lived private `{url}` |

Ticket requests share the upload rate limit. All three responses use
`Cache-Control: private, no-store`. Media is still subject to channel access,
ownership and reference checks, quotas, and scanning. See the
[SDK automation examples](bot-api-quickstart.md#task-tracker-automation) for
custom fields, explicit clears, and resumable attachment commits.

## Authority and federation

The guild home is authoritative for tracker content. Human writes made on a
replica travel over the signed, replay-bounded guild management transport and
are authorized again at the home before they commit. Bot tokens and DPoP
proofs are target-bound, so the Python SDK sends tracker requests straight to
the authority instead of relaying credentials through another instance.

Replicas serve reads from a bounded cache. Once the requester passes the
replica's live `VIEW_CHANNEL` and tracker-read checks, a missing or invalidated
board is fetched from the home through a signed, paginated snapshot endpoint.
The home serves a peer only while that origin has an active guild member and
at least one of its members can view the channel. Snapshot cursors are
authenticated and tied to the board version, and a concurrent change forces a
restart. Before swapping in the new cache and applying storage quotas, the
replica checks the task count, contiguous lane and task order, identities,
timestamps, creator profiles (included even for former members), and current
assignee memberships.

Every authoritative change also emits an ordered
`guild.tracker.board.invalidate` event in the same transaction. Replicas
discard the stale board and queue a local `TRACKER_BOARD_UPDATE` full refresh.
These invalidations don't restart unrelated guild snapshots. A full guild
resync purges tracker caches, and removing the channel or guild deletes them.
If the home can't supply a coherent snapshot, the read fails with the usual
temporary federation-unavailable response rather than a mixed or stale board.

Deleting the channel deletes its board, lanes, tasks, and nonce records. The
schema downgrade refuses to run while tracker channels exist, so a rollback
can't discard task data.

## PostgreSQL storage verification

The normal unit suite doesn't need a database. To check the PostgreSQL
constraints, apply the current migrations to a disposable database and run:

```console
KAEDE_TRACKER_TEST_DATABASE_URL=postgresql+asyncpg://kaede:kaede@127.0.0.1:5432/kaede \
  pytest -q tests/test_tracker_postgres.py
```

It covers deferred lane and task swaps, per-creator nonce uniqueness,
membership `ON DELETE SET NULL`, channel cascades, and the tracker outbox on
PostgreSQL 16.
