# Interactive deployment setup

Run the Bash setup wizard from the repository root:

```sh
./setup.sh
# or
make setup
```

The script only generates repository-local configuration. It does not start
containers, install packages or proxy files, request certificates, change the
firewall, or reload nginx/Caddy.

The wizard asks about:

- the production domain, federation admission, and optional admin token;
- the loopback port for Kaede's internal Caddy edge;
- an optional host nginx configuration for hosts where nginx owns 80/443,
  referencing operator-supplied TLS certificate and private-key paths;
- bundled Garage, AWS S3, Backblaze B2, Cloudflare R2, or generic S3 storage;
- Mailtrap API, Mailtrap SMTP, AWS SES SMTP, generic SMTP, or no email;
- optional KLIPY GIF search and Cloudflare Turnstile registration/adaptive sign-in protection;
- optional LiveKit voice/video and observability services; and
- worker counts, upload limits, and non-conflicting host ports.

If [`gum`](https://github.com/charmbracelet/gum) is already installed, the
script uses it for the interface and hidden credential prompts. Otherwise it
uses a colored, dependency-free Bash interface. The script never installs gum.
Use `--plain` to force the built-in interface or `--dry-run` to collect and
validate answers without writing anything.

## Generated files

- `.env` — complete production settings and generated secrets, mode `0600`;
- `deploy/compose.generated.yml` — selects Garage or external S3 and optional profiles;
- `deploy/generated/kaede.nginx.conf` — only when host nginx is requested;
- `deploy/generated/README.txt` — exact validation and startup guidance.

The generated environment also records the numeric UID and GID that own
`.env`. The one-shot preflight validators use that identity so they can read
the private bind mount without running as root or gaining additional
capabilities. Rerun the wizard after moving the deployment to another account
or host.

The script uses OpenSSL's CSPRNG for independent application, gateway, proxy,
database, Dragonfly, Garage, LiveKit, Grafana, and admin secrets. Existing
non-placeholder durable secrets are preserved on reruns, especially
`KAEDE_SECRET_KEY`, because it encrypts stored instance signing material. The
script refuses to change an established instance domain.

Writes are staged, sensitive files are private, output symlinks/hard links are
rejected, and `flock` prevents concurrent runs when available. Changed files
are backed up under `.kaede-backups/`; remove obsolete backups after confirming
the deployment and retain only copies required by your recovery policy. If a
credential was rotated, securely remove backups containing the retired value.

The wizard does not generate, request, renew, install, or replace TLS
certificates. When host nginx or LiveKit TURN needs TLS, it records paths to
certificate and private-key files that already exist on the host. The defaults
follow Certbot's usual `/etc/letsencrypt/live/<domain>/` layout, but any absolute
paths may be supplied.

Selecting **Disabled (no email at signup)** makes registration use only a
username and password. Kaede does not collect an email address, issue a
verification token, or enqueue mail for those accounts. Email changes and
self-service password recovery are unavailable, so operators should establish
an administrative account-recovery policy before enabling this mode. The wizard
requires a separate confirmation because a forgotten password cannot be
recovered by the user.

KLIPY requires a provider API key. The key remains backend-only: browsers query
Kaede, and Kaede returns a bounded, validated list of provider-hosted media.
Turnstile requires the widget's public site key and private secret. The wizard
stores the latter under Cloudflare's `TURNSTILE_SECRET` name and never prints
either private credential. Registration validation binds successful tokens to
the instance hostname and distinct `kaede-register` or `kaede-login` action.
Firebase mobile push is optional. The wizard explains that operators must first
create a Firebase project, register Android package `chat.kaede.mobile`, place
the downloaded client file at `mobile/android/app/google-services.json`, and
generate a private service-account key from **Project settings > Service
accounts**. When selected, the wizard reports whether the Android client file is
present, then lets the operator read a local non-symlink service-account JSON
file or paste the JSON through a hidden multiline prompt. File mode rejects
`google-services.json` as a backend credential; paste mode ends with
`KAEDE_FIREBASE_JSON_END` on a line by itself. The wizard stores only the
private key's base64 representation in the operator environment. The Android and iOS
Firebase application files are installed separately at build/signing time and
remain ignored by Git. FCM itself does not require billing or Google Analytics.

## After setup

Review the generated files, then validate without starting the application:

```sh
make env-check
make generated-compose-check
```

On the first explicit `docker compose up`, the one-shot `migrate` service creates
an empty database schema by applying all Alembic revisions in order. On later
starts it applies only pending revisions. Instance bootstrap follows migration,
and database-facing application services wait for the whole step to succeed.

If a host nginx file was generated, install it manually in nginx's `http`
context, run `nginx -t`, and reload nginx yourself. The internal edge remains
on the selected `127.0.0.1` port, so it does not conflict with an existing nginx
listener on 80/443.

For external S3, keep all three buckets private and configure provider CORS for
browser `PUT`, `GET`, and `HEAD` requests from the exact Kaede HTTPS origin.
For voice, choose whether to keep an existing port set, have setup select an
available set, or enter all five host ports manually. Each LiveKit deployment on
one host needs unique control, RTC TCP, RTC UDP, TURN/TLS TCP, and TURN UDP
ports. Review those ports, host/provider firewall rules, NAT forwarding, and
certificate paths before enabling the profile. Automatic selection only checks
listeners at setup time, so start the deployment before assigning those ports to
anything else.
