# Interactive deployment setup

Run the Bash setup wizard from the repository root:

```sh
./setup.sh
# or
make setup
```

The wizard writes repository-local configuration and installs Kaede's
per-user systemd update timer. It doesn't start containers, install packages
or proxy files, request certificates, change the firewall, or reload
nginx/Caddy. The update timer is the only host service it touches.

Run `make tools` first if kubectl is missing. K3s must already be installed
and Ready; setup checks access but doesn't install the cluster. For a first
installation, follow the [README setup steps](../README.md#setup), which list
the answers for a single-server install.

If [`gum`](https://github.com/charmbracelet/gum) is installed, the wizard uses
it for the interface and hidden credential prompts. Otherwise it falls back to
a colored, dependency-free Bash interface; it never installs gum. Pass
`--plain` to force the built-in interface, or `--dry-run` to collect and
validate answers without writing anything.

## Choices

| Setting | What to prepare |
| --- | --- |
| Domain and proxy | Instance domain, DNS, TLS certificate/key paths, loopback edge port, and optional host nginx configuration |
| Kubernetes | Readable kubeconfig, production context, dedicated namespace, storage class/capacity, and local image imports or a registry |
| Storage | Bundled Garage, or credentials and three private buckets at an S3-compatible provider |
| Email | SMTP or Mailtrap credentials, or no-email registration |
| Federation | Open federation, or an allowlist of trusted instances with an administrator API token |
| Optional features | LiveKit voice/video, private message search, GIF search, Turnstile, mobile push, and monitoring |
| Image matching | Optional Microsoft PhotoDNA subscription key for checking unencrypted images |
| Updates | Optional source-update schedule and executable backup hook |
| Capacity | Worker counts, upload limits, host ports, and optional federation/cache quotas |

### TLS certificates

The wizard never obtains or installs certificates. It records the paths of
existing certificate and key files for host nginx and LiveKit TURN. Defaults
follow Certbot's `/etc/letsencrypt/live/<domain>/` layout; any absolute path
works.

### Storage

For bundled Garage, setup creates the buckets for you. For an external
provider, prepare the buckets, credentials, and CORS rules described under
[object storage](operator.md#object-storage) before running setup.

### Email

**Disabled (no email at signup)** registers users with only a username and
password; no address is collected and no mail is sent. Email changes and
self-service password recovery are unavailable, so decide how staff will
recover accounts first. The wizard asks you to confirm this separately.

### GIF search and Turnstile

KLIPY needs a provider API key, which stays on the backend; browsers search
through Kaede. Turnstile needs the widget's site key and private secret. The
wizard stores the secret as `TURNSTILE_SECRET` and never prints private
values. Tokens are bound to the instance hostname and the `kaede-register`
or `kaede-login` action.

### Mobile push

The wizard recommends the public Kaede relay, which works with the official
app and needs no Firebase credentials. It shows what metadata the relay sees
before enabling it. You can instead choose direct Firebase for a separately
signed custom app, or turn off closed-app delivery.

On the relay authority itself, the wizard can enable the relay service and
read the Firebase service-account JSON from a local non-symlink file. Only
relay workers receive it, and the deployment validator checks it before
startup. The public Android and iOS Firebase app files are build-time inputs
ignored by Git. See [mobile push delivery](mobile-push.md).

### Voice ports

Keep the existing ports, let setup find five free ones, or enter them
yourself. Each LiveKit deployment on a host needs its own set. Automatic
selection only checks listeners while setup runs, so deploy before giving
those ports to anything else. Defaults and firewall rules are in
[hosts, certificates, and ports](operator.md#hosts-certificates-and-ports).

### Storage limits

On a new deployment, **Keep recommended defaults** skips the quota questions.
On a rerun, **Keep existing limits** preserves custom values.

- **Customize common storage limits** covers the main byte budgets: the
  rolling DM cache, remote-guild budgets, retained-history imports, remote
  inbox bytes, and the remote-media cache.
- **Advanced** adds event counts, identity and relationship caps, row counts,
  per-origin, aggregate, and hard ceilings, grant and page limits, and
  in-flight concurrency.

Counts accept `K`, `M`, and `B` (`250K`, `2.5M`). Bytes accept decimal
`KB`/`MB`/`GB`/`TB` or binary `K`/`M`/`G`/`T` and `KiB`/`MiB`/`GiB`/`TiB`.
Setup shows the parsed values before writing and rejects a scoped or cache
limit above its aggregate or hard limit, as preflight does. Higher limits
don't reserve disk, so leave room for indexes, WAL, maintenance, and backups.
What each limit controls is in
[federation storage budgets](operator.md#federation-storage-budgets) and
[upload quotas](operator.md#upload-quotas).

### Automatic updates

Automatic installation is off by default. Setup still installs
`kaede-auto-update.timer` in your user's systemd configuration, where it only
checks for updates and notifies instance owners. If setup can't reach your
user's systemd manager, it leaves `AUTO_UPDATE_ENABLED=false` and prints a
warning instead of claiming the timer is active. With local image imports, the
timer also needs the import helper and sudoers rule from
[README step 4](../README.md#4-download-kaede-and-enable-local-image-imports).
[Optional automatic updates](operator.md#optional-automatic-updates) covers
what the updater does, lingering, logs, and the cron fallback.

## Generated files

- `.env` — complete production settings and generated secrets, mode `0600`;
- `.kaede-kubernetes.json` — pins cluster identity, namespace, image delivery, and storage;
- `deploy/generated/kaede.nginx.conf` — only when host nginx is requested;
- `deploy/generated/README.txt` — exact validation and startup guidance.

The deployment splits `.env` into a separate Secret per application role; see
[reverse-proxy credentials](operator.md#reverse-proxy-credentials) for who
receives what. The environment's operator GID is used only for optional
PhotoDNA file access.

Secrets come from OpenSSL's CSPRNG, with independent values for the
application, gateway, proxy, database, Dragonfly, Garage, LiveKit, Grafana,
and admin credentials.

## Reruns

A rerun preserves existing durable secrets that aren't placeholders,
especially `KAEDE_SECRET_KEY`, which encrypts stored instance signing
material. It refuses to change an established instance domain.

- Quota tuning is kept when you choose either keep option. The only automatic
  change targets exact defaults written by older setup versions:
  retained-history messages go from 250,000 to 2,000,000 and the remote-media
  cache from 20 GiB to 100 GiB. Other values you set are left alone.
- Settings setup doesn't ask about, such as `KAEDE_LANDING_PAGE` and the
  `KAEDE_LEGAL_*` fields, are copied into the new `.env` unchanged.
- Push delivery defaults to your existing choice.
- Credentials and endpoints for a feature you turn off are dropped.

Writes are staged, sensitive files are private, output symlinks and hard
links are rejected, and `flock` prevents concurrent runs when available.
Changed files are backed up under `.kaede-backups/`. Once the deployment is
confirmed, remove backups your recovery policy doesn't need. If you rotated a
credential, securely delete backups that contain the old value.

## After setup

Review the generated files, then run the checks from
[README step 6](../README.md#6-configure-your-instance):

```sh
make env-check
make kubernetes-check
```

If a host nginx file was generated, install it in nginx's `http` context
yourself, run `nginx -t`, and reload nginx, as in
[README step 7](../README.md#7-start-kaede-and-connect-nginx). The internal
edge stays on the selected `127.0.0.1` port, so it doesn't conflict with an
existing nginx listener on 80/443.
