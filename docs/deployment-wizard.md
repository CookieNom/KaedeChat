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

The wizard asks about:

- the production domain, federation admission, and optional admin token;
- the loopback port for Kaede's internal Caddy edge;
- an optional host nginx configuration for hosts where nginx owns 80/443,
  referencing TLS certificate and private-key paths you supply;
- bundled Garage, AWS S3, Backblaze B2, Cloudflare R2, or generic S3 storage;
- Mailtrap API, Mailtrap SMTP, AWS SES SMTP, generic SMTP, or no email;
- optional KLIPY GIF search and Cloudflare Turnstile registration/adaptive
  sign-in protection;
- optional typo-tolerant message search backed by a private, bundled
  Meilisearch service (enabled by default for new deployments);
- optional LiveKit voice/video and observability services;
- optional source-based automatic updates, including Git remote, branch,
  interval, and an executable pre-update backup hook; and
- worker counts, upload limits, non-conflicting host ports, and optional
  federation/storage quota tuning.

Federation and remote-cache sizing is optional. **Keep recommended defaults**
on a new deployment, or **Keep existing limits** on a rerun, asks no individual
quota questions and preserves custom values already in `.env`. **Customize
common storage limits** exposes the operationally useful high-water marks:
retained federation-event bytes per origin and instance, the rolling DM cache,
remote-guild replica bytes per guild and origin, one retained-history import,
and downloaded remote media. **Advanced** additionally exposes event counts,
identity/relationship abuse caps, DM hard ceilings, replica row counts,
concurrent media transfer, and history grant/page ceilings.

Count fields accept plain integers or `K`, `M`, and `B` suffixes (for example
`250K` and `2.5M`). Byte fields accept decimal `KB`, `MB`, `GB`, and `TB`, or
binary `K`, `M`, `G`, `T`, `KiB`, `MiB`, `GiB`, and `TiB`. Setup shows the
parsed count or byte value after each answer and prints a core quota summary
before writing anything. Paired prompts constrain their next answer, so a
per-scope or cache value cannot exceed its aggregate or hard ceiling. If you
raise the attachment limit above remote-transfer capacity, setup raises the
in-flight limits enough to receive one maximum attachment. Raising limits does
not reserve disk — keep headroom for PostgreSQL index/WAL/vacuum/backup and
object-store lifecycle. Stay on the default path unless monitoring shows
sustained capacity pressure.

If you already have [`gum`](https://github.com/charmbracelet/gum) installed,
the script uses it for the interface and hidden credential prompts. Otherwise
it falls back to a colored, dependency-free Bash interface. It never installs
gum. Use `--plain` to force the built-in interface, or `--dry-run` to collect
and validate answers without writing anything.

## Generated files

- `.env` — complete production settings and generated secrets, mode `0600`;
- `deploy/compose.generated.yml` — selects Garage or external S3 and optional profiles;
- `deploy/generated/kaede.nginx.conf` — only when host nginx is requested;
- `deploy/generated/README.txt` — exact validation and startup guidance.

The generated environment also records the numeric UID and GID that own
`.env`. The one-shot preflight validators use that identity so they can read
the private bind mount without running as root or gaining additional
capabilities. If you move the deployment to another account or host, rerun the
wizard.

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
local, non-symlink file. Compose passes that credential only to relay workers,
and the standalone deployment validator checks it before startup. The public
Android/iOS Firebase application files remain build-time inputs and are ignored
by Git. See [mobile push delivery](mobile-push.md).

Automatic updates are disabled by default. When you enable them, setup installs
a `kaede-auto-update.timer` in your user's systemd configuration. Its first run
reconciles and records the current commit. The updater never accepts a dirty
tracked checkout, detached head, force-pushed history, downgrade, or
non-fast-forward merge. It builds and runs preflight before stopping services;
then it runs the configured backup hook, quiesces application writers, applies
migrations, restarts, and waits for health. Declining a backup hook requires a
separate warning confirmation. The timer needs the same Docker access your user
has for manual deployment commands.

## After setup

Review the generated files, then validate without starting the application:

```sh
make env-check
make generated-compose-check
```

Inspect or change the update timer later with `make auto-update-status`,
`make auto-update-enable`, and `make auto-update-disable`. If setup cannot
reach your user's systemd manager, it leaves `AUTO_UPDATE_ENABLED=false` and
prints a warning rather than claiming the timer is active. See the operator
guide for lingering, logs, failure handling, and a cron fallback.

On the first explicit `docker compose up`, the one-shot `migrate` service
creates an empty database schema by applying all Alembic revisions in order. On
later starts it applies only pending revisions. Instance bootstrap follows
migration, and database-facing application services wait for the whole step to
succeed.

If a host nginx file was generated, install it manually in nginx's `http`
context, run `nginx -t`, and reload nginx yourself. The internal edge stays on
the selected `127.0.0.1` port, so it does not conflict with an existing nginx
listener on 80/443.

For external S3, keep all three buckets private and configure provider CORS for
browser `PUT`, `GET`, and `HEAD` requests from the exact Kaede HTTPS origin.

For voice, choose whether to keep an existing port set, let setup pick an
available set, or enter all five host ports manually. Each LiveKit deployment
on one host needs unique control, RTC TCP, RTC UDP, TURN/TLS TCP, and TURN UDP
ports. Review those ports, host/provider firewall rules, NAT forwarding, and
certificate paths before enabling the profile. Automatic selection only checks
listeners at setup time, so start the deployment before assigning those ports
to anything else.
