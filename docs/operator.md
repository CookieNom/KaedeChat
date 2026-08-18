# Operator guide

This guide covers the supplied deployment and release controls. Reviewing your
infrastructure, threat model, capacity, restoration process, and key-rotation
policy is still your job.

## Hosts, certificates, and ports

Production assumes host nginx owns public TCP 80/443. If you run the bundled
Garage, your certificate must cover both `chat.example.com` and
`media.chat.example.com`. External-S3 deployments drop the unused media
virtual host and only need the main application name. The internal Caddy edge
binds only to `127.0.0.1:18081`, and the API diagnostic binding defaults to
`127.0.0.1:18082`. If you change the edge port during a manual setup, update
both `.env` and the host-nginx upstream.

LiveKit uses host networking and sits behind the `voice` Compose profile, so
the default topology doesn't bind its host ports. The edge returns 404 for
`/livekit` unless `KAEDE_VOICE_ENABLED=true`; the voice preflight requires
that exact opt-in. Once you've enabled voice with both the setting and
`--profile voice`, allow the selected RTC/TURN traffic through the host and
provider firewalls: TCP `LIVEKIT_RTC_TCP_PORT`, UDP `LIVEKIT_RTC_UDP_PORT`,
UDP `KAEDE_TURN_UDP_PORT`, and TCP `LIVEKIT_TURN_TLS_PORT`. Keep
`LIVEKIT_CONTROL_PORT` and the API/edge loopback ports blocked from external
interfaces. The TURN certificate paths must be absolute host paths, and the
certificate name must match `KAEDE_DOMAIN`.

The defaults are control TCP 7880, RTC TCP 7881, RTC UDP 7882, TURN/TLS TCP
5349, and TURN UDP 13478. Every host-networked LiveKit process on the same
host needs its own five-port set. The setup wizard can find and reserve an
available set automatically, or take one you pick by hand. Automatic
selection avoids conflicts with listeners present while setup runs. It can't
stop another process from claiming a selected port before Compose starts.

## Secrets and initial configuration

The recommended path is the interactive setup wizard:

```sh
make setup
```

The wizard generates compatible keys securely and preserves existing durable
secrets. It configures Garage or an external S3 provider, the email mode, and
the optional services, and it can render a configuration for host-level
nginx. It never starts the topology or reloads host nginx. If you select
automatic updates, it installs or removes only the current user's systemd
timer. See [the deployment-wizard guide](deployment-wizard.md) for every
option and generated file. If you used the wizard, run the two-file Compose
command in `deploy/generated/README.txt`.

Rerunning the wizard preserves existing quota tuning. As a one-time upgrade,
it recognizes the exact defaults written by older Kaede setup versions and
raises `KAEDE_FEDERATION_HISTORY_MAX_MESSAGES` from 250,000 to 2,000,000 and
`KAEDE_MEDIA_REMOTE_CACHE_BYTES` from 20 GiB to 100 GiB. It leaves other
values alone. If you maintain `.env` by hand, remove those two old
assignments to inherit the current defaults, or set them yourself after
checking available PostgreSQL and object-storage capacity.

The quota menu is optional. Keep the current or recommended limits and it
asks no sizing questions at all. Common mode covers the rolling DM cache,
remote-guild byte budgets, retained-history import, remote inbox bytes, and
the remote-media LRU cache. Advanced mode exposes the remaining abuse, hard,
per-origin, aggregate, grant, page, and in-flight ceilings. Counts accept
`K`/`M`/`B`. Byte sizes accept decimal `KB`/`MB`/`GB`/`TB`, binary
`K`/`M`/`G`/`T`, and IEC names such as `GiB`. Setup echoes the parsed base
value, keeps paired prompts consistent (a scoped or cache limit can't exceed
its aggregate or hard limit), and prints a summary before writing `.env`.
None of this allocates capacity or checks that the host has enough disk.

The email provider menu includes a disabled mode. With email disabled,
registration needs only a username and password, and no verification or
delivery intent is created. Email changes and self-service password recovery
are disabled too. Document an operator-assisted recovery policy before
choosing it.

For a manual setup instead, copy the template and replace every placeholder.

Copy `.env.example` to `.env`, restrict it to the service operator, and never
commit it:

```sh
cp .env.example .env
chmod 600 .env
```

Before starting any service, validate both the file itself and the effective
application environment:

```sh
make env-check
```

The target rejects duplicate assignments, group/world-readable production
files, documented placeholder credentials, disabled production malware
scanning, and unknown `KAEDE_` settings. Compose's `--env-file` normally
affects interpolation only, so Kaede also loads the selected file into its
isolated preflight process. That catches misspellings that would otherwise
fall back to a default. For a file other than the repository-root `.env`, run
`make env-check ENV_FILE=/absolute/path/to/kaede.env`, then set both
`--env-file` and `KAEDE_OPERATOR_ENV_FILE` to that same absolute path for
later direct Compose commands.

Generate independent values. These commands produce characters that are safe
in headers, YAML, and the PostgreSQL URL:

```sh
openssl rand -base64 32 | tr '+/' '-_'       # KAEDE_SECRET_KEY
openssl rand -hex 32                          # KAEDE_PROXY_SECRET
openssl rand -hex 32                          # KAEDE_EDGE_SECRET
openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' # KAEDE_GATEWAY_SECRET_KEY
openssl rand -hex 32                          # KAEDE_ADMIN_TOKEN (optional)
openssl rand -hex 24                          # POSTGRES_PASSWORD
openssl rand -hex 32                          # DRAGONFLY_PASSWORD
openssl rand -hex 32                          # GARAGE_RPC_SECRET
openssl rand -hex 32                          # GARAGE_ADMIN_TOKEN
printf 'GK%s\n' "$(openssl rand -hex 16)"     # KAEDE_MEDIA_S3_ACCESS_KEY (Garage)
openssl rand -hex 32                          # KAEDE_MEDIA_S3_SECRET_KEY (Garage)
openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' # GRAFANA_ADMIN_PASSWORD
printf 'LK%s\n' "$(openssl rand -hex 8)"      # LIVEKIT_API_KEY
openssl rand -hex 32                          # LIVEKIT_API_SECRET
```

Use a different value for every line. The PostgreSQL password goes into both
`POSTGRES_PASSWORD` and `KAEDE_DATABASE_URL`; the Dragonfly password goes
into both `DRAGONFLY_PASSWORD` and `KAEDE_DRAGONFLY_URL`. Set the public
HTTPS `KAEDE_APP_URL`, the email sender/backend credentials, and an optional
random `KAEDE_ADMIN_TOKEN`. When enabling the `voice` profile, also set
`KAEDE_VOICE_ENABLED=true`, `KAEDE_VOICE_PUBLIC_URL=wss://<domain>/livekit`,
the LiveKit keys, the five port settings, and the certificate paths. The
control port in `KAEDE_VOICE_LIVEKIT_URL` must match `LIVEKIT_CONTROL_PORT`.

Preserve `KAEDE_SECRET_KEY`. The instance signing key, pending email-outbox
messages, and other protected application material stored in PostgreSQL
can't be decrypted without it. Changing or losing it can strand pending
verification and reset mail, so restore and rotate it only through a
reviewed application workflow. Preserve the distinct
`KAEDE_GATEWAY_SECRET_KEY` as well; it satisfies strict process
configuration without handing the instance master key to the gateway.

Optional interaction services are disabled by default. Set
`KAEDE_KLIPY_ENABLED=true` with a private `KAEDE_KLIPY_API_KEY` to expose the
GIF picker. Set `KAEDE_TURNSTILE_ENABLED=true`, `KAEDE_TURNSTILE_SITE_KEY`,
and the private `TURNSTILE_SECRET` to require Turnstile during registration
and after a failed sign-in attempt. The API key and Turnstile secret belong
only in backend environments; neither appears in public configuration
responses.

Closed-app notifications normally use the public Kaede relay. Set
`KAEDE_PUSH_RELAY_ENABLED=true`; an ordinary home needs no Firebase
credential. The relay URL and logical origin are pinned separately because
`push.kaede.chat` serves transport for the `kaede.chat` signing authority.
Provider tokens go from the official app straight to the relay. Your home
keeps only opaque subscriptions and wake secrets.

Only the relay operator sets `KAEDE_PUSH_RELAY_SERVICE_ENABLED=true` and
`KAEDE_PUSH_RELAY_FCM_SERVICE_ACCOUNT_B64`. The service-account credential
must reach relay workers only. Never let it reach API processes, browsers,
mobile apps, logs, or ordinary federated homes. Direct FCM is still
available with `KAEDE_PUSH_ENABLED` for a separately signed custom app
distribution; it does not notify the official app.

See [mobile push delivery](mobile-push.md) for the data flow, privacy table,
E2EE behavior, conversion procedure, queue failure semantics, and
custom-build requirements.

## End-to-end encryption activation

New encrypted-room activation is enabled by default and fails closed whenever
the room, client, or participating home cannot satisfy the protocol. Before a
public launch, complete the release gates and client compatibility checks in
[the E2EE protocol and rollout guide](e2ee.md), or explicitly set
`KAEDE_E2EE_ACTIVATION_ENABLED=false` on every participating home. Mixed
settings safely reject new proposals; they never downgrade an active encrypted
room. Turning the flag off later hides new activation but leaves rekey,
recovery, selective-disclosure reports, and active encrypted rooms operational.

Set the same `KAEDE_EDGE_SECRET` in `.env` and in the nginx
`X-Kaede-Edge-Secret` header. It must differ from `KAEDE_PROXY_SECRET`. The
internal edge receives only the domain and these two edge credentials.
Application processes don't receive Garage administration, LiveKit, or
host-edge secrets.

## Object storage

Kaede requires an S3-compatible object store; Garage is only the default.
Pick exactly one deployment mode.

For self-hosted Garage, keep `KAEDE_MEDIA_STORAGE_BACKEND=garage` and start
with `deploy/compose.yml`. The provider-neutral `storage-init` service
creates all three private buckets idempotently. Single-node Garage has no
replica redundancy, so back up both its metadata and data volumes
independently.

For AWS S3, Backblaze B2, or another compatible service, start from
`.env.s3.example`, pre-create three private buckets, and set:

- `KAEDE_MEDIA_STORAGE_BACKEND=s3`;
- the provider's HTTPS API origin in both `KAEDE_MEDIA_S3_ENDPOINT` and
  `KAEDE_MEDIA_PUBLIC_BASE_URL`;
- the exact SigV4 region in `KAEDE_MEDIA_S3_REGION` (`us-west-004` for a B2
  endpoint such as `s3.us-west-004.backblazeb2.com`);
- `KAEDE_MEDIA_S3_ADDRESSING_STYLE=path` unless the provider requires
  virtual-hosted requests;
- `KAEDE_MEDIA_S3_CREATE_BUCKETS=false`; and
- an access key and secret with read, write, delete, and bucket-HEAD access
  to all three configured buckets. Set `KAEDE_MEDIA_S3_SESSION_TOKEN` only
  for temporary credentials.

External providers must permit browser `PUT` requests from the exact
`https://<KAEDE_DOMAIN>` origin with the `Content-Type` request header. Don't
make any bucket public: all reads and writes use short-lived SigV4 URLs. The
application rejects redirects and production HTTP endpoints. Backblaze calls
its service B2. It needs an S3-compatible application key, endpoint, and
matching region, and its API uses path-style endpoints of the form
`https://s3.<region>.backblazeb2.com/<bucket>`.
Provider references: [AWS S3 request addressing](https://docs.aws.amazon.com/AmazonS3/latest/userguide/VirtualHosting.html)
and [Backblaze's S3-compatible API](https://www.backblaze.com/apidocs/introduction-to-the-s3-compatible-api).

Kaede deletes the current object name. If provider versioning is enabled,
older versions may stay billable and recoverable even though Kaede can no
longer serve them. Disable versioning where supported, or configure a
reviewed lifecycle rule that expires noncurrent/hidden versions within your
retention and privacy requirements. Backblaze B2 buckets are versioned by
default, so the lifecycle step is required there for bounded physical
retention.

Use the external-storage override for every Compose command:

```sh
cp .env.s3.example .env
chmod 600 .env
docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.s3.yml config --quiet
docker compose --env-file .env \
  -f deploy/compose.yml -f deploy/compose.s3.yml up -d --build \
  --wait --wait-timeout 180
```

The override removes Garage from the rendered service set. `storage-init`
verifies all three pre-created buckets before API and worker startup, and it
fails closed on bad credentials, missing buckets, or an unreachable provider.
Virtual addressing puts the bucket before the endpoint host, so it requires
DNS hosts and bucket names without dots. Keep the worker and ClamAV healthy:
originals stay unavailable by design when a scan can't reach a clean result.

Changing this setting doesn't migrate existing objects. A live Garage-to-S3
or provider-to-provider move must copy all three buckets with object keys
unchanged, verify counts and hashes, stop writers for the final
synchronization, and only then change the backend configuration.

Browser `PUT` URLs can write only staging keys. The worker copies the exact
bytes that passed type validation and malware scanning to a server-only,
content-addressed clean key, then switches the database reference atomically.
It keeps the staging-key reference until the presign lifetime has elapsed. So
a client that rewrites staging after an early deletion still can't affect
served bytes, and the cleanup sweep deletes the rewrite.

The nginx example gives only the media virtual host a 101 MiB body allowance,
bounded concurrent connections, request rate, and five-minute proxy timeouts.
The main API/federation host stays limited to 2 MiB. The media path streams
request bodies instead of buffering them on host disk. If you raise
`KAEDE_MEDIA_MAX_ATTACHMENT_BYTES`, update and review the media-server
`client_max_body_size` too; the application ticket and signed PUT length are
still the authoritative per-object checks.

## Microsoft PhotoDNA image matching

PhotoDNA is optional because Microsoft distributes its Edge Hash generator
under a separate confidential license. Do not add the SDK archive, native
libraries, Python wrapper, WebAssembly build, or generated hashes to this
repository, an image, an artifact, or a log. Extract the licensed SDK on each
host into an operator-owned directory whose root contains `clientlibrary/python`
and the platform native library. Keep other/world permissions closed and grant
read/traverse access only to the operator's primary group (directories `0750`,
files `0640`). Set:

```env
KAEDE_PHOTODNA_ENABLED=true
KAEDE_PHOTODNA_SUBSCRIPTION_KEY=<Microsoft subscription key>
PHOTODNA_EDGEHASHGENERATOR=/absolute/host/path/to/photodna-sdk
```

Compose mounts that directory read-only at `/opt/photodna` only in preflight,
API, and worker containers. API and worker retain the image's dedicated
unprivileged UID `10001`; Compose grants only the supplemental
`OPERATOR_ENV_GID` (default `1000`) needed to read the SDK. Preflight runs as
the operator UID/GID because it also validates the mode-`0600` operator env
file. Set `OPERATOR_ENV_UID`/`OPERATOR_ENV_GID` to the account that owns the
SDK directory, and ensure that group has only read/traverse permission. This
avoids both world-readable confidential files and changing the long-running
services away from their image identity.
Preflight loads the native library and rejects an incomplete or incompatible
installation before the API starts. The matcher URL is fixed in code to
`https://api.microsoftmoderator.com/photodna/v1.0/MatchHash`; allow outbound
TCP 443 to `api.microsoftmoderator.com` and never substitute HTTP.

For plaintext local uploads the worker runs magic/type validation and ClamAV,
creates an Edge Hash V2 in an isolated child process, and submits only that hash
to Microsoft before any clean-key promotion. Plaintext images fetched from a
federated home receive the same PhotoDNA decision before entering the local
remote-media cache. A positive result is deleted from staging or the temporary
remote spool, marked quarantined, and creates one automated `illegal_content`
case in Administration → Reports. The report retains the provider tracking ID,
source/violation/distance flags, attachment and optional uploader/message
references, MIME type, and ordinary SHA-256 incident identifier. It retains no
image bytes, thumbnail, object key, or PhotoDNA hash. Provider/generator failure
is fail-closed: the object stays unavailable and normal task retries continue.
The adapter rejects animations above 256 frames or 25 million decoded pixels
in total. It also takes an advisory kernel lock on the mounted SDK directory,
so the API's Uvicorn processes and the media worker run only one native image
decode at a time even though they are separate processes and containers. Keep
the SDK on a local filesystem (or one with working POSIX `flock` semantics).

PhotoDNA cannot inspect an E2EE attachment because the server receives only
ciphertext and does not have the room key. The existing E2EE activation warning
therefore states that server-side file and malware scanning stops. A recipient
can still submit a client-decrypted report, but Kaede must never silently upload
E2EE plaintext to PhotoDNA.

Kaede locally excludes plaintext images below `160x160` pixels. MatchHash
receives a fixed-size Edge Hash rather than the source file, so Kaede does not
create a source-byte-size bypass; Microsoft remains authoritative for the
current upper eligibility window and status `3208` is treated as a terminal
provider-ineligible result rather than leaving an upload stuck in retries.
Other generator or provider errors remain fail-closed and retryable, while
MIME validation, ClamAV, dimension limits, and normal image processing still
apply.

## Validate and start

The checks below may pull or build images but publish no project ports.
`make check`, `make test`, and each acceptance target use distinct disposable
Compose project names and clean up their own volumes.

```sh
make lock                 # only after dependency declarations change
make env-check            # validate the production .env before any startup
make compose-check
make check
make test
make audit
make migration-check
make identity-check
make chat-check
make federation-check
make federation-tls-check
make media-check
make voice-check
make release-check
```

Passing these checks doesn't replace a deployment-specific security,
capacity, backup, and recovery review. To start the production topology:

```sh
docker compose --env-file .env -f deploy/compose.yml config --quiet
docker compose --env-file .env -f deploy/compose.yml up -d --build \
  --wait --wait-timeout 180
docker compose --env-file .env -f deploy/compose.yml ps
curl --fail http://127.0.0.1:18082/health/ready
```

The one-shot preflight runs before data services. Then `migrate` applies
every pending Alembic revision in dependency order through `head` and
idempotently bootstraps the instance identity. API, gateway, worker, and
Caddy start only after that one-shot service succeeds. On an empty database
this creates a fresh schema; on later starts it upgrades the existing one. A
stored domain mismatch, migration failure, or invalid secret fails closed.

The API health check uses readiness rather than liveness and includes a
75-second start period for the snowflake worker-ID quarantine. Caddy waits
for that check, and `--wait` keeps the startup command pending until the
dependency is ready.

Install the nginx example in its `http` context and replace every example
value. Keep the resulting secret-bearing file root-owned and
non-world-readable (for example, mode `0600`). Then use your distribution's
normal validation and reload commands (commonly `nginx -t` followed by
`systemctl reload nginx`). Don't reload a configuration that failed
validation.

The example defines HTTP-context zones for the exact `/gateway`, `/livekit`
signaling, and federation-link upgrade routes, and for the separate media
virtual host. The `/livekit` location must forward both its ordinary
validation requests and the `/livekit/rtc` WebSocket upgrade; routing it
through the ordinary catch-all strips the signaling upgrade. Gateway
admission allows at most 20 incomplete/active upgrades per source address,
with 10 new upgrades per second and a burst of 20. If you serve unusually
large groups behind one NAT, you can raise those values after measuring
reconnect bursts, but keep both the connection and request limits. Kaede also
keeps an independent process-local cap, so bypassing host nginx through a
trusted loopback path can't create an unbounded pre-authentication queue.

## Development CA

`make dev` is the only target that publishes the Alpha/Beta edge ports. It
uses Caddy's local CA at `https://alpha.localhost:18443` and
`https://beta.localhost:18443`. If the browser doesn't trust it, export the
CA after the stack starts and import it into the development machine or
browser trust store:

```sh
docker compose -f deploy/compose.dev.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt ./kaede-dev-root.crt
```

Never install this development CA on production systems. Run `make dev-down`
when finished. Add `-v` by hand only when you mean to delete the development
databases and the CA.

`make federation-tls-check` uses a separate one-hour ephemeral CA inside its
disposable Compose project and publishes no host ports. The validation-only
`KAEDE_FEDERATION_CA_FILE` setting lets Alpha/Beta trust that CA. Settings
reject both custom federation trust roots and peer URL overrides in
production.

## Backup and restore boundary

A recoverable backup set contains all of the following, taken at one quiesced
writer boundary:

- a PostgreSQL custom-format dump;
- the Garage data and metadata volumes, or a provider-native protected copy
  of all three external S3 buckets;
- `.env` or an equivalent secret-manager export, especially
  `KAEDE_SECRET_KEY`, `KAEDE_GATEWAY_SECRET_KEY`, the object-store keys, and
  the LiveKit keys;
- the deployed revision, migration head, internal-edge/host-nginx
  configuration, and TLS certificate automation state.

First place host nginx in maintenance mode and stop every application writer.
If the voice profile is active, stop LiveKit too. For bundled Garage, stop it
only after the application writers have stopped. Leave PostgreSQL running so
it can produce a consistent dump:

```sh
docker compose --env-file .env -f deploy/compose.yml \
  stop caddy api gateway worker scheduler livekit
# Garage deployments only, after the writer stop completes:
docker compose --env-file .env -f deploy/compose.yml stop garage
```

Create a PostgreSQL dump with a destination you control, and verify that
`pg_restore --list` can read it while writers are still stopped:

```sh
install -d -m 0700 /var/backups/kaede
docker compose --env-file .env -f deploy/compose.yml exec -T postgres \
  pg_dump -U kaede -d kaede -Fc > /var/backups/kaede/kaede-postgres.dump
pg_restore --list /var/backups/kaede/kaede-postgres.dump >/dev/null
```

Before restarting writers, take a protected copy or provider snapshot of all
three object buckets. A Garage backup must include both its metadata and data
volumes; a copy of only one is unusable. For external S3, record the provider
snapshot/version boundary and verify object counts. Encrypt the database
dump, the object copy, and the secret-manager export at rest. Test
restoration of the combined set, not each component on its own.

After every component is copied and verified, start Garage first when it's in
use, then start the application topology. Remove maintenance mode only after
readiness succeeds. Don't restart writers between the database dump and the
object-copy boundary.

Dragonfly stores leases, sessions, presence, cache, and task transport. Its
snapshot improves continuity, but it's not a substitute for the PostgreSQL
and object-store backups.

Restore into a separate Compose project, database, volumes, and object bucket
namespace first. Don't point a drill at production buckets: cleanup jobs are
allowed to delete objects. Keep restored SMTP, federation egress, webhooks,
and public DNS/firewall exposure disabled, and use different loopback ports
from the live instance. The restored database must use the original domain
and `KAEDE_SECRET_KEY` to verify protected identity material, which means the
clone must never be network-visible at the same time as the authoritative
instance. Run `alembic upgrade head`, bootstrap, and validate
identity/chat/federation behavior before any cutover. A backup that hasn't
passed an isolated restore drill isn't usable.

Keep both the worker and scheduler services running for identity email
delivery. API requests commit encrypted delivery intents even if Dragonfly
can't accept the immediate wake; the scheduler's minute sweep is the recovery
path. Operational logs include only opaque outbox IDs and retry counters,
never recipient addresses, message bodies, provider errors, or one-time
links. The console backend is a development-only exception and must never be
selected in production.

## Upgrade and rollback

### Optional automatic updates

The supplied automatic updater runs on the host because this deployment
builds the Kaede web and backend images from the local source tree. It's
disabled by default. Enable it in `make setup`, or manage it later:

```sh
make auto-update-enable
make auto-update-status
make auto-update-run
make auto-update-disable
journalctl --user -u kaede-auto-update.service
```

The user running the timer must own this checkout and `.env`, have Docker
Compose access, and have a working user systemd manager. On servers where the
user manager stops at logout, an administrator must enable lingering for the
Kaede service account, then verify the timer:

```sh
sudo loginctl enable-linger kaede
systemctl --user list-timers kaede-auto-update.timer
```

Replace `kaede` with the actual unprivileged service account. Lingering is a
host policy decision; setup never enables it. Don't run the timer as root
merely to avoid configuring Docker access.

The settings are ordinary non-secret entries in `.env`:

```dotenv
AUTO_UPDATE_ENABLED=false
AUTO_UPDATE_REMOTE=origin
AUTO_UPDATE_BRANCH=main
AUTO_UPDATE_INTERVAL=6h
AUTO_UPDATE_JITTER=30m
# AUTO_UPDATE_BACKUP_HOOK=/usr/local/sbin/kaede-backup
AUTO_UPDATE_WAIT_TIMEOUT_SECONDS=300
```

Intervals are `6h`, `12h`, `1d`, or `1w`. The timer adds the configured
random delay so fetches don't synchronize across hosts. The backup hook must
be an absolute, regular, non-symlink executable. It runs only after new
images and preflight succeed but before services stop, with
`KAEDE_UPDATE_FROM`, `KAEDE_UPDATE_TO`, and `KAEDE_ROOT` in its environment.
It must exit nonzero unless it has created and verified a database and
object-store backup at one consistent boundary.

Each run takes a local lock. It refuses a dirty tracked checkout or a
detached/wrong branch, fetches only the configured branch, and verifies that
the new commit is a descendant of the current one. It won't follow a
force-push, a downgrade, or divergent local history. It then validates and
builds before any downtime, runs the backup hook, stops `caddy`, `api`,
`gateway`, `worker`, and `scheduler`, runs the new migration image once,
starts the topology without rebuilding, and waits for Compose health checks.
The deployed commit is recorded, so a failed deployment stays retryable even
when Git already reached the target commit.

The updater first runs itself from a private temporary copy, so a
fast-forward can't replace the shell program while that same program is still
executing. Configure Git authentication to work non-interactively for the
timer account. Never put a personal access token directly in the remote URL
or the systemd unit.

If fetching, preflight, building, or the backup hook fails, the old services
keep running. If migration or startup fails, writers stay stopped: inspect
the journal and Compose logs, keep the public edge in maintenance mode, and
use the reviewed manual recovery procedure below. The updater won't guess at
schema downgrades or restore a database for you.

Treat write access to the configured Git branch and remote as production code
execution. Protect the GitHub organization and maintainers with MFA and
branch protection, require reviewed, green changes before merging, and prefer
a stable release branch over a development branch. Git transport authenticity
doesn't replace review of the code being deployed.

On a host without systemd, set `AUTO_UPDATE_ENABLED=true` only after
reviewing the same risks, and invoke `deploy/auto-update.sh run` from the
service account's cron. Redirect output to a protected log and set up
equivalent alerting; cron has weaker missed-run handling and status
visibility than the supplied timer.

Other viable architectures have different tradeoffs:

- Published application images plus a registry watcher give you immutable
  digests and faster pull/restart cycles. Kaede does not currently publish
  the web/backend images, though, and a watcher needs highly privileged
  Docker-socket access and can't safely coordinate Kaede's backup/migration
  boundary on its own.
- A GitHub Actions deployment over SSH or a self-hosted runner can require
  CI, approvals, and signed releases before rollout. It adds runner and
  credential trust, GitHub availability, and the same backup/migration
  orchestration. A good later choice for a larger operation, not a simpler
  local default.
- A plain cron entry is widely available but has poorer logging, randomized
  scheduling, missed-run behavior, and enable/disable ergonomics than
  systemd.

### Manual upgrade and rollback

Before upgrading: take and verify backups, record `alembic current`, review
every new migration downgrade, and render the new Compose configuration.
Never let an old application writer overlap a new schema migration. Build the
new images first, place host nginx in maintenance mode, stop `caddy`, `api`,
`gateway`, `worker`, and `scheduler`, and then run the new `migrate` image
exactly once:

```sh
make env-check
docker compose --env-file .env -f deploy/compose.yml build
docker compose --env-file .env -f deploy/compose.yml \
  stop caddy api gateway worker scheduler
docker compose --env-file .env -f deploy/compose.yml \
  run --rm --no-deps migrate
docker compose --env-file .env -f deploy/compose.yml \
  up -d --no-build --wait --wait-timeout 180
```

For the migration that introduces server-only clean media keys, wait at least
`KAEDE_MEDIA_UPLOAD_TTL_SECONDS` after quiescing the old deployment before
running the migration. That guarantees no browser credential issued by the
old version can still rewrite an already-clean legacy key. Keep maintenance
mode in place until readiness, smoke checks, the migration head, and instance
discovery have all been verified. The startup `migrate` gate may run again
during `up`; its upgrade and bootstrap operations are idempotent.

API workers quarantine newly available snowflake worker IDs for 60 seconds
before becoming ready. Allow at least this startup window in
external health checks and deployment orchestration. Bypassing it can
reintroduce ID collisions after a Dragonfly state loss.

Application rollback is safe only while the old code understands the current
schema. If it doesn't, stop writers and either restore the pre-upgrade backup
or run a specifically reviewed Alembic downgrade with the matching code.
Never guess at a downgrade target. Never restore PostgreSQL without the
matching `KAEDE_SECRET_KEY`.

The origin-scoped event migration refuses to downgrade if the
same federation event ID exists under multiple origin domains, or the same
guild event ID exists under multiple guild domains. The legacy schema can't
represent either valid state without destroying authenticated history. Keep
the newer revision, or archive an origin through a separately reviewed
procedure. Don't delete rows merely to force a downgrade.

The encrypted-email-outbox migration invalidates legacy pending email-change
confirmations because older rows held the target address as plaintext JSON.
Users with an in-flight change must request a fresh confirmation after this
upgrade. Verification and password-reset credentials are unaffected.

Rotate the instance federation signing key inside a running API container:

```sh
docker compose --env-file .env -f deploy/compose.yml exec -T api kaede rotate-key
```

The command takes a database-wide identity lock, verifies the stored keypair
with the current `KAEDE_SECRET_KEY`, and atomically installs a uniquely named
Ed25519 key. The former key stays in `old_verify_keys`. Keep it for at least
the configured federation event-retention window before a future reviewed
retirement workflow.

After the command's reported overlap deadline, retire that exact historical
key with `kaede retire-key <key-id>`. The command refuses the current key and
refuses early retirement. `--force-compromised` bypasses the overlap only for
an active key compromise and can make queued historical envelopes
unverifiable. Record the incident and notify federation peers before using
it.

## Federation allowlists and blocklists

Set `KAEDE_FEDERATION_MODE=allowlist` to require explicit peer approval. The
admin API is authenticated with `KAEDE_ADMIN_TOKEN`; send the token in a
protected header and never put it in a URL.
`GET /api/v1/admin/federation/blocks/export` produces Mastodon-compatible
CSV, and `POST /api/v1/admin/federation/blocks/import` accepts a bounded CSV
body. A `silence` block holds non-security traffic; a `suspend` block holds
all peer traffic. Security reconciliation events stay durable either way.
Export before bulk changes, review subdomain inclusion, and keep the export
with the deployment revision. Removing a block schedules authoritative
replica reconciliation rather than blindly releasing stale writes.

### Federation storage budgets

`make setup` can edit these values without raw byte calculations. Common
tuning covers the principal retained-cache budgets; advanced tuning covers
every admission and aggregate ceiling. Keeping the default choice preserves
current values. Setup enforces the same cross-setting relationships as
production preflight, and `make env-check` stays the final
deployment-boundary check after manual edits.

Retained inbox claims and signed event envelopes are bounded independently
for each remote origin and for the whole instance. The defaults are five
million claims and 16 GiB of envelopes per origin, with a 50-million-claim
and 160 GiB instance-wide ceiling. These are admission ceilings, not
reservations. Raising them doesn't allocate disk, and the database still
needs normal free-space, WAL, index, vacuum, and backup headroom. Adjust
`KAEDE_FEDERATION_INBOX_MAX_EVENTS_PER_ORIGIN`,
`KAEDE_FEDERATION_INBOX_MAX_BYTES_PER_ORIGIN`,
`KAEDE_FEDERATION_INBOX_MAX_EVENTS_TOTAL`, and
`KAEDE_FEDERATION_INBOX_MAX_BYTES_TOTAL` for your expected peer and guild
volume. Keep the global limits at least as large as their per-origin
counterparts.

When either budget is full, newly signed events get a retryable
`KAED_FED_INBOX_QUOTA_EXCEEDED` result and are not claimed or applied.
Delivery can resume after retention frees space or you raise the limit. The
daily federation-retention task removes expired rows and reconciles quota
counters against retained database state. It also removes inaccessible
remote guild replicas after the final local membership is gone. Admission
locks the singleton global ledger before the applicable origin ledger, so
concurrent origins can't each overshoot the instance-wide ceiling. Alert on
repeated quota responses: they can mean an undersized deployment, a stuck
retention worker, or an abusive peer. Blocking a peer stops new application
traffic; it doesn't replace normal retention or a reviewed database-capacity
plan.

Peer discovery metadata is bounded too. The defaults retain at most 10,000
remote instance records and 512 verification-key IDs per peer. Set
`KAEDE_FEDERATION_MAX_REMOTE_INSTANCES` and
`KAEDE_FEDERATION_PEER_KEY_HISTORY_LIMIT` to match your intended federation
reach. New, previously unknown peers are rejected once either applicable cap
is reached. Already-known peers keep working unless they try to add more key
IDs. The nightly retention job deletes retired keys after the signed-event
retention window, so ordinary key rotation eventually frees capacity. A peer
must allocate a new key ID when its key material changes.

Current peers also advertise replay-protected signed requests. Once that
capability is observed it's pinned for the peer; removing it from discovery
is treated as a security downgrade. Legacy peers stay on the version 1
signing form during rolling upgrades, while updated peers automatically use
one-time, signature-bound request nonces.

Federated state outside guild replicas is capped separately:

- Pending friend requests are limited per recipient, per origin, and per
  recipient/origin pair.
- Remote profiles and third-party identity namespaces stay charged to the
  peer that introduced them until physical garbage collection.
- Media deletion markers are retained only for the signed-event retention
  window and are capped per origin.
- Cross-instance DMs use trigger-maintained conversation/message/byte
  ledgers for the conversation authority and for each remote origin.

The per-remote-origin DM defaults are kept below the shared authority totals
so one peer can't consume the capacity reserved for every other peer
when this instance is the DM authority. Hard defaults allow one million
conversations, 50 million messages, and 320 GiB at an authority; a single
remote origin is limited to 100,000 conversations, 10 million messages, and
64 GiB. A single conversation has a five-million-message / 32 GiB hard
ceiling.

Non-authoritative DM replicas normally stay far below those safety backstops.
Each conversation keeps a rolling cache of up to the newest 250,000
remote-authored message copies or 2 GiB of replaceable remote-authored rows,
whichever fills first. Locally authored user data is durable and is never
evicted based on an acknowledgement from another instance. Pins, the actual
newest message, unfinished mention/push projections, and locally owned
attachment source rows are also protected.

Older remote-authored pages are fetched on demand from the signed
conversation authority and are not re-persisted. A temporary authority outage
leaves recent cached messages visible and shows an actionable Retry.
On-demand attachment requests are bound end-to-end to the exact signed
conversation, message, and attachment references; a cached object is still
re-authorized with its origin before it's served to a user. The rendered
same-origin HMAC paths expire after 15 minutes. An authenticated participant
can transparently renew an expired, authentic path, but the home still
repeats the exact origin authorization before serving either cached or newly
fetched bytes. Retry keeps working on an old rendered page without the path
becoming a public or cross-conversation media capability.

DM pins are structurally limited to one row per retained message. Reactions
are not accepted as federated DM child events. The authoritative client
mutation path caps a single DM message at 100 distinct reaction rows and
reports a clear limit error, so reaction churn can't bypass the message/byte
policy. Rolling eviction begins only after the authority advertises
`dm-history-page/1`; a rolling upgrade therefore can't strand history on an
older peer. The authority never prunes history to meet a replica cache
target.

Tune the replica cache targets with
`KAEDE_FEDERATION_DM_REPLICA_CACHE_MESSAGES_PER_CONVERSATION` and
`KAEDE_FEDERATION_DM_REPLICA_CACHE_BYTES_PER_CONVERSATION`; each must stay at
or below its corresponding hard per-conversation ceiling. Tune the
`KAEDE_FEDERATION_PENDING_RELATIONSHIPS_*`,
`KAEDE_FEDERATION_REMOTE_USERS_PER_INTRODUCER`,
`KAEDE_FEDERATION_THIRD_PARTY_INSTANCES_PER_INTRODUCER`,
`KAEDE_FEDERATION_REMOTE_MEDIA_TOMBSTONES_PER_ORIGIN`, and
`KAEDE_FEDERATION_DM_MAX_*` settings conservatively. Startup rejects fairness
limits that exceed their aggregate boundary.

When a pending relationship allowance is reached, the receiving server
returns the privacy-preserving terminal code
`KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED`. It doesn't disclose which
recipient/origin dimension filled. The sender clears only the exact
still-pending request identified by its correlation token and tells the
initiating user that the request was not delivered. Newer requests,
friendships, and blocks are untouched.

Remote identity and instance namespace limits report
`FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED` or
`FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED` to local API clients (HTTP 507),
and their `KAED_FED_*` counterparts to peers. Public responses omit current
and maximum counts. Affected DM opens and proxy writes fail visibly rather
than staying pending. A remote-guild replica enters `quota_paused` without
advancing its sequence, so synchronization can safely resume when capacity is
available.

Durable remote-guild replicas have a second, independent high-water mark. A
trigger-maintained ledger counts messages, reactions, memberships, attachment
metadata, message projections, history staging/provenance, and structural
guild rows. Membership charges include a conservative companion allowance for
the remote identity profile they materialize. Byte estimates include each
serialized SQL row plus heap and index allowances; downloaded media objects
are governed by `KAEDE_MEDIA_REMOTE_CACHE_BYTES` instead. A plain retained
message normally incurs at least one 4 KiB message row and one 2 KiB
projection row. Retained-history provenance adds at least another 1 KiB row,
and staging temporarily adds at least 4 KiB. Each reaction is at least 1 KiB,
each attachment metadata row at least 4 KiB, and each member at least 4 KiB.
Size PostgreSQL with these conservative charges, not average message text
size.

Defaults allow 20 million rows / 64 GiB per remote guild and 100 million rows
/ 320 GiB across all guilds from one origin. That gives a two-million-message
history import room for projections, provenance, reactions, members, and
structural rows instead of letting the import budget consume the entire
replica budget by itself. Configure these with
`KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_GUILD`,
`KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_GUILD`,
`KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_ORIGIN`, and
`KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_ORIGIN`. Origin limits must not be
below the corresponding guild limits.

Live replication and retained-history merges use the same atomic ledger. An
operation that would cross a high-water mark is rolled back before its guild
sequence advances, and the replica enters `quota_paused` with
`KAED_FED_REPLICA_QUOTA_EXCEEDED`. Raising the applicable limit permits
retry. Revocation, channel purge, history cleanup, and orphan-guild deletion
release their charges transactionally through the same ledger. Treat a paused
replica as a capacity-planning signal or a potentially abusive peer, and
review the origin before raising a limit substantially.

The daily retention cycle also removes at most 5,000 aged remote user
profiles and 5,000 unused remote instance namespaces per run by default. It
does so only after the identity has no durable foreign-key reference from any
guild, DM, message, reaction, attachment, relationship, moderation, or
history row. The collector derives those checks from the database model and
uses non-blocking row locks, so new reference types are preserved
automatically and concurrent activity can't race a destructive cascade. The
default grace period is 30 days; configure it with
`KAEDE_FEDERATION_REMOTE_IDENTITY_RETENTION_DAYS` (minimum 7 days). Bound the
work per cycle with `KAEDE_FEDERATION_REMOTE_IDENTITY_GC_BATCH_SIZE`.

Federated media has independent live-transfer and retained-cache budgets.
Each cache miss atomically reserves the configured maximum attachment size
across API replicas, then streams into a bounded spool file for local type
validation and ClamAV scanning. Defaults allow 256 MiB in flight per remote
origin and 512 MiB across the instance. Tune
`KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_PER_ORIGIN` and
`KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_TOTAL`, keeping the latter at
least as large and each at least one maximum attachment. These live-transfer
guards are kept much smaller than retained storage: raising them only
raises concurrent network, memory, and spool-file exposure.

`KAEDE_MEDIA_REMOTE_CACHE_BYTES` defaults to 100 GiB. It's a strict admission
ceiling serialized with the eviction worker, not an eventual target. The
cache is pruned in least-recently-accessed order to a 90% low-water mark, on
top of TTL and signed-deletion cleanup. A full cache schedules eviction and
returns a retryable error instead of allowing unbounded object-store growth.
This rolling remote cache is separate from the unchanged 10 GiB authoritative
per-user upload quota. The bundled alert fires if cache admission still
reaches the ceiling; that can mean a stalled eviction worker, undeletable
orphan objects, or a genuinely undersized target.

## Private message search

New setup runs offer typo-tolerant message search backed by the bundled
Meilisearch service. It binds only to the private Compose `data` network, and
its master key is generated into the mode-0600 operator `.env`. The key is
never sent to browsers, mobile clients, desktop clients, or federation peers.
Disable search with `KAEDE_SEARCH_ENABLED=false`. On an existing deployment,
rerun `make setup`, choose search, then apply the generated configuration and
migrations normally. The generated `COMPOSE_PROFILES=search` activates the
bundled service only when search is enabled, so opting out doesn't leave an
idle search container running.

The relevant settings are `KAEDE_SEARCH_ENABLED`, `KAEDE_SEARCH_URL`,
`KAEDE_SEARCH_MASTER_KEY`, `KAEDE_SEARCH_INDEX_PREFIX`,
`KAEDE_SEARCH_REQUEST_TIMEOUT_SECONDS`, `KAEDE_SEARCH_BATCH_SIZE`, and
`KAEDE_SEARCH_FEDERATION_TIMEOUT_SECONDS`. The setup defaults suit a normal
deployment. A larger batch catches up faster after installation or a rebuild
but increases database, worker, and indexing load. Never publish the
Meilisearch port or reuse its master key outside this deployment.

The SQL queue is durable and holds references plus retry state, never a
second copy of message content. Meilisearch is disposable. To repair or
replace it while chat stays online, run:

```sh
make search-rebuild            # refresh every authoritative SQL message
make search-rebuild RESET=1    # first discard and recreate the private index
```

The UI reports while indexing is catching up. A Meilisearch outage makes
search temporarily unavailable but doesn't block sending, receiving,
federation, or history. Monitor the worker task `search.index_sweep`,
`kaede_search_index_pending_messages`, and
`kaede_search_index_retrying_messages`. Repeated `SEARCH_BACKEND_UNAVAILABLE`
means the URL, key, service health, disk, or an index task failed. Restore
the service and rebuild; don't edit message rows to repair a derived index.

Only plaintext channels are indexed. Setting a channel encryption policy to
`e2ee` queues removal of all of its indexed documents, and API/SQL/federation
authorization independently prevents stale candidates from being returned
while that removal drains. Future encrypted-room support must not reuse this
plaintext index or upload search tokens/terms without a separate reviewed
protocol.

## Federated retained history

The `guild-history-sync/1` extension is advertised automatically. Export
stays disabled for every guild until an authorized guild administrator
enables the guild default or a channel override.
`KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED` can disable all inbound historical
imports on this instance. You can also bound grants and resource use with:

- `KAEDE_FEDERATION_HISTORY_EXPORT_TTL_MINUTES`
- `KAEDE_FEDERATION_HISTORY_PAGE_MESSAGES` and
  `KAEDE_FEDERATION_HISTORY_PAGE_BYTES`
- `KAEDE_FEDERATION_HISTORY_MAX_PAGES`
- `KAEDE_FEDERATION_HISTORY_MAX_BYTES`
- `KAEDE_FEDERATION_HISTORY_MAX_REACTIONS`
- `KAEDE_FEDERATION_HISTORY_MAX_DURATION_SECONDS`
- `KAEDE_FEDERATION_HISTORY_MERGE_CHUNK_SIZE`
- `KAEDE_FEDERATION_HISTORY_MAX_MESSAGES`

Current peers advertise `guild-history-sync/2-recent-first`; version 1
remains available for rolling upgrades. Defaults cap one import at two
million messages, ten million reactions, 250,000 pages, 32 GiB of validated
payloads, and two hours. A larger replica ceiling doesn't make an individual
import unbounded. Raise these import limits only after checking database and
worker capacity.

Authority-side grants are also bounded while active. Defaults permit 1,000
exports and 100,000 per-channel grant rows per requesting origin, with global
ceilings of 10,000 exports and 1,000,000 grant rows. Configure
`KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_PER_ORIGIN`,
`KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_TOTAL`,
`KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_PER_ORIGIN`, and
`KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_TOTAL`. Admission is
transaction-serialized across API workers. A full budget returns retryable
`KAED_FED_HISTORY_CAPACITY`. Expired grants stop counting immediately, and
the retention job removes their physical rows later.

Imported history is a local replica. Policy and permission loss trigger an
immediate best-effort purge when the authoritative update arrives, and a
five-minute reconciliation sweep repairs missed notifications. Monitor failed
`federation.history_sync` tasks: an offline peer can delay both imports and
purge instructions. No protocol can guarantee deletion by a malicious or
modified peer after data has been sent, so enable export only for peers whose
data-handling policy you accept.

## Observability boundary

The optional profile defines Prometheus, a provisioned Kaede overview
dashboard in Grafana, alert rules, and a Loki endpoint. It doesn't mount or
proxy the Docker socket: project-label filtering isn't an access-control
boundary, and a socket reader could inspect unrelated host containers.
Grafana binds only to `127.0.0.1:18084` by default and requires the
externally supplied administrator password. Set a unique
`GRAFANA_ADMIN_PASSWORD` of at least 20 characters; observability preflight
rejects a blank or documented placeholder before any profile service starts.
Prometheus, Loki, and Grafana have restart policies and readiness checks.

Metrics cover API health, connected gateway sessions, pending/failed
federation delivery, delivery failures, and task duration/run/failure totals.
Federation metrics also expose retained remote event rows/bytes, configured
inbox capacity, trigger-accounted replica rows/bytes, quota-paused guilds,
quota deferrals, the configured remote-media LRU ceiling, and admissions
rejected at that ceiling. The bundled alerts warn at 80% inbox utilization,
on any inbox rejection, when remote-media eviction can't make room, and when
a remote guild stays quota-paused.

Loki deliberately has no privileged host log collector. If you need
centralized logs, connect a separately reviewed collector.

## Explicit v1 operational boundaries

Kaede v1 has no online rotation command for `KAEDE_SECRET_KEY`. The
federation signing-key rotation commands don't rotate this
database-encryption key. Keep it unchanged and restore it with the database
until a dedicated envelope-key rotation migration exists.

The general Taskiq worker is one high-trust process for email, federation,
media, and voice work, so it receives the combined credentials those jobs
require. Protect and monitor it as part of the application trust boundary.
If you need stronger compartmentalization, split queues and settings before
treating those roles as isolated.

Repository image references use fixed version tags, not registry digests, and
the built-in audit checks cover language dependencies rather than base-image
OS packages. A production release process should mirror or digest-pin
approved images and run an image/SBOM vulnerability scanner. LiveKit health
and RTC/TURN reachability are separate from API database/Dragonfly readiness.
Monitor its HTTP and media-plane ports externally whenever the voice profile
is enabled.
