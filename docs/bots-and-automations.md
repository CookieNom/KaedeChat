# Bots and automations

Kaede applications provide bot accounts, commands, interactive components, and
event-driven automation. They work in local and federated guilds alike. If
you've written Discord bots, the API should feel familiar, but authority always
stays with the instance that owns each guild or conversation.

The management interfaces for application owners and instance operators are
covered in [Instance Administration and Developer Portal](administration-and-developer-portals.md).

## Platform model

An **application** is the developer-owned configuration for an integration. It
contains the public identity, commands, install templates, keys, policies, and
worker registrations.

A **bot user** is the non-human account attached to one application. It can join
guilds, receive roles, send messages, and appear in member lists. It has no
password, human session, recovery codes, or personal settings.

An **application home** is the instance in the application's immutable composite
ID, such as `123@apps.example`. It manages the application and publishes its
signed manifest and key history. It is an identity control plane, not a proxy
for bot traffic.

A **target instance** owns the resource the bot is using. A bot installed in a
guild on `chat.example` connects directly to `chat.example` for that guild's API
and Gateway traffic.

An **installation** is the target's grant to an application. It records the
guild, permissions, event intents, data access, channel restrictions, E2EE mode,
and grant revision.

A **worker** is a bot runtime or deployment identity. Workers have separate
asymmetric keys. Each worker can be limited to particular target domains,
scopes, intents, and concurrent Gateway sessions.

```text
                                REST + Bot Gateway
                         +------------------------------> Instance A
                         |
Bot worker --------------+------------------------------> Instance B
                         |
                         +------------------------------> Instance C

Application home:
  developer management, signed identity, keys, commands, policies,
  installation notifications, and revocation publication
```

One worker normally opens one multiplexed Gateway connection per target, not
one connection per guild. Because the application home sits outside the data
path, an unavailable home does not interrupt established installations.

The target remains authoritative. A signed application manifest proves who
published it. It does not prove that a bot is installed or permitted to act.

## Automation types

| Type                    | Reads ambient events             | Typical use                                        |
| ----------------------- | -------------------------------- | -------------------------------------------------- |
| Channel webhook         | No                               | Build output, alerts, and simple posting           |
| Interaction application | No by default                    | Slash commands and E2EE-safe interaction workflows |
| Event bot               | Only approved intents and fields | Moderation, logging, and stateful integrations     |

Existing `kwh_` channel webhook secrets remain write-only. They cannot be
upgraded to bot credentials. User-account automation and silent user
impersonation are not supported.

## Application identity and worker keys

Every application has an immutable composite reference such as
`123@apps.example` and a linked bot account. The application home is the
control plane for its profile, commands, install templates, target policy, and
worker authorizations. It is not a proxy for normal bot traffic.

The application home publishes a manifest in Kaede's existing signed federation
envelope. The manifest includes:

- the application and bot-account composite references
- the public profile, active status, scopes, intents, and permission ceiling
- exact allow or deny rules for target instances
- active install templates and command definitions
- active worker public keys and their authorization ceilings
- manifest and command generations

Targets verify the application home's instance signature and bind every
referenced identity to that origin. They enforce strict schemas and size
limits, and cache only the records needed for an active installation. Before
accepting an unknown or stale worker, a target refreshes worker authorization
directly from the application home.

You create a hash-only `kb1_ctl_` control credential in the Developer Portal.
During one-time enrollment, the Python wrapper generates an Ed25519 worker key
locally and submits only its public key. The private key is written to an
owner-only state directory and is used for target assertions and DPoP proofs.
Control credentials can enroll workers and publish commands. They cannot
connect as the bot, call runtime routes, or authenticate a human session.

E2EE device keys are a separate concern from the worker API key. The current API
carries typed encrypted envelopes and records the installation's E2EE mode, but
it never treats the worker signing key as an encryption key.

## Direct target authentication

Bot credentials use a separate authentication namespace from human sessions.
They cannot enter login, MFA, account, relationship, user settings, or
application-ownership routes.

Each target acts as the authorization server for its installations:

1. The worker sends a short-lived, signed client assertion to the target.
2. The target validates the registered worker key and its current delegation.
3. The target returns a short-lived opaque access token bound to a DPoP key.
4. Each REST request and Gateway login includes a fresh DPoP proof.
5. Authorization intersects the token ceiling with the current installation
   revision and live guild permissions.

The worker signs the canonical assertion bytes:

```text
kaede-worker-assertion-v1
123@apps.example
456789
https://chat.example/api/v1/bots/token
1786644000
1786644060
random-single-use-value
```

The request body carries those fields plus the URL-safe Ed25519 signature. The
target checks each one: the exact application reference, worker ID, audience,
60-second maximum assertion lifetime, worker validity and target allowlist,
current installation, and a single-use nonce.

Access tokens are random opaque values stored as hashes. A token is bound to:

- the application, bot user, worker, and target issuer
- the intersection of application and worker scope and intent ceilings
- the enrolled worker public-key thumbprint
- creation, eight-minute expiry, last use, and revocation

The target-token lifetime is eight minutes. Workers obtain another token with
their key rather than keeping a permanent target secret. Tokens never appear in
query strings, redirects, logs, or WebSocket URLs.

## Installation

A guild installation begins from a Kaede client or a portable bot invite. The
person approving it must currently have `MANAGE_GUILD` on the authoritative
guild.

1. The target resolves and verifies the application home's signed manifest.
2. It applies the target operator's instance blocks and the developer's exact
   target policy.
3. It checks that the signed-in user currently has `MANAGE_GUILD`.
4. The consent page shows the bot identity and origin, requested scopes,
   intents, permission bits, E2EE mode, and retention warning.
5. The target creates the visible bot member, managed bot role, installation,
   grant revision, audit record, and federated guild mutations in one
   transaction.
6. An enrolled worker then authenticates and connects directly to that target.

The install link uses the user's existing Kaede session. Bot tokens and control
credentials never enter the browser flow. A remote guild selected from a local
replica sends the install request to the authoritative guild home for the final
permission check and commit.

Uninstall is authoritative and immediate. It marks the installation revoked,
revokes target tokens, removes the managed role and bot membership, and
publishes the guild changes. It does not wait for the application home to be
online.

## Portable invite links

The application home publishes install templates at stable HTTPS URLs:

```text
https://apps.example/applications/123/install/moderation
```

The last path component is a signed template ID. A template defines requested
scopes, intents, permissions, supported contexts, optional channel restrictions,
and default E2EE mode. You can publish separate `commands-only`, `moderation`,
and `workflow` links without placing mutable permission bitfields in the URL.

Invite links contain no access token, worker key, receipt, reusable code, or
unsigned permission override. Locale and a suggested guild ID may be carried as
non-authoritative UI hints.

### Guild selection across federation

Opening an invite shows the application's icon, name, `BOT` badge, immutable
origin, publisher, verification state, description, and a plain-language access
summary. The user can add it to the current guild or choose another guild they
manage, including guilds on federated instances.

For a remote guild, the user's home sends a signed, actor-bound install intent
to the guild home. The guild home rechecks the user, membership, `MANAGE_GUILD`,
template revision, and its local policies. It returns a short-lived consent
challenge bound to the user, guild, application, grants, and expiry. The user
confirms that exact challenge through their existing home instance. A second
account on the guild home is not required.

If the authority is offline, lacks the capability, or rejects the application,
the client shows the specific reason and allows a later retry. The UI reports
success only after the authoritative transaction commits.

### Native invite embeds

Plaintext bot invite links may render as Kaede-native embeds:

```text
+----------------------------------------------------------+
| BOT APPLICATION                                          |
| [icon] Poll Garden                       Verified         |
|        123@apps.example                                  |
|                                                          |
| Polls, reminders, and scheduled announcements.           |
| Requests: Commands, Send messages, Attach files          |
|                            [Details] [Add to guild]       |
+----------------------------------------------------------+
```

Clients build this card from the verified manifest and template projection, not
from developer HTML. Text and images are bounded and sanitized. Counts, reviews,
and verification claims appear only when the displaying instance can verify
them. The add button always opens guild selection or the full consent screen;
it never installs immediately.

In an E2EE conversation, the server cannot inspect messages for invite links.
The client displays a normal link and resolves a native preview only after an
explicit action or a user-enabled preview policy. Resolution reveals the
application ID to the relevant instance but never shares room content or keys.

Deleting a template disables new installs without changing existing grants.
Expanding a template also never expands an existing installation. Invite
resolution is cached within bounded limits. It is rate-limited by user, source,
application, origin, and target.

## Federation policy

Policy is applied independently at each layer.

Application target policy supports:

- `open`
- `allowlist`
- `blocklist`
- `local_only`

An instance operator can disable remote applications, require review, or allow
and block exact origins and applications. The operator can also suspend an
application, publisher, key, or origin. A guild can be stricter than its
instance, and a worker can refuse targets that the developer does not trust.

Rules match only exact canonical HTTPS issuers or exact composite
application IDs. Wildcard and suffix rules are not supported. An explicit
denial or existing instance block always wins.

Policy changes show affected installations before confirmation and suspend them
immediately by default. Denials return a stable reason code and record the policy
layer and matching rule.

## Authorization controls

Scopes, intents, guild permissions, and data grants serve different purposes:

- A **scope** allows an API capability.
- An **intent** selects real-time event categories and fields.
- A **guild or channel permission** determines where the bot may act.
- A **data grant** allows sensitive content or history to be disclosed.

Effective access is the intersection of every applicable limit:

```text
application declaration
intersection installation approval
intersection worker and token ceiling
intersection bot roles and channel overrides
intersection current resource state
intersection instance and guild policy
intersection encryption mode
```

Supported scopes:

- `applications.commands`
- `interactions.respond`
- `guilds.read`, `guilds.manage`
- `channels.read`, `channels.manage`
- `members.read`
- `roles.read`, `roles.manage`
- `messages.metadata`, `messages.content`, `messages.history`
- `messages.send`, `messages.edit.own`, `messages.delete.own`
- `messages.manage`
- `attachments.read`, `attachments.write`
- `reactions.read`, `reactions.write`
- `moderation.members`, `moderation.messages`
- `voice.states.read`, `voice.moderate`
- `invites.manage`, `webhooks.manage`, `emojis.manage`
- `tasks.read`, `tasks.write`, `tasks.manage`
- `dm.send`

Supported intents:

- `guilds`
- `guild_members` and `guild_presences` (privileged)
- `guild_messages`
- `message_content` (privileged)
- `message_reactions`
- `guild_typing`
- `voice_states`
- `interactions`
- `guild_tasks`

An interaction-only bot normally needs only `applications.commands`,
`interactions.respond`, and the `interactions` intent. Being able to view a
channel does not imply access to content or history. A guild role cannot bypass
privileged-intent review, an installation ceiling, operator policy, or E2EE
consent.

Guild bots do not gain access to members' direct messages. `dm.send` covers only
opening the documented outbound conversation and sending into it. Ambient and
inbound direct-message access are outside this contract, and there is no
direct-message event intent. Bot attachment uploads require a guild
installation. DM creation, sending, and typing bind to one caller-selected
active installation. That exact installation must still grant `dm.send` and
`messages.send`; consent from another guild doesn't count.

## REST API

Bot endpoints use `/api/v1` and reuse the existing resource schemas and business
logic. Only handlers written to accept an `ActorPrincipal` admit a bot
principal.

### Developer management

These routes use an authenticated human or team member session:

| Method       | Path                                                  | Purpose                                  |
| ------------ | ----------------------------------------------------- | ---------------------------------------- |
| `POST`       | `/api/v1/applications`                                | Create an application and bot user       |
| `GET`        | `/api/v1/applications`                                | List owned or team-managed applications  |
| `GET/PATCH`  | `/api/v1/applications/{app}`                          | Read or update application configuration |
| `GET/POST`   | `/api/v1/applications/{app}/credentials`              | Create a named control credential        |
| `DELETE`     | `/api/v1/applications/{app}/credentials/{credential}` | Revoke a credential                      |
| `GET`        | `/api/v1/applications/{app}/instance-rules`           | List exact-domain target rules           |
| `PUT/DELETE` | `/api/v1/applications/{app}/instance-rules/{domain}`  | Set or remove an exact-domain rule       |
| `GET/POST`   | `/api/v1/applications/{app}/install-templates`        | List or create invite templates          |
| `GET`        | `/api/v1/applications/{app}/installations`            | List installations and health            |

Secrets and one-time enrollment codes are shown once. Later reads return only a
label, safe prefix/suffix, timestamps, expiry, last use, and revocation state.

### Installation management

| Method   | Path                                                      | Purpose                        |
| -------- | --------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/v1/bot-invites/{app}/{template}`                    | Resolve the signed invite card |
| `POST`   | `/api/v1/guilds/{guild}/integrations/bots`                | Confirm and commit an install  |
| `GET`    | `/api/v1/guilds/{guild}/integrations/bots`                | List installations             |
| `DELETE` | `/api/v1/guilds/{guild}/integrations/bots/{installation}` | Revoke immediately             |

Installation responses contain no worker or bot secret.

### Worker authentication

| Method   | Path                                              | Purpose                                        |
| -------- | ------------------------------------------------- | ---------------------------------------------- |
| `POST`   | `/api/v1/bot-control/applications/{app}/workers`  | Register a delegated worker public key         |
| `PUT`    | `/api/v1/bot-control/applications/{app}/commands` | Publish commands from deployment tooling       |
| `DELETE` | `/api/v1/applications/{app}/workers/{worker}`     | Revoke a worker and its sessions               |
| `POST`   | `/api/v1/bots/token`                              | Issue a DPoP-bound target token                |
| `GET`    | `/api/v1/bots/@me`                                | Read worker, installation, and target ceilings |

### Resource operations

Supported resource operations include:

- Guild, channel, role, member, and permission lookup, including opt-in member
  presence hydration.
- Guild update; channel and role create/update/reorder/delete; and member role
  assignment, removal, and atomic replacement.
- Forum channel policy, atomic forum-post creation, public/private/announcement
  threads, active and archived thread listing, lifecycle updates, and thread
  membership.
- Cursor-paginated message history when every required grant allows it.
- Message send and edit/delete-own operations, plus message moderation behind
  its own scope.
- Reactions and independently scoped attachment metadata/downloads.
- Installation-owned upload tickets with installation-level byte accounting.
- Guild invite, webhook, and emoji management.
- Voice mute/deafen, disconnect, and move moderation.
- Outbound direct-message creation without ambient DM reads.
- Allowed-mention controls that default to no broad role or everyone mentions.
- Typed `encrypted` and `content_unavailable` fields instead of empty content.

The principal runtime routes are:

| Capability         | Routes                                                                                                                                   |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Guild management   | `PATCH /api/v1/bots/guilds/{guild}`                                                                                                      |
| Channel management | `POST/PATCH /api/v1/bots/guilds/{guild}/channels`; `PATCH/DELETE /api/v1/bots/guilds/{guild}/channels/{channel}`                         |
| Forums and threads | create/list below `/api/v1/bots/channels/{parent}/threads`; existing-message create below `.../messages/{message}/threads`; lifecycle and membership below `/api/v1/bots/channels/{thread}` |
| Role management    | `POST/PATCH /api/v1/bots/guilds/{guild}/roles`; `PATCH/DELETE /api/v1/bots/guilds/{guild}/roles/{role}`; member-role `PUT/DELETE` routes |
| Attachments        | `POST /api/v1/bots/channels/{channel}/attachments`; `GET /api/v1/bots/attachments/{attachment}` and `/{variant}`                         |
| Invites            | `POST/GET /api/v1/bots/guilds/{guild}/invites`; `DELETE .../invites/{code}`                                                              |
| Webhooks           | create/list/update/rotate/delete below `/api/v1/bots/guilds/{guild}`                                                                     |
| Emojis             | list, upload ticket, commit, and delete below `/api/v1/bots/guilds/{guild}/emojis`                                                       |
| Voice moderation   | `PATCH/DELETE .../members/{user}/voice`; `POST .../voice/move`                                                                           |
| Outbound DM        | `POST /api/v1/bots/dms`                                                                                                                  |

Mutable guild, channel, and role payloads carry a `version`. Update requests
must send it through `If-Match`; stale updates fail instead of silently
overwriting another moderator's work. Role reorders carry one version per role.

`attachments.write` does not invent a local-human storage owner for a remote
bot identity. Pending and finalized bytes belong to the exact installation
that created the ticket, under the instance's configured upload and storage
limits. A bot cannot bind that ticket in another installation. On the read
side, `attachments.read` can download a human-authored attachment only once
it's bound to a message in a channel where that same installation still has
normal view/history permissions. Signed object-storage redirects never receive
bot tokens or DPoP proof headers.

## Commands and interactions

Commands are the preferred interface when an application does not need ambient
message access. Kaede supports:

- Slash commands.
- User and message context commands.
- Immediate responses and deferred responses within the interaction lifetime.

Commands are application-global and become available after authoritative
registration. Each atomic command replacement increments the signed application
command generation.

Example command definition:

```json
{
  "name": "poll",
  "type": "chat_input",
  "description": "Create a poll",
  "default_member_permissions": ["SEND_MESSAGES"],
  "contexts": ["guild"],
  "options": [
    {
      "type": "subcommand",
      "name": "create",
      "description": "Create a new poll",
      "options": [
        {
          "type": "string",
          "name": "question",
          "description": "The question to ask",
          "required": true,
          "min_length": 1,
          "max_length": 500
        }
      ]
    }
  ]
}
```

Names use a stable normalized grammar. Descriptions, localization data, option
counts, nesting, and total bytes are bounded and validated by the authority.

### Command and response routes

| Method    | Path                                  | Purpose                                    |
| --------- | ------------------------------------- | ------------------------------------------ |
| `GET/PUT` | `/api/v1/applications/{app}/commands` | Read or atomically replace global commands |

### Delivery

An interaction carries everything a handler needs: its ID, application and
installation references, guild and channel references, the invoking user, the
command definition, bounded options, an encryption payload when required, and
an expiry time.

Delivery uses the direct Bot Gateway. Events are at-least-once and carry a
stable interaction ID, so handlers must be idempotent.

An interaction remains valid for fifteen minutes. A bot may respond immediately
or defer before sending its one channel response. E2EE responses must carry an
encrypted message envelope.

## Bot Gateway

The Bot Gateway uses a separate authentication and session namespace from user
connections. A worker obtains an eight-minute target token, connects to
`/api/v1/bots/gateway`, and proves possession of its enrolled Ed25519 key.

The target sends a heartbeat interval. The worker identifies with its token,
proof, and last per-topic cursors. `READY` lists active installations visible
to that worker. Event frames carry a type, topic, topic sequence, and filtered
payload. The server retains a bounded topic backlog. A cursor older than that
backlog receives an explicit `GAP` event instead of silently missing data.

Intent selection controls delivery. Scopes and current installation grants
control fields and actions. Message content and attachments are filtered
independently: content needs the privileged intent plus `messages.content`,
and attachment projections need `attachments.read`. The event category itself
also needs its read scope on both the worker and installation (for example
`messages.metadata`, `reactions.read`, `members.read`, or
`voice.states.read`). Sparse presence and voice projections carry the
canonical guild context from the authorized topic, so a multi-instance worker
doesn't have to infer the authority from a user or channel reference.

Sessions have hard limits. Gateway session counts are atomically limited per
worker, token expiry is checked while the connection is open, and frames are
capped at 1 MiB. Heartbeats keep the session lease alive.

## Rate limits and backpressure

Runtime REST requests consume shared Dragonfly token buckets for both the
application and worker. The defaults allow 1,200 requests per application and
600 per worker each minute on a target. Some routes have their own smaller
buckets: token issuance, control operations, application creation, invite
resolution, interaction creation, and federated installs. Message sends,
reactions, uploads, slow mode, mention limits, and other reused chat actions
keep their ordinary per-resource limits.

A limited request returns `429`, `Retry-After`, and the standard
`X-RateLimit-*` headers. The response body has the stable `RATE_LIMITED` code
and `retry_after_ms`. The Python wrapper reads `Retry-After` and retries with
a bounded delay. It keeps a separate HTTP client, token, Gateway connection, and
reconnect loop for each target, so one offline instance does not prevent other
targets from operating.

## End-to-end encrypted conversations

Bot authorization is already fail-closed for E2EE. Servers never expose
plaintext content or plaintext history from an encrypted channel. Message
responses use `content: null`, retain the encrypted envelope when authorized,
and mark unavailable content explicitly.

The same rule applies to forums and threads. Their titles, tags, counts,
membership, and archive/lock state are ordinary metadata, but post and reply
content follows the child thread's encryption mode. A bot cannot create a post
in an E2EE-required forum or write to an active E2EE thread until a verified
bot-device MLS participant protocol exists; those requests fail instead of
falling back to plaintext.

Install templates support three E2EE modes:

- `disabled` rejects commands and bot messages in encrypted channels.
- `interaction_only` accepts only an explicit `encrypted_payload` submitted
  with a command. It grants no ambient message or history access.
- `participant` permits encrypted message envelopes only after the bot is an
  explicit room participant. It still never enables server-side plaintext
  search or history.

The current server, federation, UI, and Python model preserve those mode and
payload boundaries so the forthcoming device/key-distribution protocol can be
added without changing the Bot API contract. Until that protocol supplies and
verifies bot device keys, applications must treat encrypted payloads as opaque
ciphertext. No route falls back to plaintext.

Once a bot is deliberately made a cryptographic participant, its operator is a
recipient and can retain anything it decrypts. Removing access can stop future
delivery, but it cannot erase data already copied by that operator. Pre-install
history remains excluded by default.

## Storage model

The two bot migrations add durable PostgreSQL records for:

- developer teams and members
- applications and bot identities
- hash-only control credentials
- workers and public keys
- exact target rules, install templates, and commands
- installations and short-lived target-token digests
- interactions
- instance administrator grants and audit events
- Trust & Safety reports

Composite references retain both snowflake and origin. Database constraints
bind bot users to applications and enforce fixed role and state values. They
keep grants non-negative, prevent duplicate installations and command names,
and preserve complete foreign references. Deleting an application cascades its
private control records. Report and audit records follow their own retention
policy.

Dragonfly stores request nonces, DPoP replay markers, rate buckets, Gateway
session counts, topic cursors, and bounded event backlogs. Raw control tokens,
target access tokens, and worker private keys are never stored in plaintext by
the server.

## Python SDK

The first-party `kaede-bot` package is asynchronous and keeps a familiar
decorator-based interface. See
[Python bot API quickstart](bot-api-quickstart.md) for the complete enrollment
and startup flow.

One-time enrollment uses a control credential and stores the generated worker
private key in an owner-only directory. Normal startup loads that state:

```python
import asyncio

import kaede_bot as kaede

bot = kaede.Client(
    worker_state=kaede.WorkerState.load("/run/secrets/kaede-worker"),
    intents=kaede.Intents.default(),
)


@bot.command(name="ping", description="Check whether the bot is awake")
async def ping(interaction: kaede.Interaction) -> None:
    await interaction.respond("Pong!")


asyncio.run(bot.start("https://chat.example", "https://community.example"))
```

The package provides:

- `WorkerState.enroll`, safe local key persistence, and control-token command
  synchronization
- direct multi-target token exchange and DPoP signing
- heartbeat, cursor resume, bounded reconnect, and `Retry-After` handling
- `Client.event` and `Client.command` decorators
- typed resources and events: guilds, channels, members, roles, messages, and
  attachments; invites, webhooks, emojis, and task trackers; interactions,
  moderation, presence, and voice
- scoped CRUD and moderation helpers, safe presigned uploads/downloads,
  message history and reactions, and user lookup
- `content_unavailable`, encrypted payload, and composite-reference handling

A snowflake is never converted into a username. `fetch_user(EntityRef)`
resolves the authoritative profile. `User.handle` returns normal
`username@instance` formatting, while `User.mention` retains the full
composite reference.

Task tracker channels have a separate scope and event boundary. `tasks.read`
allows board fetches and is required alongside the `guild_tasks` intent for
tracker Gateway events. `tasks.write` admits task CRUD, while `tasks.manage`
admits lane and board-setting operations. The bot's current tracker permission
bits and channel overwrites are checked again for every request. See the
[task tracker channel contract](task-tracker.md#bots-and-applications) for the
routes, typed SDK resources, concurrency rules, and dispatches.

## User-facing behavior

User settings links to the responsive Developer Portal. Every active local user
may create applications. The portal manages personal or shared teams, the
application profile and defaults, commands, and hash-only control credentials.
It also manages worker public keys, install templates, exact instance policy
rules, and current installations. Secrets are shown once.

Portable invite links open a native consent card. The guild chooser lists only
guilds where the user can manage the guild, including federated replicas. The
card shows requested API scopes, Gateway intents, permission bits, E2EE
behavior, application origin, support and privacy links, and the third-party
retention warning. Guild settings has a separate Integrations page for
installed bots and immediate revocation.

Bot users have an authoritative `account_type=bot` discriminator and
application reference. Member lists and bot-authored messages render an
accessible `BOT` badge next to the name. The badge comes from trusted account
data, never from a nickname, role, embed, webhook name, or remote HTML. Bot
usernames use ordinary account formatting with a collision-resistant numeric
suffix. API payloads include the readable `username@instance` handle alongside
the composite snowflake reference.

## Operational rules

- A token works only at its issuing target and only with its DPoP key.
- Bot credentials never authenticate a human session.
- Current installation state and permissions are checked for every action and
  subscription.
- Remote schemas, sizes, counts, references, audiences, and generations are
  validated even when signed.
- Mutations, interactions, and event delivery are replay-bounded and
  idempotent.
- The SDK does not automatically fetch URLs found in message content.
- Target queues, parsers, storage, and retries are isolated.
- Revocation stops future target access but cannot erase data a bot copied.
- API authorization never implies trust in an E2EE device key.
- Bot loops are limited through provenance, hop limits, mention rules, duplicate
  suppression, intent defaults, and channel write limits.

Implementation order, migrations, and release checks are maintained in
[Instance Administration and Developer Portal](administration-and-developer-portals.md).
