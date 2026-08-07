# Operator guide

This guide covers the supplied deployment and release controls. Operators remain
responsible for reviewing their infrastructure, threat model, capacity,
restoration process, and key-rotation policy.

## Hosts, certificates, and ports

Production assumes host nginx owns public TCP 80/443. A Garage deployment's
certificate must cover both `chat.example.com` and `media.chat.example.com`.
External-S3 deployments remove the unused media virtual host and need only the
main application name. The internal Caddy edge is deliberately bound only to `127.0.0.1:18081`;
the API diagnostic binding defaults to `127.0.0.1:18082`. If the edge port
changes during a manual setup, update both `.env` and the host-nginx upstream.

The LiveKit foundation uses host networking and is isolated behind the `voice`
Compose profile, so the default topology does not bind its host ports. The edge also
returns 404 for `/livekit` unless `KAEDE_VOICE_ENABLED=true`; the voice preflight
requires that exact opt-in. When voice is intentionally enabled with both the
setting and `--profile voice`, allow the selected RTC/TURN traffic through the
host and provider firewalls: TCP `LIVEKIT_RTC_TCP_PORT`, UDP
`LIVEKIT_RTC_UDP_PORT`, UDP `KAEDE_TURN_UDP_PORT`, and TCP
`LIVEKIT_TURN_TLS_PORT`. Keep `LIVEKIT_CONTROL_PORT` and the API/edge loopback
ports blocked from external interfaces. The TURN certificate paths must be
absolute host paths and the certificate name must match `KAEDE_DOMAIN`.

The defaults are control TCP 7880, RTC TCP 7881, RTC UDP 7882, TURN/TLS TCP
5349, and TURN UDP 13478. Every host-networked LiveKit process on the same host
must use a different five-port set. The setup wizard can find and reserve an
available set automatically, or accept a manually selected set. Automatic
selection prevents conflicts with listeners present while setup runs; it cannot
prevent another process from claiming a selected port before Compose starts.

## Secrets and initial configuration

The recommended path is the interactive setup wizard:

```sh
make setup
```

It securely generates compatible keys, preserves existing durable secrets,
configures Garage or an external S3 provider plus the email mode and
optional service choices, and can render a configuration for host-level nginx.
It writes configuration only and never starts the topology or reloads host
nginx. See [the deployment-wizard guide](deployment-wizard.md) for every option
and generated file. Wizard users must use the two-file Compose command in
`deploy/generated/README.txt`.

The email provider menu includes a disabled mode. In that mode registration
requires only a username and password, and no verification or delivery intent
is created. Email changes and self-service password recovery are also disabled;
document an operator-assisted recovery policy before selecting it.

For a manual setup instead, copy the template and replace every placeholder:

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
scanning, and unknown `KAEDE_` settings. Compose's `--env-file` normally affects
interpolation only, so Kaede also loads the selected file into its isolated
preflight process to catch misspellings that would otherwise fall back to a
default. For a file other than repository-root `.env`, use
`make env-check ENV_FILE=/absolute/path/to/kaede.env`; set both `--env-file` and
`KAEDE_OPERATOR_ENV_FILE` to that same absolute path for subsequent direct
Compose commands.

Generate independent values. These commands intentionally produce characters
that are safe in headers, YAML, and the PostgreSQL URL:

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

Use different values for every line. Put the PostgreSQL password into both
`POSTGRES_PASSWORD` and `KAEDE_DATABASE_URL`, and put the Dragonfly password
into both `DRAGONFLY_PASSWORD` and `KAEDE_DRAGONFLY_URL`. Set the public HTTPS
`KAEDE_APP_URL`, email sender/backend credentials, and an optional random
`KAEDE_ADMIN_TOKEN`. When enabling the `voice` profile, also set
`KAEDE_VOICE_ENABLED=true`, `KAEDE_VOICE_PUBLIC_URL=wss://<domain>/livekit`, plus
the LiveKit keys, five port settings, and certificate paths. The control port in
`KAEDE_VOICE_LIVEKIT_URL` must match `LIVEKIT_CONTROL_PORT`. Preserve
`KAEDE_SECRET_KEY`: the instance signing key, pending email-outbox messages, and
other protected application material stored in PostgreSQL cannot be decrypted
without it. Changing or losing this value can strand pending verification/reset
mail, so restore and rotate it only through a reviewed application workflow. Also
preserve the distinct `KAEDE_GATEWAY_SECRET_KEY`; it satisfies strict process
configuration without exposing the instance master key to the gateway.

Optional interaction services are disabled by default. Set
`KAEDE_KLIPY_ENABLED=true` with a private `KAEDE_KLIPY_API_KEY` to expose the GIF
picker. Set `KAEDE_TURNSTILE_ENABLED=true`, `KAEDE_TURNSTILE_SITE_KEY`, and the
private `TURNSTILE_SECRET` to require Turnstile during registration. The API key
and Turnstile secret belong only in backend environments; neither is included
in public configuration responses.

Set the same `KAEDE_EDGE_SECRET` in `.env` and the nginx
`X-Kaede-Edge-Secret` header. It must differ from `KAEDE_PROXY_SECRET`. The
selected internal edge receives only the domain and these two edge credentials; application processes
do not receive Garage administration, LiveKit, or host-edge secrets.

## Object storage

Kaede requires an S3-compatible object store, but Garage is only the default.
Choose exactly one deployment mode.

For self-hosted Garage, keep `KAEDE_MEDIA_STORAGE_BACKEND=garage` and start with
`deploy/compose.yml`. The provider-neutral `storage-init` service creates all
three private buckets idempotently. Single-node Garage has no replica
redundancy; back up both its metadata and data volumes independently.

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
- an access key and secret with read, write, delete, and bucket-HEAD access to
  all three configured buckets. Set `KAEDE_MEDIA_S3_SESSION_TOKEN` only for
  temporary credentials.

External providers must permit browser `PUT` requests from the exact
`https://<KAEDE_DOMAIN>` origin with the `Content-Type` request header. Do not
make any bucket public: all reads and writes use short-lived SigV4 URLs. The
application rejects redirects and production HTTP endpoints. Backblaze calls
its service B2 and requires an S3-compatible application key, endpoint, and
matching region; its API uses path-style endpoints of the form
`https://s3.<region>.backblazeb2.com/<bucket>`.
Provider references: [AWS S3 request addressing](https://docs.aws.amazon.com/AmazonS3/latest/userguide/VirtualHosting.html)
and [Backblaze's S3-compatible API](https://www.backblaze.com/apidocs/introduction-to-the-s3-compatible-api).

Kaede deletes the current object name. If provider versioning is enabled, older
versions may remain billable and recoverable even though Kaede can no longer
serve them. Disable versioning where supported or configure a reviewed lifecycle
rule that expires noncurrent/hidden versions within the operator's retention and
privacy requirements. Backblaze B2 buckets are versioned by default, so this
lifecycle step is required for bounded physical retention there.

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
verifies all three pre-created buckets before API and worker startup, and fails
closed on bad credentials, missing buckets, or an unreachable provider. Virtual
addressing puts the bucket before the endpoint host and therefore requires DNS
hosts and bucket names without dots. Keep the worker and ClamAV healthy:
originals deliberately remain unavailable when a scan cannot reach a clean
result.

Changing this setting does not migrate existing objects. A live Garage-to-S3 or
provider-to-provider move must copy all three buckets with object keys unchanged,
verify counts and hashes, stop writers for the final synchronization, and only
then change the backend configuration.

Browser `PUT` URLs can write only staging keys. The worker copies the exact
bytes that passed type validation and malware scanning to a server-only,
content-addressed clean key and switches the database reference atomically. It
retains the staging-key reference until the presign lifetime has elapsed, so a
client that rewrites staging after an early deletion still cannot affect served
bytes and the cleanup sweep will delete the rewrite.

The nginx example gives only the media virtual host a 101 MiB body allowance,
bounded concurrent connections, request rate, and five-minute proxy timeouts;
the main API/federation host remains limited to 2 MiB. The media path streams
request bodies instead of buffering them on host disk. If
`KAEDE_MEDIA_MAX_ATTACHMENT_BYTES` is raised, update and review the media-server
`client_max_body_size` as well; the application ticket and signed PUT length
remain the authoritative per-object checks.

## Validate and start

The checks below may pull/build images but publish no project ports. `make check`,
`make test`, and each acceptance target use distinct disposable Compose project
names and clean their own volumes.

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

Passing these checks does not replace a deployment-specific security, capacity,
backup, and recovery review.
To start the production topology intentionally:

```sh
docker compose --env-file .env -f deploy/compose.yml config --quiet
docker compose --env-file .env -f deploy/compose.yml up -d --build \
  --wait --wait-timeout 180
docker compose --env-file .env -f deploy/compose.yml ps
curl --fail http://127.0.0.1:18082/health/ready
```

The one-shot preflight runs before data services, then `migrate` applies every
pending Alembic revision in dependency order through `head` and idempotently
bootstraps the instance identity. API, gateway, worker, and Caddy start only
after that one-shot service succeeds. This creates a fresh schema on an empty
database and upgrades an existing schema on later starts. A stored domain
mismatch, migration failure, or invalid secret fails closed.
The API health check uses readiness rather than liveness and includes a 75-second
start period for the snowflake worker-ID quarantine. Caddy waits for that check,
and `--wait` keeps the startup command pending until the dependency is ready.

Install the nginx example in its `http` context, replace every example value,
and keep the resulting secret-bearing file root-owned and non-world-readable
(for example, mode `0600`). Then use the distribution's normal validation and
reload commands (commonly `nginx -t` followed by `systemctl reload nginx`). Do
not reload a configuration that failed validation.

The example defines HTTP-context zones for the exact `/gateway`, `/livekit`
signaling, and federation-link upgrade routes and for the separate media virtual
host. The `/livekit` location must forward both its ordinary validation requests
and the `/livekit/rtc` WebSocket upgrade; routing it through the ordinary
catch-all strips the signaling upgrade. Gateway admission
allows at most 20 incomplete/active upgrades per source address, with 10 new
upgrades per second and a burst of 20. Operators serving unusually large groups
behind one NAT may raise those values after measuring reconnect bursts, but
should retain both the connection and request limits. Kaede also keeps an
independent process-local cap, so bypassing host nginx through a trusted
loopback path cannot create an unbounded pre-authentication queue.

## Development CA

`make dev` is the only target that intentionally publishes the Alpha/Beta edge
ports. It uses Caddy's local CA at `https://alpha.localhost:18443` and
`https://beta.localhost:18443`. If the browser does not trust it, export the CA
after the stack starts and import it into the development machine/browser trust
store:

```sh
docker compose -f deploy/compose.dev.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt ./kaede-dev-root.crt
```

Never install this development CA on production systems. Run `make dev-down`
when finished; add `-v` manually only when intentionally deleting development
databases and the CA.

`make federation-tls-check` uses a separate one-hour ephemeral CA inside its
disposable Compose project and publishes no host ports. The validation-only
`KAEDE_FEDERATION_CA_FILE` setting lets Alpha/Beta trust that CA; settings reject
both custom federation trust roots and peer URL overrides in production.

## Backup and restore boundary

A recoverable backup set contains all of the following from one quiesced writer
boundary:

- a PostgreSQL custom-format dump;
- Garage data and metadata volumes, or a provider-native protected copy of all
  three external S3 buckets;
- `.env` or an equivalent secret-manager export, especially
  `KAEDE_SECRET_KEY`, `KAEDE_GATEWAY_SECRET_KEY`, object-store keys, and LiveKit
  keys;
- the deployed revision, migration head, internal-edge/host-nginx configuration, and TLS
  certificate automation state.

First place host nginx in maintenance mode and stop every application writer.
If the voice profile is active, stop LiveKit as well. For bundled Garage, stop
it only after the application writers have stopped; leave PostgreSQL running so
it can produce a consistent dump:

```sh
docker compose --env-file .env -f deploy/compose.yml \
  stop caddy api gateway worker scheduler livekit
# Garage deployments only, after the writer stop completes:
docker compose --env-file .env -f deploy/compose.yml stop garage
```

Create a PostgreSQL dump with an operator-controlled destination and verify that
`pg_restore --list` can read it while writers remain stopped:

```sh
install -d -m 0700 /var/backups/kaede
docker compose --env-file .env -f deploy/compose.yml exec -T postgres \
  pg_dump -U kaede -d kaede -Fc > /var/backups/kaede/kaede-postgres.dump
pg_restore --list /var/backups/kaede/kaede-postgres.dump >/dev/null
```

Before restarting writers, take a protected copy or provider snapshot of all
three object buckets. A Garage backup must include both its metadata and data
volumes; copying only one is unusable. For external S3, record the provider
snapshot/version boundary and verify object counts. Encrypt the database dump,
object copy, and secret-manager export at rest, and test restoration of the
combined set rather than testing each component independently.

After every component has been copied and verified, start Garage first when it
is in use, then start the application topology and remove maintenance mode only
after readiness succeeds. Do not restart writers between the database dump and
the object-copy boundary.

Dragonfly stores leases, sessions, presence, cache, and task transport; its
snapshot improves continuity but is not a substitute for PostgreSQL/object-store
backups. Restore into a separate Compose project, database, volumes, and object
bucket namespace first. Do not point a drill at production buckets: cleanup
jobs are allowed to delete objects. Keep restored SMTP, federation egress,
webhooks, and public DNS/firewall exposure disabled, and use different loopback
ports from the live instance. The restored database must use the original
domain and `KAEDE_SECRET_KEY` to verify protected identity material, which means
the clone must never be network-visible at the same time as the authoritative
instance. Run `alembic upgrade head`, bootstrap, and validate
identity/chat/federation behavior before any cutover. A backup that has not
passed an isolated restore drill is not considered usable.

Keep both the worker and scheduler services running for identity email delivery.
API requests commit encrypted delivery intents even if Dragonfly cannot accept
the immediate wake; the scheduler's minute sweep provides the recovery path.
Operational logs include only opaque outbox IDs and retry counters, not recipient
addresses, message bodies, provider errors, or one-time links. The console backend
is the explicit development exception and must never be selected in production.

## Upgrade and rollback

Before upgrading, take and verify backups, record `alembic current`, review every
new migration downgrade, and render the new Compose configuration. Never let an
old application writer overlap a new schema migration. Build the new images
first, place host nginx in maintenance mode, stop `caddy`, `api`, `gateway`,
`worker`, and `scheduler`, and then run the new `migrate` image exactly once:

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
running the migration. This ensures no browser credential issued by the old
version can still rewrite an already-clean legacy key. Keep maintenance mode in
place until readiness, smoke checks, migration head, and instance discovery have
all been verified. The startup `migrate` gate may execute again during `up`; its
upgrade and bootstrap operations are idempotent.

API workers deliberately quarantine newly available snowflake worker IDs for 60
seconds before becoming ready. Allow at least this startup window in external
health checks and deployment orchestration; bypassing it can reintroduce ID
collisions after a Dragonfly state loss.

Application rollback is safe only while the old code understands the current
schema. If it does not, stop writers and restore the pre-upgrade backup or run a
specifically reviewed Alembic downgrade using the matching code. Never guess at
a downgrade target and never restore PostgreSQL without the matching
`KAEDE_SECRET_KEY`. The origin-scoped event migration deliberately refuses to
downgrade if the same federation event ID exists under multiple origin domains,
or the same guild event ID exists under multiple guild domains. The legacy schema
cannot represent either valid state without destroying authenticated history.
Retain the newer revision or archive an origin through a separately reviewed
procedure; do not delete rows merely to force a downgrade.

The encrypted-email-outbox migration invalidates legacy pending email-change
confirmations because older rows held the target address as plaintext JSON. Users
with an in-flight change must request a fresh confirmation after this upgrade;
verification and password-reset credentials are unaffected.

Rotate the instance federation signing key inside a running API container:

```sh
docker compose --env-file .env -f deploy/compose.yml exec -T api kaede rotate-key
```

The command takes a database-wide identity lock, verifies the stored keypair with
the current `KAEDE_SECRET_KEY`, and atomically installs a uniquely named Ed25519
key. The former key remains in `old_verify_keys`; retain it for at least the
configured federation event-retention window before a future reviewed retirement
workflow.

After the command's reported overlap deadline, retire that exact historical key
with `kaede retire-key <key-id>`. The command refuses the current key and refuses
early retirement. `--force-compromised` bypasses the overlap only for an active
key compromise and can make queued historical envelopes unverifiable; record the
incident and notify federation peers before using it.

## Federation allowlists and blocklists

Set `KAEDE_FEDERATION_MODE=allowlist` to require explicit peer approval. The
admin API is authenticated with `KAEDE_ADMIN_TOKEN`; provide it through a
protected header and never place it in a URL.
`GET /api/v1/admin/federation/blocks/export` produces Mastodon-compatible CSV,
while `POST /api/v1/admin/federation/blocks/import` accepts a bounded CSV body. A
`silence` block holds non-security traffic and a
`suspend` block holds all peer traffic; security reconciliation events remain
durable. Export before bulk changes, review subdomain inclusion, and retain the
export with the deployment revision. Removing a block schedules authoritative
replica reconciliation rather than blindly releasing stale writes.

## Federated retained history

The `guild-history-sync/1` extension is advertised automatically. Export remains
disabled for every guild until an authorized guild administrator enables the
guild default or a channel override. `KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED`
can disable all inbound historical imports on this instance. Operators may also
bound grants and resource use with
`KAEDE_FEDERATION_HISTORY_EXPORT_TTL_MINUTES`,
`KAEDE_FEDERATION_HISTORY_PAGE_MESSAGES`, and
`KAEDE_FEDERATION_HISTORY_PAGE_BYTES`, `KAEDE_FEDERATION_HISTORY_MAX_PAGES`,
`KAEDE_FEDERATION_HISTORY_MAX_BYTES`, `KAEDE_FEDERATION_HISTORY_MAX_REACTIONS`,
`KAEDE_FEDERATION_HISTORY_MAX_DURATION_SECONDS`,
`KAEDE_FEDERATION_HISTORY_MERGE_CHUNK_SIZE`, and
`KAEDE_FEDERATION_HISTORY_MAX_MESSAGES`. Current peers advertise
`guild-history-sync/2-recent-first`; version 1 remains available for rolling
upgrades.

Imported history is a local replica. Policy and permission loss trigger an
immediate best-effort purge when the authoritative update arrives, and a
five-minute reconciliation sweep repairs missed notifications. Monitor failed
`federation.history_sync` tasks: an offline peer can delay both imports and purge
instructions. No protocol can guarantee deletion by a malicious or modified
peer after data has been sent, so operators should enable export only for peers
whose data-handling policy they accept.

## Observability boundary

The optional profile defines Prometheus, a provisioned Kaede overview dashboard
in Grafana, alert rules, and a Loki endpoint. It does not mount or proxy the
Docker socket: project-label filtering is not an access-control boundary, and a
socket reader could inspect unrelated host containers. Grafana binds only to
`127.0.0.1:18084` by default and requires the externally supplied administrator
password. Set a unique `GRAFANA_ADMIN_PASSWORD` of at least 20 characters;
observability preflight rejects a blank or documented placeholder before any
profile service starts. Prometheus, Loki, and Grafana have restart policies and
readiness checks. Metrics cover API health, connected gateway sessions, pending/failed
federation delivery, delivery failures, and task duration/run/failure totals.
Loki intentionally has no privileged host log collector; connect a separately
reviewed collector if centralized logs are required.

## Explicit v1 operational boundaries

Kaede v1 has no online rotation command for `KAEDE_SECRET_KEY`. The federation
signing-key rotation commands do not rotate this database-encryption key; keep
it unchanged and restore it with the database until a dedicated envelope-key
rotation migration exists. The general Taskiq worker is also one high-trust
process for email, federation, media, and voice work and therefore receives the
combined credentials those jobs require. Protect and monitor it as part of the
application trust boundary; deployments needing stronger compartmentalization
must split queues and settings before treating those roles as isolated.

Repository image references use fixed version tags, not registry digests, and
the built-in audit checks cover language dependencies rather than base-image OS
packages. A production release process should mirror or digest-pin approved
images and run an image/SBOM vulnerability scanner. LiveKit health and RTC/TURN
reachability remain separate from API database/Dragonfly readiness; monitor its
HTTP and media-plane ports externally whenever the voice profile is enabled.
