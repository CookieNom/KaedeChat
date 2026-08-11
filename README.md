# Kaede Chat

Kaede Chat is a self-hosted chat platform with federation built in. Users keep a
single account on their home instance and can join guilds, make friends, send
direct messages, and participate in calls across instance boundaries.

Kaede uses immutable `username@domain` handles, much like email. A guild's home
instance remains authoritative for its membership, permissions, messages, and
moderation decisions. Other instances retain the data their local users are
allowed to access and send writes back to the authority for validation.

## Features

- Guild text and voice channels, roles, channel overrides, invites, moderation,
  reactions, pins, attachments, webhooks, and audit logs
- Direct messages, friend requests, blocking, presence, unread state, and
  two-party calls
- Signed server-to-server delivery with retry queues, sequence recovery,
  permission-filtered replication, and optional retained-history transfer
- Private S3-compatible media storage with malware scanning and image/video
  derivatives; Garage is included as the default self-hosted backend
- LiveKit voice, video, and screen sharing
- A static SvelteKit web client backed by FastAPI, PostgreSQL, and Dragonfly
- A Tauri desktop client for Windows, macOS, and Linux that shares the web UI
  while Rust provides native audio devices, push-to-talk, voice activity,
  speech processing, camera, and desktop capture
- A mobile-first Flutter client for Android and iOS with full chat and guild
  administration, offline-tolerant message state, native LiveKit calls,
  biometric app locking, and category-aware push notifications

The federation wire format is documented in
[docs/kaede-fed-v1.md](docs/kaede-fed-v1.md). Architectural and operational
details are in [docs/architecture.md](docs/architecture.md) and
[docs/operator.md](docs/operator.md).

## Setup

Docker with the Compose plugin is required. Development and validation tools run
inside containers, so Python, Node, uv, and pnpm do not need to be installed on
the host.

Run the interactive setup wizard:

```sh
make setup
```

The wizard creates a private `.env`, selects Garage or an external S3-compatible
provider, configures optional email, voice, KLIPY GIF, and Turnstile services,
can configure optional Firebase Cloud Messaging and source-based automatic
updates, and can render a host nginx configuration. It does not start
containers, install nginx files, reload nginx, or obtain certificates. It only
installs or removes a per-user systemd timer when that option is explicitly
selected. See
[docs/deployment-wizard.md](docs/deployment-wizard.md) for all available options.

### Optional automatic updates

Kaede's production Compose topology builds the web and backend from this Git
checkout. The recommended updater therefore runs on the host: it fetches the
configured remote/branch, permits only a clean fast-forward, builds and
preflights before downtime, optionally invokes an operator backup hook, stops
writers, migrates once, restarts, and waits for health checks. Enable it during
`make setup`, or change it later:

```sh
make auto-update-enable
make auto-update-status
make auto-update-run       # immediate, operator-requested check
make auto-update-disable
```

Automatic migrations make verified backups especially important. Read the
[automatic-update safety and recovery notes](docs/operator.md#optional-automatic-updates)
before enabling it. No updater is enabled by default.

### Optional Firebase mobile notifications

Firebase Cloud Messaging is required only for reliable notifications after the
mobile process has been suspended or terminated. FCM is a no-cost Firebase
product and does not require billing or Google Analytics, but creating the
Firebase project requires a Google account.

Before choosing Firebase in `make setup`:

1. Create a project in the [Firebase console](https://console.firebase.google.com/).
2. Add an Android application with package name `chat.kaede.mobile`.
3. Download its `google-services.json` client configuration and save it as
   `mobile/android/app/google-services.json`.
4. Create a dedicated Google Cloud service account with only **Firebase Cloud
   Messaging API Admin** (`roles/firebasecloudmessaging.admin`), generate a JSON
   key for it, and save the file outside version control. Revoke and replace the
   key immediately if it is ever pasted into chat, logs, or an issue tracker.
5. Run `make setup` and enable Firebase Cloud Messaging. Choose either to read
   the private service-account JSON from a local file or paste its complete
   contents into the hidden multiline prompt. For pasted input, finish with
   `KAEDE_FIREBASE_JSON_END` on a line by itself. The wizard stores only the
   base64 representation in the private `.env` file.
6. Rebuild the mobile application, restart the Kaede API and worker processes,
   and enable system notifications from Kaede's notification settings.

These are two different files: `google-services.json` identifies the Android
client and is bundled into the APK; the service-account JSON authorizes the
backend to send notifications and must never be bundled, logged, or committed.
Both must belong to the same Firebase project. Kaede works in the foreground
without either file, but terminated-process delivery does not.

FCM transport is TLS-protected but not end-to-end encrypted, so Kaede sends FCM
only a short-lived random wake token. The authenticated app redeems that token
directly from the Kaede API and creates the visible notification locally;
sender names, message text, and channel/message references never enter the FCM
payload. See [mobile/README.md](mobile/README.md#firebase-cloud-messaging-setup)
for build details and the exact privacy boundary.

For a manual setup:

```sh
cp .env.example .env
chmod 600 .env
make env-check
```

Replace every placeholder before starting the application. The complete startup,
upgrade, backup, and rollback procedures are in the
[operator guide](docs/operator.md).

## Development and validation

Common checks are exposed through the root Makefile:

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

These targets use isolated Compose project names and do not publish application
ports. Run `make lock` after changing dependency declarations. Run `make dev`
only when you intend to start both development instances.

### Native desktop client

The supported desktop client packages the same Svelte interface as the web app
inside a locked-down Tauri shell. Rust owns credentials, gateway sessions,
object uploads, operating-system notifications, native camera and screen
capture, CPAL input/output, LiveKit voice, global push to talk, voice activity,
echo cancellation, and local noise suppression. No microphone PCM crosses the
JavaScript bridge. Adaptive Turnstile challenges use a restricted, short-lived
in-app verification window rather than an external browser.

The former Slint client remains under `desktop/legacy-slint/` as an archived
experimental client and is not part of current builds or releases.

Install Rust 1.92 and the platform dependencies listed in
[desktop/README.md](desktop/README.md), then run:

```sh
make desktop-check
make desktop-test
make desktop-build
cargo +1.92.0 run --locked --manifest-path desktop/Cargo.toml -p kaede-tauri
```

The [desktop architecture](desktop/docs/architecture.md),
[platform support](desktop/docs/platform-support.md), and
[release guide](desktop/docs/releasing.md) document native permissions,
packaging, signing, and known operating-system constraints.

### Mobile clients

Android and iOS share a Flutter presentation and domain layer while retaining
native platform integrations for secure credentials, biometrics, notifications,
media capture, and LiveKit audio processing. The mobile interface uses compact
navigation, sheets, gesture actions, paginated lists, and adaptive media rather
than embedding the desktop or web layout.

Build and release prerequisites, Firebase configuration, signing, and platform
permissions are documented in [mobile/README.md](mobile/README.md).

## Production routing

The default production layout expects host nginx to own ports 80 and 443. The
containerized Caddy edge binds to `127.0.0.1:18081` and performs internal routing;
it does not request TLS certificates. Garage deployments also use a
`media.<domain>` virtual host, while external-S3 deployments do not.

Voice is disabled unless `KAEDE_VOICE_ENABLED=true` and the Compose `voice`
profile is selected. The public nginx route must preserve WebSocket upgrades for
`/gateway` and `/livekit`; the setup wizard's nginx template includes both.

The two-instance development edge uses loopback ports `18083` and `18443` by
default. All loopback and TURN ports can be changed in `.env` when they conflict
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
Migration checks require only disposable PostgreSQL, and lockfile tooling runs as
the invoking UID/GID so generated files remain editable on user-namespaced hosts.
