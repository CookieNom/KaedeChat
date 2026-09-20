# Operator guide

Use this guide to configure, monitor, back up, and upgrade a Kaede instance.
For a new deployment, start with the [README setup steps](../README.md#setup).
The [wizard reference](deployment-wizard.md) explains each generated file.

[Configuration](#secrets-and-initial-configuration) · [Storage](#object-storage) ·
[Backups](#backup-and-restore-boundary) · [Upgrades](#upgrade-and-rollback) ·
[Federation](#federation-allowlists-and-blocklists) · [Monitoring](#observability-boundary)

## Hosts, certificates, and ports

Production assumes host nginx owns public TCP 80/443. If you run the bundled
Garage, your certificate must cover both `chat.example.com` and
`media.chat.example.com`. External-S3 deployments drop the unused media
virtual host and only need the main application name. The internal Caddy edge
binds only to `127.0.0.1:18081`, and the API diagnostic binding defaults to
`127.0.0.1:18082`. If you change the edge port during a manual setup, update
both `.env` and the host-nginx upstream.

LiveKit uses host networking and starts only when `KAEDE_VOICE_ENABLED=true`. The edge returns 404 for
`/livekit` unless `KAEDE_VOICE_ENABLED=true`; the voice preflight requires
that exact opt-in. Once you have enabled voice, allow the selected RTC/TURN traffic through the host and
provider firewalls: TCP `LIVEKIT_RTC_TCP_PORT`, UDP `LIVEKIT_RTC_UDP_PORT`,
UDP `KAEDE_TURN_UDP_PORT`, and TCP `LIVEKIT_TURN_TLS_PORT`. Keep
`LIVEKIT_CONTROL_PORT` and the API/edge loopback ports blocked from external
interfaces. The TURN certificate paths must be absolute host paths, and the
certificate name must match `KAEDE_DOMAIN`.

The defaults are control TCP 7880, RTC TCP 7881, RTC UDP 7882, TURN/TLS TCP
5349, and TURN UDP 13478. Every host-networked LiveKit process on the same
host needs its own five-port set. The setup wizard can find an available set or accept one you pick by hand. Automatic
selection avoids conflicts with listeners present while setup runs. It can't
stop another process from claiming a selected port before Kubernetes starts the pod.

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
option and generated file. If you used the wizard, follow the commands in `deploy/generated/README.txt`.

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

## Public landing and operator policies

`KAEDE_LANDING_PAGE=default` keeps the project homepage. With no operator legal
configuration, direct visits to `/terms` and `/privacy` show an explicit
non-policy notice; Kaede does not publish template placeholders as though they
were operative terms.

Set `KAEDE_LANDING_PAGE=custom` only after the operator has reviewed the policy
copy for its jurisdiction and supplied all six public build-time fields:

- `KAEDE_LEGAL_INSTANCE_NAME` — the instance name users recognize;
- `KAEDE_LEGAL_OPERATOR_NAME` — the operator's real legal identity;
- `KAEDE_LEGAL_CONTACT_EMAIL` — a monitored address for legal, privacy, and
  account-access requests;
- `KAEDE_LEGAL_EFFECTIVE_DATE` — a real `YYYY-MM-DD` policy date;
- `KAEDE_LEGAL_MINIMUM_AGE` — an integer from 1 through 120; and
- `KAEDE_LEGAL_JURISDICTION` — the governing jurisdiction selected by the
  operator and its counsel.

These values are embedded into the public static frontend and are not secrets.
A custom build fails if any field is absent or malformed; a partially filled
policy also fails under the default landing. Changing the landing variant or
any legal field requires rebuilding `frontend-build`. Kaede currently has no
full account export, account migration, or self-service account-deletion
control. The supplied policy says so and directs privacy and account-disabling
requests to `KAEDE_LEGAL_CONTACT_EMAIL`; it does not promise that contacting the
operator automatically deletes stored or federated data. Do not claim a
settings workflow or deletion outcome unless the deployment actually adds and
verifies one.

## Manual environment configuration

Before starting any service, validate both the file itself and the effective
application environment:

```sh
make env-check
```

The target rejects duplicate assignments, group/world-readable production
files, documented placeholder credentials, disabled production malware
scanning, and invalid settings. Application preflight also rejects unknown
`KAEDE_` settings. For another file, pass `ENV_FILE=/absolute/path/to/kaede.env`
to both validation and deployment targets.

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
random `KAEDE_ADMIN_TOKEN`. When enabling voice, also set
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

## Optional services

Optional interaction services are disabled by default. Set
`KAEDE_KLIPY_ENABLED=true` with a private `KAEDE_KLIPY_API_KEY` to expose the
GIF picker. Set `KAEDE_TURNSTILE_ENABLED=true`, `KAEDE_TURNSTILE_SITE_KEY`,
and the private `TURNSTILE_SECRET` to require Turnstile during registration
and after a failed sign-in attempt. The API key and Turnstile secret belong
only in backend environments; neither appears in public configuration
responses.

Guild stickers use the ordinary scanned media pipeline. Configure the per-guild
and per-file bounds with `KAEDE_MEDIA_STICKER_LIMIT` and
`KAEDE_MEDIA_MAX_STICKER_BYTES`. Cropping is always available and preserves GIF
animation. Background removal is opt-in because rembg loads an
ONNX model and materially increases worker memory: set `REMBG_HOME`, preseed that model cache
for every media worker, size worker memory for the selected model, then set
`KAEDE_MEDIA_STICKER_BACKGROUND_REMOVAL_ENABLED=true`. Keep it disabled when the
model is not locally available; production workers must not depend on a runtime
model download.

Closed-app notifications normally use the public Kaede relay. Set
`KAEDE_PUSH_RELAY_ENABLED=true`; an ordinary home needs no Firebase
credential. The relay URL and logical origin are pinned separately because
`push.kaede.chat` serves transport for the `kaede.chat` signing authority.
Provider tokens go from the official app straight to the relay. Your home
keeps only opaque subscriptions and wake secrets.

Only the relay operator sets `KAEDE_PUSH_RELAY_SERVICE_ENABLED=true` and
`KAEDE_PUSH_RELAY_FCM_SERVICE_ACCOUNT_B64`. For iOS calling, it also sets the
`KAEDE_PUSH_RELAY_APNS_KEY_B64`, `KAEDE_PUSH_RELAY_APNS_KEY_ID`,
`KAEDE_PUSH_RELAY_APNS_TEAM_ID`, and `KAEDE_PUSH_RELAY_APNS_TOPIC` variables.
The provider credentials must reach relay workers only. Never let them reach
API processes, browsers, mobile apps, logs, or ordinary federated homes. Direct FCM is still
available with `KAEDE_PUSH_ENABLED` for a separately signed custom app
distribution; it does not notify the official app.

For verified mobile links on a signed custom build, set
`KAEDE_MOBILE_ANDROID_SHA256_CERT_FINGERPRINTS` to the comma-separated signing
certificate fingerprints and `KAEDE_MOBILE_IOS_APP_IDS` to the comma-separated
`TEAM_ID.bundle_id` identifiers. Kaede then serves Android Asset Links and the
Apple App Site Association document from the required `/.well-known` paths.

See [mobile push delivery](mobile-push.md) for the data flow, privacy table,
E2EE behavior, conversion procedure, queue failure semantics, and
custom-build requirements.

## End-to-end encryption activation

New encrypted-room activation is enabled by default and fails closed whenever
the room, client, or a participating home can't satisfy the protocol. Before a
public launch, work through the release gates and client compatibility checks
in [the E2EE protocol and rollout guide](e2ee.md), or set
`KAEDE_E2EE_ACTIVATION_ENABLED=false` on every participating home. Mixed
settings reject new proposals; they never downgrade an active encrypted room.
Turning the flag off later hides new activation. Rekey, recovery,
selective-disclosure reports, and active encrypted rooms keep working.

## Reverse-proxy credentials

Set the same `KAEDE_EDGE_SECRET` in `.env` and in the nginx
`X-Kaede-Edge-Secret` header. It must differ from `KAEDE_PROXY_SECRET`. The
internal edge receives only the domain and these two edge credentials.
Application processes don't receive Garage administration, LiveKit, or
host-edge secrets.

## Object storage

Kaede requires an S3-compatible object store; Garage is only the default.
Pick exactly one deployment mode.

For self-hosted Garage, keep `KAEDE_MEDIA_STORAGE_BACKEND=garage`. The provider-neutral `storage-init` service
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

Configure browser `GET` and `HEAD` for the exact Kaede HTTPS origin. The
attachment bucket must allow `PUT` from any HTTPS origin so remote members can
upload report evidence directly to the moderation authority. Permit the
`Content-Type` header; the presigned URL provides upload authorization. Don't
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

Choose external S3 in `make setup`; no Garage workload or PVC is then created.
For another locally hosted S3 service, select the same `s3` mode and use an
endpoint reachable from the pods. Production requires TLS for external S3;
development can use HTTP. `host.k3d.internal` reaches the Docker host from k3d,
but browsers need a separately reachable public URL. `storage-init`
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
It keeps the staging-key reference until strictly more than 16 minutes after
the presigned `PUT` expires. That exceeds the official clients' 15-minute
request bound, so a rewrite begun just before expiry still can't affect served
bytes and the cleanup sweep deletes it. Third-party upload clients must enforce
the same 15-minute request bound, or the object store must enforce equivalent
staging-prefix lifecycle cleanup.

The nginx example gives only the media virtual host a 101 MiB body allowance,
bounded concurrent connections, request rate, and five-minute proxy timeouts.
The main API/federation host stays limited to 2 MiB. The media path streams
request bodies instead of buffering them on host disk. If you raise
`KAEDE_MEDIA_MAX_ATTACHMENT_BYTES`, update and review the media-server
`client_max_body_size` too; the application ticket and signed PUT length are
still the authoritative per-object checks.

## Microsoft PhotoDNA image matching

PhotoDNA is optional because Microsoft distributes its Edge Hash generator
under a separate confidential license. Don't add the SDK archive, native
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

Kubernetes mounts that directory read-only at `/opt/photodna` in preflight,
API, and worker pods. They run as UID `10001`, with supplemental group
`OPERATOR_ENV_GID` (default `1000`) to read the SDK. Keep group read/traverse
permissions restricted. k3d mounts the same directory into its node first.

Preflight loads the native library and rejects an incomplete or incompatible
installation before the API starts. The matcher URL is fixed in code to
`https://api.microsoftmoderator.com/photodna/v1.0/MatchHash`; allow outbound
TCP 443 to `api.microsoftmoderator.com` and never substitute HTTP.

For plaintext local uploads the worker runs magic/type validation and ClamAV,
creates an Edge Hash V2 in an isolated child process, and submits only that
hash to Microsoft before any clean-key promotion. Plaintext images fetched
from a federated home get the same PhotoDNA decision before they enter the
local remote-media cache.

On a match, the worker deletes the image from staging or the temporary remote
spool, marks it quarantined internally, and opens one automated
`illegal_content` case in Administration → Reports. The report retains the provider tracking ID, the
source/violation/distance flags, attachment and optional uploader/message
references, the MIME type, and the ordinary SHA-256 incident identifier. It
retains no image bytes, thumbnail, object key, or PhotoDNA hash. Non-admin
media APIs expose the same generic `rejected` state used for other terminal
safety-policy decisions; they never reveal the internal quarantine state.
Provider or generator failure is fail-closed: the object stays unavailable and
normal task retries continue.

The adapter terminally rejects images above 256 frames or 25 million decoded
pixels in total without calling the provider or opening a report. This is a
fail-closed policy rejection, not an infection or positive-match verdict, and
is not retried. It also takes an advisory kernel lock on the mounted SDK directory,
so the API's Uvicorn processes and the media worker run only one native image
decode at a time even though they are separate processes and containers. Keep
the SDK on a local filesystem (or one with working POSIX `flock` semantics).

PhotoDNA can't inspect an E2EE attachment: the server receives only
ciphertext and has no room key. That's why the E2EE activation warning states
that server-side file and malware scanning stops. A recipient can still
submit a client-decrypted report, but Kaede never uploads E2EE
plaintext to PhotoDNA.

Kaede rejects images with either dimension below 50 pixels. Smaller eligible
images are proportionally upscaled to the provider’s 160-pixel floor only
inside the isolated hash adapter; published bytes remain unchanged. MatchHash
receives a fixed-size Edge Hash rather than the source file, so there is no
source-byte-size bypass. The supplied SDK's preferred `PreHashV2` sample posts
only the fixed-size hash; the provider's 4 MB source-image rule belongs to the
deprecated direct-image input, which Kaede does not use. Microsoft remains
authoritative for the current upper eligibility window. Statuses `3206` and
`3208` are treated as terminal provider-ineligible results rather than leaving
an upload stuck in retries: those files receive the neutral `rejected` state,
are deleted, and are never published as clean. Other generator or provider
errors remain fail-closed and retryable. MIME validation, ClamAV, dimension
limits, and normal image processing still apply.

## Kubernetes prerequisites

Use a single-node K3s cluster for a small production instance. Install K3s
following its [official installation instructions](https://docs.k3s.io/quick-start).
Configure `/etc/rancher/k3s/config.yaml` before starting it:

```yaml
node-name: kaede-production
write-kubeconfig-mode: "0640"
write-kubeconfig-group: k3s-admin
secrets-encryption: true
disable:
  - traefik
  - servicelb
```

Create the access group, add your deployment user, and start K3s as root:

```sh
sudo groupadd -f k3s-admin
sudo usermod -aG k3s-admin "$USER"
sudo systemctl enable --now k3s
```

Log out and back in for group membership, then run `make tools` and verify:

```sh
.kaede-tools/bin/kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml get nodes
.kaede-tools/bin/kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml get pods -A
```

The node and kube-system pods must be Ready. Cluster credentials grant
administrative access; do not make the kubeconfig world-readable. Keep
TCP 6443 private or restricted to your administration network. Do not expose
Flannel UDP 8472 or the NodePort range publicly. A Proxmox/provider firewall
can enforce these restrictions; UFW is not required. The k3d API and HTTP
publications bind loopback. Host nginx remains outside both clusters.

Inside an unprivileged LXC container, Docker nesting, a writable cgroup v2
hierarchy, and the necessary host kernel modules must be available. If K3s
fails because kubelet cannot use `/dev/kmsg` in the user namespace, add:

```yaml
kubelet-arg:
  - feature-gates=KubeletInUserNamespace=true
```

Restart K3s and check its journal and node readiness. k3d already passes this
flag to its development node. Do not weaken the host container's isolation
without first diagnosing the actual failure. See the Kubernetes
[user-namespace requirements](https://kubernetes.io/docs/tasks/administer-cluster/kubelet-in-userns/).
Development uses separate pod/service CIDRs from the host K3s defaults.

### Local production image imports

`make setup` defaults to local image imports. Docker builds the images; K3s
uses its own containerd store. On the single K3s server, install the small
root-owned import helper once:

```sh
sudo install -o root -g root -m 0755 deploy/kubernetes/import-image.sh /usr/local/sbin/kaede-import-image
sudo visudo -f /etc/sudoers.d/kaede-image-import
```

For deployment user `cookie`, add this exact sudoers entry (substitute your
actual account name):

```sudoers
cookie ALL=(root) NOPASSWD: /usr/local/sbin/kaede-import-image ""
```

Then validate with `sudo visudo -c`. The helper accepts no arguments and imports
an image archive from stdin into K3s's `k8s.io` namespace. It is the only sudo
operation used by deployment and unattended updates. Keep it root-owned and
review changes before reinstalling it. This flow assumes the command runs on
the single production K3s node. For multiple nodes or a remote cluster, choose
a registry instead and provision an image-pull Secret if necessary.

## Validate and start

```sh
make setup
make env-check
make kubernetes-check
make deploy
make status
make logs SERVICE=api
```

`.kaede-kubernetes.json` pins the kubeconfig, context, namespace, cluster UID,
storage class/capacity, and image delivery. Every production action verifies
the saved cluster identity. `KUBE_CONFIG=/path/to/config.json` selects another
saved deployment, and `ENV_FILE=/path/to/operator.env` selects its environment.
Never change namespace or storage class to work around a failed rollout: that
would select different data.

Preflight validates application settings before stateful services start. The
migration Job runs once before any new writer starts, then storage-init verifies
all S3 buckets. Readiness gates the API, gateway, frontend, and internal edge.
Host nginx reaches loopback bridge pods on the configured edge/diagnostic ports;
ordinary application rollouts leave these bridges and stateful services running.
The supplied topology is intended for a single node and local-path volumes.
Storage requests are Kubernetes capacities, not a substitute for free-space
monitoring or application quotas.

## Development CA

Default k3d development uses HTTP on loopback hostnames, with no CA installation.
For real-device HTTPS, keep TLS on host nginx using your trusted certificate,
and set the development `.env` public URLs accordingly. Federation TLS acceptance
checks generate a temporary CA inside their disposable test cluster; they never
install it into your host trust store.

## Backup and restore boundary

Back up the database, private object buckets, Dragonfly snapshots, `.env`, and
`.kaede-kubernetes.json` as one recovery set. Preserve `KAEDE_SECRET_KEY`, domain,
and storage keys. PVCs and single-node Garage are persistent storage, not backups.
External object-store versioning and retention need their own recovery policy.
Keep copies off the host and exercise restoration before relying on them.

For a consistent maintenance backup, place host nginx in maintenance mode and
scale API, gateway, worker, and scheduler to zero. Wait for all writer pods to
terminate before dumping PostgreSQL. Use explicitly selected kubeconfig/context
and namespace with every kubectl command. Do not delete PVCs to stop services.
Take Garage metadata and data snapshots together after Garage has stopped; a
live file copy of its metadata is not a consistent backup. Snapshot Dragonfly
before stopping it. Capture provider-hosted S3 using its documented backup tools.
Restore into a separate cluster/namespace and isolated buckets for a drill,
without allowing a duplicate federation authority to contact public peers.

### Migrate an existing Compose instance

The one-time cutover needs downtime. Keep the old checkout, containers, and
volumes until the new instance is verified. Do not run old and new writers
against the same identity or object store simultaneously. First prepare K3s,
disable the old update timer, run setup (retaining secrets and domain),
install the image helper, and build
images before the outage:

```sh
make auto-update-disable
make tools
make setup
PATH="$PWD/.kaede-tools/bin:$PATH" python3 deploy/kubernetes/manage.py build
# Find and inspect the exact old project; this does not stop anything:
docker compose ls
python3 deploy/kubernetes/legacy.py inspect --project kaede-chat
```

Disable the old update timer. Put nginx in maintenance mode, then export:

```sh
make auto-update-disable
python3 deploy/kubernetes/legacy.py export --project kaede-chat --backup /path/to/new-backup
```

Export stops that project's writers, dumps PostgreSQL, snapshots Dragonfly,
stops the remaining containers, and archives bundled Garage/search/monitoring
volumes. It copies the private operator environment and records checksums.
An interrupted export leaves the source stopped; inspect the error and resume
from the retained containers, rather than starting both stacks. External S3
objects stay in their existing buckets and require a separate backup.

Restore only into a fresh namespace, using the same component versions:

```sh
PATH="$PWD/.kaede-tools/bin:$PATH" python3 deploy/kubernetes/legacy.py restore --backup /path/to/new-backup
make deploy
```

Restore refuses an existing namespace or mismatched identity/storage keys. Check
readiness, instance discovery, login, history, media download/upload, and voice
before removing maintenance mode. After new writes occur, reverting to old
volumes would lose those writes; use a reviewed reverse migration or backup
restore instead. `make legacy-down PROJECT=kaede-chat` stops retained legacy
containers without deleting containers, networks, or volumes. No Compose
manifest is required for this shutdown tool.

For a development cutover, create the empty node with `make dev-cluster`, then
use `legacy.py restore --development --backup ...` and `make dev`. This restores
data into the separate k3d cluster. Keep the same development `.env` and stop the
old project first so its HTTP and LiveKit ports can be reused.

## Upgrade and rollback

### Optional automatic updates

`make auto-update-run` works even with scheduling disabled. It requires a clean
tracked checkout on the configured branch and accepts only fast-forward Git
updates. It builds/imports images, runs preflight and the executable
`AUTO_UPDATE_BACKUP_HOOK` if configured, then rolls compatible applications.
The hook receives `KAEDE_UPDATE_FROM`, `KAEDE_UPDATE_TO`, and `KAEDE_ROOT` during
an auto-update. Nonzero exit stops deployment. Image builds/imports happen before
any workload replacement; Docker build failure leaves the running application alone.

New API/gateway pods must pass readiness before old pods terminate. Old gateway
pods retain established sockets for a bounded grace period, then clients must
reconnect/resume. Load balancing directs new connections to ready pods; it cannot
move an established WebSocket. This reduces update disruption but does not make
single-node hardware, storage upgrades, or LiveKit replacement highly available.

Changed migrations or stateful/host-network infrastructure configuration refuse
an unattended update. The checkout may already be newer, but the deployed commit
is recorded only after all workloads become ready. Compatible failed application
rollouts restore previous Deployment specifications; the release marker remains
unchanged. Inspect `make status`, `make logs SERVICE=api`, and Job logs before
retrying. Schema changes are never automatically downgraded.

Enable or inspect scheduling with:

```sh
make auto-update-enable
make auto-update-status
journalctl --user -u kaede-auto-update.service
make auto-update-disable
```

Unattended updates require a successful initial `make deploy`; they never
initialize an empty replacement for a legacy instance. The deployment user needs
Docker, kubeconfig, and the local import sudo rule.
For a user timer to run after logout, an administrator can run
`sudo loginctl enable-linger <user>`. On non-systemd hosts, cron can invoke
`deploy/auto-update.sh run` from the checkout with its tool PATH configured.
The same updater locks prevent overlapping timer/cron runs. Intervals are
`6h`, `12h`, `1d`, or `1w`; jitter and backup-hook settings are in `.env`.

### Manual upgrade and rollback

For a reviewed schema/infrastructure change, back up and put nginx in
maintenance mode, then run:

```sh
make env-check
make deploy MAINTENANCE=1
make status
```

Maintenance stops writers before updating infrastructure and running migrations.
If a migration fails, writers stay stopped. Resolve the migration or restore the
matching backup before resuming; never start incompatible old code. Maintenance
removes disabled optional workloads while retaining their PVCs.
It keeps previous credential/config revisions for rollback. Old Secrets/ConfigMaps
can be removed once no live pod, Job, or retained ReplicaSet references them.

The password-KDF-v2 cutover migration refuses to run while any local human
account still lacks version-2 authentication and vault salts. Before deploying this
release, check the old database while the old release is still available:

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

Every returned account must complete the normal password-recovery flow on the
old release before this migration runs. Do not use a same-password login
upgrade or manufacture salts in SQL: neither can safely convert the
password-encrypted account vault. Accounts without a working recovery address
need an explicit account-replacement/recovery decision before rollout. Re-run
the query and proceed only when it returns no rows.

For the migration that introduces server-only clean media keys, wait at least
`KAEDE_MEDIA_UPLOAD_TTL_SECONDS` after quiescing the old deployment before
running the migration. That guarantees no browser credential issued by the
old version can still rewrite an already-clean legacy key. Keep maintenance
mode in place until readiness, smoke checks, the migration head, and instance
discovery have all been verified. The startup `migrate` gate may run again
during a retry; its upgrade and bootstrap operations are idempotent.

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
make exec SERVICE=api COMMAND='kaede rotate-key'
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
body. A `silence` block holds guild snapshots, guild events, and remote guild-write
proxies while permitting DM and user-identity federation; a `suspend` block holds
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
Meilisearch service. It binds only to the instance namespace, behind a private Service, and
its master key is generated into the mode-0600 operator `.env`. The key is
never sent to browsers, mobile clients, desktop clients, or federation peers.
Disable search with `KAEDE_SEARCH_ENABLED=false`. On an existing deployment,
rerun `make setup`, choose search, then apply the generated configuration and
migrations normally. `KAEDE_SEARCH_ENABLED=true` activates the
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

`KAEDE_OBSERVABILITY_ENABLED=true` enables Prometheus, a provisioned Kaede overview
dashboard in Grafana, alert rules, and a Loki endpoint. It doesn't mount or
proxy the Docker socket: project-label filtering isn't an access-control
boundary, and a socket reader could inspect unrelated host containers.
Grafana has a private ClusterIP Service and a production loopback bridge on
`KAEDE_GRAFANA_HOST_PORT` (default `18084`). It requires the
externally supplied administrator password. Set a unique
`GRAFANA_ADMIN_PASSWORD` of at least 20 characters; observability preflight
rejects a blank or documented placeholder before any monitoring service starts.
Prometheus, Loki, and Grafana have restart policies and readiness checks.

Metrics cover API health, connected gateway sessions, pending/failed
federation delivery, delivery failures, and task duration/run/failure totals.
Federation metrics also expose retained remote event rows/bytes, configured
inbox capacity, trigger-accounted replica rows/bytes, quota-paused guilds,
quota deferrals, the configured remote-media LRU ceiling, and admissions
rejected at that ceiling. The bundled alerts warn at 80% inbox utilization,
on any inbox rejection, when remote-media eviction can't make room, and when
a remote guild stays quota-paused.

Loki has no privileged host log collector. If you need
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
Monitor its HTTP and media-plane ports externally whenever the voice feature
is enabled.

## Acceptance checks

Run `make tools` first, then checks from the repository root. These targets create disposable k3d
clusters and remove those clusters on exit; they do
not publish application ports or validate an existing production deployment.

| Target | Coverage |
| --- | --- |
| `make identity-check` | Registration, email outbox, cookies/bearers, CSRF, MFA, session rotation, recovery |
| `make chat-check` | Messages and DMs, Gateway replay, roles, moderation, invites, reactions, pins, unread state |
| `make federation-check` | Discovery, signed delivery, remote joins/writes, replication, recovery, and revocation |
| `make federation-tls-check` | Federation TLS and transport validation |
| `make media-check` | Real Garage uploads, scan gating, derivatives, webhooks, and deletion |
| `make voice-check` | LiveKit grants, occupancy, call transitions, replay, and orphan-room cleanup |
| `make release-check` | Readiness warmup, rate limits, shared-stream fanout, and federation storage amplification |
| `make migration-check` | Migration up/down/up, guarded downgrades, database invariants, and schema drift |

`make check` covers backend and frontend lint, types, unit tests, and the web
build. `make audit` checks locked dependencies. Live-device media quality and
platform signing still need the checks in the client release guides.
