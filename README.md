# Kaede Chat

Kaede is a self-hosted chat platform. Keep one account on your home instance
and use it to join guilds, message friends, and make calls across other Kaede
instances. Your handle is `username@domain`; each guild's home server controls
its membership, permissions, and messages.

Kaede includes text channels, forums, threads, task boards, roles, moderation,
file sharing, search, direct and group messages, and LiveKit voice, video, and
screen sharing. Optional end-to-end encryption protects room content. Web,
Tauri desktop, and Flutter mobile clients share the same server APIs.

## Where to start

| I want to… | Start here |
| --- | --- |
| Host a Kaede instance | [Server setup below](#setup) |
| Build a bot from scratch | [Bot quickstart](docs/bot-api-quickstart.md) |
| Deploy the Discord bridge | [Bridge setup](official-bots/discord-bridge/README.md) |
| Add bot features, including federation, voice, and video | [SDK recipes](docs/bot-sdk-recipes.md) |
| Maintain an existing server | [Operator guide](docs/operator.md) |
| Build a desktop or mobile client | [Desktop](desktop/README.md) · [Mobile](mobile/README.md) |
| Understand the APIs and design | [Documentation index](docs/README.md) |

## Setup

These steps deploy the server. If you only want to run a bot on an existing
instance, use the [bot quickstart](docs/bot-api-quickstart.md); you can skip
Docker, DNS, and server setup.

### 1. Prepare the host and download Kaede

You need a Linux host with Git, Bash, Make, OpenSSL, and Docker Engine with the
Compose plugin. Your account must be able to run Docker. Python and Node for
the server build run inside containers.

For a public instance, prepare a domain pointing to this host, TLS certificates,
and a reverse proxy. The supplied configuration uses host nginx on ports
80/443. Bundled Garage storage also needs `media.<your-domain>` DNS and TLS.
Choose the instance domain carefully: account handles and federation identity
are tied to it.

```sh
git clone https://github.com/CookieNom/KaedeChat.git
cd KaedeChat
docker compose version
```

Run the remaining commands from this directory. Examples use
`chat.example.com`; replace it with your domain.

### 2. Configure the instance

The wizard writes `.env` and a Compose override for your choices. Keep TCP
80/443 public and the Caddy/API loopback ports private. Voice also needs the
[RTC/TURN ports](docs/operator.md#hosts-certificates-and-ports).

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
the proxy file, change firewall rules, or start Kaede for you. For external
S3, keep the buckets private and follow the
[exact CORS rules](docs/deployment-wizard.md#after-setup).

### 3. Start and check the services

Render the production configuration, then build and start it:

```sh
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml config --quiet

KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml \
  up -d --build --wait --wait-timeout 180
```

`preflight` checks configuration, then `migrate` applies pending database
migrations and initializes the instance. Application services wait for both to
succeed. If either fails, inspect its logs and fix the cause before restarting;
do not run a separate host migration or bypass the startup dependencies.

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

### 4. Create your account and grant owner access

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

Owner grants and removals use the CLI. Owners can delegate other staff roles
from the panel. Keep two protected owner accounts to avoid losing access.
See the [administration guide](docs/administration-and-developer-portals.md)
for roles, moderation, and application review.

### 5. Create a guild or install a bot

Open the web app, create a guild, and send a message in its text channel.
If you enabled LiveKit during setup, create a voice channel and test joining
from a second account before adding bot media features.

The Developer Portal at `/developers` is available to active local human
accounts without an administrator grant. Follow the
[bot quickstart](docs/bot-api-quickstart.md) to create an application, install
it in your guild, enroll its Python worker, publish `/ping`, and run it.

## Updates and backups

Use both Compose files for every production command. Back up PostgreSQL,
object storage, secrets, and configuration together before an upgrade.
Follow the [upgrade and rollback steps](docs/operator.md#manual-upgrade-and-rollback)
so old writers cannot run during a migration. To stop services while retaining
their data:

```sh
KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.generated.yml down
```

Adding `-v` deletes named-volume data. Use it only when you intend to erase
that deployment and have a verified backup.

Automatic updates are optional and disabled by default. Configure them in
`make setup`; [the operator guide](docs/operator.md#optional-automatic-updates)
covers backup hooks, scheduling, logs, and recovery.

## Development

The backend uses FastAPI, PostgreSQL, and Dragonfly; the web client uses
SvelteKit. Common checks run through the root Makefile:

```sh
make compose-check
make check
make test
make audit
make migration-check
```

`make dev` starts the two-instance development environment. Acceptance checks
use disposable Compose projects without public application ports:

```sh
make identity-check
make chat-check
make federation-check
make federation-tls-check
make media-check
make voice-check
make release-check
```

Run `make lock` after changing dependency declarations. Native client build
requirements and checks are in the [desktop](desktop/README.md) and
[mobile](mobile/README.md) READMEs.

For Docker inside unprivileged LXC, the supplied Compose files avoid unlimited
`memlock`, and lockfile tooling runs as the invoking UID/GID.
