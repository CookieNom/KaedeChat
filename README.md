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

These steps install **one production instance on an Ubuntu 24.04 server**
with K3s, host nginx, and locally built images. You don't need a container
registry. Run commands as your normal login user with sudo access, not from a
root shell. Go through the steps in order and stop if a command fails.

This is for a **new installation**. If you already have a Compose instance,
follow the [data migration procedure](docs/operator.md#migrate-an-existing-compose-instance)
instead of creating a second instance with the same domain.

### 1. Choose your domain and storage

The examples use `chat.example.com`. **Replace it with your own domain in
every command and wizard answer.** Pick it before anyone registers: it becomes
part of account addresses and can't simply be renamed later.

The simplest choice for file storage is **Bundled Garage**, which creates and
manages private buckets on this server. Create these DNS records, both pointing
to the server's public IP:

| Name | Purpose |
| --- | --- |
| `chat.example.com` | Website, API, federation, and voice signaling |
| `media.chat.example.com` | Uploads and downloads when using bundled Garage |

Use DNS-only records if your DNS provider also offers an HTTP proxy. Add AAAA
records only if IPv6 actually reaches this server. Behind NAT, forward the
public ports to it.

Allow inbound TCP **80 and 443** in your host, provider, or Proxmox firewall,
and keep SSH open to your administration network. Don't publicly open the
Kubernetes API (TCP 6443), Flannel (UDP 8472), database ports, or the
Kubernetes NodePort range. If your provider or hypervisor already enforces
these rules, you don't need a host firewall as well.

If you enable voice, also allow the ports setup selects. The defaults are:

| Protocol | Ports | Purpose |
| --- | --- | --- |
| TCP | `7881`, `5349` | Voice transport and TURN over TLS |
| UDP | `7882`, `13478` | Voice transport and TURN |

The LiveKit control port (`7880` by default) and Kaede's loopback ports
(`18081` and `18082`) don't need public access.

**Using an existing S3 service instead?** Select your provider or **Generic
S3-compatible** in setup. Before you start, prepare the endpoint, region,
credentials, three private buckets (attachments, derived media, and remote
cache), and the CORS rules described under
[object storage](docs/operator.md#object-storage). A separately hosted Garage
or other local S3 service works too, as long as pods can reach its endpoint.
You don't need the `media.chat.example.com` record when you use the
provider's HTTPS media endpoint.

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

If Docker Engine and Buildx are already installed from Docker's own
repository, keep that installation and leave `docker.io docker-buildx` out of
the package command.

**Log out and reconnect now** so the new group memberships take effect. Then
check Docker access without sudo:

```sh
docker info
docker buildx version
id -nG
```

The last command must list `docker` and `k3s-admin`. Everything else is built
into images, so you don't need Node.js, Rust, or PostgreSQL on the host.

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

Wait for the API, the node, and the system services:

```sh
for attempt in $(seq 1 90); do
  sudo k3s kubectl get nodes >/dev/null 2>&1 && break
  sleep 2
done
sudo k3s kubectl wait --for=condition=Ready nodes --all --timeout=180s
sudo k3s kubectl wait --for=condition=Ready pods --all -n kube-system --timeout=180s
```

Don't continue until both readiness commands succeed. If startup fails, check
`sudo journalctl -u k3s -n 100 --no-pager`.

**Proxmox or unprivileged LXC:** Docker nesting, a writable cgroup v2
hierarchy, and the host kernel modules K3s needs must already be available.
If kubelet reports that it can't use `/dev/kmsg` in the user namespace, append
this to `/etc/rancher/k3s/config.yaml`, run `sudo systemctl restart k3s`, and
repeat the readiness checks:

```yaml
kubelet-arg:
  - feature-gates=KubeletInUserNamespace=true
```

Diagnose the actual failure before weakening the container's isolation. The
Kubernetes [user-namespace requirements](https://kubernetes.io/docs/tasks/administer-cluster/kubelet-in-userns/)
explain the flag.

On an existing Kubernetes cluster, skip these commands. You'll need a readable
kubeconfig, permission to create the instance's resources, and a working
storage class. For more than one node or a remote cluster, choose registry
image delivery in step 6 instead of local imports. The rest of this guide
assumes the single-node K3s installation above.

### 4. Download Kaede and enable local image imports

Clone into a permanent location and run the remaining `make` commands from
there:

```sh
git clone https://github.com/CookieNom/KaedeChat.git
cd KaedeChat
make tools
.kaede-tools/bin/kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get nodes
```

`make tools` installs pinned, checksum-verified command-line tools into the
checkout. The node should show `Ready` without sudo. If the kubeconfig is
unreadable, check `id -nG` and reconnect after joining `k3s-admin`. Don't make
cluster credentials world-readable; they grant administrative access.

Docker builds Kaede's images, but K3s keeps its own image store. Install the
helper that imports them:

```sh
sudo install -o root -g root -m 0755 deploy/kubernetes/import-image.sh /usr/local/sbin/kaede-import-image
whoami
sudo visudo -f /etc/sudoers.d/kaede-image-import
```

In the editor, add this line with `YOUR_LOGIN` replaced by the name `whoami`
printed. Keep the final `""`, which allows the helper to run only without
arguments. Save and exit:

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

`visudo` must report `parsed OK`, and the last command must list the helper
without asking for a password. The helper reads an image archive from stdin
and imports it into K3s's `k8s.io` namespace. It's the only privileged
operation that deployments and updates use, so keep it root-owned and review
changes before reinstalling it.

### 5. Obtain your HTTPS certificate

If you already manage a certificate for these hostnames, keep it and use its
absolute certificate and key paths in step 6. Otherwise, set up Certbot
validation on the new nginx installation. DNS and inbound port 80 from step 1
must work first.

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

With external S3 and no local media hostname, remove `media.chat.example.com`
from `server_name` and drop its `-d` argument. Certbot asks for a contact
email and agreement to its terms. The resulting paths are:

- Certificate: `/etc/letsencrypt/live/chat.example.com/fullchain.pem`
- Private key: `/etc/letsencrypt/live/chat.example.com/privkey.pem`

Don't overwrite another site's nginx configuration. Step 7 replaces this
temporary HTTP-only file with Kaede's generated configuration.

### 6. Configure your instance

```sh
make setup
```

The wizard writes `.env`, `.kaede-kubernetes.json`, and a host nginx
configuration. It generates every secret, so you don't need to copy
`.env.example` or invent keys. For the single-server installation above, use
these answers:

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
| Federation admission mode | `open` |
| Automatic updates | No for the initial installation (setup still installs a notification-only timer) |
| Host nginx configuration | Yes |
| Certificate and private-key paths | The two paths from step 5 |
| Internal edge / diagnostic ports | Keep `18081` / `18082` if available |
| Storage provider | `Bundled Garage`, or your prepared S3 provider |
| Microsoft PhotoDNA matching | No, unless you hold a license |
| Write this configuration? | Yes (this prompt defaults to No) |

Choose voice and video if you opened their ports in step 1. If setup picks
different ports because some are in use, update your firewall and NAT rules
to match. Only the four transport and TURN ports need public access. Search
runs on the bundled Meilisearch, and monitoring is optional.

For email, enter your SMTP or provider credentials, or choose **Disabled (no
email at signup)**. Users then register with only a username and password and
can't recover a forgotten password by email. GIF search, Turnstile, and push
notifications are optional, and the wizard explains the credentials each one
needs. The public push relay works without hosting a relay yourself. The
[setup wizard reference](docs/deployment-wizard.md) covers every choice.

Keep `.env` private and back it up. Don't change the domain or regenerate the
instance keys after users have joined.

The wizard doesn't start Kaede, install the nginx file, obtain certificates,
or open firewall ports. When it finishes, run:

```sh
make env-check
make kubernetes-check
```

`make env-check` validates `.env`. `make kubernetes-check` tests this
checkout's deployment code without reading `.env` or contacting the cluster.
Both must pass. `deploy/generated/README.txt` records your main selections and
next steps.

### 7. Start Kaede and connect nginx

```sh
make deploy
make status
```

The first deployment builds and imports the images, creates persistent
storage, applies database migrations, checks the media buckets, and waits for
the application to become ready. This can take several minutes. Deployment
rows should show matching `READY` counts; completed setup Jobs are normal.

If deployment fails, read its error before retrying. These show the most
useful logs:

```sh
make logs SERVICE=api
make logs SERVICE=preflight
```

Review the generated nginx file, which contains your domains, certificate
paths, and a private proxy secret. Then install it in place of the temporary
file from step 5:

```sh
less deploy/generated/kaede.nginx.conf
sudo install -o root -g root -m 0600 deploy/generated/kaede.nginx.conf /etc/nginx/conf.d/kaede.conf
sudo nginx -t
sudo systemctl reload nginx
```

If `nginx -t` fails, fix the reported error before reloading. Kaede never
reloads nginx for you. Keep the generated ACME challenge locations so Certbot
can renew the certificate.

Check the application and the full HTTPS route:

```sh
curl --fail http://127.0.0.1:18082/health/ready
curl --fail https://chat.example.com/.well-known/kaede/server
```

The first should return `{"status":"ready"}` and the second your instance's
discovery document. Use your diagnostic port if it isn't `18082`. Then open
**https://chat.example.com** in a browser.

### 8. Create the owner account and test the installation

Register an account on the website and, if email is enabled, verify it. Grant
that account owner access, replacing `alice` with its username:

```sh
make exec SERVICE=api COMMAND='kaede admin-grant alice --role owner'
```

Sign in and open **https://chat.example.com/administration**. There's no
operator token to paste into the browser. Create a guild and a channel, send
a message, and upload and download an attachment. For voice, join the same
voice channel from two accounts, ideally on different networks, and test
audio.

### 9. Set up certificate renewal and backups

For the Certbot setup above, enable its timer and install a reload hook:

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

With voice enabled, also append this line so LiveKit loads the renewed TURN
certificate. Replace `kaede` if you chose another namespace:

```sh
sudo tee -a /etc/letsencrypt/renewal-hooks/deploy/kaede >/dev/null <<'SH'
/usr/local/bin/k3s kubectl -n kaede rollout restart deployment/livekit
SH
```

Restarting LiveKit can interrupt active calls. If another tool manages your
certificates, set up the same nginx reload and LiveKit restart there.

Before you accept important data, set up backups and test a restore. The
[backup guide](docs/operator.md#backup-and-restore-boundary) lists what to
back up and how. Persistent Kubernetes volumes are not backups. That completes
the initial deployment.

To adapt these steps, see the
[K3s installation reference](https://docs.k3s.io/installation/configuration),
[Ubuntu Docker packages](https://packages.ubuntu.com/noble-updates/docker-buildx),
and [Certbot guide](https://eff-certbot.readthedocs.io/en/stable/using.html).

## Updates and backups

To install the latest compatible update by hand, run:

```sh
make auto-update-run
```

Additive migrations and application changes roll out online. Other schema or
infrastructure changes stop the updater before anything changes; after a
verified backup, deploy them with:

```sh
make deploy MAINTENANCE=1
```

Scheduled updates are optional and managed with `make auto-update-enable`,
`make auto-update-disable`, and `make auto-update-status`. See
[upgrade and rollback](docs/operator.md#upgrade-and-rollback) for what each
path does and how to recover, and
[backup and restore](docs/operator.md#backup-and-restore-boundary) for the
recovery set.

## Development

Use a separate checkout, then run `make tools` and `make dev`. The
[development guide](docs/development.md) covers the local cluster,
pre-commit hooks, dependency audits, and acceptance checks.

## License

Kaede Chat's original code is available under the [MIT License](LICENSE),
which permits commercial use, modification, redistribution, and private forks
as long as the copyright and license notice are kept.

Third-party code, fonts, dependencies, and services keep their own licenses;
the MIT license doesn't replace their terms. Preserve their license and NOTICE
files when redistributing them, including those in `mobile/vendor/` and the
bundled fonts. Dependencies under MPL or LGPL keep their source-availability
and other redistribution requirements; see the
[MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/). Distributed containers
and native binaries must also satisfy the licenses of the components they
include, including [FFmpeg](https://ffmpeg.org/legal.html) and libvips.

The archived Slint client remains subject to
[Slint's licensing options](https://github.com/slint-ui/slint/blob/master/LICENSE.md)
if built or distributed. Optional proprietary integrations such as the
Microsoft PhotoDNA SDK need their own authorization and are not licensed by
this repository.
