# Interactive deployment setup

Run the Bash setup wizard from the repository root:

```sh
./setup.sh
# or
make setup
```

The script generates repository-local configuration and can optionally install
or remove Kaede's per-user systemd update timer. It does not start containers,
install packages or proxy files, request certificates, change the firewall, or
reload nginx/Caddy. The user timer is the only host service it touches, and
only after you explicitly opt into automatic updates.

## Choices

| Setting | What to prepare |
| --- | --- |
| Domain and proxy | Instance domain, DNS, TLS certificate/key paths, loopback edge port, and optional host nginx configuration |
| Kubernetes | Readable kubeconfig, production context, dedicated namespace, storage class/capacity, and local image imports or a registry |
| Storage | Bundled Garage, or credentials and three private buckets at an S3-compatible provider |
| Email | SMTP or Mailtrap credentials, or no-email registration |
| Optional features | LiveKit voice/video, private message search, GIF search, Turnstile, mobile push, and monitoring |
| Updates | Optional source-update schedule and executable backup hook |
| Capacity | Worker counts, upload limits, host ports, and optional federation/cache quotas |

For a new deployment, **Keep recommended defaults** skips individual quota
questions. On a rerun, **Keep existing limits** preserves custom values.
**Customize common storage limits** covers the main cache/history byte budgets;
**Advanced** exposes event counts, identity/relationship caps, row counts,
concurrency, and grant/page limits. Full sizing guidance is in the
[operator guide](operator.md#federation-storage-budgets).

Counts accept `K`, `M`, and `B` (`250K`, `2.5M`). Bytes accept decimal
`KB`/`MB`/`GB`/`TB` or binary `K`/`M`/`G`/`T` and `KiB`/`MiB`/`GiB`/`TiB`.
Setup shows the parsed values before writing and checks that related limits
fit together. Higher limits do not reserve disk space; leave room for database
indexes, WAL, maintenance, and backups.

If you already have [`gum`](https://github.com/charmbracelet/gum) installed,
the script uses it for the interface and hidden credential prompts. Otherwise
it falls back to a colored, dependency-free Bash interface. It never installs
gum. Use `--plain` to force the built-in interface, or `--dry-run` to collect
and validate answers without writing anything.

## Generated files

- `.env` — complete production settings and generated secrets, mode `0600`;
- `.kaede-kubernetes.json` — pins cluster identity, namespace, image delivery, and storage;
- `deploy/generated/kaede.nginx.conf` — only when host nginx is requested;
- `deploy/generated/README.txt` — exact validation and startup guidance.

Run `make tools` before setup if kubectl is missing. K3s must already be
installed and Ready; setup verifies access but does not install the cluster.
The environment's operator GID is used only for optional PhotoDNA file access.
Application Secrets are split by role instead of mounting the whole `.env`.

The script uses OpenSSL's CSPRNG for independent application, gateway, proxy,
database, Dragonfly, Garage, LiveKit, Grafana, and admin secrets. Reruns
preserve existing non-placeholder durable secrets — especially
`KAEDE_SECRET_KEY`, which encrypts stored instance signing material. The script
refuses to change an established instance domain.

Reruns also preserve your quota tuning when either keep option is selected. The
only automatic quota migrations recognize exact defaults emitted by older setup
versions: retained-history messages move from 250,000 to 2,000,000 and the
remote-media cache from 20 GiB to 100 GiB. Any other value you set is left
unchanged.

Writes are staged, sensitive files are private, output symlinks and hard links
are rejected, and `flock` prevents concurrent runs when available. Changed
files are backed up under `.kaede-backups/`. Remove obsolete backups after
confirming the deployment, keeping only what your recovery policy requires. If
you rotated a credential, securely remove backups containing the retired value.

The wizard does not generate, request, renew, install, or replace TLS
certificates. When host nginx or LiveKit TURN needs TLS, it records paths to
certificate and private-key files that already exist on the host. The defaults
follow Certbot's usual `/etc/letsencrypt/live/<domain>/` layout, but you can
supply any absolute paths.

Selecting **Disabled (no email at signup)** makes registration use only a
username and password. Kaede does not collect an email address, issue a
verification token, or enqueue mail for those accounts. Email changes and
self-service password recovery are unavailable in this mode, so establish an
administrative account-recovery policy before enabling it. The wizard requires
a separate confirmation because a forgotten password cannot be recovered by the
user.

KLIPY requires a provider API key. The key stays backend-only: browsers query
Kaede, and Kaede returns a bounded, validated list of provider-hosted media.
Turnstile requires the widget's public site key and private secret. The wizard
stores the secret under Cloudflare's `TURNSTILE_SECRET` name and never prints
either private credential. Registration validation binds successful tokens to
the instance hostname and the distinct `kaede-register` or `kaede-login`
action.

For mobile push, the wizard recommends the public Kaede relay. It works with
the official app and requires no Firebase account or provider credential on a
federated home. The wizard shows the relay's metadata boundary before enabling
it. You can instead choose direct Firebase for a separately signed custom app,
or disable closed-app delivery entirely.

If you run the wizard for the configured relay authority itself, it can enable
the relay service and read the private Firebase service-account JSON from a
local, non-symlink file. The deployment passes that credential only to relay workers,
and the standalone deployment validator checks it before startup. The public
Android/iOS Firebase application files remain build-time inputs and are ignored
by Git. See [mobile push delivery](mobile-push.md).

Automatic updates are disabled by default. When you enable them, setup installs
a `kaede-auto-update.timer` in your user's systemd configuration. Its first run
reconciles and records the current commit. The updater never accepts a dirty
tracked checkout, detached head, force-pushed history, downgrade, or
non-fast-forward merge. It builds/imports images, runs preflight and the backup
hook, and rolls compatible application changes with readiness checks. Migrations
or infrastructure changes require `make deploy MAINTENANCE=1` after a backup.
The timer needs Docker and kubeconfig access, plus the root-owned local import
helper and narrowly scoped sudoers rule when local imports are selected. See
[local imports](operator.md#local-production-image-imports).

## After setup

Review the generated files, then validate without starting the application:

```sh
make env-check
make kubernetes-check
```

Inspect or change the update timer later with `make auto-update-status`,
`make auto-update-enable`, and `make auto-update-disable`. Disabling automatic
installation keeps owner notification checks running; use
`deploy/install-auto-update.sh stop` to stop both. If setup cannot
reach your user's systemd manager, it leaves `AUTO_UPDATE_ENABLED=false` and
prints a warning rather than claiming the timer is active. See the operator
guide for lingering, logs, failure handling, and a cron fallback.

On the first `make deploy`, the migration Job creates the database schema and
bootstraps the instance before application writers start. Later deployments
with changed migration files require explicit maintenance. Preflight and
storage-init failures stop deployment before new applications are started.

If a host nginx file was generated, install it manually in nginx's `http`
context, run `nginx -t`, and reload nginx yourself. The internal edge stays on
the selected `127.0.0.1` port, so it does not conflict with an existing nginx
listener on 80/443.

For external S3, keep all three buckets private. Configure `GET` and `HEAD` CORS
for the exact Kaede HTTPS origin. The attachment bucket's browser `PUT` rule
must accept any HTTPS origin because a remote guild member can explicitly
disclose E2EE report evidence directly to the guild's moderation authority;
the short-lived presigned URL remains the upload authorization.

For voice, choose whether to keep an existing port set, let setup pick an
available set, or enter all five host ports manually. Each LiveKit deployment
on one host needs unique control, RTC TCP, RTC UDP, TURN/TLS TCP, and TURN UDP
ports. Review those ports, host/provider firewall rules, NAT forwarding, and
certificate paths before enabling voice. Automatic selection only checks
listeners at setup time, so start the deployment before assigning those ports
to anything else.
