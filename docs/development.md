# Development

## Local cluster

Use a separate checkout for development. You need Docker with Buildx, Git,
Make, Python 3.11+, and a Linux systemd user session for background Tilt. You
don't need host K3s or the production setup wizard. From that checkout, run:

```sh
make tools
make dev                 # start in background, using .env when present
make dev-logs            # follow logs; Ctrl-C stops following only
make dev-down           # keeps persistent data
make dev-federation     # opt-in, separate alpha/beta cluster
make dev-federation-down
make dev-federation-logs # follow alpha/beta logs
```

Without `.env`, development uses disposable test credentials and local Garage,
with voice, search, scanning, and monitoring off. With `.env`, enabled
features stay on, but API and gateway concurrency drops to one. Never point a
development environment at production databases or writable production S3
buckets.

Each checkout gets its own k3d cluster. Background Tilt runs as a systemd user
service, so builds continue after the command returns; it doesn't start at
boot. To run in the foreground instead:

```sh
PATH="$PWD/.kaede-tools/bin:$PATH" python3 deploy/kubernetes/dev.py up --foreground --env-file .env
```

`make dev-down` frees the cluster's CPU and RAM. Don't delete the cluster or
use `tilt down` to stop it, because either can delete persistent data.

The single-instance edge is `http://dev.localhost:28081`. An existing `.env`
uses its configured domain and `KAEDE_CADDY_HOST_PORT` with host nginx.
Alpha and beta use `http://alpha.localhost:28081` and
`http://beta.localhost:28181`. Set `DEV_HTTP_PORT` before the cluster is first
created to change the host port. Browser loopback HTTP is fine locally; to
test from another device, put host nginx with a trusted certificate in front
and set the development `.env` public URLs to match. Changing a cluster's
published ports or host mounts means recreating its node, so export its data
before deleting the cluster.

Default development needs no CA. The federation TLS acceptance checks
generate a temporary CA inside their disposable cluster and never install it
into your host trust store. The k3d API and HTTP publications bind loopback,
k3d passes the `KubeletInUserNamespace` flag to its node, and dev pod and
service CIDRs don't overlap the host K3s defaults. Host nginx stays outside
both clusters.

## Reclaiming disk space

Run `make dev-prune` (or `make dev-federation-prune`) while the dev cluster is
running and Tilt is idle. It runs `crictl rmi --prune` inside this checkout's
dev node, which removes every unused containerd image there, including old
`tilt-build-*` images. Images in use by containers are kept. Dev Deployments
keep two old ReplicaSets, but that doesn't keep their images, so rebuild with
Tilt rather than rolling back to a pruned image. Production, host Docker
images, and persistent data aren't touched. Never delete containerd or
overlay2 files by hand.

## Pre-commit hooks

Enable the checks once per clone (requires Python 3.11+):

```sh
make hooks
```

This sets the clone's `core.hooksPath`, so merge any custom hooks you already
have first. The hook checks a temporary copy of the staged files and leaves
unstaged edits alone. Depending on what changed, it runs:

- Dart formatting, Rust formatting, workflow syntax, and release metadata checks.
- Backend Ruff lint and formatting, plus `uv run --locked pip-audit
  --skip-editable`, which installs the staged locked dependencies in the
  temporary copy using uv's cache.
- Frontend Oxlint, ESLint, and Oxfmt, plus `pnpm audit --audit-level=moderate`.

The audits need network access and block the commit on vulnerabilities or
audit service errors. Install tools only for the parts you edit:

- Flutter from `mobile/.fvmrc`, on PATH or under `mobile/.fvm/flutter_sdk`
- `cd backend && uv sync --locked`
- `pnpm --dir frontend install --frozen-lockfile`
- the Rust toolchain in `desktop/rust-toolchain.toml`
- actionlint 1.7.12 for workflow edits

A missing tool or failed check blocks the commit with instructions. Format
locally and stage the fixes before retrying. Full builds, type checks,
dependency audits, and integration tests still run in CI. Bypass the hook with
`git -c core.hooksPath=/dev/null commit` only when you mean to.

## Checks and dependency audits

Run the standard code checks with:

```sh
make check
```

Scan the backend and Python SDK, frontend, desktop Rust, and mobile Dart
lockfiles for known vulnerabilities with:

```sh
make osv-audit
```

This runs a pinned OSV-Scanner container. It needs Python 3, Docker, and
network access, and it doesn't compile or install dependencies. CI runs the
same target and fails when the scan fails or a non-ignored vulnerability
scores strictly above CVSS 6.0. Findings at or below 6.0, or without a score,
are reported without failing. `make audit` runs this scan before the Python and
JavaScript audits in disposable k3d clusters. The scan doesn't cover iOS
CocoaPods, Android native dependencies, SDKs, or container OS packages.

To ignore a package, add its exact name and a nonempty reason under its OSV
ecosystem in [.github/osv-ignored-packages.json](../.github/osv-ignored-packages.json),
following the existing `crates.io` / `glib` entry. An exception covers every
version and advisory for that package. Its findings still show as `IGNORED`
with score, advisory IDs, and reason, but don't fail the job. Remove the entry
to resume enforcement, or use `{}` for no ignores. Invalid configuration and
scanner errors still fail the job.

Lockfile tooling runs as the invoking UID/GID. Native client build
requirements are in the [desktop](../desktop/README.md) and
[mobile](../mobile/README.md) READMEs.

## Acceptance checks

Run `make tools` first, then run these from the repository root. Each target
creates a disposable k3d cluster and removes it on exit. They don't publish
application ports or validate an existing production deployment.

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

`make check` covers backend and frontend lint, types, and the web build.
`make test` runs backend and frontend tests, and `make audit` checks locked
dependencies. Live-device media quality and platform signing still need the
checks in the client release guides.
