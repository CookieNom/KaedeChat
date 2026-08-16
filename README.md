# Kaede Chat

Kaede Chat is a self-hosted chat platform with federation built in. You keep one
account on your home instance and can join guilds, add friends, send direct
messages, and hop into calls on other instances without registering anywhere
else.

Handles are immutable `username@domain`, like email. A guild lives on exactly
one home instance, which stays authoritative for its membership, permissions,
messages, and moderation. Other instances keep only the data their local users
are allowed to see, and send writes back to the authority for validation.

## Features

- Guilds with text and voice channels, roles, channel overrides, invites,
  moderation, reactions, pins, attachments, webhooks, and audit logs
- Direct messages, friend requests, blocking, presence, unread state, and
  two-party calls
- Federated bots: consent-based guild installs, direct target authentication,
  scoped REST and Gateway access, slash commands, bot badges, exact-instance
  policies, and E2EE-aware grants. Bots are managed through the Developer
  Portal and built with the async `kaede-bot` Python SDK.
- Typo-tolerant message search with filters for author, mentions, content type,
  date, pins, and sort order. Remote guild and DM authorities are queried over
  signed federation without exposing search credentials.
- Signed server-to-server delivery with retry queues, sequence recovery,
  permission-filtered replication, and optional retained-history transfer
- Private S3-compatible media storage with malware scanning and image/video
  derivatives. Garage ships as the default self-hosted backend.
- LiveKit voice, video, and screen sharing
- A static SvelteKit web client backed by FastAPI, PostgreSQL, and Dragonfly
- A Tauri desktop client for Windows, macOS, and Linux. It shares the web UI
  while Rust handles the native side: audio devices, push-to-talk, voice
  activity, speech processing, camera, and desktop capture.
- A mobile-first Flutter client for Android and iOS with full chat and guild
  administration, offline-tolerant message state, native LiveKit calls,
  biometric app locking, and category-aware push notifications

The federation wire format is documented in
[docs/kaede-fed-v1.md](docs/kaede-fed-v1.md). Architectural and operational
details are in [docs/architecture.md](docs/architecture.md) and
[docs/operator.md](docs/operator.md). If you're writing a bot, start with
[docs/bot-api-quickstart.md](docs/bot-api-quickstart.md) and keep the full
[bot and automation contract](docs/bots-and-automations.md) nearby. Instance
owners and delegated staff should read the
[administration and developer portal guide](docs/administration-and-developer-portals.md).

## Setup

You need Docker with the Compose plugin. Everything else (Python, Node, uv,
pnpm) runs inside containers, so nothing has to be installed on the host.

Start with the interactive setup wizard:

```sh
make setup
```

The wizard walks you through the whole configuration. It creates a private
`.env`, picks between the bundled Garage and an external S3-compatible
provider, and sets up the optional services: email, voice, private message
search, KLIPY GIFs, and Turnstile. It also configures mobile push (the
recommended public relay or a custom direct-Firebase build) and source-based
automatic updates, offers common or advanced federation/storage quota tuning,
and can render a host nginx configuration. Quota prompts take friendly values
like `250K`, `2.5M`, `100GB`, or `100GiB`; keep the recommended or current
limits and you won't be asked any individual sizing questions.

The wizard doesn't start containers, install nginx files, reload nginx, or
obtain certificates. The only thing it ever touches on the host is a per-user
systemd timer, and only when you explicitly select that option. See
[docs/deployment-wizard.md](docs/deployment-wizard.md) for every option.

Rerunning `make setup` preserves custom quota tuning and upgrades only the
exact low cache/history defaults written by older setup versions. If you
maintain `.env` by hand, the corresponding variables and sizing guidance are in
[docs/operator.md](docs/operator.md#federation-storage-budgets).

### Production Docker deployment

Before deploying: point the instance domain at the host, install Docker Engine
with the Compose plugin, and arrange TLS certificates. If you use the bundled
Garage, `media.<your-domain>` needs DNS and a certificate too. Keep TCP 80/443
public, but don't expose the Caddy or API loopback ports. Voice adds RTC and
TURN ports, listed in the
[operator guide](docs/operator.md#hosts-certificates-and-ports).

From a clean checkout, generate the production configuration and review the
files before starting anything:

```sh
make setup
chmod 600 .env
make env-check
make generated-compose-check
```

The generated deployment is `.env`, `deploy/compose.generated.yml`, and
`deploy/generated/README.txt`. If you selected host nginx, also review and
install `deploy/generated/kaede.nginx.conf`, run `nginx -t`, and reload nginx
only after validation succeeds. The wizard won't obtain certificates, install
the proxy file, change firewall rules, or start Kaede for you. With external
S3, keep the buckets private and allow browser `PUT`, `GET`, and `HEAD`
requests from the Kaede origin, as described by the wizard.

Render the exact production topology, then build and start it:

```sh
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml config --quiet

KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml \
  up -d --build --wait --wait-timeout 180
```

Startup is migration-safe. `preflight` validates the operator configuration,
`migrate` applies every pending Alembic revision up to the repository's current
head and runs the idempotent instance bootstrap, and the API, gateway, workers,
and edge all wait for that one-shot service to succeed. Don't run Alembic
separately on the host. If migration or preflight fails, the application
services stay stopped. Find and fix the failure rather than bypassing the
dependency gate.

After startup, check the one-shot services, application logs, and readiness:

```sh
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml ps --all

KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml \
  logs --tail=200 migrate api gateway worker scheduler caddy

curl --fail http://127.0.0.1:18082/health/ready
curl --fail https://chat.example.com/.well-known/kaede/server
```

Replace `chat.example.com` and the diagnostic port with your values from setup.
`migrate`, `preflight`, `frontend-build`, and storage initialization should
exit successfully; the long-running services should be healthy. That last
discovery request exercises DNS, TLS, nginx, Caddy routing, and the federation
identity in one go.

### Accessing the Administration panel

Register a normal local account through the web interface first, then grant it
the `owner` role from the running API container:

```sh
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml \
  exec -T api kaede admin-grant alice --role owner
```

The command takes a local username (`alice`) or the complete local handle
(`alice@chat.example.com`). Remote users and bot accounts can't hold
instance-administration roles. Open `https://chat.example.com/administration`
and sign in with that account; if it was already signed in, reload the page
after granting the role. The browser never uses or exposes an operator admin
token.

Owners can delegate non-owner roles from the panel, or you can manage them from
the CLI:

```sh
# Available roles: owner, administrator, trust_safety, bot_reviewer,
# operations, and auditor.
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml \
  exec -T api kaede admin-grant bob --role trust_safety

KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml \
  exec -T api kaede admin-revoke bob --role trust_safety
```

Owner grants and removals stay CLI-only. Keep at least two protected local
owner accounts so a single lost account can't lock you out of administration.
The Developer Portal at `/developers` is separate and open to every active
local human account, no administration grant needed. Role capabilities,
instance blocking, bot review, and Trust & Safety behavior are covered in the
[administration and developer portal guide](docs/administration-and-developer-portals.md).

### Routine operation and upgrades

Always use both Compose files for production commands; dropping the generated
file can silently omit your storage and optional-service topology. Follow logs
with:

```sh
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml logs -f \
  api gateway worker scheduler caddy
```

Before an upgrade, take and verify a backup of PostgreSQL, object storage,
secrets, and configuration from one quiesced writer boundary. Then: pull only a
clean fast-forward, rerun `make setup` (it adds new settings without replacing
durable secrets or custom quota values), validate, build, stop writers, run the
new `migrate` service once, and restart. The exact sequence and rollback limits
are in [Manual upgrade and rollback](docs/operator.md#manual-upgrade-and-rollback);
the [backup boundary](docs/operator.md#backup-and-restore-boundary) explains
why a database dump alone isn't enough.

To stop Kaede while preserving named volumes:

```sh
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml down
```

Don't add `-v` unless you actually intend to delete the PostgreSQL, Dragonfly,
Garage, and other named-volume data, and have a verified way to recover it.

### Optional automatic updates

Production builds the web and backend from this Git checkout, so the
recommended updater runs on the host. It fetches the configured remote/branch,
accepts only a clean fast-forward, builds and preflights before any downtime,
optionally calls an operator backup hook, stops writers, migrates once,
restarts, and waits for health checks. Enable it during `make setup`, or change
it later:

```sh
make auto-update-enable
make auto-update-status
make auto-update-run       # immediate, operator-requested check
make auto-update-disable
```

Automatic migrations make verified backups matter even more. Read the
[automatic-update safety and recovery notes](docs/operator.md#optional-automatic-updates)
before turning this on. No updater is enabled by default.

### Mobile apps and background notifications

The official Kaede app is a federated client and can sign in to any compatible
Kaede home. Closed-app delivery goes through the Kaede-operated relay at
`push.kaede.chat`; self-hosted homes don't need (or receive) the official
Firebase credentials.

| Mobile distribution | Closed-app path | Operator requirement |
| --- | --- | --- |
| Official store/GitHub app | Official Kaede relay | Enable the relay during `make setup` |
| Community/custom app | Its own relay or direct FCM | Distinct app ID, signing identity, Firebase/APNs project, and credentials |
| No-push build | None | Foreground alerts and unread state still work |

When a device opts in, its home sends a signed, short-lived, content-free wake
through the relay. The app then fetches the notification details from its home
over an authenticated connection; encrypted message content is decrypted only
on the device. The relay is not anonymous — it can observe the requesting home,
an opaque subscription, delivery timing, network metadata, platform, and the
provider result. It never sees message text, sender names,
guild/channel/message identifiers, attachments, or encryption keys. FCM or APNs
still sees its ordinary device and delivery metadata.

The relay is optional for both the operator and the user. The official app
still connects when a home disables it; you just lose closed-app notifications.
If the relay trust boundary bothers you, build a community client with a
distinct application/bundle ID and provider, or disable push entirely. See
[docs/mobile-push.md](docs/mobile-push.md) for the protocol, privacy,
migration, and custom-build details.

For a manual setup:

```sh
cp .env.example .env
chmod 600 .env
make env-check
```

Replace every placeholder before starting the application. The complete
startup, upgrade, backup, and rollback procedures are in the
[operator guide](docs/operator.md).

## Development and validation

The root Makefile exposes the common checks:

```sh
make compose-check
make check
make test
make audit
make migration-check
```

The acceptance targets exercise larger slices against disposable services:

```sh
make identity-check
make chat-check
make federation-check
make federation-tls-check
make media-check
make voice-check
make release-check
```

These targets use isolated Compose project names and don't publish application
ports. Run `make lock` after changing dependency declarations, and `make dev`
only when you actually want both development instances running.

### Native desktop client

The desktop client wraps the same Svelte interface as the web app in a
locked-down Tauri shell. Rust owns the sensitive and native parts: credentials,
gateway sessions, object uploads, OS notifications, camera and screen capture,
CPAL input/output, LiveKit voice, global push-to-talk, voice activity, echo
cancellation, and local noise suppression. Microphone PCM never crosses the
JavaScript bridge. Adaptive Turnstile challenges open in a restricted,
short-lived in-app window rather than an external browser.

The old Slint client is archived under `desktop/legacy-slint/` and isn't part
of current builds or releases.

Install Rust 1.92 and the platform dependencies listed in
[desktop/README.md](desktop/README.md), then run:

```sh
make desktop-check
make desktop-test
make desktop-build
cargo +1.92.0 run --locked --manifest-path desktop/Cargo.toml -p kaede-tauri
```

Native permissions, packaging, signing, and known operating-system constraints
are documented in the [desktop architecture](desktop/docs/architecture.md),
[platform support](desktop/docs/platform-support.md), and
[release guide](desktop/docs/releasing.md).

### Mobile clients

Android and iOS share a Flutter presentation and domain layer, with native
integrations where they count: secure credentials, biometrics, notifications,
media capture, and LiveKit audio processing. The interface is built for phones,
with compact navigation, sheets, gesture actions, paginated lists, and adaptive
media, rather than a shrunken copy of the desktop layout.

Build and release prerequisites, Firebase configuration, signing, and platform
permissions are documented in [mobile/README.md](mobile/README.md).

## Production routing

The default production layout expects host nginx to own ports 80 and 443. The
containerized Caddy edge binds to `127.0.0.1:18081` and handles internal
routing; it doesn't request TLS certificates. Garage deployments also use a
`media.<domain>` virtual host; external-S3 deployments don't.

Voice stays disabled unless `KAEDE_VOICE_ENABLED=true` and the Compose `voice`
profile is selected. The public nginx route must preserve WebSocket upgrades
for `/gateway` and `/livekit`; the setup wizard's nginx template includes
both.

The two-instance development edge uses loopback ports `18083` and `18443` by
default. All loopback and TURN ports can be changed in `.env` if they conflict
with other services.

## Documentation

- [Architecture](docs/architecture.md)
- [Operator guide](docs/operator.md)
- [Deployment wizard](docs/deployment-wizard.md)
- [Federation protocol](docs/kaede-fed-v1.md)
- [Identity and authentication](docs/m1-identity.md)
- [Core chat](docs/m2-core-chat.md)
- [Federation implementation](docs/m3-federation.md)
- [Media and webhooks](docs/m4-media.md)
- [Voice and calls](docs/m5-voice.md)
- [Release hardening](docs/m6-hardening-release.md)
- [Reference deployment](docs/reference-deployment.md)
- [Desktop client](desktop/README.md)
- [Android and iOS clients](mobile/README.md)

### Docker inside LXC

The Compose definitions avoid an unlimited `memlock` rlimit because
unprivileged LXC hosts commonly reject it during container initialization.
Migration checks need only disposable PostgreSQL, and lockfile tooling runs as
the invoking UID/GID so generated files stay editable on user-namespaced hosts.
