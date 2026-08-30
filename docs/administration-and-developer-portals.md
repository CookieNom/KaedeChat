# Instance Administration and Developer Portal

Kaede provides two responsive management areas on the web and desktop:

- `/administration` is for instance owners and delegated staff.
- `/developers` is available to every active local human account.

The supported desktop app embeds the same web interface. Native mobile clients
provide corresponding Developer Portal and capability-gated Instance
Administration screens under User Settings.

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
generation. Authority-owned guild installations move to `suspended` and
advance their grant revision; a user installation remains owned by the user's
home and its grant revision is never rewritten by application runtime state.
The application home publishes signed, monotonic runtime snapshots so every
existing target independently requires both an active application and a valid
installation grant. Reactivating the application restores only the
suspension-owned guild rows and advances those guild grant revisions, so
targets and Gateways re-evaluate resumed access instead of reusing stale
authorization.

Suspension does not preserve private-conversation or cryptographic admission.
It terminally revokes the application's active DM capability leases, DM
grants and participant consents, MLS room-participation rows, and active voice
media sessions; affected encrypted rooms enter rekeying. After reactivation,
the worker must open a fresh DM capability and repeat the applicable consent,
room-admission, and MLS rekey flow. An old capability, consent, participation,
or media grant is never resumed merely because its source installation became
active again.

Independent removal remains terminal. A guild kick, ban, uninstall, failed
reinstall, or explicit revocation records the installation as `revoked` (and
sets its revocation timestamp); application reactivation never selects or
resurrects that row. Guild administrators therefore retain control over every
terminal membership and installation decision while an instance administrator
can safely pause and resume grants that the application suspension itself
paused.

Application suspension removes user-install commands and interaction delivery
from the active application surface without changing the independently owned
user grant. Reactivation makes a command eligible again only when that user-home
grant remains active and, on a foreign target, its bounded authority lease has
not expired. A user-revoked record remains revoked.

Only the application's home instance may perform this authority-state change.
A foreign instance administrator can enforce local target policy, but cannot
activate or suspend the remote application record; remote targets converge from
the home authority's signed runtime snapshot.

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
specific message and the reporter confirms the selective disclosure. The case
stores the disclosed text, a fingerprint of the original ciphertext, and
`server_verified=false`; moderators see that the evidence is reporter-supplied.
Room keys, other messages, and surrounding history are never included
automatically.

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
may create named teams and add human users from any federated Kaede home to
those shared workspaces.

Developer-team roles are separate from instance-administrator roles:

| Role          | Developer access                                |
| ------------- | ----------------------------------------------- |
| Owner         | Full team and application control               |
| Administrator | Application configuration and member management |
| Developer     | Workers and command definitions                 |
| Security      | Credentials, workers, and federation rules      |
| Analyst       | Read-only application and installation data     |
| Support       | Read-only support and installation data         |

The last owner cannot be demoted or removed. Personal teams remain single-user;
shared teams accept federated human members and can own at most 75 active
applications, matching Discord's developer-team capacity.

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

Developer teams and applications keep their authority when members use another
Kaede instance. The team home sends each remote member a full, signed,
revisioned projection of that member's role and applications. Each local member
has an independent durable high-water, while the shared team/application state
is fingerprinted once per team revision. A delayed older projection is ignored;
a different projection for the same member at the same revision is rejected.
Removal sends an empty revocation projection, while the team home still rechecks
current membership and role for every operation.
The latest snapshot for each remote member is coalesced and retried without an
age cutoff until the member's home accepts it. Acknowledged snapshots return to
normal bounded retention, so a long outage cannot lose a revocation and team
churn does not create an unbounded archive.

The normal qualified team and application URLs remain usable from the member's
home instance. That home forwards only a closed set of typed member, application,
private Directory preview, credential, worker, command, template, rule, installation, asset, and emoji
operations to the authority with a 15-second deadline and a one-use replay ID.
Responses are bound to the exact request, authority, operation, and expected
status. Adding a user carries the selected composite identity, but a team home
accepts mutable profile fields only from that user's own home; third-party
profiles remain opaque until a direct signed profile proof arrives.

### App Directory

The web and desktop clients expose **App Directory** from all three Discord-like
server entry points: right-click a server in the server rail and hover **Server
Settings**, use the server menu, or open **Server Settings → App Directory**.
The standalone product-page Directory remains a desktop/browser surface,
matching Discord. Native mobile still discovers reviewed applications from the
chat-bar App Launcher and opens the existing native **Add App** review flow.

An application owner opts in from the Developer Portal and supplies a short
summary, category, one to five unique tags, a full description, HTTPS support,
privacy-policy and terms links, and at least one active install template. The
product profile may order up to five total uploaded images and allowlisted
YouTube video IDs, add up to five named HTTPS links, declare supported Discord
locales, and provide a localized description for any declared locale.
User-only listings use the same consent template as guild listings, but must
also publish an active global user-install command before the listing can claim
user-install support. The product page also derives up to five popular global
slash commands from authority-local use and up to three reviewed similar apps
from category and tag overlap, and exposes **Copy Link** for its portable
product URL. Changing public identity, discovery metadata, install support, or
ordered media removes the previous approval until an instance reviewer
approves the new revision.

Team members can open **Discovery Status** and **Preview App Directory** before
public approval or checklist completion. The private draft preview keeps the
same bounded identity, media, link, locale, command, and similar-app contracts,
but permits `verified: false` and nullable incomplete product fields; it never
invents placeholder content or applies public target-policy visibility. A fixed
readiness checklist is calculated independently by the application authority,
and a remote team member reaches the same preview through the replay-bounded
application-management RPC.

Staff with `bots.manage` review opted-in applications in **Administration →
Applications**. Approval, removal, and placement in the `featured`,
`staff-picks`, or `new-and-noteworthy` collections require a visible reason;
that reason is retained in the administration audit log. A remote application's
home remains its state authority, so a foreign administrator can apply local
instance policy but cannot forge the application's approval state.

Directory searches are bounded and cursor-paged. A user may search the local
catalog or choose one exact federated domain. The user's home signs the request,
the application home applies both the developer's target policy and the
requesting instance's block state, and the consumer binds every result to the
requested origin, application reference, filters, collection, ordering, and
cursor. Silenced or suspended peers cannot enumerate the catalog.

The chat-bar **App Launcher** sits to the right of the web/desktop composer and
to the left of the native-mobile composer. It keeps recent apps scoped to the
exact signed-in composite account, groups commands from current installations,
shows reviewed collections, and searches the selected instance's entire
catalog. The optional **Directory instance** control defaults to the user's
home but accepts one canonical federated domain; malformed domains and results
whose origin differs from that selection fail closed. Apps with no command in
the current channel still open their authority-attested install review instead
of producing an inert row. An app account's chat/member profile likewise shows
**Add App**, using the bot authority's active template even when the app is not
publicly listed; ordinary friendship controls are never rendered for bots.

### Guild installation

Install templates produce shareable links at:

```text
/applications/{application-ref}/install/{template-slug}
```

The invite card shows the application home, bot identity, requested scopes,
intents, named guild permissions, privacy/support links, and E2EE disclosure. The guild
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
remain unavailable. A `participant` installation can be admitted separately to
an encrypted channel by a guild administrator. Admission snapshots the app's
verified devices, rotates room keys, and grants only post-admission history.
Until a verified bot device is admitted, encrypted payloads remain opaque and
no API falls back to plaintext. A bot operator that receives room keys becomes
a recipient and may retain anything the bot decrypts.

### Developer API

| Method             | Path                                                  | Purpose                             |
| ------------------ | ----------------------------------------------------- | ----------------------------------- |
| `GET/POST`         | `/api/v1/developer-teams`                             | List or create teams                |
| `GET/POST`         | `/api/v1/developer-teams/{team}/members`              | List or add members                 |
| `PATCH/DELETE`     | `/api/v1/developer-teams/{team}/members/{user}`       | Change or remove a member           |
| `GET/POST`         | `/api/v1/applications`                                | List or create applications         |
| `GET/PATCH`        | `/api/v1/applications/{app}`                          | Read or change an application       |
| `GET`              | `/api/v1/applications/{app}/directory-preview`        | Read the private product preview and readiness status |
| `GET/POST`         | `/api/v1/applications/{app}/credentials`              | List or create control credentials  |
| `DELETE`           | `/api/v1/applications/{app}/credentials/{credential}` | Revoke a credential                 |
| `GET/POST`         | `/api/v1/applications/{app}/workers`                  | List or enroll workers              |
| `DELETE`           | `/api/v1/applications/{app}/workers/{worker}`         | Revoke a worker                     |
| `GET/PUT`          | `/api/v1/applications/{app}/commands`                 | Read or replace commands            |
| `GET/POST`         | `/api/v1/applications/{app}/install-templates`        | Manage invite links                 |
| `GET`              | `/api/v1/applications/{app}/installations`            | List installations                  |
| `GET`              | `/api/v1/applications/{app}/instance-rules`           | List exact-domain policy rules      |
| `PUT/DELETE`       | `/api/v1/applications/{app}/instance-rules/{domain}`  | Set or remove an exact-domain rule  |
| `GET/POST`         | `/api/v1/applications/{app}/assets`                   | List or commit application assets   |
| `POST`             | `/api/v1/applications/{app}/assets/tickets`           | Reserve an application asset upload |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/assets/{asset}`           | Read, update, or remove an asset    |
| `GET/POST`         | `/api/v1/applications/{app}/emojis`                   | List or commit application emoji    |
| `POST`             | `/api/v1/applications/{app}/emojis/tickets`           | Reserve an application emoji upload |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/emojis/{emoji}`           | Read, update, or remove app emoji   |
| `GET`              | `/api/v1/application-directory`                       | Search a local or selected remote catalog |
| `GET`              | `/api/v1/application-directory/bot-profiles/{bot}`    | Resolve a bot profile's authority-owned Add App action |
| `GET`              | `/api/v1/application-directory/{app}`                 | Read a reviewed application product page |
| `POST`             | `/api/v1/bot-control/applications/{app}/workers`      | Enroll using a control token        |
| `PUT`              | `/api/v1/bot-control/applications/{app}/commands`     | Sync commands using a control token |

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
