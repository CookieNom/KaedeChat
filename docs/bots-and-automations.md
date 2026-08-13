# Bots and automations

Kaede applications provide bot accounts, commands, interactive components, and
event-driven automation across local and federated guilds. The API should feel
familiar to Discord bot developers, but authority stays with the instance that
owns each guild or conversation.

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
asymmetric keys and can be limited to particular targets, installations,
shards, scopes, and intents.

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
one connection per guild. Removing the application home from the data path also
keeps an unavailable home from interrupting established installations.

The target remains authoritative. A signed application manifest proves who
published it; it does not prove that a bot is installed or permitted to act.

## Automation types

| Type | Reads ambient events | Typical use |
| --- | --- | --- |
| Channel webhook | No | Build output, alerts, and simple posting |
| Interaction application | No by default | Commands, forms, buttons, and E2EE-safe workflows |
| Event bot | Only approved intents and fields | Moderation, logging, and stateful integrations |

Existing `kwh_` channel webhook secrets remain write-only. They cannot be
upgraded to bot credentials. User-account automation and silent user
impersonation are not supported.

## Application identity and keys

Each application has an immutable composite ID and an application root key. Key
purposes are kept separate:

```text
application root key
    +-- certifies worker delegation public keys
    +-- certifies interaction endpoint public keys
    +-- certifies separate E2EE application/device public-key bindings
```

The root certifies those public keys; it does not derive or hold an E2EE device
private key. Device private keys are generated and retained by the bot runtime.

### Managed keys

Kaede-managed root custody is the default and does not require a separate KMS.
Setup creates a versioned application-key wrapping key in the deployment's
existing secret boundary. The application home generates and encrypts each root,
uses it only for application identity operations, and includes the wrapping key
in the operator's encrypted secret backup. The root is not displayed or copied
into a worker container.

Workers enroll with a one-time code. The SDK generates the worker key locally
and sends only its public key. The managed root signs the resulting delegation.
Normal REST, Gateway, and message traffic never requires the root private key.

Managed custody does not give the application home access to E2EE conversation
content. Encryption device keys are separate and remain with the bot runtime.

### External custody

Applications that need their own KMS, HSM, or offline signer can register a root
public key and complete ownership challenges with an external signer. The
Developer Portal provides adapters and copyable signing payloads, while the
Python SDK provides KMS and HSM integrations.

Moving between managed and external custody is a root rotation. Targets accept
a transition signed by both roots. If the old root is unavailable, each affected
installation must be reapproved. An application home cannot silently replace a
root already pinned by a target.

### Signed manifest

The application home publishes a bounded signed manifest containing:

- Application and bot-user composite IDs.
- Name, description, icon, publisher, and application status.
- Root and worker-delegation public keys, key IDs, and validity periods.
- Interaction endpoint and endpoint-ownership key when HTTP delivery is used.
- Exact OAuth redirect URIs.
- Declared scopes, intents, permissions, commands, and E2EE modes.
- Application target policy and exact allow or deny rules.
- Manifest, keyset, command, and revocation generations.
- Revoked key IDs and signed root transitions.
- Supported protocol capabilities.

Targets retrieve manifests through Kaede's bounded federation transport.
Redirects are not followed, domains are canonicalized, response sizes and
durations are limited, and private or unsafe network destinations are rejected.
At installation time, the target pins the application ID, root fingerprint, and
accepted manifest digest.

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

Example worker assertion:

```json
{
  "iss": "123@apps.example",
  "sub": "123@apps.example",
  "aud": "https://chat.example/api/v1/bots/token",
  "worker_id": "production-eu-2",
  "delegation_generation": 8,
  "iat": 1786644000,
  "exp": 1786644060,
  "jti": "random-single-use-value"
}
```

The target checks the exact issuer and audience, a narrow time window, key
validity, delegation ceilings, and a bounded replay cache. It may also require a
target-issued DPoP nonce.

Access tokens are random opaque values stored as hashes. A token is bound to:

- Application, bot user, worker, and target issuer.
- Installation set or shard range.
- Scope and intent ceilings.
- Installation and worker generations.
- DPoP key thumbprint.
- Creation, expiry, last use, and revocation.

The normal lifetime is five to ten minutes. Workers obtain another token with
their key rather than keeping a permanent target secret. Tokens never appear in
query strings, redirects, logs, or WebSocket URLs.

## Installation

A guild installation begins from a Kaede client or a portable bot invite. The
person approving it must currently have `MANAGE_GUILD` on the authoritative
guild.

1. The target resolves and verifies the signed application manifest.
2. It applies instance, application, and guild policies.
3. It verifies the installer and current guild permission.
4. The consent page shows the application's origin, publisher, verification
   state, permissions, intents, channel access, message/history access, E2EE
   behavior, and external retention warning.
5. The installer may narrow optional grants before approval.
6. The target creates the bot member, installation, initial grant revision, and
   audit event in one transaction.
7. The target returns a signed, non-secret installation receipt.
8. An enrolled worker connects directly to the target.

Browser authorization uses an exact registered HTTPS redirect URI,
authorization code, PKCE S256, `state`, issuer binding, short expiry, and single
use. Implicit grants are not supported.

Changing permissions, intents, channel restrictions, data grants, or E2EE mode
increments the installation's grant revision. Tokens and Gateway sessions stop
receiving authority removed by a newer revision.

Uninstall is immediate at the target. It deactivates the installation, advances
the revision, revokes tokens, closes Gateway sessions, removes queued non-audit
events, rotates an E2EE epoch when needed, removes the bot member, and sends a
best-effort signed notice to the application home. An outage elsewhere cannot
delay local revocation.

## Portable invite links

The application home publishes install templates at stable HTTPS URLs:

```text
https://apps.example/applications/123/install/moderation
```

The last path component is a signed template ID. A template defines requested
scopes, intents, permissions, supported contexts, optional channel restrictions,
and default E2EE mode. Developers can provide separate `commands-only`,
`moderation`, and `workflow` links without placing mutable permission bitfields
in the URL.

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
confirms that exact challenge through their existing home instance; a second
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
resolution is cached within bounded limits and rate-limited by user, source,
application, origin, and target.

## Federation policy

Policy is applied independently at each layer.

Application target policy supports:

- `open`
- `allowlist`
- `blocklist`
- `local_only`

An instance operator can disable remote applications, require review, allow or
block exact origins/applications, and suspend an application, publisher, key, or
origin. A guild can be stricter than its instance. A worker can also refuse
targets that the developer does not trust.

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
- `guilds.read`, `channels.read`, `members.read`, `roles.read`
- `messages.metadata`, `messages.content`, `messages.history`
- `messages.send`, `messages.edit.own`, `messages.delete.own`
- `messages.manage`
- `attachments.read`, `attachments.write`
- `reactions.read`, `reactions.write`
- `moderation.members`, `moderation.messages`
- `voice.states.read`
- `dm.send`

Supported intents:

- `guilds`
- `guild_members` and `guild_presences` (privileged)
- `guild_messages`
- `message_content` (privileged)
- `message_reactions`
- `voice_states`
- `interactions`

An interaction-only bot normally needs only `applications.commands`,
`interactions.respond`, and the `interactions` intent. Being able to view a
channel does not imply access to content or history. A guild role cannot bypass
privileged-intent review, an installation ceiling, operator policy, or E2EE
consent.

Guild bots do not gain access to members' direct messages. `dm.send` covers only
the documented outbound flow. Ambient and inbound direct-message access are
outside this contract, and there is no direct-message event intent.

## REST API

Bot endpoints use `/api/v1` and reuse the existing resource schemas and business
logic. Only handlers that intentionally accept an `ActorPrincipal` admit a bot
principal.

### Developer management

These routes use an authenticated human or team member session:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/applications` | Create an application and bot user |
| `GET` | `/api/v1/applications` | List owned or team-managed applications |
| `GET/PATCH` | `/api/v1/applications/{app}` | Read or update application configuration |
| `POST` | `/api/v1/applications/{app}/credentials` | Create a named control credential |
| `DELETE` | `/api/v1/applications/{app}/credentials/{credential}` | Revoke a credential |
| `POST` | `/api/v1/applications/{app}/keys/rotate` | Start a managed or externally signed rotation |
| `GET/PUT` | `/api/v1/applications/{app}/instance-policy` | Manage target policy |
| `GET/PUT` | `/api/v1/applications/{app}/interaction-endpoint` | Verify HTTP delivery |
| `GET/POST` | `/api/v1/applications/{app}/install-templates` | List or create invite templates |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/install-templates/{template}` | Manage a template |
| `GET` | `/api/v1/applications/{app}/installations` | List installations and health |

Secrets and one-time enrollment codes are shown once. Later reads return only a
label, safe prefix/suffix, timestamps, expiry, last use, and revocation state.

### Installation management

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/applications/{app}/authorize` | Begin browser consent |
| `GET` | `/api/v1/bot-invites/{app}/{template}` | Resolve the signed invite card |
| `POST` | `/api/v1/bot-invites/{app}/{template}/prepare` | Request a guild consent challenge |
| `POST` | `/api/v1/guilds/{guild}/integrations/bots` | Confirm and commit an install |
| `GET` | `/api/v1/guilds/{guild}/integrations/bots` | List installations |
| `GET` | `/api/v1/guilds/{guild}/integrations/bots/{installation}` | Read exact grants and health |
| `PATCH` | `/api/v1/guilds/{guild}/integrations/bots/{installation}` | Change grants with consent |
| `DELETE` | `/api/v1/guilds/{guild}/integrations/bots/{installation}` | Revoke immediately |

Installation responses contain no worker or bot secret.

### Worker authentication

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/bots/workers/register` | Register a delegated worker public key |
| `DELETE` | `/api/v1/bots/workers/{worker}` | Revoke a worker and its sessions |
| `POST` | `/api/v1/bots/token` | Issue a DPoP-bound target token |
| `POST` | `/api/v1/bots/token/revoke` | Revoke a token or session |
| `GET` | `/api/v1/bots/@me` | Read worker, installation, and target ceilings |

### Resource operations

Supported resource operations include:

- Guild, channel, role, member, and permission lookup.
- Cursor-paginated message history when every required grant allows it.
- Message send and edit/delete-own operations.
- Separately granted message and member moderation operations.
- Reactions and bounded attachment upload/download.
- Allowed-mention controls that default to no broad role or everyone mentions.
- Audit reasons and idempotency keys for mutations.
- Typed `encrypted` and `content_unavailable` fields instead of empty content.

## Commands and interactions

Commands are the preferred interface when an application does not need ambient
message access. Kaede supports:

- Slash commands.
- User and message context commands.
- Autocomplete for typed options.
- Buttons.
- String, user, role, channel, and mentionable select menus.
- Modals with bounded text inputs.
- Immediate, deferred, follow-up, edited, and private responses.

Commands may be application-global or guild-specific. Guild commands become
available after authoritative registration. Global command updates carry a
signed generation and report `pending`, `active`, `superseded`, or `failed`
rather than assuming instant propagation.

Example command definition:

```json
{
  "name": "poll",
  "type": "chat_input",
  "description": "Create a poll",
  "default_member_permissions": ["SEND_MESSAGES"],
  "dm_permission": false,
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

| Method | Path | Purpose |
| --- | --- | --- |
| `GET/PUT` | `/api/v1/applications/{app}/commands` | Atomically replace global commands |
| `GET/PUT` | `/api/v1/applications/{app}/guilds/{guild}/commands` | Atomically replace guild commands |
| `GET` | `/api/v1/applications/{app}/commands/status` | Read validation and propagation state |
| `POST` | `/api/v1/interactions/{interaction}/{token}/callback` | Send the initial response or defer |
| `PATCH/DELETE` | `/api/v1/interactions/{interaction}/{token}/response` | Change the original response |
| `POST` | `/api/v1/interactions/{interaction}/{token}/followups` | Create a follow-up |
| `PATCH/DELETE` | `/api/v1/interactions/{interaction}/{token}/followups/{message}` | Change a follow-up |

Command replacement and response mutations accept idempotency keys.

### Delivery

An interaction includes its ID and type, application, installation, target,
grant revision, resource composite IDs, invoking member permissions, command
version, typed resolved options, locale, encryption mode, and a short-lived
callback token. Context commands include only the selected user or message
projection.

Delivery uses either the direct Bot Gateway or one verified HTTPS endpoint per
application environment. Delivery is at-least-once; handlers deduplicate with
the event ID and idempotency key.

HTTP interaction requests are signed by the target. The signature covers the
timestamp, nonce, method, path, content digest, application, installation,
interaction, and issuer. Endpoint registration uses an ownership challenge.
Targets do not follow redirects and reject private, loopback, link-local,
metadata, reserved, or otherwise unsafe destinations.

The initial response deadline defaults to three seconds. A bot may defer and use
a longer bounded follow-up window. Private responses are visible only to the
invoking user and are not stored or federated as ordinary channel messages.

## Bot Gateway

The Bot Gateway shares Kaede's heartbeat, sequence, resume, and bounded-frame
behavior but has a separate authentication and session namespace from user
connections.

1. The worker obtains a DPoP-bound target token.
2. It upgrades with authorization and DPoP headers.
3. The target sends `HELLO` with heartbeat, session-start limits, maximum frame
   size, and supported schemas.
4. The worker sends `IDENTIFY` with intents, worker and shard identity,
   installation selection, SDK metadata, and capability versions.
5. The target intersects those requests with current installation grants and
   returns `READY`.
6. Events include sequence, event ID, installation, grant revision, and typed
   payload.
7. A short-lived, target- and key-bound credential supports resume. A retention
   gap returns `GAP` or `RESYNC_REQUIRED`.

One connection can multiplex many installations, but authorization and
redaction are evaluated for every event. One session per
`(application, target, shard)` is active unless the target allocates more
concurrency or accepts an explicit takeover.

Events include installation and grant changes, guild and channel lifecycle,
permitted member and role updates, messages, reactions, voice states,
interactions, rate-limit notices, gaps, and revocation. Presence and typing are
separately privileged and may be coalesced. Bot-authored messages are excluded
from other bots' message events by default to reduce loops.

## Rate limits and backpressure

Targets enforce shared atomic limits across several dimensions:

- Application and credential.
- Worker, Gateway session, and shard.
- Installation.
- Route bucket and major resource.
- Guild, channel, user, and source address.
- History, member lists, search, moderation, and attachment work.
- Queued event rows, bytes, age, and fanout.
- Interactions, autocomplete, components, and HTTP endpoint concurrency.
- Invalid requests, token assertions, identify, reconnect, and resume attempts.

Adding workers does not increase application or installation ceilings. Guild
slowmode, upload limits, mention protections, and moderation rules still apply.

A limited request returns `429`, `Retry-After`, stable bucket headers, and a
machine-readable body:

```json
{
  "code": "BOT_RATE_LIMITED",
  "message": "This bot is sending requests too quickly.",
  "retry_after": 1.25,
  "global": false,
  "scope": "installation",
  "bucket": "messages:create:channel"
}
```

The SDK learns limits from responses rather than hard-coding Discord values.
Defaults are operator-configurable with hard safety ceilings for untrusted
traffic. Capacity load shedding returns `503`; it is not disguised as a rate
limit. User-invoked commands receive a visible retry or failure state.

Workers isolate each target with independent connection, parser, retry, queue,
memory, disk, attachment, and concurrency budgets. A malicious or broken target
cannot exhaust every other connection. Remotely supplied retry delays are
clamped to safe SDK bounds.

## End-to-end encrypted conversations

A bot can participate in E2EE only when users explicitly give one of its
verified encryption endpoints access. The bot operator then becomes a party
that can read the disclosed plaintext; Kaede cannot promise deletion after the
bot receives it.

### Interaction-only mode

This is the default in encrypted conversations:

- The bot receives no ambient message content, attachment keys, or history.
- The invoking client constructs only the command, form, or selected context.
- The client encrypts that payload to the verified application or device key.
- The target relays ciphertext without decrypting or replacing it.
- A private result can be encrypted directly to the invoking device.

Authenticated data binds the room, installation, device, application key,
event, timestamp, target, and encryption epoch. This prevents replay and
cross-room substitution.

### Participant mode

A guild administrator and affected participants may add a bot as a
cryptographic room participant. The client keeps a visible disclosure that the
bot operator can read content delivered after admission.

- The bot uses a signed E2EE device key separate from API credentials.
- Clients verify it against the pinned application and installation.
- Admission starts a new room encryption epoch.
- Removal or revocation immediately starts another epoch.
- Pre-installation history keys are denied by default.
- Attachment keys are granted separately.
- Reliable reconnect uses bounded ciphertext catch-up from the admission point.

Server-side plaintext search, indexing, link previews, content moderation, and
history APIs remain unavailable for encrypted messages. A participant bot may
keep its own index of content it can decrypt, but that is a separate persistent
data disclosure shown during consent.

API responses distinguish `encrypted`, `content_unavailable`, and
`permission_denied`. They do not represent unavailable plaintext as an empty
string. E2EE bot support must use Kaede's audited device and room-key protocol;
the application API reserves the encryption modes and typed payloads needed for
that integration.

## Storage model

Durable PostgreSQL models:

- `BotApplication`: owner team, bot user, composite ID, profile, status,
  custody mode, policy and manifest generations, defaults, interaction settings,
  and timestamps.
- `BotApplicationKey`: purpose, public key, key ID, validity, generation,
  transition proof, custody reference, and revocation.
- `BotCredential`: digest, safe display characters, label, ceiling, creation,
  expiry, last use, and revocation.
- `BotInstallation`: application and guild references, bot member, installer,
  grants, restrictions, E2EE mode, manifest digest, revision, status, and dates.
- `BotWorker`: public key, target/install/shard ceilings, scopes, intents,
  generation, session cap, validity, and status.
- `BotInstanceRule`: exact target origin or application ID, decision, and audit
  metadata.
- `BotInstallTemplate`: stable slug, grants, contexts, E2EE default, digest,
  generation, status, and presentation data.
- `ApplicationCommand`: application, optional guild, normalized definition,
  schema digest, generation, state, and dates.
- `BotToken`: target-local digest, DPoP thumbprint, worker and installation
  bounds, generations, expiry, last use, and revocation.

Gateway sessions, DPoP nonces, replay IDs, callback tokens, rate buckets, shard
leases, and private-response state are short-lived Dragonfly data.

Remote applications and installations have quotas for record count, command
bytes, queued events, media, and retention. Removing or suspending an
installation cleans up its non-audit transient state without deleting required
security and moderation records.

## Python SDK

The first-party Python package is asynchronous and follows familiar
`discord.py` conventions without copying Discord's wire protocol. Worker
enrollment is a one-time provisioning step:

```console
kaede-bot worker enroll \
  --application 123@apps.example \
  --state-dir /var/lib/poll-garden/kaede
```

The command prompts for the enrollment code without echoing it, consumes it
once, and does not retain it. It generates the worker private key locally and
atomically stores the key, delegation, and target metadata in the state
directory. That directory must be persistent and readable only by the bot
process, such as an owner-only host directory, protected container volume, or
operating-system credential store. The stored key material must not be committed
or baked into an image. Environment variables may hold only the state-directory
path, never the key or delegation content.

Normal startup loads the persisted worker state and does not use an enrollment
code:

```python
import os

import kaede
from kaede.ext import commands

bot = commands.Bot(
    application="123@apps.example",
    worker_state=kaede.WorkerState.load(os.environ["KAEDE_BOT_STATE_DIR"]),
    intents=kaede.Intents.default(),
)


@bot.event
async def on_ready(target: kaede.Instance) -> None:
    print(f"Connected to {target.domain}")


@bot.command(name="ping", description="Check whether the bot is responding")
async def ping(ctx: commands.Context) -> None:
    await ctx.respond("Pong!", private=True)


bot.run()
```

The SDK includes:

- Typed client, application, installation, instance, guild, channel, member,
  role, message, and interaction models.
- Decorators for slash and context commands, autocomplete, buttons, selects,
  and modals.
- Typed permissions, `await channel.send(...)`, and async pagination.
- Local worker-key generation and enrollment.
- Managed-root enrollment plus external signer, KMS, and HSM adapters.
- Independent connection pools, rate buckets, and circuit breakers per target.
- Heartbeat, resume, bounded reconnect, sharding, deduplication, gap handling,
  idempotency, and safe retries.
- Explicit bounded caches rather than unbounded member or message retention.
- Domain-qualified IDs; numeric IDs are never assumed to be globally unique.
- `EncryptedPayload`, `ContentUnavailable`, and `HistoryUnavailable` types.
- Typed exceptions with error code, trace ID, issuer, retry metadata, and bucket.
- An ASGI interaction adapter that verifies signatures before dispatch.
- A raw-event interface for unsupported extensions.

FastAPI OpenAPI is the REST source of truth. Gateway and interaction payloads
use checked-in discriminated JSON Schemas. CI runs the SDK against backend
contract fixtures and compatibility tests.

## User-facing behavior

The Developer Portal covers application creation, team access, bot profile,
commands, credentials, worker enrollment, key custody and rotation, interaction
endpoints, scopes, intents, federation policy, install templates, invite
previews, installation health, rates, errors, and audit history.

Guild Integrations keeps channel webhooks and applications separate. An
installation page shows the bot's identity and origin, exact permissions,
intents, data grants, channel restrictions, content/history access, E2EE mode,
connection state, and recent audit activity. Guild administrators can narrow
grants, disable the integration, or revoke it.

### Bot badges

Bot accounts display a platform-owned `BOT` badge:

- Immediately after the display name in member lists and member search.
- After the author name and before secondary metadata on bot-authored messages.
- In replies, pins, threads, search results, profiles, and moderation previews.
- In compact, mobile drawer, web, and desktop layouts.

The badge has an accessible name and is not conveyed by color alone. Opening it
shows `Bot account` and the immutable application origin. A publisher or
verification mark is separate from the `BOT` badge.

Only the authoritative account discriminator and linked application ID can
produce the badge. A nickname, role, message, embed, webhook name, or remote HTML
cannot imitate it. Webhooks use a distinct `WEBHOOK` label.

Bot-authored messages and profiles show their application origin. Broad
mentions, automatic link fetching, and other amplification-prone behavior are
off unless explicitly requested and permitted.

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
