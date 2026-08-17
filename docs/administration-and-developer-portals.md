# Instance Administration and Developer Portal

Kaede provides two responsive web areas:

- `/administration` is for instance owners and delegated staff.
- `/developers` is available to every active local human account.

The supported desktop app embeds the same web interface. Native mobile clients
do not include these management pages.

## Instance Administration

### Granting access

Administrative access belongs to an ordinary local user account. The host grants
the first owner role from the CLI:

```sh
docker compose exec api kaede admin-grant alice --role owner
```

Additional examples:

```sh
docker compose exec api kaede admin-grant bob --role trust_safety
docker compose exec api kaede admin-revoke bob --role trust_safety
```

Accepted roles are `owner`, `administrator`, `trust_safety`, `bot_reviewer`,
`operations`, and `auditor`. The command accepts a local `username` or
`username@this-instance`. Remote and bot accounts are rejected. Grant and revoke
operations are idempotent and use a database advisory lock.

Owner grants and removals remain CLI-only. An owner can delegate or revoke every
non-owner role from the Administration page. Production instances should keep
at least two owner-capable local accounts.

The browser never receives `KAEDE_ADMIN_TOKEN`. Every request uses the normal
local-user session and reloads active `InstanceAdminGrant` rows. Disabled users,
expired or revoked grants, remote users, and bot users are rejected.

### Roles

| Role           | Access                                                |
| -------------- | ----------------------------------------------------- |
| Owner          | Every administration capability; CLI controlled       |
| Administrator  | Operators, users, instances, bots, reports, and audit |
| Trust & Safety | Reports, local-user state, and instance policy        |
| Bot reviewer   | Bot applications and the audit log                    |
| Operations     | Instance policy and the audit log                     |
| Auditor        | Read-only reports and audit log                       |

`GET /api/v1/administration/@me` returns the caller's roles and effective
capabilities. The panel hides mutation controls the caller cannot use, but the
API still repeats every authorization check.

### Panel sections

**Overview** shows counts for local users, known instances, bot applications,
active bot installations, open reports, and blocked instances.

**Users** supports bounded local-username search and lets staff disable or
enable accounts. An administrator cannot disable their own account through this
endpoint.

**Applications** lists known local and federated bot applications. Staff with
`bots.manage` can suspend an application. Suspension advances its revocation
generation and suspends active installations. Re-enabling the application does
not silently restore guild grants; guild administrators keep control of bot
membership and installation state.

**Instances** manages exact-domain `silence` and `suspend` rules using Kaede's
existing `InstanceBlock` enforcement. Subdomains are included only when the
operator selects that option. Policy changes go through the existing lock and
reconciliation path, so queues and replicas observe the new rule consistently.

**Operators** lists active grants. Owners can add or revoke non-owner roles.
Owner rows are visible but remain CLI managed.

**Audit** lists recent instance actions with actor, action, target, safe
metadata, and timestamp. Sensitive secrets and message bodies are not written
to audit metadata.

### Reports and Trust & Safety

Users can report an accessible plaintext message from its context menu and view
the status of their own reports at `/reports`. Categories are fixed in code:

- spam
- harassment
- hate
- threats
- sexual content
- child safety
- impersonation
- privacy
- malware or phishing
- other

When a report is submitted, the server rechecks the reporter's channel access.
It then copies the message content, author reference, channel reference, and
message timestamp into the case, so later edits or deletion cannot erase the
review record. The reporter cannot read the evidence snapshot, assignee, or
internal resolution.

An E2EE message can be reported only after the reporter's client decrypts that
specific message and the reporter explicitly confirms selective disclosure.
The case stores the disclosed text, a fingerprint of the original ciphertext,
and `server_verified=false`; moderators see that the evidence is
reporter-supplied. Room keys, other messages, and surrounding history are never
included automatically.

The Trust & Safety queue is instance-level; guild moderators do not receive it.
Authorized staff can triage, review, request more information, record action or
no action, mark duplicates, and reopen cases. Report creation is rate limited to
10 per local user per hour.

### Administration API

| Method      | Path                                               | Purpose                          |
| ----------- | -------------------------------------------------- | -------------------------------- |
| `GET`       | `/api/v1/administration/@me`                       | Current roles and capabilities   |
| `GET`       | `/api/v1/administration/overview`                  | Instance counts                  |
| `GET/POST`  | `/api/v1/administration/operators`                 | List or grant non-owner roles    |
| `DELETE`    | `/api/v1/administration/operators/{grant}`         | Revoke a non-owner role          |
| `GET`       | `/api/v1/administration/users`                     | Search local accounts            |
| `PATCH`     | `/api/v1/administration/users/{user}`              | Disable or enable an account     |
| `GET`       | `/api/v1/administration/applications`              | List bot applications            |
| `PATCH`     | `/api/v1/administration/applications/{app}`        | Activate or suspend an app       |
| `GET/PATCH` | `/api/v1/administration/reports[/{report}]`        | Review and update reports        |
| `GET/PUT`   | `/api/v1/administration/instances/blocks`          | List or create instance rules    |
| `DELETE`    | `/api/v1/administration/instances/blocks/{domain}` | Remove an instance rule          |
| `GET`       | `/api/v1/administration/audit`                     | Read recent audit events         |
| `POST`      | `/api/v1/reports`                                  | Submit a plaintext report        |
| `GET`       | `/api/v1/reports/@me`                              | Read the reporter-safe case view |

## Developer Portal

Every active local human user may create applications. Opening `/developers`
provisions a protected **Personal** team if the account does not have one yet.
Personal is always available and stays private to that account. It owns new
applications by default, even when the user also belongs to shared teams. Users
may create named teams and add other local users to those shared workspaces.

Developer-team roles are separate from instance-administrator roles:

| Role          | Developer access                                |
| ------------- | ----------------------------------------------- |
| Owner         | Full team and application control               |
| Administrator | Application configuration and member management |
| Developer     | Workers and command definitions                 |
| Security      | Credentials, workers, and federation rules      |
| Analyst       | Read-only application and installation data     |
| Support       | Read-only support and installation data         |

The last owner cannot be demoted or removed. Team membership is limited to local
human accounts.

### Applications

Creating an application creates a normal federated bot user with
`account_type=bot`. Bot usernames use regular `username@domain` formatting and
a unique numeric suffix. API payloads also include the bot's composite snowflake
reference. The Python SDK exposes both forms as `User.handle` and `User.ref`;
snowflakes are never decoded into usernames.

An application page manages:

- display name, description, support and privacy URLs
- requested REST scopes, Gateway intents, permissions, and E2EE modes
- scoped control credentials
- Ed25519 worker keys and exact target-domain delegation
- global slash and context command definitions
- guild invite templates
- exact-instance allow and deny rules
- active and suspended installations

Control credentials are shown once and stored as hashes. They carry only
`workers.manage` and `commands.manage`. They cannot authenticate a user session,
a bot REST request, or a Gateway connection. Revocation is immediate for future
control operations.

Workers generate their Ed25519 private key locally. Only the public key is
enrolled. A worker exchanges a short assertion for an eight-minute target token.
Every REST or Gateway request carries a nonce-based DPoP proof bound to the
HTTP method, path, query, token, and worker key.

### Guild installation

Install templates produce shareable links at:

```text
/applications/{application-ref}/install/{template-slug}
```

The invite card shows the application home, bot identity, requested scopes,
intents, permission bits, privacy/support links, and E2EE disclosure. The guild
chooser includes only guilds where the current user has Manage Guild or
Administrator. The authoritative guild home rechecks that permission before
committing the installation.

The same link works for a guild on another Kaede instance. The target retrieves
a signed manifest from the application home and applies both operators'
federation policy. It then creates the bot member and role at the guild
authority and stores the installation grant. Bot workers connect directly to
that target instance; runtime traffic is not proxied through the application
home.

Guild administrators manage installed bots at
`/g/{guild-ref}/integrations`. Removal revokes future access, removes the bot
member and role, advances the grant revision, and sends a signed uninstall to a
remote application home when required.

### Commands and E2EE

Command definitions use a bounded Discord-style schema. Option types include
subcommands, strings, integers, numbers, booleans, users, channels, roles,
mentionables, and attachments. Names, descriptions, choices, numeric and string
bounds, option counts, nesting depth, and total command-set bytes are validated.

Installed slash commands appear in the channel composer. Invocations are
permission checked by the authoritative guild home and delivered over the bot's
direct Gateway. A bot can respond immediately or defer within the 15-minute
interaction lifetime.

In plaintext channels, content and history require their own scope and the
`message_content` intent. In an E2EE channel, server-side content and history
remain unavailable. `interaction_only` accepts only the encrypted command
payload a user submits with the command; nothing arrives ambiently.
`participant` reserves the permission boundary for the forthcoming device/key
protocol. Until a verified bot device is admitted, encrypted payloads remain
opaque and no API falls back to plaintext. A bot operator that later receives
room keys becomes a recipient and may retain anything the bot decrypts.

### Developer API

| Method         | Path                                                  | Purpose                             |
| -------------- | ----------------------------------------------------- | ----------------------------------- |
| `GET/POST`     | `/api/v1/developer-teams`                             | List or create teams                |
| `GET/POST`     | `/api/v1/developer-teams/{team}/members`              | List or add members                 |
| `PATCH/DELETE` | `/api/v1/developer-teams/{team}/members/{user}`       | Change or remove a member           |
| `GET/POST`     | `/api/v1/applications`                                | List or create applications         |
| `GET/PATCH`    | `/api/v1/applications/{app}`                          | Read or change an application       |
| `GET/POST`     | `/api/v1/applications/{app}/credentials`              | List or create control credentials  |
| `DELETE`       | `/api/v1/applications/{app}/credentials/{credential}` | Revoke a credential                 |
| `GET/POST`     | `/api/v1/applications/{app}/workers`                  | List or enroll workers              |
| `DELETE`       | `/api/v1/applications/{app}/workers/{worker}`         | Revoke a worker                     |
| `GET/PUT`      | `/api/v1/applications/{app}/commands`                 | Read or replace commands            |
| `GET/POST`     | `/api/v1/applications/{app}/install-templates`        | Manage invite links                 |
| `GET`          | `/api/v1/applications/{app}/installations`            | List installations                  |
| `GET`          | `/api/v1/applications/{app}/instance-rules`           | List exact-domain policy rules      |
| `PUT/DELETE`   | `/api/v1/applications/{app}/instance-rules/{domain}`  | Set or remove an exact-domain rule  |
| `POST`         | `/api/v1/bot-control/applications/{app}/workers`      | Enroll using a control token        |
| `PUT`          | `/api/v1/bot-control/applications/{app}/commands`     | Sync commands using a control token |

See [Bot API quickstart](bot-api-quickstart.md) for the Python SDK and
[Bot and automation API](bots-and-automations.md) for runtime routes, Gateway
events, rate limits, federation, and E2EE behavior.

## Storage and migrations

The schema is introduced by migrations `94397280832f` and `b84e2f6a19d7`.
They add administrator grants/audit, developer teams, applications, credentials,
workers, policies, templates, commands, installations, tokens, interactions,
and reports. Bot users reuse the existing user/member/message payloads with an
explicit bot discriminator, so badges work in rosters and beside message authors
without a separate identity system.

Secrets are never stored in plaintext. Control and target tokens are hash-only,
and worker private keys stay outside Kaede. Installation, token, interaction,
report, and federation paths have explicit count, byte, time, replay, and rate
bounds.
