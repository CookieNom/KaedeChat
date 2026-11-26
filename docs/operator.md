# Operator guide

Use this guide to configure, monitor, back up, and upgrade a Kaede instance.
For a new deployment, start with the [README setup steps](../README.md#setup).
The [setup wizard reference](deployment-wizard.md) explains each wizard choice
and generated file.

[Configuration](#secrets-and-initial-configuration) · [Storage](#object-storage) ·
[Backups](#backup-and-restore-boundary) · [Upgrades](#upgrade-and-rollback) ·
[Federation](#federation-allowlists-and-blocklists) · [Monitoring](#observability-boundary)

## Hosts, certificates, and ports

Host nginx owns public TCP 80/443. With bundled Garage, the certificate must
cover both `chat.example.com` and `media.chat.example.com`; external S3 needs
only the main name. If you change the edge port by hand, update both `.env`
and the nginx upstream.

LiveKit uses host networking and runs only when `KAEDE_VOICE_ENABLED=true`
(otherwise the edge returns 404 for `/livekit`). TURN certificate paths must
be absolute host paths, and the certificate name must match `KAEDE_DOMAIN`.

| Setting | Default | Exposure |
| --- | --- | --- |
| Internal Caddy edge | `127.0.0.1:18081` | Loopback only |
| API diagnostic binding | `127.0.0.1:18082` | Loopback only |
| `LIVEKIT_CONTROL_PORT` | TCP 7880 | Block externally |
| `LIVEKIT_RTC_TCP_PORT` | TCP 7881 | Public, when voice is on |
| `LIVEKIT_RTC_UDP_PORT` | UDP 7882 | Public, when voice is on |
| `LIVEKIT_TURN_TLS_PORT` | TCP 5349 | Public, when voice is on |
| `KAEDE_TURN_UDP_PORT` | UDP 13478 | Public, when voice is on |

Open the public voice ports in both host and provider firewalls. Each
LiveKit process on a host needs its own five ports; see
[voice ports](deployment-wizard.md#voice-ports).

## Secrets and initial configuration

The recommended path is the interactive setup wizard:

```sh
make setup
```

See the [setup wizard reference](deployment-wizard.md), then follow
`deploy/generated/README.txt`.

For a manual setup, copy the template, keep it private, never commit it, and
replace every placeholder:

```sh
cp .env.example .env
chmod 600 .env
```

You'll also need `.kaede-kubernetes.json` with `kubeconfig`, `context`,
`namespace`, `cluster_uid` (the kube-system namespace UID), and
`image_repository`.

A hand-maintained `.env` from an older setup may still carry the old defaults
`KAEDE_FEDERATION_HISTORY_MAX_MESSAGES=250000` and a 20 GiB
`KAEDE_MEDIA_REMOTE_CACHE_BYTES`. Current defaults are 2,000,000 and 100 GiB.
Remove the old lines to inherit them, or set values that fit your PostgreSQL
and object-storage capacity.

## Public landing and operator policies

`KAEDE_LANDING_PAGE=default` keeps the project homepage. Use
`KAEDE_LANDING_PAGE=custom` only after reviewing the policy copy for your
jurisdiction and setting all six public build-time fields:

| Variable | Value |
| --- | --- |
| `KAEDE_LEGAL_INSTANCE_NAME` | The instance name users recognize |
| `KAEDE_LEGAL_OPERATOR_NAME` | The operator's real legal identity |
| `KAEDE_LEGAL_CONTACT_EMAIL` | A monitored address for legal, privacy, and account-access requests |
| `KAEDE_LEGAL_EFFECTIVE_DATE` | A real `YYYY-MM-DD` policy date |
| `KAEDE_LEGAL_MINIMUM_AGE` | An integer from 1 through 120 |
| `KAEDE_LEGAL_JURISDICTION` | The governing jurisdiction chosen by the operator and its counsel |

These values are public, not secrets. The build fails if any is missing or
malformed, including a partly filled policy under the default landing.
Changing the landing or any legal field needs `make deploy` to rebuild the
frontend. When the policy text changes, including through a Kaede release,
update `KAEDE_LEGAL_EFFECTIVE_DATE` and redeploy.

With the fields set, signup requires agreement to the terms and privacy
policy. Without them, there's no agreement step, and `/terms` and `/privacy`
say no policy is published. Deploy the frontend and API together. The 0.1.104
mobile apps show the policies in their verification window; if they report
that the policies couldn't load, check that `/terms.html` and `/privacy.html`
serve the full, dated text.

Check the policy against how deletion works before you publish it. Users can
delete content or their account in Settings after re-entering their password
and any MFA code; guild and developer-team owners must transfer or delete
those first. The identity row keeps the email and username reserved,
child-safety records stay until their retention ends, and removal on other
instances is best effort. The supplied policy sends privacy requests to
`KAEDE_LEGAL_CONTACT_EMAIL` and doesn't promise that contacting the operator
deletes stored or federated data. Kaede has no account export or migration.

### Support form

The custom homepage links to `/support`, which emails requests to
`KAEDE_LEGAL_CONTACT_EMAIL`. Set that address in the API runtime too, and
configure the email worker: `KAEDE_EMAIL_BACKEND=mailtrap_api` with
`KAEDE_MAILTRAP_API_TOKEN`, or `smtp` with `KAEDE_SMTP_URL`, plus
`KAEDE_EMAIL_FROM_ADDRESS`. The worker and scheduler must be running.

Requests are encrypted in the outbox, retried for up to seven days, and
cleaned up seven days after that. The form is off when the recipient is unset
or email is disabled. It uses Turnstile when configured and allows three
requests per hour per IP and 100 per hour per instance. Visitor addresses
aren't verified.

## Manual environment configuration

Before starting any service, validate the file:

```sh
make env-check
```

It rejects duplicate assignments, group- or world-readable files, placeholder
credentials, disabled malware scanning, and invalid settings. Preflight during
`make deploy` also rejects unknown `KAEDE_` settings. For another file, pass
`ENV_FILE=/absolute/path/to/kaede.env` to both validation and deployment.

Generate a different value for every line:

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
openssl rand -hex 32                          # KAEDE_SEARCH_MASTER_KEY (when search is enabled)
```

Put the PostgreSQL password in both `POSTGRES_PASSWORD` and
`KAEDE_DATABASE_URL`, and the Dragonfly password in both `DRAGONFLY_PASSWORD`
and `KAEDE_DRAGONFLY_URL`. Set the public HTTPS `KAEDE_APP_URL` and the email
credentials. For voice, set `KAEDE_VOICE_ENABLED=true`,
`KAEDE_VOICE_PUBLIC_URL=wss://<domain>/livekit`, the LiveKit keys, the five
ports, and the certificate paths; the port in `KAEDE_VOICE_LIVEKIT_URL` must
match `LIVEKIT_CONTROL_PORT`.

Never lose or change `KAEDE_SECRET_KEY`. It decrypts the instance signing key,
pending outbox mail, and other protected data in PostgreSQL, and v1 has no
rotation for it (see [v1 boundaries](#explicit-v1-operational-boundaries)).
Keep the separate `KAEDE_GATEWAY_SECRET_KEY` too, which lets the gateway run
without the master key.

## Optional services

### GIF search and Turnstile

`KAEDE_KLIPY_ENABLED=true` with a private `KAEDE_KLIPY_API_KEY` enables the
GIF picker. `KAEDE_TURNSTILE_ENABLED=true`, `KAEDE_TURNSTILE_SITE_KEY`, and
the private `TURNSTILE_SECRET` require Turnstile at registration and after a
failed sign-in. Keep the API key and secret in backend environments only.

### Stickers

Guild stickers go through the normal scanned media pipeline, bounded by
`KAEDE_MEDIA_STICKER_LIMIT` (per guild) and `KAEDE_MEDIA_MAX_STICKER_BYTES`
(per file). Cropping is always available and keeps GIF animation.

Background removal loads an ONNX model and needs much more worker memory. To
enable it, preseed the model into the worker's `rembg-models` volume (mounted
at `/models/rembg`), size worker memory for it, then set
`KAEDE_MEDIA_STICKER_BACKGROUND_REMOVAL_ENABLED=true`. Production workers must
not download models at runtime.

### Mobile push

Closed-app notifications go through the public Kaede relay by default
(`KAEDE_PUSH_RELAY_ENABLED=true`), with no Firebase credential needed. Set it
to `false` to stop sending wakes. The relay URL and logical origin are pinned
separately because `push.kaede.chat` serves transport for the `kaede.chat`
signing authority. Your home keeps only opaque subscriptions and wake
secrets.

Only the relay operator sets `KAEDE_PUSH_RELAY_SERVICE_ENABLED=true`,
`KAEDE_PUSH_RELAY_FCM_SERVICE_ACCOUNT_B64`, and, for iOS calling,
`KAEDE_PUSH_RELAY_APNS_KEY_B64`, `KAEDE_PUSH_RELAY_APNS_KEY_ID`,
`KAEDE_PUSH_RELAY_APNS_TEAM_ID`, and `KAEDE_PUSH_RELAY_APNS_TOPIC`. These must
reach relay workers only, never API processes, clients, logs, or other homes.

`KAEDE_PUSH_ENABLED` enables direct FCM for a separately signed custom app; it
doesn't notify the official app. See [mobile push delivery](mobile-push.md)
for the data flow, privacy, E2EE behavior, and custom builds.

### Verified mobile links

For a signed custom build, set `KAEDE_MOBILE_ANDROID_SHA256_CERT_FINGERPRINTS`
(comma-separated signing fingerprints) and `KAEDE_MOBILE_IOS_APP_IDS`
(comma-separated `TEAM_ID.bundle_id`). Kaede then serves Android Asset Links
and the Apple App Site Association file under `/.well-known`.

## End-to-end encryption activation

Encrypted-room activation is on by default and fails closed when a room,
client, or participating home can't support it. Before a public launch, work
through the release gates in [the E2EE guide](e2ee.md), or set
`KAEDE_E2EE_ACTIVATION_ENABLED=false` on every participating home. Mixed
settings reject new proposals but never downgrade an active room. Turning it
off later only hides new activation; rekey, recovery, disclosure reports, and
active rooms keep working.

## Reverse-proxy credentials

Set the same `KAEDE_EDGE_SECRET` in `.env` and in the nginx
`X-Kaede-Edge-Secret` header. It must differ from `KAEDE_PROXY_SECRET`.

Each role gets its own Secret rather than the whole `.env`:

- The internal edge gets only the domain and the two edge credentials.
- API and worker processes get LiveKit credentials, but no Garage admin or
  host-edge secrets.
- The preflight Job gets the complete configuration.
- The `storage-init` Job gets only the object-store settings and credentials.

## Object storage

Kaede needs one S3-compatible object store with three private buckets.

### Bundled Garage

Keep `KAEDE_MEDIA_STORAGE_BACKEND=garage`; `storage-init` creates the buckets.
Single-node Garage has no redundancy, so back up its metadata and data
volumes.

### External S3

This covers AWS S3, Backblaze B2, and other compatible services, including
local ones. Choose it in `make setup` (no Garage workload or PVC is created),
or start from `.env.s3.example`, pre-create the buckets, and set:

- `KAEDE_MEDIA_STORAGE_BACKEND=s3`;
- the provider's HTTPS API origin in both `KAEDE_MEDIA_S3_ENDPOINT` and
  `KAEDE_MEDIA_PUBLIC_BASE_URL`;
- the exact SigV4 region in `KAEDE_MEDIA_S3_REGION` (`us-west-004` for a B2
  endpoint such as `s3.us-west-004.backblazeb2.com`);
- `KAEDE_MEDIA_S3_ADDRESSING_STYLE=path` unless the provider requires
  virtual-hosted requests;
- `KAEDE_MEDIA_S3_CREATE_BUCKETS=false`; and
- a key with read, write, delete, and bucket-HEAD access to all three
  buckets, plus the version permissions under
  [removal](#removal-and-client-caches) if you use PhotoDNA. Set
  `KAEDE_MEDIA_S3_SESSION_TOKEN` only for temporary credentials.

Configure CORS in the provider's bucket settings:

- `GET` and `HEAD` from the exact Kaede HTTPS origin on all three buckets.
- On the attachments bucket, also `PUT` from any HTTPS origin with the
  `Content-Type` header (or `*`) allowed. Remote guild members upload report
  evidence straight to the moderation authority; the presigned URL authorizes
  it.

Never make a bucket public. Kaede rejects redirects and, in production, HTTP
endpoints. The endpoint must be reachable from pods, not just the host
(`host.k3d.internal` works from k3d, but browsers need a public URL).
Virtual-hosted addressing needs bucket and host names without dots. B2 needs
an S3-compatible application key and uses path-style URLs like
`https://s3.<region>.backblazeb2.com/<bucket>`. See
[AWS S3 request addressing](https://docs.aws.amazon.com/AmazonS3/latest/userguide/VirtualHosting.html)
and [Backblaze's S3-compatible API](https://www.backblaze.com/apidocs/introduction-to-the-s3-compatible-api).

`storage-init` checks all three buckets before the API and workers start and
fails on bad credentials, missing buckets, or an unreachable provider.

Kaede deletes only the current object version. With versioning on, old
versions stay billable and recoverable. Disable versioning, or add a lifecycle
rule that expires noncurrent and hidden versions. B2 versions by default, so
it needs that rule.

### Changing storage backends

Changing the backend doesn't move objects. Copy all three buckets with keys
unchanged, verify counts and hashes, stop writers for the final sync, then
switch the configuration.

### Upload staging and size limits

Browser `PUT` URLs write only to staging keys. After type validation and
malware scanning, the worker copies the bytes to a server-only clean key.
Keep the worker and ClamAV healthy, because unscanned originals stay
unavailable. Staging references are kept until more than 16 minutes after
the presigned `PUT` expires. Third-party upload clients must keep requests
under 15 minutes, or the store must expire the staging prefix equivalently.

The nginx example lets only the media host accept bodies up to the maximum
attachment size plus 1 MiB (16 MiB by default), with connection, rate, and
five-minute timeout limits; the main host stays at 2 MiB. If you raise
`KAEDE_MEDIA_MAX_ATTACHMENT_BYTES`, raise the media server's
`client_max_body_size` too.

## Upload quotas

| Variable | Default | Limits |
| --- | --- | --- |
| `KAEDE_MEDIA_USER_QUOTA_BYTES` | 10 GiB | Total stored bytes per user; `0` is unlimited |
| `KAEDE_MEDIA_DAILY_UPLOAD_QUOTA_BYTES` | `0` (unlimited) | Upload bytes per uploader per UTC calendar day |

The daily allowance covers all media uploads on this server, including
encrypted attachments and bot uploads, and counts from when you enable it.
Each upload ticket reserves its declared size, and cancelled, expired, or
deleted uploads aren't refunded. Deleting files does free stored-byte quota.
Per-file and concurrent-upload limits always apply.

## Microsoft PhotoDNA image matching

PhotoDNA is optional because Microsoft licenses its Edge Hash generator
confidentially. Never add the SDK, its native libraries, wrappers, builds, or
generated hashes to this repository, an image, an artifact, or a log.

### Installing the SDK

Extract the SDK on each host into an operator-owned directory whose root
contains `clientlibrary/python` and the native library. Grant access only to
the operator's primary group (directories `0750`, files `0640`), on a local
filesystem or one with working POSIX `flock`. Then set:

```env
KAEDE_PHOTODNA_ENABLED=true
KAEDE_PHOTODNA_SUBSCRIPTION_KEY=<Microsoft subscription key>
KAEDE_SAFETY_EVIDENCE_KEY=<separate 64-character hexadecimal encryption key>
PHOTODNA_EDGEHASHGENERATOR=/absolute/host/path/to/photodna-sdk
```

The directory is mounted read-only at `/opt/photodna` in preflight, API, and
worker pods, which run as UID `10001` with supplemental group
`OPERATOR_ENV_GID` (default `1000`). Preflight rejects an incomplete or
incompatible SDK. Allow outbound TCP 443 to `api.microsoftmoderator.com`; the
matcher URL is fixed to
`https://api.microsoftmoderator.com/photodna/v1.0/MatchHash`.

`setup.sh` generates `KAEDE_SAFETY_EVIDENCE_KEY`. On an existing install, use
`openssl rand -hex 32`. Back it up for as long as any evidence exists; losing
it makes evidence unreadable, and a missing key leaves matching content
unavailable.

Deploy the API, worker, media proxy, and client updates together after
migrations.

### What gets checked

Plaintext local uploads and plaintext images fetched from federated homes are
validated, scanned with ClamAV, and hashed in an isolated process. Only the
hash goes to Microsoft. Ordinary E2EE attachments can't be checked, because
the server has only ciphertext. A decrypted copy that a recipient discloses in
a report is checked like an upload.

- Images over 256 frames or 25 million total decoded pixels are rejected
  without a provider call. This is a policy rejection, not a match, and isn't
  retried.
- Images under 50 pixels in either dimension are rejected. Small eligible
  images are upscaled to 160 pixels for hashing only.
- Provider statuses `3206` and `3208` mark the file `rejected` and delete it.
- Other generator or provider errors fail closed and retry.

Only one native decode runs at a time across the API and worker, enforced by
`flock` on the SDK directory.

### Matches and evidence

A match blocks delivery. The exact bytes are encrypted into the attachments
bucket under `evidence/`, and a case opens under **Administration → Child
safety** with the provider tracking ID, match flags, account details, and
message references. Perceptual hashes aren't kept. Ordinary copies are
removed only after evidence is preserved. Staff confirm removal or dismiss
the case, which releases the content. Reporter-supplied evidence is marked
unverified and doesn't prove the alleged sender uploaded those bytes. A match on it doesn't hide the original until staff confirm.

If object storage is down, the evidence is held encrypted in the database
until the worker can copy and verify it. Back up the database, the evidence
bucket, and its key.

Keep `evidence/` out of media lifecycle expiry rules. Only the authenticated
staff endpoint serves it. It shares the bucket with ordinary media, so anyone
with the object-store credential can reach the ciphertext.

### Removal and client caches

Confirmed cases block new links and retry deletion of originals, thumbnails,
cached copies, stored versions, and delete markers. The queue shows
**Removal pending** until storage checks pass, after upload permissions and
the completion grace period expire. The media proxy rechecks safety state on
each read, but direct S3 download links may work until deletion succeeds or
they expire; use an access-gated proxy if you need instant revocation.

External S3 keys need `s3:ListBucketVersions`, `s3:DeleteObjectVersion`,
`s3:DeleteObject`, and object HEAD/read on the media buckets (on Backblaze,
list, read, and delete file capabilities). Missing permissions, retention
locks, or storage failures leave removal pending, retried every minute. Don't
put retention locks on ordinary media. Evidence can't be disposed of until
ordinary removal is verified.

Removal covers this instance's storage only, not remote origins, CDNs, device
exports, or backups. Configure any CDN not to cache private media. Updated
clients purge their caches after reconnecting; offline, older, or modified
clients and exported files can't be reached.

## Kubernetes prerequisites

[README step 3](../README.md#3-install-k3s) installs single-node K3s, which
suits a small instance. Never make the kubeconfig world-readable. Keep TCP
6443 private, and don't expose Flannel UDP 8472 or the NodePort range. A
provider firewall is enough; UFW isn't required.

### Local production image imports

By default Docker builds images and a root-owned helper loads them into K3s's
containerd. Install it as in
[README step 4](../README.md#4-download-kaede-and-enable-local-image-imports).
It takes no arguments, reads an image archive from stdin into the `k8s.io`
namespace, and is the only sudo operation deployments and updates use. Review
changes before reinstalling it. For several nodes or a remote cluster, use a
registry and, if needed, an image-pull Secret.

## Validate and start

Deploy commands are in [README steps 6 and 7](../README.md#6-configure-your-instance).

`.kaede-kubernetes.json` pins the kubeconfig, context, namespace, cluster UID,
storage, and image delivery, and every production action checks the cluster
identity. `KUBE_CONFIG=/path/to/config.json` selects another saved deployment
and `ENV_FILE=/path/to/operator.env` its environment. Never change the
namespace or storage class to work around a failed rollout; that selects
different data.

A deployment runs preflight, then the migration Job (which creates and
bootstraps the schema on first run) before any new writer starts, then
`storage-init`, then readiness-gated API, gateway, frontend, and edge. A
preflight or storage-init failure stops before any new application starts.
Application rollouts leave the loopback bridges and stateful services
running. The topology targets a single node with local-path volumes; watch
free disk yourself.

## Backup and restore boundary

Back up these together as one recovery set:

- PostgreSQL
- the private object buckets
- Dragonfly snapshots
- `.env`, including `KAEDE_SECRET_KEY`, the domain, and storage keys
- `.kaede-kubernetes.json`

PVCs and single-node Garage are not backups. Keep copies off the host, test
restores, and give external object-store versioning its own policy. A
dedicated backup pod needs ingress for its selector on the data service's
port; `kubectl exec` backups can use the pod's local socket.

For a consistent maintenance backup:

1. Put host nginx in maintenance mode.
2. Scale the API, gateway, worker, and scheduler to zero, and wait for their
   pods to terminate.
3. Dump PostgreSQL.
4. Snapshot Dragonfly before stopping it.
5. Stop Garage, then snapshot its metadata and data together. A live copy of
   its metadata isn't consistent.
6. Capture provider-hosted S3 with the provider's tools.

Name the kubeconfig, context, and namespace on every kubectl command, and
never delete PVCs to stop services. Run restore drills in a separate
namespace with isolated buckets, and keep the copy from contacting public
peers.

### Migrate an existing Compose instance

The cutover needs downtime. Keep the old checkout, containers, and volumes
until the new instance is verified, and never run both against the same
identity or object store.

Before the outage, prepare K3s, turn off automatic installs, run setup with
the same secrets and domain, install the image helper, and build the images:

```sh
make auto-update-disable
make tools
make setup
PATH="$PWD/.kaede-tools/bin:$PATH" python3 deploy/kubernetes/manage.py build
# Find and inspect the exact old project; this does not stop anything:
docker compose ls
python3 deploy/kubernetes/legacy.py inspect --project kaede-chat
```

Stop the update timer, put nginx in maintenance mode, then export:

```sh
deploy/install-auto-update.sh stop
python3 deploy/kubernetes/legacy.py export --project kaede-chat --backup /path/to/new-backup
```

Export stops the old writers, dumps PostgreSQL, snapshots Dragonfly, stops
the rest, archives the bundled Garage, search, and monitoring volumes, copies
the operator environment, and records checksums. If it's interrupted, the
source stays stopped; fix the error and resume rather than starting both
stacks. External S3 objects stay in place and need their own backup.

Restore only into a fresh namespace, with the same component versions:

```sh
PATH="$PWD/.kaede-tools/bin:$PATH" python3 deploy/kubernetes/legacy.py restore --backup /path/to/new-backup
make deploy
```

Restore refuses an existing namespace or mismatched keys. Check readiness,
discovery, login, history, media, and voice before leaving maintenance mode.
After new writes, going back to the old volumes loses them; use a reverse
migration or backup restore instead. `make legacy-down PROJECT=kaede-chat`
stops leftover legacy containers without deleting anything, and needs no
Compose manifest.

For development, create the empty node with `make dev-cluster`, run
`legacy.py restore --development --backup ...`, then `make dev`. Keep the
same `.env` and stop the old project first so its ports are free.

## Upgrade and rollback

### Optional automatic updates

`make auto-update-run` works even with scheduling off. It needs a clean
tracked checkout on the configured branch and refuses dirty checkouts,
detached heads, force-pushed history, downgrades, and non-fast-forward
merges. It builds and imports images first (a build failure changes
nothing), runs preflight, runs `AUTO_UPDATE_BACKUP_HOOK` if set, then rolls
out compatible changes.

The backup hook runs after preflight on every deployment, while writers are
still running. During an auto-update it gets `KAEDE_UPDATE_FROM`,
`KAEDE_UPDATE_TO`, and `KAEDE_ROOT`. A nonzero exit stops the deployment.

New API and gateway pods must be ready before old ones stop. Open WebSockets
get a grace period, then reconnect. Single-node hardware, storage upgrades,
and LiveKit replacement still cause downtime.

The deployed commit is recorded only once all workloads are ready; the first
run after enabling records the current commit. A failed compatible rollout
restores the previous Deployment specs. Check `make status`,
`make logs SERVICE=api`, and Job logs before retrying. Schema changes are
never rolled back automatically.

#### Online migrations

Unattended updates accept additive migrations: new tables and their indexes,
nullable columns, and columns with constant defaults. Every pending revision
must qualify, even across skipped releases. Anything else needs maintenance,
including changed migration history, unrecognized Python or SQL, backfills,
indexes on existing tables, constraint changes, and stateful or host-network
infrastructure. The first deployment of the migration runner also needs
maintenance to record its baseline.

Online migrations run one transaction per revision with a 1-second lock
timeout and a 30-second statement timeout. A failure leaves running
workloads alone, though earlier revisions may have committed; fix the cause
and retry the same release.

Workers roll out first and the frontend last. Startup checks accept extra
columns so the previous release can run on the new schema, but that isn't
full compatibility. Developers must keep queue payloads and stored data
readable by the previous release and keep irreversible activations, such as
consent or privacy changes, out of the rollback window.

#### Scheduling and notices

Enable or inspect scheduling with:

```sh
make auto-update-enable
make auto-update-status
journalctl --user -u kaede-auto-update.service
make auto-update-disable
```

Setup schedules update checks even when automatic installation
(`AUTO_UPDATE_ENABLED`) is off. `make auto-update-disable` keeps the
notification-only checks; `deploy/install-auto-update.sh stop` stops
everything. On an existing install, `make auto-update-disable` adds the
notification timer. `make update-notices` checks once without installing.
Intervals are `6h`, `12h`, `1d`, or `1w`; jitter and the backup hook are set
in `.env`.

Local instance owners get one DM per new upstream revision from **Kaede
System**, marked **SYSTEM**. It's a plaintext, local-only account that can't
log in, receive replies, or federate. The checker reports whether an update
can install online, needs maintenance, or needs review (changed deployment
code or an unverifiable baseline). It never runs code from the update, and
notification failures don't block installation. The first deployment with
system notices needs maintenance.

#### Host requirements

Unattended updates need a successful first `make deploy` and never replace a
legacy instance. The deployment user needs Docker, kubeconfig access, and
(for local imports) the helper's sudo rule.

To keep the user timer running after logout, an administrator can run
`sudo loginctl enable-linger <user>`. Without systemd, have cron run
`deploy/auto-update.sh run` from the checkout with its tool PATH set. Locks
prevent overlapping timer and cron runs.

### Manual upgrade and rollback

For a schema or infrastructure change, update the checkout (unless the
updater already did), back up, put nginx in maintenance mode, and run:

```sh
make env-check
make deploy MAINTENANCE=1
make status
```

Maintenance stops writers before infrastructure changes and migrations, so
service is interrupted. If a migration fails, writers stay stopped; fix it or
restore the matching backup, and never start incompatible old code.
Maintenance removes disabled optional workloads but keeps their PVCs. Old
Secrets and ConfigMaps are kept for rollback; delete them once nothing
references them. Stay in maintenance until readiness, smoke checks, the
migration head, and discovery check out. Retrying is safe; the `migrate`
gate is idempotent.

API workers wait 60 seconds before becoming ready to avoid snowflake ID
collisions. Allow for that in external health checks.

Roll back the application only if the old code understands the current
schema. Otherwise stop writers and restore the pre-upgrade backup, or run a
reviewed Alembic downgrade with matching code. Never guess a downgrade
target, and never restore PostgreSQL without its `KAEDE_SECRET_KEY`.

#### Release-specific migrations

**Password KDF v2.** The migration won't run while any local human account
lacks version-2 salts. Before upgrading, run this on the old database:

```sql
SELECT id, username
FROM users
WHERE is_local
  AND account_type = 'human'
  AND (
    password_kdf_version IS DISTINCT FROM 2
    OR password_auth_salt IS NULL
    OR e2ee_vault_salt IS NULL
  );
```

Every listed account must complete password recovery on the old release.
Don't create salts in SQL or rely on a same-password login; neither converts
the encrypted vault. Decide what to do about accounts with no recovery
address, and proceed only when the query returns nothing.

**Server-only clean media keys.** After stopping the old deployment, wait at
least `KAEDE_MEDIA_UPLOAD_TTL_SECONDS` before running this migration so no
old upload credential is still valid.

**Origin-scoped events.** This migration can't be downgraded if one event ID
exists under several origin or guild domains. Keep the newer revision, and
don't delete rows to force a downgrade.

**Encrypted email outbox.** Pending email-change confirmations are
invalidated, and users must request new ones. Verification and reset links
still work.

### Federation signing keys

Rotate the instance federation signing key inside a running API container:

```sh
make exec SERVICE=api COMMAND='kaede rotate-key'
```

The old key stays in `old_verify_keys` for the longer of the event-retention
window and 8 days. Then retire it with `kaede retire-key <key-id>`, which
refuses the current key, keys still needed for pending deletion proofs, and
early retirement. `--force-compromised` skips the wait for an active
compromise only, and can make queued envelopes unverifiable; record the
incident and notify peers first.

## Federation allowlists and blocklists

Set `KAEDE_FEDERATION_MODE=allowlist` to require explicit peer approval. The
admin API uses `KAEDE_ADMIN_TOKEN`; send it in a header, never a URL.

- `GET /api/v1/admin/federation/blocks/export` produces Mastodon-compatible
  CSV.
- `POST /api/v1/admin/federation/blocks/import` accepts a bounded CSV body.

A `silence` block holds guild traffic but allows DMs and identity updates; a
`suspend` block holds everything. Export before bulk changes, review
subdomain inclusion, and keep the export with the deployment revision.
Removing a block triggers a replica reconciliation.

### Rolling upgrades with peers

Once a peer advertises replay-protected requests, dropping that capability is
treated as a downgrade. Legacy peers keep the version 1 signing form during
upgrades.

Let pending encrypted-room proposals finish before rotating API workers;
proposals from before device generations were recorded must be made again.

### Federation storage budgets

The [wizard's storage limits menu](deployment-wizard.md#storage-limits)
edits these and checks that they fit together; run `make env-check` after
manual edits. They're admission ceilings, not reservations, so leave
database headroom for WAL, indexes, vacuum, and backups. Alert on repeated
quota errors: they can mean an undersized deployment, a stuck retention
worker, or an abusive peer.

#### Inbox

| Variable | Default |
| --- | --- |
| `KAEDE_FEDERATION_INBOX_MAX_EVENTS_PER_ORIGIN` | 5,000,000 claims |
| `KAEDE_FEDERATION_INBOX_MAX_BYTES_PER_ORIGIN` | 16 GiB |
| `KAEDE_FEDERATION_INBOX_MAX_EVENTS_TOTAL` | 50,000,000 claims |
| `KAEDE_FEDERATION_INBOX_MAX_BYTES_TOTAL` | 160 GiB |

Keep totals at least as large as per-origin values. A full budget returns
retryable `KAED_FED_INBOX_QUOTA_EXCEEDED`, and delivery resumes once daily
retention frees space or you raise the limit.

#### Peer metadata

| Variable | Default |
| --- | --- |
| `KAEDE_FEDERATION_MAX_REMOTE_INSTANCES` | 10,000 remote instances |
| `KAEDE_FEDERATION_PEER_KEY_HISTORY_LIMIT` | 512 key IDs per peer |

At either cap, new peers (or new key IDs) are rejected. Retired keys are
deleted after the signed-event retention window.

#### Relationships, identities, and deletion markers

| Variable | Bounds |
| --- | --- |
| `KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_RECIPIENT` | Pending friend requests per local recipient |
| `KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_RECIPIENT_ORIGIN` | Per recipient/origin pair |
| `KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_ORIGIN` | Per remote origin |
| `KAEDE_FEDERATION_REMOTE_USERS_PER_INTRODUCER` | Remote profiles charged to the introducing peer |
| `KAEDE_FEDERATION_THIRD_PARTY_INSTANCES_PER_INTRODUCER` | Third-party namespaces charged to the introducing peer |
| `KAEDE_FEDERATION_REMOTE_MEDIA_TOMBSTONES_PER_ORIGIN` | Media deletion markers per origin |

Tune these conservatively; startup rejects per-scope limits above their
aggregate. A full relationship allowance returns
`KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED`, and the sender tells the user
the request wasn't delivered. Identity and namespace limits return
`FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED` or
`FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED` (HTTP 507) locally and
`KAED_FED_*` codes to peers. Affected DM opens and writes fail visibly, and a
remote guild replica pauses as `quota_paused` until capacity returns.

Daily retention removes up to
`KAEDE_FEDERATION_REMOTE_IDENTITY_GC_BATCH_SIZE` (default 5,000) unreferenced
remote profiles and namespaces per run, after
`KAEDE_FEDERATION_REMOTE_IDENTITY_RETENTION_DAYS` (default 30, minimum 7).

#### Cross-instance DMs

| Variable | Default |
| --- | --- |
| `KAEDE_FEDERATION_DM_MAX_CONVERSATIONS_PER_AUTHORITY` | 1,000,000 |
| `KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_AUTHORITY` | 50,000,000 |
| `KAEDE_FEDERATION_DM_MAX_BYTES_PER_AUTHORITY` | 320 GiB |
| `KAEDE_FEDERATION_DM_MAX_CONVERSATIONS_PER_REMOTE_ORIGIN` | 100,000 |
| `KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_REMOTE_ORIGIN` | 10,000,000 |
| `KAEDE_FEDERATION_DM_MAX_BYTES_PER_REMOTE_ORIGIN` | 64 GiB |
| `KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_CONVERSATION` | 5,000,000 |
| `KAEDE_FEDERATION_DM_MAX_BYTES_PER_CONVERSATION` | 32 GiB |
| `KAEDE_FEDERATION_DM_REPLICA_CACHE_MESSAGES_PER_CONVERSATION` | 250,000 |
| `KAEDE_FEDERATION_DM_REPLICA_CACHE_BYTES_PER_CONVERSATION` | 2 GiB |

The `MAX` values are hard backstops; keep per-origin values below the
authority totals so one peer can't use everyone's capacity. Replicas normally
keep only a rolling cache of the newest remote-authored messages, up to the
cache targets, which must not exceed the per-conversation maximums. Local
messages, pins, the newest message, and pending notifications are never
evicted. Older pages load on demand from the conversation authority, with a
Retry option if it's offline. A DM message can have at most 100 distinct
reactions.

#### Remote-guild replicas

| Variable | Default |
| --- | --- |
| `KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_GUILD` | 20,000,000 rows |
| `KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_GUILD` | 64 GiB |
| `KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_ORIGIN` | 100,000,000 rows |
| `KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_ORIGIN` | 320 GiB |

Origin limits must be at least the guild limits. The ledger counts every
replicated row; downloaded media counts against
`KAEDE_MEDIA_REMOTE_CACHE_BYTES` instead. Size PostgreSQL with these minimum
charges, not average message length:

| Row | Minimum charge |
| --- | --- |
| Retained message | 4 KiB message row + 2 KiB projection row |
| Retained-history provenance | +1 KiB |
| History staging (temporary) | +4 KiB |
| Reaction | 1 KiB |
| Attachment metadata | 4 KiB |
| Member | 4 KiB |

When a change crosses a limit, the oldest replaceable remote messages in that
guild are evicted locally, aiming for 5% headroom. Local messages, pins,
bookmarks, and thread starters are kept. Evicted history can't be scrolled
back to or re-imported. If not enough can be freed, the guild pauses with
`KAED_FED_REPLICA_QUOTA_EXCEEDED` until you raise the limit; review the
origin before raising it much.

#### Federated media

| Variable | Default |
| --- | --- |
| `KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_PER_ORIGIN` | 256 MiB |
| `KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_TOTAL` | 512 MiB |
| `KAEDE_MEDIA_REMOTE_CACHE_BYTES` | 100 GiB |

In-flight limits cover downloads being scanned. Keep the total at least the
per-origin value and both at least one maximum attachment; raising them only
adds network, memory, and spool use. The remote cache is a strict ceiling,
pruned least-recently-used to 90%. When it's full, fetches fail with a
retryable error and the bundled alert fires, which can mean a stalled
eviction worker, orphan objects, or an undersized cache.

## Private message search

Setup offers message search on a bundled, private Meilisearch service. Its
master key lives in `.env` and never leaves the server; don't publish its
port or reuse the key. `KAEDE_SEARCH_ENABLED=false` turns search off and
removes the service. To add search to an existing deployment, rerun
`make setup`, choose search, back up, and run `make deploy MAINTENANCE=1`.

The settings are `KAEDE_SEARCH_ENABLED`, `KAEDE_SEARCH_URL`,
`KAEDE_SEARCH_MASTER_KEY`, `KAEDE_SEARCH_INDEX_PREFIX`,
`KAEDE_SEARCH_REQUEST_TIMEOUT_SECONDS`, `KAEDE_SEARCH_BATCH_SIZE`, and
`KAEDE_SEARCH_FEDERATION_TIMEOUT_SECONDS`. A larger batch catches up faster
but adds load.

The index is disposable. To repair or replace it while chat stays online,
run:

```sh
make search-rebuild            # refresh every authoritative SQL message
make search-rebuild RESET=1    # first discard and recreate the private index
```

A Meilisearch outage only affects search. Monitor `search.index_sweep`,
`kaede_search_index_pending_messages`, and
`kaede_search_index_retrying_messages`. Repeated `SEARCH_BACKEND_UNAVAILABLE`
points to the URL, key, service health, disk, or a failed index task; fix the
service and rebuild rather than editing message rows.

Only plaintext channels are indexed. Switching a channel to `e2ee` removes
its documents. Remote guild search returns only messages in the local replica
and reports partial coverage.

## Federated retained history

`guild-history-sync/1` and `/2` are advertised automatically, but export
stays off until a guild administrator enables it for the guild or a channel.
`KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED` turns off all inbound imports.
These settings bound a single import; raise them only after checking
database and worker capacity:

| Variable | Default |
| --- | --- |
| `KAEDE_FEDERATION_HISTORY_MAX_MESSAGES` | 2,000,000 |
| `KAEDE_FEDERATION_HISTORY_MAX_REACTIONS` | 10,000,000 |
| `KAEDE_FEDERATION_HISTORY_MAX_PAGES` | 250,000 |
| `KAEDE_FEDERATION_HISTORY_MAX_BYTES` | 32 GiB |
| `KAEDE_FEDERATION_HISTORY_MAX_DURATION_SECONDS` | Two hours |

`KAEDE_FEDERATION_HISTORY_EXPORT_TTL_MINUTES`,
`KAEDE_FEDERATION_HISTORY_PAGE_MESSAGES`,
`KAEDE_FEDERATION_HISTORY_PAGE_BYTES`, and
`KAEDE_FEDERATION_HISTORY_MERGE_CHUNK_SIZE` tune grant lifetime, page size,
and merge batches.

Active grants on the authority side are bounded too. A full budget returns
retryable `KAED_FED_HISTORY_CAPACITY`.

| Variable | Default |
| --- | --- |
| `KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_PER_ORIGIN` | 1,000 |
| `KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_TOTAL` | 10,000 |
| `KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_PER_ORIGIN` | 100,000 |
| `KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_TOTAL` | 1,000,000 |

Imported history is a local replica, purged when the guild revokes access,
with a five-minute sweep for missed updates. Monitor failed
`federation.history_sync` tasks, since an offline peer delays imports and
purges. A modified peer can't be forced to delete data it received, so enable
export only for peers you trust with it.

## Observability boundary

`KAEDE_OBSERVABILITY_ENABLED=true` enables Prometheus, a Grafana dashboard,
alert rules, and Loki. Grafana is reachable on a loopback bridge at
`KAEDE_GRAFANA_HOST_PORT` (default `18084`) and needs a unique
`GRAFANA_ADMIN_PASSWORD` of at least 20 characters.

The stack doesn't mount the Docker socket, which would expose unrelated host
containers, and Loki has no host log collector; connect a reviewed collector
if you need centralized logs.

Metrics cover API health, gateway sessions, federation delivery, task runs,
and the federation quotas above. Bundled alerts fire at 80% inbox use, on
any inbox rejection, when remote-media eviction can't make room, and when a
remote guild stays quota-paused.

## Explicit v1 operational boundaries

- There's no rotation for `KAEDE_SECRET_KEY`; federation key rotation doesn't
  touch it. Keep it unchanged and restore it with the database.
- The general Taskiq worker handles email, federation, media, and voice, so it
  holds all their credentials. Protect it accordingly, or split the queues
  and settings if you need isolation.
- Images use fixed version tags, not digests, and the built-in audits don't
  scan base-image OS packages. Mirror or digest-pin images and run an image
  scanner in production.
- LiveKit health isn't part of API readiness. With voice enabled, monitor its
  HTTP and media ports externally.

## Acceptance checks

The `make *-check` acceptance targets run in disposable k3d clusters and
don't test a production deployment. They're listed in the
[development guide](development.md#acceptance-checks).
