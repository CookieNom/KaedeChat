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
- A native Slint desktop client for Windows, macOS, and Linux with selectable
  audio devices, push-to-talk, voice activity, camera, and desktop capture

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
and can render a host nginx configuration. It writes configuration only; it does not start services,
install nginx files, reload nginx, or obtain certificates. See
[docs/deployment-wizard.md](docs/deployment-wizard.md) for all available options.

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

The Rust workspace under `desktop/` uses the same home-instance REST, gateway,
media, federation, and LiveKit contracts as the web client. The chat interface
is native Slint; adaptive Turnstile challenges use a restricted, short-lived
system web view inside the application rather than opening the full client in a
browser.

Install Rust 1.92 and the platform dependencies listed in
[desktop/README.md](desktop/README.md), then run:

```sh
make desktop-check
make desktop-test
cargo +1.92.0 run --locked --manifest-path desktop/Cargo.toml -p kaede-desktop
```

The [desktop architecture](desktop/docs/architecture.md),
[platform support](desktop/docs/platform-support.md), and
[release guide](desktop/docs/releasing.md) document native permissions,
packaging, signing, and known operating-system constraints.

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

### Docker inside LXC

The Compose definitions avoid an unlimited `memlock` rlimit because
unprivileged LXC hosts commonly reject it during container initialization.
Migration checks require only disposable PostgreSQL, and lockfile tooling runs as
the invoking UID/GID so generated files remain editable on user-namespaced hosts.
