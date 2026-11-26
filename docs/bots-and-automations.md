# Bots and automations

This is the bot API reference: authentication, installations, scopes, REST
routes, Gateway events, and encryption. For a runnable first bot and feature
examples, use the [Bot SDK guide](bot-api-quickstart.md).

The management interfaces for application owners and instance operators are
covered in [Instance Administration and Developer Portal](administration-and-developer-portals.md).

[Authentication](#direct-target-authentication) · [Installation](#installation) ·
[Scopes](#authorization-controls) · [REST](#rest-api) ·
[Interactions](#commands-and-interactions) · [Gateway](#bot-gateway) ·
[E2EE](#end-to-end-encrypted-conversations)

## Platform model

An **application** is the developer-owned configuration for an integration. It
contains the public identity, commands, install templates, keys, policies, and
worker registrations.

A **bot user** is the non-human account attached to one application. It can join
guilds, receive roles, send messages, and appear in member lists. It has no
password, human session, recovery codes, or personal settings.

An **application home** is the instance in the application's immutable composite
ID, such as `123@apps.example`. It manages the application and publishes its
signed manifest and key history. It is a control plane, not a proxy: runtime
bot traffic goes directly to the resource owner.

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

A worker normally opens one multiplexed Gateway connection per target, not one
per guild. An unavailable application home doesn't interrupt established
installations.

The target remains authoritative. A signed application manifest proves who
published it. It does not prove that a bot is installed or permitted to act.

## Automation types

| Type | Reads ambient events | Typical use |
| --- | --- | --- |
| Channel webhook | No | Build output, alerts, and simple posting |
| Interaction application | No by default | Slash commands and E2EE-safe interaction workflows |
| Event bot | Only approved intents and fields | Moderation, logging, and stateful integrations |

Existing `kwh_` channel webhook secrets remain write-only. They cannot be
upgraded to bot credentials. User-account automation and silent user
impersonation are not supported.

## Application identity and worker keys

Every application has an immutable composite reference such as
`123@apps.example` and a linked bot account. The application home publishes a
manifest in Kaede's signed federation envelope. It contains:

- the application, owning developer-team, and bot-account composite references
- the public profile, active status, declared scopes and intents, and default
  install permissions
- exact allow or deny rules for target instances
- active install templates and command definitions
- active worker public keys and their authorization ceilings
- manifest and command generations

Targets verify the home's instance signature and bind every referenced identity
to that origin, including the developer team. They enforce strict schemas and
size limits and cache only what an active installation needs. A replay at the
same generation must be exactly equivalent. Conflicting same-generation data
and lower-generation rollbacks fail closed. Children missing from a replay are
removed only when their generation strictly advances. Before accepting an
unknown or stale worker, a target refreshes the worker's authorization from the
application home.

You create a hash-only `kb1_ctl_` control credential in the Developer Portal.
During one-time enrollment, the Python wrapper generates an Ed25519 worker key
locally and submits only its public key. The private key goes to an owner-only
state directory and signs target assertions and DPoP proofs. Control
credentials can enroll workers and publish commands. They cannot connect as the
bot, call runtime routes, or authenticate a human session.

E2EE device keys are separate from the worker API key. Each participating
worker enrolls a verified `kbe_` MLS device at the application home, keeps its
signed KeyPackage supply there, and holds its MLS private state locally. The
worker signing key is never accepted as an MLS identity, message, or media key.
See [Participant-device lifecycle](#participant-device-lifecycle).

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
target checks the exact application reference, worker ID, and audience. It also
checks the 60-second maximum assertion lifetime, worker validity and target
allowlist, the current installation, and a single-use nonce.

Access tokens are random opaque values stored as hashes. A token is bound to:

- the application, bot user, worker, and target issuer
- the intersection of application and worker scope and intent ceilings
- the enrolled worker public-key thumbprint
- creation, eight-minute expiry, last use, and revocation

Workers get a new token with their key instead of keeping a permanent target
secret. A token works only at its issuing target and only with its DPoP key.
Tokens never appear in query strings, redirects, logs, or WebSocket URLs.

## Installation

A guild installation begins from a Kaede client or a portable bot invite. The
person approving it must currently have `MANAGE_GUILD` on the authoritative
guild and already hold every permission the bot requests.

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

An application's `default_permissions` seeds new install links, much like
Discord's Default Install Settings. It isn't a permanent cap. The approved
template permissions initialize the bot's managed role, and administrators edit
that role afterwards under **Guild settings → Roles**. Current roles and channel
overwrites decide guild permissions, so changes there take effect without
reinstalling or changing the installation revision. The original bits are kept
only as a record of the initial grant. Managed bot roles can't be manually
assigned, unassigned, or deleted, and uninstalling removes the role. Other roles
can still be assigned to the bot.

API scopes, event intents, installation channel restrictions, and
encrypted-channel consent stay independent. A role permission doesn't add a
missing scope or authorize encrypted participation. **Integrations** shows the
initial permissions and lets administrators change channel restrictions.

Uninstall is authoritative and immediate. It marks the installation revoked,
revokes target tokens, removes the managed role and bot membership, and
publishes the guild changes. It does not wait for the application home to be
online.

### User installations

Discord-style user installs are a separate, commands-only integration type.
They make application commands available to the installing user in the
approved `guild`, `bot_dm`, and `private_channel` contexts. They don't create a
guild member or make the bot a participant in a private conversation.

The grant is limited to `applications.commands`, `interactions.respond`, and
optional `attachments.read`/`attachments.write` for interaction media. It can't
grant ambient message or channel/DM events, outbound DMs, calls, or voice.
Explicit command and component interactions still arrive over the application's
Gateway. In a guild without `USE_EXTERNAL_APPS`, a user-installed command
response is forced ephemeral, as on Discord.

Users review, narrow command contexts, and revoke these grants under **User
settings → Authorized apps**. Guild-installed bots stay under **Guild settings →
Integrations**.

When a command runs through another federated authority, that authority keeps
only a signed, bounded mirror of the user-home grant. The mirror lease lasts at
most 20 minutes, a little longer than the 15-minute interaction response window.
Commands, Gateway targets, DM/E2EE admission, and media checks all reject an
expired foreign mirror immediately. A sweep that runs at least once a minute
removes its target presence and cryptographic admission even if the user home
never sends an uninstall. A newer signed grant can renew the mirror, but a
target can't invent or advance its authority revision.

## App Directory discovery

Applications can opt in to the reviewed App Directory. A listing needs a
published public profile, a category, one to five tags, HTTPS policy and
support links, and an active install template. A listing that offers user
installation also needs an active global user-install command.

Profiles can add up to five named HTTPS links, supported Discord locales, and
localized descriptions keyed by those locales. The media carousel holds at most
five entries, each an owned store image or an allowlisted YouTube video ID.

Instance reviewers approve one public revision and can add it to curated
collections. Changing the public profile, install settings, or store media sends
the listing back for review. A listing is never an installation grant;
installing still goes through the portable invite and consent flow.

The Directory is a desktop and browser surface, opened from a server-rail
right-click, the server menu, or Server Settings. A product page shows the
verified origin and listing details, up to five popular global slash commands,
up to three reviewed similar apps, the install choices, and **Copy link**.

Team members can open a private preview before approval. It returns the draft
with incomplete fields left null, plus an ordered readiness checklist, and
never fills in placeholder content. Remote applications serve the preview
through the management RPC, so drafts never reach the public Directory routes
or non-members.

Federated search targets one exact domain. The local instance sends a signed,
rate-limited request to that application home and rejects any result whose
identity, origin, filters, ordering, cursor, media, or install claims don't
match the request. Developer target rules and instance silence/suspension rules
apply before results are listed.

The chat-bar **App Launcher** sits right of the composer on web and desktop and
left of it on mobile. It combines recents, installed commands, reviewed
collections, and catalog search. Its instance selector defaults to the user's
home and can target one federated Directory authority, and responses are
rebound to the selected origin before display. Apps without commands open the
consent review for their active install template. Bot
profiles use the same lookup for **Add App** and never show friendship actions.

## Portable invite links

The application home publishes install templates at stable HTTPS URLs:

```text
https://apps.example/applications/123@apps.example/install/moderation
```

The last path component is the template slug. A template defines requested
scopes, intents, permissions, supported contexts, and default E2EE mode. You can
publish separate `commands-only`, `moderation`, and `workflow` links without
putting mutable permission bitfields in the URL. Channel and category
restrictions belong to the target guild, not the template, and are edited after
installation under **Server Settings → Integrations**. An empty restriction set
means every channel the bot role and channel overrides still allow.

Invite links contain no access token, worker key, receipt, reusable code, or
unsigned permission override. Locale and a suggested guild ID may be carried as
non-authoritative UI hints.

Templates can't be edited, deleted, or disabled yet. To change what an invite
requests, create a template with a new slug and share that link. A new template
never expands an existing installation.

Invite resolution isn't cached. The instance resolving the link allows each
client address 30 lookups a minute. For a remote application, its home also
limits each requesting instance to 300 manifest fetches a minute.

### Guild selection across federation

Opening an invite shows the application's name, bot handle, home instance,
description, and requested access. For guild installs, the chooser lists guilds
where the user has Manage Guild or Administrator, including federated guilds.
The first one is selected; the user can pick another before authorizing.

For a remote guild, the user's home sends a signed, actor-bound install intent
to the guild home. The guild home rechecks the user, membership,
`MANAGE_GUILD`, the active application and template, and its local policies. It
installs with the template's current grants; the consent page doesn't submit a
reviewed template revision. No second account on the guild home is needed.

If installation fails, the page shows an error and lets the user try again. The
UI reports success only after the authoritative transaction commits.

### Native invite embeds

Plaintext bot invite links may render as Kaede-native embeds:

```text
+----------------------------------------------------------+
| [icon]  APP AUTHORIZATION                                |
|         Poll Garden                                      |
|         apps.example                                     |
|                                                          |
| Polls, reminders, and scheduled announcements.           |
| 3 API scopes    disabled E2EE                            |
| [Review and add]                                         |
+----------------------------------------------------------+
```

Clients build the card from the resolved invite, not from developer HTML. It
shows the bot's avatar, application name and home domain, the template or
application description, the number of requested API scopes, and the E2EE mode.
There are no counts, reviews, or verification badges. **Review and add** opens
the full consent page; it never installs immediately.

In an E2EE conversation the server can't see invite links. The client shows a
normal link and resolves a preview only after a user action or a user-enabled
preview policy. Resolving reveals the application ID to the relevant instance,
never room content or keys.

## Federation policy

Each layer applies its own policy. Application target policy is one of `open`,
`allowlist`, `blocklist`, or `local_only`.

The developer adds `allow` or `deny` rules for exact instance domains, with no
wildcard or suffix matching. A `deny` rule applies under every policy, while
`allow` rules matter only for `allowlist`. The application home is always
allowed and can't have a rule of its own. Workers can narrow their target
domains further.

Operators have their own controls. The application home's administrators can
suspend a local application, and any operator can block a whole instance.
Instance blocks match one domain, plus its subdomains if the operator selects
that option. A `deny` rule or an instance block always wins over an `allow`.

A policy or rule change applies without a confirmation step. The application
home publishes a new runtime snapshot. Each target the change excludes suspends
the application's guild installations there and revokes its live tokens, then
restores them if a later change allows that target again. An excluded instance
can't fetch the manifest, so its invites resolve as `BOT_INVITE_NOT_FOUND`.

Suspending an application at its home advances its runtime generation. Guild
installations can be suspended and later restored with a new grant revision.
User installations belong to the user's home and are never rewritten; targets
check the signed application state and the signed user grant separately.
Kicked, banned, uninstalled, or revoked grants stay terminal.

Suspension permanently revokes DM capabilities, DM participant grants and
consents, MLS room participation, and live voice media. After reactivation the
bot needs a fresh DM capability and any required consent, admission, and rekey.
Only the application home can change the application's authority state, and
only the user home can change a user-install revision.

## Authorization controls

Scopes, intents, guild permissions, and data grants do different jobs:

- A **scope** allows an API capability.
- An **intent** selects real-time event categories and fields.
- A **guild or channel permission** determines where the bot may act.
- A **data grant** allows sensitive content or history to be disclosed.

Effective access is the intersection of every applicable limit:

```text
application-declared scopes, intents, and target policy
intersection installation approval
intersection worker and token ceiling
intersection bot roles and channel overrides
intersection current resource state
intersection instance and guild policy
intersection encryption mode
```

The guild permission evaluator treats a bot as an ordinary member. Its roles,
category and channel overwrites, administrator grant, timeouts, and AutoMod
interaction blocks resolve the same way as a human's. Scopes and installation
grants only narrow that result. Role and member changes use the same
highest-role checks, so a bot can't create, move, assign, remove, moderate, or
edit anything at or above its own highest role, and can't act on the guild
owner.

Supported scopes:

- `applications.commands`, `applications.assets.manage`,
  `applications.emojis.manage`
- `interactions.respond`
- `audit_logs.read`
- `automod.rules.read`, `automod.rules.manage`, `automod.executions.read`
- `guilds.read`, `guilds.manage`, `guilds.assets.manage`
- `channels.read`, `channels.manage`, `channels.overwrites.read`,
  `channels.overwrites.manage`
- `members.read`
- `roles.read`, `roles.manage`
- `events.read`, `events.manage`
- `expressions.read`, `expressions.manage`
- `installations.read`, `integrations.read`, `integrations.manage`
- `messages.metadata`, `messages.content`, `messages.history`
- `messages.send`, `messages.edit.own`, `messages.delete.own`,
  `messages.manage`
- `attachments.read`, `attachments.write`
- `reactions.read`, `reactions.write`
- `polls.read`, `polls.write`
- `moderation.bans`, `moderation.members`, `moderation.messages`,
  `moderation.prune`
- `soundboard.read`, `soundboard.use`, `soundboard.manage`
- `voice.states.read`, `voice.connect`, `voice.listen`, `voice.speak`,
  `voice.stream`, `voice.moderate`
- `invites.read`, `invites.manage`, `webhooks.read`, `webhooks.manage`,
  `emojis.manage`
- `tasks.read`, `tasks.write`, `tasks.manage`
- `dm.send`

Supported intents follow Discord's current families, plus a few Kaede additions:

- Guild-side: guild, moderation, expression, integration, webhook, invite,
  voice, presence, message, reaction, typing, scheduled-event, AutoMod, and poll.
- Direct-message: message, reaction, typing, and poll.
- Kaede-specific: `interactions` and `guild_tasks`. The older `voice_states`,
  `message_reactions`, and `guild_typing` aliases stay valid.

`guild_members`, `guild_presences`, and `message_content` match Discord's
privileged intents. Kaede has no separate review for them; installation
approval grants them like any other intent. Content and member data still need
`messages.content` and `members.read`.

A command-only guild bot normally needs only `applications.commands`,
`interactions.respond`, and the `interactions` intent. Being able to invoke or
answer a command doesn't imply access to channel content or history. A guild
role can't bypass an installation ceiling, operator policy, or E2EE consent.

Guild bots get no ambient access to members' direct messages. A bot can reach
only a conversation where its bot user is a participant. Every DM REST call is
bound to one capability derived from one active guild installation that the
caller selects. That installation must grant `dm.send` plus the operation's
ordinary scope, such as `messages.history`, `reactions.write`, or `polls.read`.
Grants from different installations are never combined. DM Gateway events use
the matching direct-message intent and the same single-grant rule for metadata,
content, attachments, and history. User installs can't open a DM capability.

DM uploads use the selected installation's quota and need `attachments.write`;
reads need `attachments.read`. Revoking the installation, removing the bot from
the conversation, or reducing either scope removes access immediately.

### Federated DM capability routing

Direct-message access can involve three authorities without making the
application home a data proxy:

```text
A = application and bot home
B = selected guild-install or user-install authority
C = deterministic conversation authority

worker -> A: open or refresh the DM for one qualified installation
A -> B: request B's original signed installation-capability proof
A -> C: create or resolve the conversation with that proof
worker -> C: direct REST, Bot Gateway, calls, and voice using the capability
```

B's proof binds one stable grant ID to:

- the installation kind and qualified installation, application, and bot
- the target human, the DM pair, and C
- the effective scopes, intents, restrictions, and E2EE mode
- the source grant revision

A and C verify and keep B's original signature. Neither can widen it or swap in
a different installation. The lease lasts at most ten minutes, and the SDK
refreshes it through A before expiry. The protocol accepts either signed
installation kind, but user installs can't currently grant `dm.send` and
`direct_messages`, so outbound DMs open from a guild installation.

A routine refresh extends the expiry without changing the authorization
revision. A change to the source grant, scopes, intents, restrictions, E2EE
mode, suspension, or revocation advances the revision and publishes a monotonic
tombstone to A and C. REST and Gateway authorization fail closed as soon as the
exact capability is missing, expired, superseded, or revoked.

Calls are hosted at C and pin each bot participant to its grant ID and
authorization revision. Renewing an unchanged lease keeps the call alive. A real
authorization change ends only that bot's call and media admission.

The refresh request carries only the opaque grant ID. A derives a fresh signed
proof from the recorded source and ignores any installation, human, pair,
channel, or authority fields from the worker. It confirms that the grant ID,
source lineage, conversation, and C are unchanged before returning the new
expiry.

The SDK doesn't persist B's proof, and the worker never receives a reusable
one. At startup, a worker enrolled with `dm.send` pages its active leases from A
with `GET /api/v1/bots/dm-capabilities?limit={n}&after={opaque_kbdg_cursor}` and
force-refreshes each with
`POST /api/v1/bots/dm-capabilities/{kbdg_id}/refresh`. It then runs a
separate Gateway and refresh loop at C for each one. A terminal authorization
failure or an expired lease removes that context; transient failures are
retried until expiry. A commands-only worker skips DM bootstrap.

## REST API

Bot endpoints use `/api/v1` and reuse the existing resource schemas and business
logic. Only routes under `/api/v1/bots` and others that explicitly accept
`BotPrincipal` admit a bot.

### Developer management

These routes use an authenticated human or team member session:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET/POST` | `/api/v1/developer-teams` | List or create developer teams |
| `GET/POST` | `/api/v1/developer-teams/{team}/members` | List or add team members |
| `PATCH/DELETE` | `/api/v1/developer-teams/{team}/members/{user}` | Change a member's role or remove them |
| `POST` | `/api/v1/applications` | Create an application and bot user |
| `GET` | `/api/v1/applications` | List owned or team-managed applications |
| `GET/PATCH` | `/api/v1/applications/{app}` | Read or update application configuration |
| `GET` | `/api/v1/applications/{app}/directory-preview` | Read the team-only product preview and readiness checklist |
| `GET/POST` | `/api/v1/applications/{app}/credentials` | List or create named control credentials |
| `DELETE` | `/api/v1/applications/{app}/credentials/{credential}` | Revoke a credential |
| `GET/POST` | `/api/v1/applications/{app}/workers` | List workers or enroll a worker public key |
| `GET` | `/api/v1/applications/{app}/instance-rules` | List exact-domain target rules |
| `PUT/DELETE` | `/api/v1/applications/{app}/instance-rules/{domain}` | Set or remove an exact-domain rule |
| `GET/POST` | `/api/v1/applications/{app}/install-templates` | List or create invite templates |
| `GET` | `/api/v1/applications/{app}/installations` | List installations and health |
| `GET/POST` | `/api/v1/applications/{app}/assets` | List or commit application assets |
| `POST` | `/api/v1/applications/{app}/assets/tickets` | Reserve an application asset upload |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/assets/{asset}` | Read, update, or remove an asset |
| `GET/POST` | `/api/v1/applications/{app}/emojis` | List or commit application emoji |
| `POST` | `/api/v1/applications/{app}/emojis/tickets` | Reserve an application emoji upload |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/emojis/{emoji}` | Read, update, or remove app emoji |
| `GET` | `/api/v1/application-directory` | Search reviewed local or remote apps |
| `GET` | `/api/v1/application-directory/bot-profiles/{bot}` | Resolve a bot profile's Add App action |
| `GET` | `/api/v1/application-directory/{app}` | Read a reviewed app product page |

Qualified team and application references route these calls to their authority,
so a federated team member manages apps from their own home instance. The
authority rechecks the member's live team role. Asset and emoji uploads count
against that application authority's quota.

Secrets and one-time enrollment codes are shown once. Later reads return only a
label, safe prefix/suffix, timestamps, expiry, last use, and revocation state.

### Installation management

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/bot-invites/{app}/{template}` | Resolve the signed invite card |
| `POST` | `/api/v1/guilds/{guild}/integrations/bots` | Confirm and commit an install |
| `GET` | `/api/v1/guilds/{guild}/integrations/bots` | List installations |
| `PATCH` | `/api/v1/guilds/{guild}/integrations/bots/{app}` | Update channel restrictions |
| `DELETE` | `/api/v1/guilds/{guild}/integrations/bots/{app}` | Revoke immediately |
| `GET/POST` | `/api/v1/users/@me/application-installations` | List or create the caller's user installations |
| `PATCH/DELETE` | `/api/v1/users/@me/application-installations/{installation}` | Narrow or revoke one user installation |

Installation responses contain no worker or bot secret.

### Worker authentication

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/bot-control/applications/{app}/workers` | Register a delegated worker public key |
| `PUT` | `/api/v1/bot-control/applications/{app}/commands` | Publish global commands from deployment tooling |
| `PUT` | `/api/v1/bot-control/applications/{app}/guilds/{guild}/commands` | Publish one guild's commands from deployment tooling |
| `DELETE` | `/api/v1/applications/{app}/workers/{worker}` | Revoke a worker and its sessions |
| `POST` | `/api/v1/bot-workers/targets` | List the target instances that currently have installations |
| `POST` | `/api/v1/bot-workers/home-token` | Issue a DPoP-bound token at the application home |
| `POST` | `/api/v1/bots/token` | Issue a DPoP-bound target token |
| `GET` | `/api/v1/bots/@me` | Read the bot user, worker ID, and token scopes and intents at the application home |

Both `/bot-workers` routes take the same signed worker assertion as
`/bots/token` and work only at the application home. Target discovery returns
domains, not installation lists, filtered by the target policy and the worker's
domain restrictions. The SDK polls again after the interval the response sets,
currently 30 seconds. The home token covers app-owned resources that don't need
an installation: `/bots/@me`, application assets and emoji, DM capability
setup, and E2EE device management.

### Resource operations

Supported resource operations include:

- Guild, channel, role, member, and permission lookup, with opt-in member
  presence.
- Guild update; channel and role create/update/reorder/delete; member role
  assignment, removal, and atomic replacement.
- Forum channel policy, atomic forum-post creation, public, private, and
  announcement threads, active and archived listing, lifecycle updates, and
  thread membership.
- Cursor-paginated message history when every required grant allows it.
- Message send and edit/delete-own, rich embeds, live-reference forwarding,
  polls, announcement publishing and follows, attachment-backed voice messages,
  and message moderation behind its own scope.
- Reaction add/remove/list, removing another member's reaction, clearing one
  emoji or all reactions, and separately scoped attachment metadata and
  downloads.
- Installation-owned upload tickets with per-installation byte accounting.
- Guild invite, webhook execution and messages, emoji/sticker, application
  asset/emoji, soundboard, AutoMod, prune, bulk-ban, and audit-log management.
- Voice and external scheduled events: CRUD, status transitions, subscriber
  pagination, subscriptions for human members, and validated invite targets.
- Voice join, listen, speak, and video receive; file and soundboard playback;
  mute/deafen/disconnect/move moderation.
- Direct messages under one capability (see
  [Authorization controls](#authorization-controls)): creation, history,
  messages, reactions, poll reads and finalization, pins, typing, calls, voice,
  and attachments.
- Typed `encrypted` and `content_unavailable` fields instead of empty content.

Allowed mentions follow Discord. Ordinary bot messages parse visible user, role,
and everyone mentions by default. Interaction and webhook messages default to
visible user mentions only. Explicit policies, including reply-author
notification, survive federation and edits.

Bot typing uses the same routing and authorization as sending a message. A
guild bot needs its active installation, `messages.send`, channel restriction,
membership, and channel visibility. A DM bot also needs the unexpired
capability grant and revision for that conversation, with both `dm.send` and
`messages.send`. Federated typing is a ten-second, direct, per-recipient signal
that is never queued. Token webhooks have no typing route.

Announcement following works like Discord. The **Follow** action is in the
announcement channel. The resulting Channel Follower is a type-2 webhook managed
from **Guild settings → Integrations → Webhooks** in the destination guild.
Following needs source `channels.read` plus channel visibility, and target
`webhooks.manage` plus `MANAGE_WEBHOOKS`; it doesn't need send permission in the
source. Destination managers can rename the follower, replace or clear its
avatar, move it to another eligible plaintext text channel, or delete it.

Bots and humans get the same federation behavior. When the application home,
source guild, and target guild are on different instances, the worker supplies
separate receiver-bound actor intents. Follow setup runs
prepare/accept/finalize and is idempotent per generation, and each published copy is delivered by its own durable job. Edits
and source deletions still reach delivered copies after an unfollow. Copied
interactive controls work only when the same application has an active
`applications.commands` installation and membership in the target guild.

Soundboard list, get, and play calls go straight to the guild authority, even
for federated guilds. It checks the target token, active installation
membership, granted scope, live channel permissions and restrictions, and
current voice occupancy. Playback capabilities are short-lived and go only to
current room occupants.

- Discord-compatible `POST /channels/{channel}/send-soundboard-sound` returns
  `204 No Content`.
- The Python voice SDK uses the bot-only
  `POST /channels/{channel}/soundboard-playback-grants` extension instead, so it
  can mix the sound into its own voice transport. It downloads grants only from
  the exact HTTPS `media.<guild-authority>` host and refuses redirects,
  credentials, alternate production ports, and other hosts.

Sound creation, editing, and deletion stay on the guild home. Editing or
deleting the bot's own sound needs the create-expression permission; another
creator's sound needs the manage-expression permission.

### Guild message search

`Client.search_guild_messages()` sends the request directly to the qualified
guild authority. Both the worker token and the installation must grant
`messages.history` and `messages.content` and include the privileged
`message_content` intent. The bot member must keep `VIEW_CHANNEL` and
`READ_MESSAGE_HISTORY` in every returned channel. Attachment projections appear
only when both grants also include `attachments.read`.

The bot API supports Discord's current guild-search filters:

- text and slop, channels, authors and signed user/bot/webhook author types
- user, role, and everyone mentions; replied users and messages; pinned state
- snowflake or timestamp bounds and every documented `has` value, including
  sound, sticker, poll, and snapshot
- embed type and provider, link hostname, attachment filename and extension,
  and NSFW inclusion
- relevance or timestamp sorting and opaque pagination

Unknown, conflicting, malformed, or unsupported filter values are rejected, not
ignored. The current Meilisearch backend supports only its native phrase slop
of `2`, and any other explicit slop is rejected.

End-to-end encrypted messages are never indexed. The response lists only
encrypted channels the bot can currently view, so search coverage can't reveal
a hidden channel. The SDK revalidates every result against the requested guild
and channel before exposing it.

The principal runtime routes are:

| Capability | Routes |
| --- | --- |
| Guild management | `PATCH /api/v1/bots/guilds/{guild}` |
| Channel management | `POST/PATCH /api/v1/bots/guilds/{guild}/channels`; `PATCH/DELETE /api/v1/bots/guilds/{guild}/channels/{channel}` |
| Forums and threads | create/list below `/api/v1/bots/channels/{parent}/threads`; existing-message create below `.../messages/{message}/threads`; lifecycle and membership below `/api/v1/bots/channels/{thread}` |
| Role management | `POST/PATCH /api/v1/bots/guilds/{guild}/roles`; `PATCH/DELETE /api/v1/bots/guilds/{guild}/roles/{role}`; member-role `PUT/DELETE` routes |
| Attachments | `POST /api/v1/bots/channels/{channel}/attachments`; `GET /api/v1/bots/attachments/{attachment}` and `/{variant}` |
| Invites | create/guild-list/revoke with expiry, use limits, temporary membership, verified live-stream targets, event association, role grants, and target-user management below `/api/v1/bots/guilds/{guild}/invites`; create and delete accept `X-Audit-Log-Reason`, while `DELETE` returns the deleted typed Invite with HTTP 200, including across federation; channel managers list one exact channel at `/api/v1/bots/guilds/{guild}/channels/{channel}/invites` |
| Webhooks | create/list/update/rotate/delete below `/api/v1/bots/guilds/{guild}`; token execution and message fetch/edit/delete on the webhook API |
| Expressions | guild emoji/sticker list/get/upload/edit/delete and role/availability restrictions; application asset and emoji CRUD below `/api/v1/bots/applications/@me` |
| Rich messages | embeds, components/views, poll create/read/end, forwarding, reactions, announcement follows, and crosspost operations below `/api/v1/bots/channels/{channel}` |
| AutoMod/moderation | AutoMod rule CRUD; prune estimate/execute; bulk bans with per-member failures; ordinary member/ban operations |
| Soundboard | guild sound list/upload/edit/delete and voice-channel play operations |
| Audit log | cursor-paged and filtered `GET /api/v1/bots/guilds/{guild}/audit-logs` |
| Voice moderation | `PATCH/DELETE .../members/{user}/voice`; `POST .../voice/move` |
| Direct messages | `POST /api/v1/bots/dms` at A; capability-bound message, reaction, poll-read/end, pin, typing, call, voice, and attachment routes directly at C under `/api/v1/bots/channels/{channel}` |

Private-channel payloads use Discord's public channel type numbers: `DM` is
type `1` and `GROUP_DM` is type `3`. API, Gateway, command-option, and SDK
projections always expose a group conversation as `3`, whatever the internal
storage type. A user-installed command may run in a group DM, but applications
can't start, join, or connect voice to a group-DM call.

As on Discord, applications can create a poll, read its voters, receive vote
events, and end a poll they authored. They can't cast or remove a vote. Bot vote
routes return `BOT_POLL_VOTE_UNSUPPORTED`; `polls.write` doesn't let a bot vote
on a human's behalf.

Mutable guild, channel, and role payloads carry a `version`. Update requests
must send it through `If-Match`, and stale updates fail instead of overwriting
another moderator's work. Role reorders carry one version per role.

Upload bytes, pending and finalized, belong to the installation that created
the ticket, under the instance's upload and storage limits. A bot can't bind
that ticket in another installation. `attachments.read` can download a
human-authored attachment only after it's bound to a message in a channel where
the same installation still has view and history permissions. Signed
object-storage redirects never receive bot tokens or DPoP proof headers.

## Commands and interactions

Commands are the preferred interface when an application doesn't need ambient
message access. Kaede supports:

- Slash commands.
- User and message context commands.
- Guild-installed and user-installed commands in guild, bot-DM, and private
  channel contexts.
- Strict typed options, nested subcommands, choices and bounds, channel-type
  restrictions, attachment options, and dynamic autocomplete.
- Immediate and deferred responses, original-response fetch/edit/delete,
  ephemeral responses, follow-ups, and response attachments within the
  interaction lifetime.
- Buttons, string/user/role/mentionable/channel selects, Kaede checkboxes,
  text-input modals, and Discord-style callback views with timeout,
  persistence, interaction checks, and stale-view fencing.
- Polls in channel or private interaction responses, with human voting, voter
  pagination, application finalization, and real-time updates.

Commands are global by default. You can also publish a separate command set for
one guild where the application is installed. Either set becomes available
after authoritative registration, and each atomic replacement increments the
signed application command generation.

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

Names use a stable normalized grammar. The authority validates and bounds
descriptions, localization data, option counts, nesting, and total size.

### Command and response routes

| Method | Path | Purpose |
| --- | --- | --- |
| `GET/PUT` | `/api/v1/applications/{app}/commands` | Read or atomically replace global commands |
| `GET/PUT` | `/api/v1/applications/{app}/guilds/{guild}/commands` | Read or atomically replace one guild's commands |
| `GET` | `/api/v1/applications/{app}/guilds/{guild}/commands/permissions` | List a guild's command permission overrides |
| `GET/PUT` | `/api/v1/applications/{app}/guilds/{guild}/commands/{command}/permissions` | Read or replace one command's overrides in a guild |
| `POST` | `/api/v1/bots/interactions/{interaction}/callback` | Send the one initial callback |
| `GET/PATCH/DELETE` | `/api/v1/bots/interactions/{interaction}/responses/@original` | Read or mutate the original response |
| `POST` | `/api/v1/bots/interactions/{interaction}/followups` | Create a follow-up |
| `GET/PATCH/DELETE` | `/api/v1/bots/interactions/{interaction}/followups/{response_id}` | Read or mutate one follow-up by durable ID |
| `POST` | `/api/v1/bots/interactions/{interaction}/responses/{@original\|response_id}/polls/expire` | End the exact public or private interaction poll |

The command permission routes use a signed-in guild manager's session, not a
bot token.

### Delivery

An interaction carries its ID, application and installation references, guild
and channel references, the invoking user, the command definition, bounded
options, an encryption payload when required, and an expiry time. As on
Discord, it also captures at invocation `version: 1`, the invoker and guild
locales, effective application permissions, the attachment-size limit, and
every install owner that authorized it. Owner references stay
instance-qualified. A guild-sourced bot-DM interaction reports `"0"` as its
public guild-install owner; Kaede keeps the real source installation privately
for routing.

Guild interactions expose the invoking `member`, including its permission
snapshot, instead of a duplicate top-level `user`. Private interactions expose
`user`. Component and modal-submit interactions include a safe source `message`
projection for both durable and ephemeral sources. An ephemeral projection is
marked non-durable and can't be used as a channel message. E2EE content stays
inside its opaque envelope.

Interactions arrive over the direct Bot Gateway. Delivery is at-least-once with
a stable interaction ID, so handlers must be idempotent.

Clients may include an optional BCP-47-style `locale` (2–16 characters) in
interaction requests. The user's home server uses the saved account language,
or the supplied device language when the preference is `system`. Older clients
without a device locale fall back to `en-US`. This applies to commands,
autocomplete, buttons and selects, and modal submissions.

Users can turn off **Share my language with bots and apps** in Privacy settings
(`PATCH /api/v1/users/@me/settings` with `share_locale_with_bots: false`).
Sharing is on by default. When it's off, new interactions carry `locale: "und"`
(undetermined), including across federation, and the real language isn't
forwarded. The Python SDK exposes this as `interaction.locale is None`; older
SDK versions still parse the `"und"` string. `guild_locale` describes the guild
and doesn't depend on this setting. Turning sharing off doesn't recall language
already sent with earlier interactions.

An interaction token is valid for fifteen minutes. A bot must respond or
acknowledge within 3 seconds, or the interaction fails with
`INTERACTION_ACK_TIMEOUT`. After acknowledging, it can create or edit the
deferred original response. The server validates callback types and response
state transitions. Component callbacks are bound to the authoring application
and current view version. E2EE responses must carry an encrypted message
envelope.

The callback route supports Discord's `with_response` query. `true` returns an
interaction callback response wrapper, with `resource.message` when the callback
creates or updates a message. `false` or no query returns an empty `204`.
Kaede's legacy `/response` route still returns the created message with `201`.
Current Python SDK releases always send `with_response=true` and unwrap message
callbacks.

A public response is a channel `Message`; a private or ephemeral response is a
separate response object. When an interaction helper creates, fetches, or edits
a public original or follow-up, the Python SDK attaches a private, write-once
binding: the interaction ID, original or follow-up kind, follow-up response ID,
target, and install context. `Message.edit()`, `delete()`, and `end_poll()` then
use the interaction routes above, so commands-only user installs can manage
their own public responses without channel authority. Other channel operations
stay unavailable when the response has no guild-install or DM grant.

Private responses never become `Message` resources. Editing, deleting, and
ending their polls go through the `Interaction` helpers. Poll finalization
verifies the interaction record and authoring application; a public one also
updates the channel poll and sends the normal message update.

Webhook-token messages get their own immutable binding. Standard, Slack, and
GitHub execution with `wait=true`, and token-authenticated fetch and edit,
privately keep the webhook ID, token, target, and optional `thread_id`. Their
`Message.edit()` and `delete()` stay on
`/api/v1/webhooks/{webhook}/{token}/messages/{message}`. A generic payload can't
supply this binding, and the token never appears in resource representations.
Generic channel actions fail locally instead of borrowing a bot grant.
`Message.end_poll()` is unavailable because there's no token-scoped poll-expire
route.

## Bot Gateway

The Bot Gateway uses a separate authentication and session namespace from user
connections. A worker gets a target token, connects to `/api/v1/bots/gateway`,
and proves possession of its enrolled Ed25519 key.

The target sends a heartbeat interval. The worker identifies with its token,
proof, and last per-topic cursors. `READY` lists the active installations
visible to that worker and the active conversation capabilities at that target.
Event frames carry a type, topic, topic sequence, and filtered payload. DM and
call events name their exact capability lineage and come from conversation
authority C, not through A or B. The server keeps a bounded backlog per topic.
A cursor older than the backlog gets an explicit `GATEWAY_GAP` event instead of
silently missing data.

There is no session-ID or opcode-6 Resume flow. After any disconnect, the SDK
waits with exponential backoff from one to thirty seconds plus jitter, gets or
refreshes the target token, and sends a new Identify with the persisted topic
cursors. A cursor advances only after application handlers finish, so a crash
replays unfinished work. Routine DM lease renewal keeps the capability's cursor
namespace; a real authorization revision starts a new grant-revision namespace.

The authority closes the connection with one of these codes:

| Code | Reason |
| --- | --- |
| `4000` | The voice authority for a voice command is unavailable |
| `4008` | Gateway command or presence update rate limit exceeded |
| `4009` | Live authorization changed; reconnect with a fresh token |
| `4400` | Invalid frame, cursor set, or command payload, or an unsupported opcode |
| `4401` | Identify was missing or authentication failed |
| `4403` | Invalid intents or E2EE device ID, or a member request without the required intent |
| `4408` | Missed heartbeat window |
| `4429` | The worker's Gateway session limit is reached |

The SDK reconnects after every close. On `4009` it first drops cached tokens
for that connection and reconciles affected DM leases at A; a terminal lease
failure removes that capability. Each failure also dispatches `GATEWAY_ERROR`
with the target and next backoff, and healthy targets keep running.

Intents control which events are delivered. Scopes and the current installation
or DM capability control fields and actions. Content and attachments are
filtered separately: content needs the privileged intent plus
`messages.content`, and attachment projections need `attachments.read`. Each
event category also needs its read scope on both the worker and the runtime
grant, such as `messages.metadata`, `reactions.read`, `members.read`, or
`voice.states.read`. Sparse presence and voice events carry the guild context
from the authorized topic, so a multi-instance worker doesn't have to infer the
authority from a user or channel reference.

Token expiry is checked while the connection is open, frames are capped at
1 MiB, and heartbeats keep the session lease alive.

Each worker has a Gateway session limit from 1 to 16, set at enrollment.
Enrollment defaults to 16. Each target counts the worker's open connections
atomically, so its guild Gateway and every DM capability Gateway at that target
share one budget. An excess connection closes with `4429`. The `/bots/token`
response reports the limit as `session_limit`, and the Python SDK refuses extra
connections locally with `BOT_GATEWAY_SESSION_LIMIT`. If a target omits the
field, the SDK assumes a limit of 1.

Workers may send Discord's documented app commands on each connection:

| Opcode | Purpose |
| --- | --- |
| 3 | Update the bot's status and bounded activity set |
| 4 | Join, move, set self-mute/deaf on, or disconnect the enrolled bot's voice session |
| 8 | Request guild member chunks by query or user, gated by the required intent |
| 31 | Request authorized soundboard sets |
| 43 | Request ephemeral voice-channel information |

Every guild command is rechecked against the live installation, worker, scopes,
intents, permissions, and target authority. Qualified member references avoid
same-snowflake ambiguity across instances. Presence commands are limited to five
per 20 seconds, and all commands share a bounded session rate. Discord's
undocumented opcode 12 is reserved for Kaede's first-party client and is never
sent or accepted by the bot SDK.

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
reconnect loop for each target, so one offline instance doesn't stop the others.

## End-to-end encrypted conversations

Servers never expose plaintext content or history from an encrypted channel to
a bot. Message responses use `content: null`, keep the encrypted envelope when
authorized, and mark unavailable content explicitly.

Install templates support two E2EE modes:

- `disabled` rejects commands and bot messages in encrypted channels.
- `participant` permits encrypted interactions, message envelopes, files, and
  supported calls only after a verified device is admitted to the room. It never
  enables server-side plaintext search or history.

There is no callback-only exception. Encrypted commands and component
submissions need the same active MLS participation as ambient encrypted events
and responses.

Forums and threads follow the same rule. Titles, tags, counts, membership, and
archive/lock state are ordinary metadata, but post and reply content follows the
child thread's encryption mode. An E2EE-required forum never accepts a
plaintext starter:

1. The bot creates an empty child shell with a nonce-bound reservation tied to
   its worker, installation revision, and `kbe_` device.
2. That device activates the child MLS room.
3. The bot claims the reservation with one rich-v2 encrypted starter whose ID
   matches the thread ID. Retries must be byte-for-byte identical.

A participant bot can read or write later encrypted content only while that
device is active in the child room. No route falls back to plaintext while
admission or rekeying is incomplete.

Participant bots forward rich-v2 messages with the same source proof as human
clients. Before sending, the worker calls
`POST /api/v1/bots/channels/{source}/messages/{message}/forward-authorize` with
the exact destination channel, mode, and client nonce. The source authority
rechecks one installation's or DM capability's history, content, and attachment
grants and the device's E2EE history floor; grants from separate installs
aren't combined. The worker then decrypts as an admitted source participant,
builds an author-free depth-one snapshot, reuploads attachments, encrypts for an
E2EE destination, and sends the signed proof with the normal message create.
Forwarding from E2EE to plaintext is a disclosure the bot chooses to make.
Poll, call, and activity messages can't be forwarded, as on Discord, and no
forwarding path falls back to plaintext.

Token webhooks have no forwarding capability, because Discord-compatible
webhook execution has no Forward Message field. An application that needs to
forward uses its bot installation and, in encrypted rooms, an admitted `kbe_`
device. A `kwe_` webhook automation device can create and edit its own
encrypted messages and claim a reserved encrypted forum starter, but can't
borrow a bot installation's source-read proof.

### Participant-device lifecycle

Device administration is worker-authenticated and always routed to application
home A:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/bots/e2ee/devices/challenge` | Create a five-minute registration challenge |
| `POST` | `/api/v1/bots/e2ee/devices` | Prove and register this worker's MLS device |
| `GET` | `/api/v1/bots/e2ee/devices` | List active devices and KeyPackage inventory |
| `POST` | `/api/v1/bots/e2ee/devices/{kbe_id}/key-packages` | Upload identity-signed MLS KeyPackages |
| `DELETE` | `/api/v1/bots/e2ee/devices/{kbe_id}` | Revoke this worker's device immediately |
| `GET` | `/api/v1/bots/channels/{channel}/e2ee/participation` at authority C | Read the exact runtime participation status |

Registration binds the application, worker authority ID, device identity key,
MLS credential digest, one-use challenge, and device signature. KeyPackage
uploads bind the `kbe_` device, device generation, supported cipher suite,
expiry, and every package digest. A claimed package can't be reused. Device
snapshots are signed by the application home and generation-fenced when a
guild, user-install, or conversation authority imports them.

The Python SDK maps these routes directly:

- `create_e2ee_device_challenge()` then
  `complete_e2ee_device_registration()`, or the combined
  `register_e2ee_device()`
- `e2ee_devices()` for device and unclaimed-KeyPackage inventory
- `upload_e2ee_key_packages()` for one signed batch of 1–50 packages
- `replenish_e2ee_key_packages()` to register if needed and maintain a bounded
  pool
- `revoke_e2ee_device()` for immediate revocation and rekeying
- `e2ee_participation()` at C for the exact channel and installation or DM
  capability

For a guild channel, a guild administrator admits or revokes the application
under **Guild settings → Integrations**. In a private conversation, every human
participant consents separately. Admission creates pending device rows, a
future-only history floor, and a rekey requirement. The normal room
prepare/commit flow claims each pending device's KeyPackage and writes its
Welcome and the membership commit to the ordered MLS control log. Only then is
the device marked active. Bot REST, Gateway, interaction, file, call, and voice
paths all recheck the same active device and installation or DM capability.

The worker processes ciphertext with its local MLS provider, and the server
never receives its private state or plaintext. Revoking the room grant or the
device stops new delivery, ends that device's encrypted media session, and
rekeys the room before content resumes.

Once a bot is a cryptographic participant, its operator is a recipient and can
keep anything it decrypts. Removing access stops future delivery but can't erase
data the operator already copied. History from before the install is excluded
by default.

### Encrypted voice participants

Bot voice uses the same verified participant identity and MLS group as the
channel. Token issuance requires the approved `kbe_` device ID and returns a
short-lived media context: encryption policy generation, MLS epoch,
`livekit-e2ee-v1`, `AES-256-GCM`, a group-bound media-session digest, and the
matching media epoch. The SDK recomputes the digest from the local 32-byte
group ID, so the grant doesn't need to reveal the group ID and can't substitute
a different group.

`VoiceE2EEContext` requires the real MLS provider, device ID, composite channel
reference, group ID, and epoch. It checks the provider epoch before and after
exporting 32 bytes with the cross-client `kaede livekit v1` exporter label and
the exact session, epoch, and room context. Only then does `LiveKitTransport`
put the key into LiveKit's native `E2EEOptions` provider in `RoomOptions`,
before connecting, subscribing, or publishing. There is no plaintext fallback
and no SDK-only frame cipher.

For encrypted calls using the SDK's LiveKit transport, a worker selects
`LiveKitTransport(kaede_e2ee=True)` and loads the Kaede native library matched
to Python LiveKit 1.1.18. That library derives 256-bit AES-GCM frame keys with
HKDF and the `kaede-livekit-v1` ratchet salt, including after key rotation.
Plaintext calls use the default transport. Setup steps are in
[Encrypted bot voice](bot-api-quickstart.md#encrypted-bot-voice).

Device revocation and control-log changes invalidate the context immediately,
and an active client also watches the provider epoch. Disconnecting disables
LiveKit E2EE, replaces the active key slot, destroys the room handle, clears the
SDK's key copies, and releases the server reservation if it's still held. An MLS
epoch change first fences the old media generation and room. The worker then
uses a fresh context and server grant for a new room. It never swaps a new key
under an old token or reuses a media-session ID.

## Storage model

PostgreSQL holds developer teams, applications and bot identities, control
credentials, workers and public keys, target rules, install templates, commands,
installations, target-token digests, signed DM capabilities, bot E2EE devices
and KeyPackages, room participation, DM consent, interactions, instance
administrator grants, audit events, and Trust & Safety reports.

Composite references keep both snowflake and origin. Deleting an application
cascades its private control records. Report and audit records follow their own
retention policy.

Dragonfly stores request nonces, DPoP replay markers, rate buckets, Gateway
session counts, topic cursors, and bounded event backlogs. The server never
stores raw control tokens, target access tokens, or worker private keys in
plaintext.

## User-facing behavior

Every active local human user can create applications from the Developer
Portal, which is linked from User settings. Portal workflows are in
[Administration and Developer Portal](administration-and-developer-portals.md).

The invite's authorization page lists what the bot requests (see
[Installation](#installation)), plus the application home, any support and
privacy links, and a warning that revocation can't erase what the operator
already received. Apps with user installs also let the user choose where to use
their commands.

Bot users carry an authoritative `account_type=bot` discriminator and an
application reference. Member lists and bot-authored messages show an
accessible `APP` badge next to the name. The badge comes only from trusted
account data, never from a nickname, role, embed, webhook name, or remote HTML.
Bot usernames use ordinary account formatting with a collision-resistant
numeric suffix. API payloads include the readable `username@instance` handle
next to the composite snowflake reference.

## Operational rules

- Current installation state and permissions are checked for every action and
  subscription.
- Remote schemas, sizes, counts, references, audiences, and generations are
  validated even when signed.
- Mutations, interactions, and event delivery are replay-bounded and
  idempotent.
- The SDK doesn't fetch URLs found in message content.
- Each target has its own queues, parsers, storage, and retries.
- API authorization never implies trust in an E2EE device key.
- Bot loops are limited by provenance, hop limits, mention rules, duplicate
  suppression, intent defaults, and channel write limits.
