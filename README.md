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
| Build a bot and add features | [Bot SDK guide](docs/bot-api-quickstart.md) |
| Deploy the Discord bridge | [Bridge setup](official-bots/discord-bridge/README.md) |
| Maintain an existing server | [Operator guide](docs/operator.md) |
| Build a desktop or mobile client | [Desktop](desktop/README.md) · [Mobile](mobile/README.md) |
| Understand the APIs and design | [Documentation index](docs/README.md) |

## Setup

The steps below install **one production instance on an Ubuntu 24.04 server**,
using K3s, host nginx, and local image builds. You do not need a container
registry. Run commands as your normal login user with sudo access, not from a
root shell. Run each step in order and stop if a command fails.

This is a **new installation** walkthrough. If you already have a Compose
instance, use the [data migration procedure](docs/operator.md#migrate-an-existing-compose-instance)
instead of creating a second instance with the same domain.

### 1. Choose your domain and storage

This example uses `chat.example.com`. **Replace it with your own domain in
all commands and wizard answers below.** Choose it before registering accounts;
it becomes part of account addresses and cannot simply be renamed later.

For the simplest installation, choose **Bundled Garage** for file storage.
Kaede will create and manage its private buckets on this server. Create these
DNS records, both pointing to the server's public IP:

| Name | Purpose |
| --- | --- |
| `chat.example.com` | Website, API, federation, and voice signaling |
| `media.chat.example.com` | Uploads and downloads when using bundled Garage |

Use DNS-only records if your DNS provider also offers an HTTP proxy. Only add
AAAA records if IPv6 actually reaches this server. If the server is behind NAT,
forward the public ports to it.

Allow inbound TCP **80 and 443** in your host, provider, or Proxmox firewall.
Keep SSH available to your administration network. Do not publicly open the
Kubernetes API (TCP 6443), Flannel (UDP 8472), database ports, or the Kubernetes
NodePort range. No additional host firewall is required if your provider or
hypervisor already enforces these rules.

If you enable voice, also allow the ports selected by setup. The defaults are:

| Protocol | Ports | Purpose |
| --- | --- | --- |
| TCP | `7881`, `5349` | Voice transport and TURN over TLS |
| UDP | `7882`, `13478` | Voice transport and TURN |

The LiveKit control port (`7880` by default) and Kaede's loopback ports
(`18081` and `18082`) do not need public access.

**Using an existing S3 service instead?** Select your provider or
**Generic S3-compatible** in setup. Have its endpoint, region, credentials,
and three private bucket names ready: attachments, derived media, and remote
cache. A separately hosted Garage or other local S3 service is supported too;
its endpoint must be reachable from Kubernetes pods, not just from the host.
Production requires HTTPS for generic S3 endpoints. You do not need the
`media.chat.example.com` DNS record when using the provider's HTTPS media endpoint.

Configure CORS in your S3 provider before using uploads:

- Allow `GET` and `HEAD` from `https://chat.example.com` on all three buckets.
- On the attachments bucket, also allow `PUT` from any HTTPS origin and allow
  request headers (`*`). Federated moderation evidence can be uploaded from
  another instance; the presigned URL authorizes the upload.
- Keep the buckets private. Leave bucket creation disabled in setup if you
  created them yourself. Provider CORS formats differ; enter these rules using
  your provider's bucket settings.

### 2. Install the host packages

On a fresh Ubuntu 24.04 server:

```sh
sudo apt update
sudo apt install -y git make python3 openssl curl ca-certificates util-linux \
  docker.io docker-buildx nginx certbot
sudo systemctl enable --now docker nginx
sudo groupadd -f k3s-admin
sudo usermod -aG docker,k3s-admin "$USER"
```

These commands use Ubuntu's Docker packages. If Docker Engine and Buildx are
already installed from Docker's own repository, keep that installation and omit
`docker.io docker-buildx` from the package command.

**Log out and reconnect now** so both new group memberships take effect. Then
check Docker access without sudo:

```sh
docker info
docker buildx version
id -nG
```

The last command must list `docker` and `k3s-admin`. All application dependencies
are built into images; you do not need to install Node.js, Rust, or PostgreSQL
on the host.

### 3. Install K3s

K3s runs the production containers. nginx stays on the host, so disable K3s's
bundled ingress controller and load balancer before starting it:

```sh
sudo install -d -m 755 /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<'YAML'
node-name: kaede-production
write-kubeconfig-mode: "0640"
write-kubeconfig-group: k3s-admin
secrets-encryption: true
disable:
  - traefik
  - servicelb
YAML
curl -fsSL https://get.k3s.io -o /tmp/kaede-install-k3s.sh
sudo env INSTALL_K3S_VERSION=v1.36.4+k3s1 INSTALL_K3S_SKIP_START=true \
  sh /tmp/kaede-install-k3s.sh
sudo systemctl enable --now k3s
```

Allow time for the API to start, then wait for the node and system services:

```sh
for attempt in $(seq 1 90); do
  sudo k3s kubectl get nodes >/dev/null 2>&1 && break
  sleep 2
done
sudo k3s kubectl wait --for=condition=Ready nodes --all --timeout=180s
sudo k3s kubectl wait --for=condition=Ready pods --all -n kube-system --timeout=180s
```

Do not continue until both readiness commands succeed. If startup fails, inspect
`sudo journalctl -u k3s -n 100 --no-pager`.

**Proxmox/unprivileged LXC:** Docker nesting, cgroup v2, and the host kernel
support required by K3s must already be available. If kubelet reports that it
cannot use `/dev/kmsg` in the user namespace, append this to
`/etc/rancher/k3s/config.yaml`, restart with `sudo systemctl restart k3s`, and
repeat the readiness checks:

```yaml
kubelet-arg:
  - feature-gates=KubeletInUserNamespace=true
```

For an existing Kubernetes cluster, skip the installation commands. You will
need a readable kubeconfig, permission to create the instance's resources, and
a working storage class. The rest of this walkthrough assumes the local,
single-node K3s installation above.

### 4. Download Kaede and enable local image imports

Choose a permanent checkout location. Run the remaining `make` commands from
this directory:

```sh
git clone https://github.com/CookieNom/KaedeChat.git
cd KaedeChat
make tools
.kaede-tools/bin/kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get nodes
```

`make tools` installs the pinned, checksum-verified command-line tools into this
checkout. The node should show `Ready`, without needing sudo. If the kubeconfig
is unreadable, check `id -nG` and reconnect after adding the `k3s-admin` group;
do not make cluster credentials world-readable.

Docker builds Kaede's images, but K3s has a separate image store. Install the
helper that imports them:

```sh
sudo install -o root -g root -m 0755 deploy/kubernetes/import-image.sh /usr/local/sbin/kaede-import-image
whoami
sudo visudo -f /etc/sudoers.d/kaede-image-import
```

In the editor, add the following line, replacing `YOUR_LOGIN` with the username
printed by `whoami`. Keep the final `""`; it restricts the helper to no arguments.
Save and exit:

```sudoers
YOUR_LOGIN ALL=(root) NOPASSWD: /usr/local/sbin/kaede-import-image ""
```

Then fix the file permissions and validate the rule:

```sh
sudo chown root:root /etc/sudoers.d/kaede-image-import
sudo chmod 0440 /etc/sudoers.d/kaede-image-import
sudo visudo -c
sudo -n -l /usr/local/sbin/kaede-import-image
```

`visudo` must report `parsed OK`, and the final command must list the helper
without asking for a password. This is the only privileged operation used by
normal deployments and local-image updates.

### 5. Obtain your HTTPS certificate

If you already manage a certificate for these hostnames, keep it and use its
absolute certificate/key paths in step 6. Otherwise, the following sets up
Certbot validation on a new nginx installation. DNS and inbound port 80 from
step 1 must be working first.

```sh
sudo mkdir -p /var/www/letsencrypt
sudo tee /etc/nginx/conf.d/kaede.conf >/dev/null <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name chat.example.com media.chat.example.com;
    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }
    location / { return 404; }
}
NGINX
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/letsencrypt \
  --cert-name chat.example.com \
  -d chat.example.com -d media.chat.example.com
```

For external S3 without a local media hostname, remove `media.chat.example.com`
from `server_name` and omit its `-d` argument. Certbot will ask for a contact
email and agreement to its terms.

The resulting paths are:

- Certificate: `/etc/letsencrypt/live/chat.example.com/fullchain.pem`
- Private key: `/etc/letsencrypt/live/chat.example.com/privkey.pem`

Do not overwrite another site's nginx configuration. The temporary Kaede
HTTP-only file above will be replaced with Kaede's generated configuration in
step 7.

### 6. Configure your instance

```sh
make setup
```

The wizard creates `.env`, `.kaede-kubernetes.json`, and a host nginx
configuration. For the single-server installation above, use these answers:

| Wizard question | Answer |
| --- | --- |
| Instance domain | `chat.example.com` (your actual domain) |
| Production kubeconfig | `/etc/rancher/k3s/k3s.yaml` |
| Kubernetes context | `default`, as shown by the wizard |
| Namespace | `kaede` |
| Image delivery | `local` |
| Image repository prefix | Keep `kaede.local`; no registry is contacted |
| Storage class / capacity | `local-path` / `20Gi` per volume |
| Image-pull Secret | Leave blank |
| Host nginx configuration | Yes |
| Certificate and private-key paths | The two paths from step 5 |
| Internal edge / diagnostic ports | Keep `18081` / `18082` if available |
| Storage provider | `Bundled Garage`, or your prepared S3 provider |
| Automatic updates | No for the initial installation |

Choose voice/video if you opened its ports in step 1. If setup selects different
ports because some are occupied, update your firewall/NAT rules to match the
five ports it displays; only the four transport/TURN ports need public access.
Search is available through bundled Meilisearch. Monitoring is optional.

For email, supply your SMTP/provider credentials, or choose **Disabled (no email
at signup)**. With email disabled, users register with a username and password
and cannot use email-based password recovery. GIF search, Turnstile, and push
notifications are optional; the wizard explains their required credentials.
Using the public push relay does not require hosting a relay yourself.

You do not need to copy `.env.example` or invent encryption/storage keys: setup
generates the required secrets. Keep `.env` private and back it up. Do not
change the domain or regenerate the instance keys after users have joined.

The wizard **does not** start Kaede, install nginx configuration, obtain
certificates, or open firewall ports. After it finishes, run:

```sh
make env-check
make kubernetes-check
```

Both must pass before deployment. The generated
`deploy/generated/README.txt` records the ports and services you selected.

### 7. Start Kaede and connect nginx

```sh
make deploy
make status
```

The first deployment builds and imports the images, creates persistent storage,
applies database migrations, checks the media buckets, and waits for the
application to become ready. It can take several minutes. Deployment rows
should show matching `READY` counts; completed setup Jobs are normal.

If deployment fails, read its error before retrying. Useful diagnostics are:

```sh
make logs SERVICE=api
make logs SERVICE=preflight
```

Review the generated nginx file locally; it contains the domains, certificate
paths, and a private proxy secret. Install it in place of the temporary file
from step 5:

```sh
less deploy/generated/kaede.nginx.conf
sudo install -o root -g root -m 0600 deploy/generated/kaede.nginx.conf /etc/nginx/conf.d/kaede.conf
sudo nginx -t
sudo systemctl reload nginx
```

If `nginx -t` fails, correct the reported error before reloading. Kaede does not
reload nginx for you. Keep the generated ACME challenge locations so Certbot
can renew the certificate.

Check the application and the full HTTPS route:

```sh
curl --fail http://127.0.0.1:18082/health/ready
curl --fail https://chat.example.com/.well-known/kaede/server
```

The first should return `{"status":"ready"}`; the second should return your
instance's discovery document. Substitute your selected diagnostic port if it
is not `18082`. Then open **https://chat.example.com** in a browser.

### 8. Create the owner account and test the installation

Register an account through the website. If you enabled email, complete email
verification. Grant that local account owner access, replacing `alice` with
its username:

```sh
make exec SERVICE=api COMMAND='kaede admin-grant alice --role owner'
```

Sign in and open **https://chat.example.com/administration**. You do not put an
operator token into the browser. Create a guild and a channel, send a message,
and upload and download an attachment. For voice, join the same voice channel
from two accounts, preferably on different networks, and test audio.

### 9. Set up certificate renewal and backups

For the Certbot installation above, enable its timer and install a reload hook:

```sh
sudo systemctl enable --now certbot.timer
sudo install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/kaede >/dev/null <<'SH'
#!/bin/sh
set -eu
[ "$RENEWED_LINEAGE" = /etc/letsencrypt/live/chat.example.com ] || exit 0
nginx -t
systemctl reload nginx
SH
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/kaede
sudo certbot renew --dry-run
```

If you enabled voice, also append this line to the hook so LiveKit loads the
renewed TURN certificate. Replace `kaede` if you selected another namespace:

```sh
sudo tee -a /etc/letsencrypt/renewal-hooks/deploy/kaede >/dev/null <<'SH'
/usr/local/bin/k3s kubectl -n kaede rollout restart deployment/livekit
SH
```

Restarting LiveKit can interrupt active calls. If another tool manages your
certificates, arrange the equivalent nginx reload and LiveKit restart there.

Before accepting important data, arrange backups of PostgreSQL, media storage,
cache snapshots, `.env`, and `.kaede-kubernetes.json`. Keep a copy off the server
and test restoration. Persistent Kubernetes volumes are not backups. See
[backup and restore](docs/operator.md#backup-and-restore-boundary) for maintenance
and recovery procedures; the initial deployment is now complete.

The [K3s installation reference](https://docs.k3s.io/installation/configuration),
[Ubuntu Docker packages](https://packages.ubuntu.com/noble-updates/docker-buildx),
and [Certbot guide](https://eff-certbot.readthedocs.io/en/stable/using.html)
cover the underlying host tools if you need to adapt this walkthrough.

## Updates and backups

```sh
make auto-update-run
```

The updater fast-forwards a clean checkout, builds and imports images, runs
preflight and your backup hook, and rolls out compatible application changes.
New API/gateway pods must be ready before old ones terminate. Existing
WebSockets receive a bounded grace period and then reconnect/resume; sockets
cannot transfer between processes. Failed compatible application rollouts
restore the previous application specifications.

Schema or infrastructure changes stop unattended deployment before active
workloads change. After a verified backup, deploy those explicitly:

```sh
make deploy MAINTENANCE=1
```

Maintenance deployments stop writers and may interrupt service. Single-node
storage, voice-server replacement, and host failures are not highly available.
Back up PostgreSQL, object storage, cache snapshots, `.env`, and configuration
together. See [updates and recovery](docs/operator.md#upgrade-and-rollback).
Scheduled updates remain optional: `make auto-update-enable`,
`make auto-update-disable`, and `make auto-update-status` manage the user timer.

## Development

Use a separate checkout for development. You need Docker with Buildx, Git,
Make, Python 3.11+, and a Linux systemd user session for background Tilt. You
do not need to install host K3s or run the production setup wizard. From that
checkout, run:

```sh
make tools
make dev                 # start in background, using .env when present
make dev-logs            # follow logs; Ctrl-C stops following only
make dev-down           # keeps persistent data
make dev-federation     # opt-in, separate alpha/beta cluster
make dev-federation-down
make dev-federation-logs # follow alpha/beta logs
```

Without `.env`, development uses disposable test credentials and local Garage;
voice, search, scanning, and monitoring are off. With `.env`, enabled features
are retained, but API and gateway concurrency is reduced to one. Never point
a development environment at production databases or writable production S3
buckets. Each checkout has its own cluster. Background Tilt uses a Linux systemd
user service; builds continue after the command returns. It is not enabled at
boot. For foreground operation, run:

```sh
PATH="$PWD/.kaede-tools/bin:$PATH" python3 deploy/kubernetes/dev.py up --foreground --env-file .env
```

`make dev-down` releases its CPU and RAM; do not delete the cluster or use `tilt down` to stop it, because that
can delete persistent data.

The default single-instance edge is `http://dev.localhost:28081`; an existing
`.env` uses its configured domain and `KAEDE_CADDY_HOST_PORT` with host nginx.
Alpha/beta use `http://alpha.localhost:28081` and
`http://beta.localhost:28181`. Set `DEV_HTTP_PORT` before first cluster creation
to change the host port. Browser loopback HTTP works for local development;
use host nginx and trusted TLS when testing from another device. Changing a
cluster’s published ports or host mounts requires recreating its node; export
its data before deleting the cluster.


Enable automatic pre-commit checks once per clone (requires Python 3.11+):

```sh
make hooks
```

The hook checks a temporary copy of staged files, preserving unstaged edits.
Depending on the changed paths, it runs Dart formatting, backend Ruff lint and
formatting, frontend ESLint/Prettier, Rust formatting, workflow syntax, and
release metadata checks. Backend changes also run `uv run --locked pip-audit
--skip-editable`; frontend changes run `pnpm audit --audit-level=moderate`.
These audits require network access and block commits on vulnerabilities or
audit service errors. The backend audit installs the staged locked dependencies
in the temporary copy using uv's cache. Install tools only for the components you edit:
Flutter from `mobile/.fvmrc` (on PATH or under `mobile/.fvm/flutter_sdk`),
`cd backend && uv sync --locked`, `pnpm --dir frontend install --frozen-lockfile`,
the Rust toolchain in `desktop/rust-toolchain.toml`, and actionlint 1.7.12 for
workflow edits. Missing tools or failed checks block the commit with instructions;
format files locally and stage the fixes before retrying.

`make hooks` sets this clone's `core.hooksPath`; integrate any existing custom
hooks before enabling it. Full builds, type checks, dependency audits, and
integration tests still run in CI. Use `git -c core.hooksPath=/dev/null commit`
only when deliberately bypassing the hook.

Run the standard code checks with:

```sh
make check
```

The
[operator guide](docs/operator.md#acceptance-checks) lists the focused acceptance
checks. Native client build requirements are in the
[desktop](desktop/README.md) and [mobile](mobile/README.md) READMEs.

For unprivileged LXC, see the operator guide’s K3s prerequisites. Lockfile
tooling runs as the invoking UID/GID.

## License

Kaede Chat's original code is available under the [MIT License](LICENSE),
which permits commercial use, modification, redistribution, and private forks
while requiring preservation of the copyright and license notice.

Third-party code, fonts, dependencies, and services retain their own licenses;
the MIT license does not replace their terms. Preserve their license and
NOTICE files when redistributing them, including those in `mobile/vendor/`
and the bundled fonts. Dependencies covered by MPL or LGPL retain their
applicable source-availability and other redistribution requirements; see the
[MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/). Distributed containers
and native binaries must also satisfy the licenses of the components they
include, including [FFmpeg](https://ffmpeg.org/legal.html) and libvips.

The archived Slint client remains subject to
[Slint's licensing options](https://github.com/slint-ui/slint/blob/master/LICENSE.md)
if built or distributed. Optional proprietary integrations such as the
Microsoft PhotoDNA SDK require their own authorization and are not licensed
by this repository.
