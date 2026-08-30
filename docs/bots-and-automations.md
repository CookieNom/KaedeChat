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

- the application, owning developer-team, and bot-account composite references
- the public profile, active status, declared scopes and intents, and default
  install permissions
- exact allow or deny rules for target instances
- active install templates and command definitions
- active worker public keys and their authorization ceilings
- manifest and command generations

Targets verify the application home's instance signature and bind every
referenced identity to that origin. A target that learns an application from
an install manifest therefore creates any temporary team projection under the
signed team identity, never under a synthetic application-derived ID; a later
developer-team snapshot converges in either arrival order. Equal-generation
replays must be exactly equivalent; conflicting same-generation projections
and lower-generation rollbacks fail closed. Children omitted from a replay are
removed only when their governing generation strictly advances. Targets
enforce strict schemas and size limits, and cache only the records needed for
an active installation. Before accepting an unknown or stale worker, a target
refreshes worker authorization directly from the application home.

You create a hash-only `kb1_ctl_` control credential in the Developer Portal.
During one-time enrollment, the Python wrapper generates an Ed25519 worker key
locally and submits only its public key. The private key is written to an
owner-only state directory and is used for target assertions and DPoP proofs.
Control credentials can enroll workers and publish commands. They cannot
connect as the bot, call runtime routes, or authenticate a human session.

E2EE device keys are separate from the worker API key. Each participating
worker enrolls a verified `kbe_` MLS device at the application home, maintains
its signed KeyPackage supply there, and keeps its MLS private state locally.
The worker signing key authorizes Bot API requests; it is never accepted as an
MLS identity, message, or media key.

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

Discord-style user installs are a separate, commands-only integration type.
They make application commands available to the installing user in the
approved `guild`, `bot_dm`, and `private_channel` contexts, but they do not
create a guild member or make the bot a participant in a private conversation.
The public grant is limited to `applications.commands`,
`interactions.respond`, and optional `attachments.read`/`attachments.write` for
interaction media. It cannot grant ambient message or channel/DM event access,
outbound DMs, calls, or voice, but explicit command and component interactions
still arrive over the application's Gateway. In a guild without
`USE_EXTERNAL_APPS`, a user-installed command response is forced ephemeral,
matching Discord's external-app safety boundary.

Users review, narrow command contexts, and revoke these account grants under
**User settings → Authorized apps**. Guild-installed bots remain under
**Guild settings → Integrations**; the two consent surfaces are not merged.

When a command executes through another federated authority, that authority
materializes only a signed, bounded mirror of the user-home grant. The mirror
lease lasts at most 20 minutes, slightly longer than the 15-minute interaction
response window. Every command, Gateway target, DM/E2EE admission, and media
check uses the same effective-installation predicate and rejects an expired
foreign mirror immediately. A minute-bounded reconciliation sweep removes its
target presence and cryptographic admission even if the user home never sends a
later uninstall. A newer signed grant can renew the mirror; a target cannot
invent or advance its authority revision.

## App Directory discovery

Applications can opt in to the reviewed App Directory after publishing their
public profile, category, one to five tags, HTTPS policy/support links, and an
active install template. A listing that advertises user installation must also
have an active global user-install command; declaring the install type alone is
not enough. Instance reviewers approve a specific public revision and may place
it in bounded curated collections. A later public-profile, install, or store
media change revokes that approval for review. The ordered carousel contains at
most five total entries: owned store images or allowlisted YouTube video IDs.
Profiles may also publish up to five named HTTPS links, supported Discord
locales, and localized descriptions whose keys are a subset of those locales.

The App Directory is a desktop/browser discovery surface. It is available from
a server-rail right-click, the server menu, and Server Settings rather than as a
permanent global rail destination. Its product pages show the verified application origin,
description, category and tags, the mixed media carousel, policy/support and
named external links, localized descriptions, up to five popular global slash
commands, up to three reviewed similar apps, and the active guild/user consent
choices, plus **Copy Link**. Installation still goes through the normal portable
invite and authoritative consent flow; a directory listing is never itself an
installation grant.

Any current team member can request the private Directory preview before
approval or checklist completion. The authority returns a strict draft product
shape with nullable incomplete fields plus a fixed ordered readiness checklist
and status; it never fabricates placeholder content. Qualified remote
applications use the same replay-bounded management RPC, so draft metadata is
never exposed through the public Directory federation routes or to a
non-member.

Federated search is explicit by exact domain. The local instance sends a signed,
rate-limited request to that application home, validates the strict bounded
response, and rejects results whose identity, origin, filter, collection,
ordering, cursor, media, or install claims do not match the request. Developer
target rules and instance silence/suspension rules apply before enumeration, so
discovery does not bypass federation policy.

The Discord-style chat-bar **App Launcher** is placed on the composer's right on
web/desktop and left on mobile. It combines account-scoped recents, installed
commands, reviewed collections, and whole-catalog search. A compact instance
selector defaults to the authenticated home and can target one canonical
federated Directory authority; every response is rebound to that selected
origin before display. Zero-command rows resolve the authority-owned active
install template and open the normal consent review. Bot profiles use the same
strict lookup for **Add App** and never expose human friendship actions.

## Portable invite links

The application home publishes install templates at stable HTTPS URLs:

```text
https://apps.example/applications/123/install/moderation
```

The last path component is a signed template ID. A template defines requested
scopes, intents, permissions, supported contexts, and default E2EE mode. You can
publish separate `commands-only`, `moderation`, and `workflow` links without
placing mutable permission bitfields in the URL. Concrete channel/category
restrictions are owned by the target guild, not the portable template, and are
edited after installation under **Server Settings → Integrations**. An empty
restriction set means every channel still allowed by the bot role and live
channel overrides.

Invite links contain no access token, worker key, receipt, reusable code, or
unsigned permission override. Locale and a suggested guild ID may be carried as
non-authoritative UI hints.

### Guild selection across federation

Opening an invite shows the application's icon, name, `APP` badge, immutable
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
| APP AUTHORIZATION                                        |
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

An application-home suspension advances the application's own runtime
generation. It may suspend non-revoked, authority-owned guild installations and
later restore only those same rows with a new guild grant revision. It never
rewrites a user-home-owned user installation's status or grant revision;
targets require the signed application runtime state and signed user grant as
independent fences. Kicked, banned, uninstalled, or independently revoked
grants remain terminal.
For safety, DM capabilities, DM participant grants and consents, MLS room
participation, and live voice media state are also terminally revoked during the
suspension. Reactivation therefore requires a fresh DM capability and any
required participant consent, admission, and rekey; it never resurrects a
private-conversation or media bearer. Only the application home can change the
application authority state, and only the user home can change a user-install
authority revision. A target operator may still deny the app locally.

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
application-declared scopes, intents, and target policy
intersection installation approval
intersection worker and token ceiling
intersection bot roles and channel overrides
intersection current resource state
intersection instance and guild policy
intersection encryption mode
```

A bot identity is an ordinary guild member for the guild permission evaluator.
Its base role, assigned roles, category/channel overwrites, administrator grant,
timeouts, and AutoMod interaction blocks are resolved by the same code as a
human member. Bot scopes and installation grants only narrow that result; they
never bypass it. Role/member mutations also use the same strict highest-role
checks, so a bot cannot create, move, assign, remove, moderate, or edit a target
at or above its own highest role (and cannot act on the guild owner).

Like Discord's Default Install Settings, an application's
`default_permissions` value is the permission request used to seed a new install
link; it is not a permanent application-wide cap. The exact permissions approved
from a portable install template become that installation's immutable-at-revision
ceiling. Later role or overwrite changes may narrow the bot further, but cannot
raise it above the approved installation grant without a new grant revision.

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

Supported intents include Discord's current guild, moderation, expression,
integration, webhook, invite, voice, presence, message, reaction, typing,
scheduled-event, AutoMod, and poll families. Direct-message, reaction, typing,
and poll families are also represented. Kaede adds `interactions` and
`guild_tasks`, and keeps the previously published `voice_states`,
`message_reactions`, and `guild_typing` aliases valid. `guild_members`,
`guild_presences`, and `message_content` remain privileged.

A command-only guild bot normally needs only `applications.commands`,
`interactions.respond`, and the `interactions` intent. A user install is always
commands-only and cannot request the broader guild/runtime scopes in the list
above. Being able to invoke a command or respond to its interaction does not
imply access to channel content or history. A guild role cannot bypass
privileged-intent review, an installation ceiling, operator policy, or E2EE
consent.

Guild bots do not gain ambient access to members' direct messages. A bot can
access only a conversation in which its bot user is an actual participant, and
every DM REST call binds to one capability derived from a caller-selected
active guild installation. That source installation must grant `dm.send` plus
the operation's ordinary scope (for example `messages.history`,
`reactions.write`, or `polls.read`); grants from different installations are
never combined. Direct-message Gateway events use the corresponding
direct-message intent and apply the same single-grant rule to metadata,
content, attachments, and history gates. The commands-only user-install grant
cannot open this ambient DM capability.

DM attachment uploads use the selected installation's quota and require
`attachments.write`; reads independently require `attachments.read`. Revoking
the installation, removing the bot from the conversation, or reducing either
scope immediately removes access.

### Federated DM capability routing

Direct-message access can involve three different authorities without turning
the application home into a data proxy:

```text
A = application and bot home
B = selected guild-install or user-install authority
C = deterministic conversation authority

worker -> A: open or refresh the DM for one qualified installation
A -> B: request B's original signed installation-capability proof
A -> C: create or resolve the conversation with that proof
worker -> C: direct REST, Bot Gateway, calls, and voice using the capability
```

B's proof binds one stable grant ID to the installation kind and qualified
installation, application, bot, target human, DM pair, C, effective scopes and
intents, restrictions, E2EE mode, and source grant revision. A and C verify and
retain B's original signature; neither can widen it or substitute a different
installation. The lease lasts at most ten minutes and the SDK refreshes it
through A before expiry.

The protocol preserves guild/user source lineage so authorities can validate
either signed installation kind, but the current public user-install policy is
commands-only and cannot supply the required `dm.send` and `direct_messages`
grant. Public outbound DM opening therefore uses a qualified guild
installation.

A routine lease refresh extends expiry without changing the authorization
revision. A source grant, scope, intent, restriction, E2EE-mode, suspension, or
revocation change advances the revision and publishes a monotonic tombstone to
A and C. Runtime REST and Gateway authorization fail closed as soon as the
exact capability is missing, expired, superseded, or revoked. Calls are hosted
at C and pin each bot participant to its grant ID and authorization revision;
renewing an unchanged lease keeps the call alive, while a real authorization
change invalidates only that bot's call and media admission.

The refresh request carries only the opaque grant ID. A derives a fresh signed
proof from the recorded source instead of accepting replacement installation,
human, pair, channel, or authority fields from the worker. It verifies that the
grant ID, source lineage, conversation, and C are unchanged before returning
the new expiry.

The SDK does not persist B's proof. At startup, a worker enrolled with `dm.send`
pages active leases from A with `GET /api/v1/bots/dm-capabilities` and an opaque
`kbdg_` `after` cursor, force-refreshes each retained grant, and then opens a
separate Gateway and refresh loop at C for every exact grant. A terminal
authorization failure or an expired lease removes that runtime context;
transient failures are retried only until expiry. A commands-only worker skips
DM bootstrap.

## REST API

Bot endpoints use `/api/v1` and reuse the existing resource schemas and business
logic. Only handlers written to accept an `ActorPrincipal` admit a bot
principal.

### Developer management

These routes use an authenticated human or team member session:

| Method             | Path                                                  | Purpose                                                    |
| ------------------ | ----------------------------------------------------- | ---------------------------------------------------------- |
| `POST`             | `/api/v1/applications`                                | Create an application and bot user                         |
| `GET`              | `/api/v1/applications`                                | List owned or team-managed applications                    |
| `GET/PATCH`        | `/api/v1/applications/{app}`                          | Read or update application configuration                   |
| `GET`              | `/api/v1/applications/{app}/directory-preview`        | Read the team-only product preview and readiness checklist |
| `GET/POST`         | `/api/v1/applications/{app}/credentials`              | Create a named control credential                          |
| `DELETE`           | `/api/v1/applications/{app}/credentials/{credential}` | Revoke a credential                                        |
| `GET`              | `/api/v1/applications/{app}/instance-rules`           | List exact-domain target rules                             |
| `PUT/DELETE`       | `/api/v1/applications/{app}/instance-rules/{domain}`  | Set or remove an exact-domain rule                         |
| `GET/POST`         | `/api/v1/applications/{app}/install-templates`        | List or create invite templates                            |
| `GET`              | `/api/v1/applications/{app}/installations`            | List installations and health                              |
| `GET/POST`         | `/api/v1/applications/{app}/assets`                   | List or commit application assets                          |
| `POST`             | `/api/v1/applications/{app}/assets/tickets`           | Reserve an application asset upload                        |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/assets/{asset}`           | Read, update, or remove an asset                           |
| `GET/POST`         | `/api/v1/applications/{app}/emojis`                   | List or commit application emoji                           |
| `POST`             | `/api/v1/applications/{app}/emojis/tickets`           | Reserve an application emoji upload                        |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/emojis/{emoji}`           | Read, update, or remove app emoji                          |
| `GET`              | `/api/v1/application-directory`                       | Search reviewed local or remote apps                       |
| `GET`              | `/api/v1/application-directory/bot-profiles/{bot}`    | Resolve a bot profile's Add App action                     |
| `GET`              | `/api/v1/application-directory/{app}`                 | Read a reviewed app product page                           |

Qualified team and application references route these operations to their
authority, so a federated team member uses the same management surface from
their own home instance. The authority rechecks the member's live team role;
asset and emoji uploads remain quota-bound to that exact application authority.

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
- Message send and edit/delete-own operations, rich embeds, live-reference
  forwarding, polls, announcement publishing/follows, attachment-backed voice
  messages, plus message moderation behind its own scope.
- Reaction add/remove/list, removal of another member's reaction, clear-one-
  emoji and clear-all operations, and independently scoped attachment
  metadata/downloads.
- Installation-owned upload tickets with installation-level byte accounting.
- Guild invite, full webhook execution/message, emoji/sticker, application
  asset/emoji, soundboard, AutoMod, prune, bulk-ban, and audit-log management.
- Voice and external scheduled-event CRUD, status transitions, subscriber
  pagination, subscriptions for human members, and validated invite targets.
- Voice join/listen/speak/video receive, programmable file playback,
  soundboard playback, and mute/deafen/disconnect/move moderation.
- Participant-bound direct-message creation, history, messages, reactions,
  poll reads/finalization, pins, typing, calls, voice, and attachments under one
  exact capability derived from one source guild installation.
- Discord-compatible allowed-mention controls: ordinary bot messages parse
  visible user, role, and everyone mentions by default, while interaction and
  webhook messages default to visible user mentions only. Explicit policies,
  including reply-author notification, survive federation and message edits.
- Typed `encrypted` and `content_unavailable` fields instead of empty content.

Bot typing uses the same resource-authority routing and live authorization as
message sends: a guild bot needs its exact active installation, granted
`messages.send` scope, channel restriction, membership, and channel visibility;
a DM bot additionally needs the exact unexpired capability grant and revision
for that conversation with both `dm.send` and `messages.send`. Federated typing
is a ten-second, direct, per-recipient ephemeral signal and is never queued for
later delivery. Discord-compatible token webhooks do not expose a typing route.

Announcement following mirrors Discord's placement and ownership model. The
**Follow** action lives in an announcement channel, while the resulting
Channel Follower is a type-2 webhook managed from **Guild settings →
Integrations → Webhooks** in the destination guild. Creating a follow requires
source `channels.read` plus channel visibility, and target `webhooks.manage`
plus `MANAGE_WEBHOOKS`; it does not require send-message permission in the
source. Destination managers can rename the follower, replace or clear its
avatar, move it to another eligible plaintext text channel, or delete it.

Human and bot flows have the same federation semantics. A bot worker supplies
separate receiver-bound actor intents when its application home, source guild
and target guild are on different instances. Follow setup is
prepare/accept/finalize and generation-idempotent; publishing fans out through
independent durable jobs. Edits and source deletions continue to converge for
already-delivered copies after unfollow. Copied interactive controls dispatch
only when the same qualified application has an active
`applications.commands` installation and membership in the target guild; a
source-guild installation is never portable authority.

Soundboard list/get/play methods are authority-transparent for federated guilds.
The SDK calls the guild authority directly; the application home is not a
runtime proxy. The guild authority verifies the target token, active
installation membership, current granted scope, live channel
permissions/restrictions, and authoritative voice occupancy. Playback
capabilities are short-lived and delivered only to current room occupants.
Discord-compatible calls to `POST /channels/{channel}/send-soundboard-sound`
return `204 No Content`; the Python voice SDK instead uses Kaede's bot-only
`POST /channels/{channel}/soundboard-playback-grants` extension so it can mix
the same single authorized action into its voice transport. The SDK downloads
grants only from the exact HTTPS `media.<guild-authority>` host and
refuses redirects, credentials, alternate production ports, and unrelated
hosts. Guild sound creation, editing, and deletion remain on the guild home;
editing or deleting the bot's own sound needs the create-expression permission,
while another creator's sound needs the manage-expression permission.

### Guild message search

`Client.search_guild_messages()` sends the request directly to the qualified
guild authority. The worker token and exact installation must both grant
`messages.history` and `messages.content`, both must include the privileged
`message_content` intent, and the bot member must retain `VIEW_CHANNEL` and
`READ_MESSAGE_HISTORY` in every returned channel. Attachment projections are
included only when both runtime grants also include `attachments.read`.

The bot API supports Discord's current guild-search filter surface: text and
slop, one or more channels, authors and signed user/bot/webhook author types,
user/role/everyone mentions, replied users/messages, pinned state, snowflake or
timestamp bounds, all documented `has` values (including sound, sticker, poll,
and snapshot), embed type/provider, link hostname, attachment filename/
extension, NSFW inclusion, relevance/timestamp sorting, and opaque pagination.
Unknown, conflicting, malformed, or unsupported filter values fail closed
instead of being silently ignored. The current Meilisearch backend supports its
native phrase slop of `2`; another explicit slop is rejected rather than
pretending to apply it.

End-to-end encrypted messages are never indexed. The response names only
encrypted channels the bot can currently view, so even the search-coverage
projection cannot disclose a hidden channel. Every authority result is
strictly revalidated against the requested guild and channel before the SDK
exposes it.

The principal runtime routes are:

| Capability         | Routes                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Guild management   | `PATCH /api/v1/bots/guilds/{guild}`                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Channel management | `POST/PATCH /api/v1/bots/guilds/{guild}/channels`; `PATCH/DELETE /api/v1/bots/guilds/{guild}/channels/{channel}`                                                                                                                                                                                                                                                                                                                                             |
| Forums and threads | create/list below `/api/v1/bots/channels/{parent}/threads`; existing-message create below `.../messages/{message}/threads`; lifecycle and membership below `/api/v1/bots/channels/{thread}`                                                                                                                                                                                                                                                                  |
| Role management    | `POST/PATCH /api/v1/bots/guilds/{guild}/roles`; `PATCH/DELETE /api/v1/bots/guilds/{guild}/roles/{role}`; member-role `PUT/DELETE` routes                                                                                                                                                                                                                                                                                                                     |
| Attachments        | `POST /api/v1/bots/channels/{channel}/attachments`; `GET /api/v1/bots/attachments/{attachment}` and `/{variant}`                                                                                                                                                                                                                                                                                                                                             |
| Invites            | create/guild-list/revoke with expiry, use limits, temporary membership, verified live-stream targets, event association, role grants, and target-user management below `/api/v1/bots/guilds/{guild}/invites`; create and delete accept `X-Audit-Log-Reason`, while `DELETE` returns the deleted typed Invite with HTTP 200, including across federation; channel managers list one exact channel at `/api/v1/bots/guilds/{guild}/channels/{channel}/invites` |
| Webhooks           | create/list/update/rotate/delete below `/api/v1/bots/guilds/{guild}`; token execution and message fetch/edit/delete on the webhook API                                                                                                                                                                                                                                                                                                                       |
| Expressions        | guild emoji/sticker list/get/upload/edit/delete and role/availability restrictions; application asset and emoji CRUD below `/api/v1/bots/applications/@me`                                                                                                                                                                                                                                                                                                   |
| Rich messages      | embeds, components/views, poll create/read/end, forwarding, reactions, announcement follows, and crosspost operations below `/api/v1/bots/channels/{channel}`                                                                                                                                                                                                                                                                                                |
| AutoMod/moderation | AutoMod rule CRUD; prune estimate/execute; bulk bans with per-member failures; ordinary member/ban operations                                                                                                                                                                                                                                                                                                                                                |
| Soundboard         | guild sound list/upload/edit/delete and voice-channel play operations                                                                                                                                                                                                                                                                                                                                                                                        |
| Audit log          | cursor-paged and filtered `GET /api/v1/bots/guilds/{guild}/audit-logs`                                                                                                                                                                                                                                                                                                                                                                                       |
| Voice moderation   | `PATCH/DELETE .../members/{user}/voice`; `POST .../voice/move`                                                                                                                                                                                                                                                                                                                                                                                               |
| Direct messages    | `POST /api/v1/bots/dms` at A; capability-bound message, reaction, poll-read/end, pin, typing, call, voice, and attachment routes directly at C under `/api/v1/bots/channels/{channel}`                                                                                                                                                                                                                                                                       |

DM capability restart and renewal use
`GET /api/v1/bots/dm-capabilities?limit={n}&after={opaque_kbdg_cursor}` and
`POST /api/v1/bots/dm-capabilities/{kbdg_id}/refresh` at A. These routes return
opaque capability state; the worker never receives a reusable B proof.

Private-channel payloads use Discord's public channel type numbers: `DM` is
type `1` and `GROUP_DM` is type `3`. The persistence model may share an internal
DM channel type, but API, Gateway, command-option, and SDK projections expose a
group conversation as `3`. A user-installed command may run in that private
context, but applications cannot start, join, or connect voice to a group-DM
call.

Like Discord, Kaede applications may create a poll, read its voters, receive
vote events, and end a poll they authored, but they cannot cast or remove a
vote. Bot vote routes deliberately return `BOT_POLL_VOTE_UNSUPPORTED`; the
`polls.write` scope is not permission to impersonate a human voter.

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

| Method             | Path                                                                                      | Purpose                                          |
| ------------------ | ----------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `GET/PUT`          | `/api/v1/applications/{app}/commands`                                                     | Read or atomically replace global commands       |
| `POST`             | `/api/v1/bots/interactions/{interaction}/callback`                                        | Send the one initial callback                    |
| `GET/PATCH/DELETE` | `/api/v1/bots/interactions/{interaction}/responses/@original`                             | Read or mutate the original response             |
| `POST`             | `/api/v1/bots/interactions/{interaction}/followups`                                       | Create a follow-up                               |
| `GET/PATCH/DELETE` | `/api/v1/bots/interactions/{interaction}/followups/{response_id}`                         | Read or mutate one follow-up by durable ID       |
| `POST`             | `/api/v1/bots/interactions/{interaction}/responses/{@original\|response_id}/polls/expire` | End the exact public or private interaction poll |

### Delivery

An interaction carries everything a handler needs: its ID, application and
installation references, guild and channel references, the invoking user, the
command definition, bounded options, an encryption payload when required, and
an expiry time. Discord-compatible delivery metadata is captured once at
invocation: `version: 1`, the invoker and guild locales, effective application
permissions, the current attachment-size limit, and every guild/user install
owner that authorized the interaction. Routable owner references remain
instance-qualified. In parity with Discord, a guild-sourced bot-DM interaction
uses `"0"` as its public guild-install owner; Kaede retains the actual qualified
source installation only in its private admission lineage for federation and
routing. Guild interactions
expose the invoking `member` (including its permission snapshot) instead of a
duplicate top-level `user`; private
interactions expose `user`. Component and modal-submit interactions include a
safe source `message` projection for both durable and ephemeral sources. An
ephemeral projection is explicitly non-durable and cannot be used as a channel
message; E2EE content stays inside its opaque envelope.

Delivery uses the direct Bot Gateway. Events are at-least-once and carry a
stable interaction ID, so handlers must be idempotent.

An interaction remains valid for fifteen minutes. A bot may respond
immediately, or acknowledge it within the initial-response window and then
materialize or edit the deferred original response. Callback types and response
state transitions are validated server-side, and component callbacks are bound
to the authoring application and current view version. E2EE responses must
carry an encrypted message envelope.

The callback route supports Discord's `with_response` query. `true` returns an
interaction callback response wrapper and includes `resource.message` when the
callback creates or updates a message; `false` or an omitted query returns an
empty `204`. Kaede's legacy `/response` convenience route continues to return
the created message with `201`. Current Python SDK releases always send
`with_response=true` and unwrap message callbacks for source compatibility.

A public response is a channel `Message`; a private or ephemeral response is an
isolated response object. For a public original or follow-up, the Python SDK
attaches a private, write-once lifecycle binding only when a trusted interaction
helper creates, fetches, or edits it. The binding retains the interaction ID,
original/follow-up kind, durable follow-up response ID, target, and guild- or
user-install context. `Message.edit()`, `delete()`, and `end_poll()` then use the
interaction routes above rather than guessing from the public channel-message
ID. This lets commands-only user installs maintain their own public responses
without borrowing ambient channel authority. Other generic channel operations
remain unavailable when that response has no guild-install or DM grant.

Private responses never become `Message` resources. Their edit, delete, and
poll-finalization lifecycle remains on the `Interaction` helpers and the same
response IDs. Public and private poll finalization both verify the interaction
record and authoring application; a public finalization updates the underlying
channel poll and dispatches its normal message update.

Webhook-token messages use a different immutable binding. Standard, Slack, and
GitHub execution with `wait=true`, plus token-authenticated message fetch and
edit, retain the webhook ID, token, target, and optional `thread_id` privately.
Their `Message.edit()` and `delete()` methods stay on
`/api/v1/webhooks/{webhook}/{token}/messages/{message}`. A generic webhook
payload cannot supply this authority, the token is excluded from resource
representations, and generic channel actions fail locally instead of borrowing
a bot grant. In particular, `Message.end_poll()` is unavailable for a
webhook-token binding because no token-scoped poll-expire route exists.

## Bot Gateway

The Bot Gateway uses a separate authentication and session namespace from user
connections. A worker obtains an eight-minute target token, connects to
`/api/v1/bots/gateway`, and proves possession of its enrolled Ed25519 key.

The target sends a heartbeat interval. The worker identifies with its token,
proof, and last per-topic cursors. `READY` lists active installations visible
to that worker plus active conversation capabilities materialized at that
target. Event frames carry a type, topic, topic sequence, and filtered payload.
Direct-message and call events name their exact capability lineage and are
delivered from conversation authority C, not relayed through application home
A or installation authority B. The server retains a bounded topic backlog. A
cursor older than that backlog receives an explicit `GAP` event instead of
silently missing data.

There is no session-ID or opcode-6 Resume flow. After any disconnect, the SDK
waits with exponential one-to-thirty-second backoff plus jitter, obtains or
refreshes the required target token, and sends a new Identify containing the
persisted topic cursors. A cursor advances only after application handlers
finish, so a crash replays unfinished work. Routine DM lease renewal retains
the capability's cursor namespace; a real authorization revision is isolated
under a new grant-revision namespace.

The authority uses close code `4009` when live authorization changes and `4408`
for a missed heartbeat window. On `4009`, the SDK reconciles affected opaque DM
leases at A before retrying; a terminal lease failure removes that capability.
Each failure also dispatches `GATEWAY_ERROR` with the target and next backoff,
without stopping healthy targets.

Intent selection controls delivery. Scopes and the current installation or DM
capability control fields and actions. Message content and attachments are
filtered independently: content needs the privileged intent plus
`messages.content`, and attachment projections need `attachments.read`. The
event category itself also needs its read scope on both the worker and exact
runtime grant (for example `messages.metadata`, `reactions.read`,
`members.read`, or `voice.states.read`). Sparse presence and voice projections
carry the canonical guild context from the authorized topic, so a
multi-instance worker doesn't have to infer the authority from a user or
channel reference.

Sessions have hard limits. Gateway session counts are atomically limited per
worker, token expiry is checked while the connection is open, and frames are
capped at 1 MiB. Heartbeats keep the session lease alive.

Workers may send Discord's documented app commands through each authority
connection: opcode 3 updates the bot's status and bounded activity set, opcode
4 joins, moves, updates self-mute/deaf, or disconnects the exact enrolled bot
voice session, opcode 8 requests query-, user-, or intent-gated guild member
chunks, opcode 31 requests authorized soundboard sets, and opcode 43 requests
ephemeral voice-channel information. Every guild command is rechecked against
the live installation, worker, scopes, intents, permissions, and target
authority; qualified member references prevent same-snowflake ambiguity across
federated identity domains. Presence commands are limited to five per 20
seconds and all commands share a bounded session rate. Discord's undocumented
private opcode 12 is reserved to Kaede's first-party client protocol and is not
generated or accepted by the bot SDK.

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
content follows the child thread's encryption mode. An E2EE-required forum
never admits a plaintext starter. The bot first creates an empty child shell
with a nonce-bound reservation tied to its exact worker, installation revision,
and `kbe_` device. After that device activates the child MLS room, it claims the
reservation with one rich-v2 encrypted starter whose ID matches the thread ID.
Retries must be byte-for-byte equivalent. A participant bot can read or write
later encrypted content only while that exact device is active in the child
room. No route falls back to plaintext while admission or rekeying is
incomplete.

Install templates support two E2EE modes:

- `disabled` rejects commands and bot messages in encrypted channels.
- `participant` permits encrypted interactions, message envelopes, files, and
  supported calls only after a verified device is explicitly admitted to the
  room. It never enables server-side plaintext search or history.

There is no callback-only exception. Encrypted commands and component
submissions require the same active MLS participation as ambient encrypted
events and responses.

Participant bots forward rich-v2 messages with the same source proof as human
clients. Before sending, the worker calls
`POST /api/v1/bots/channels/{source}/messages/{message}/forward-authorize` with
the exact destination channel/mode and client nonce. The source authority
rechecks that one installation or DM capability's history/content/attachment
grants and the worker device's E2EE history floor; grants from separate installs
cannot be combined. The worker decrypts only as an admitted source participant,
builds an author-free depth-one snapshot, reuploads attachments, encrypts for an
E2EE destination, and sends the signed proof with the normal message create.
E2EE-to-plaintext is an explicit disclosure operation. Poll/call/activity
messages are not forwardable, matching Discord, and no path falls back to
plaintext.

Discord-compatible incoming webhook execution has no Forward Message request
field, so token webhooks do not gain a separate forwarding capability. An
application that needs to forward uses its authenticated bot/worker installation
and, for encrypted rooms, an explicitly admitted `kbe_` participant device. A
`kwe_` webhook automation device may create/edit its own encrypted messages and
claim a reserved encrypted forum starter, but cannot borrow a bot installation's
source-read proof.

### Participant-device lifecycle

Device administration is worker-authenticated and always routed to application
home A:

| Method   | Path                                                                | Purpose                                      |
| -------- | ------------------------------------------------------------------- | -------------------------------------------- |
| `POST`   | `/api/v1/bots/e2ee/devices/challenge`                               | Create a five-minute registration challenge  |
| `POST`   | `/api/v1/bots/e2ee/devices`                                         | Prove and register this worker's MLS device  |
| `GET`    | `/api/v1/bots/e2ee/devices`                                         | List active devices and KeyPackage inventory |
| `POST`   | `/api/v1/bots/e2ee/devices/{kbe_id}/key-packages`                   | Upload identity-signed MLS KeyPackages       |
| `DELETE` | `/api/v1/bots/e2ee/devices/{kbe_id}`                                | Revoke this worker's device immediately      |
| `GET`    | `/api/v1/bots/channels/{channel}/e2ee/participation` at authority C | Read the exact runtime participation status  |

Registration binds the application, worker authority ID, device identity key,
MLS credential digest, one-use challenge, and device signature. KeyPackage
uploads bind the `kbe_` device, device generation, supported cipher suite,
expiry, and every package digest. Reusing a claimed package is rejected. Device
snapshots are application-home signed and generation-fenced when a guild,
user-install, or conversation authority imports them.

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
from **Guild settings → Integrations**. For a private conversation, every human
participant records consent separately. Admission creates pending device rows,
a future-only history floor, and a rekey requirement. The normal room
prepare/commit flow claims each pending device's KeyPackage, places its Welcome
and the membership commit in the ordered MLS control log, and only then marks
that device active. Bot REST, Gateway, interaction, file, call, and voice paths
all recheck the same active device and exact installation or DM capability.

The worker processes ciphertext with its local MLS provider. The server never
receives its private state or plaintext. Revoking either the room grant or the
device stops new delivery, evicts that device's active encrypted-media session,
and moves the room through a rekey before content resumes.

Once a bot is deliberately made a cryptographic participant, its operator is a
recipient and can retain anything it decrypts. Removing access can stop future
delivery, but it cannot erase data already copied by that operator. Pre-install
history remains excluded by default.

### Encrypted voice participants

Bot voice uses the same verified participant identity and MLS group as the
channel. Token issuance requires the approved `kbe_` device ID and returns a
complete, short-lived media context: encryption policy generation, MLS epoch,
`livekit-e2ee-v1`, `AES-256-GCM`, a group-bound media-session digest, and the
matching media epoch. The SDK recomputes that digest from the local 32-byte
group ID, so the bearer grant does not need to disclose the group ID and cannot
silently substitute a different group.

`VoiceE2EEContext` requires the real MLS provider, device ID, composite channel
reference, group ID, and epoch. It checks the provider epoch before and after
exporting 32 bytes with the cross-client `kaede livekit v1` exporter label and
the exact session/epoch/room context. Only then does `LiveKitTransport` put the
key into LiveKit's native `E2EEOptions` provider in `RoomOptions`, before
connect, subscription, or publication. There is no plaintext fallback and no
SDK-only or fake frame cipher.

Device revocation and control-log changes invalidate the context immediately;
an active client also watches the provider epoch. Disconnect disables LiveKit
E2EE, replaces the active key slot, destroys the room handle, clears the SDK's
mutable key copies, and conditionally releases the server reservation. MLS
epoch transitions first fence the old media generation and room. A worker then
uses a fresh context and fresh server grant for a new room; it never hot-swaps a
new key underneath an old token or reuses an old media-session ID.

## Storage model

The two bot migrations add durable PostgreSQL records for:

- developer teams and members
- applications and bot identities
- hash-only control credentials
- workers and public keys
- exact target rules, install templates, and commands
- installations, short-lived target-token digests, and signed DM capabilities
- bot E2EE devices, KeyPackages, room participation, and DM consent grants
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
- heartbeat, post-handler cursor persistence, fresh-Identify replay, bounded
  reconnect, and `Retry-After` handling
- `Client.event` and `Client.command` decorators
- typed resources and events: guilds, channels, members, roles, messages, and
  attachments; forums, threads, polls, scheduled events, invites, webhooks,
  expressions, application media, soundboard, and task trackers; interactions,
  AutoMod, moderation, presence, audit records, and voice
- arbitrary embeds, Discord-style Views, buttons, selects, checkboxes, modals,
  autocomplete, immediate/deferred/update callbacks, follow-ups, private
  responses, and authoritative timeout edits
- scoped CRUD and hierarchy-aware moderation helpers, safe presigned
  uploads/downloads, message history, complete reaction management,
  live-reference forwarding, announcements, polls, voice messages, and user
  lookup
- voice join/listen plus decoded camera and screen-share callbacks, soundboard,
  programmable PCM or local-file playback, and pause/resume/stop controls
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
accessible `APP` badge next to the name. The badge comes from trusted account
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
