export PATH := $(CURDIR)/.kaede-tools/bin:$(PATH)
DESKTOP_RUST_VERSION := $(shell sed -n 's/^channel = "\(.*\)"/\1/p' desktop/rust-toolchain.toml)
ENV_FILE ?= .env
CONFIG_GUARD := test ! -e .kaede-setup.in-progress || { echo 'setup transaction is incomplete; rerun make setup' >&2; exit 2; }
KUBE := python3 deploy/kubernetes/manage.py
KUBE_ARGS := --env-file "$(ENV_FILE)" $(if $(KUBE_CONFIG),--config "$(KUBE_CONFIG)",)

.PHONY: dev-logs dev-federation-logs
.PHONY: help setup tools deploy status logs exec dev dev-cluster dev-down dev-federation dev-federation-down kubernetes-check env-check legacy-down
.PHONY: hooks lock generate search-rebuild auto-update-enable auto-update-disable auto-update-status auto-update-run auto-update-check update-notices
.PHONY: check test audit migration migration-check identity-check chat-check media-check voice-check release-check federation-check federation-tls-check nginx-check
.PHONY: mobile-check desktop-check desktop-lint desktop-test desktop-build desktop-dev
help:
	@echo "setup                 Configure production Kubernetes, storage, and host nginx"
	@echo "tools                 Install pinned kubectl, k3d, and Tilt into this checkout (no sudo)"
	@echo "deploy                Build/import images and deploy (MAINTENANCE=1 permits migrations)"
	@echo "status / logs         Show production status / logs (SERVICE=api)"
	@echo "exec                  Run a production command (SERVICE=worker COMMAND='...')"
	@echo "dev / dev-down        Start Tilt in background / stop Tilt and cluster, retaining data"
	@echo "dev-logs              Follow development logs (Ctrl-C stops following)"
	@echo "dev-federation-logs   Follow alpha/beta logs"
	@echo "dev-federation        Start the separate alpha/beta development cluster"
	@echo "dev-federation-down   Stop the alpha/beta cluster, retaining data"
	@echo "env-check             Validate the operator environment"
	@echo "kubernetes-check      Test deployment configuration and manifest contracts"
	@echo "auto-update-run       Fetch and roll out a compatible production update"
	@echo "auto-update-enable / auto-update-disable / auto-update-status"
	@echo "check / test / audit  Run application checks in disposable k3d clusters"
	@echo "migration             Generate a migration with m='description'"
	@echo "migration-check / identity-check / chat-check / media-check / voice-check"
	@echo "release-check / federation-check / federation-tls-check"
	@echo "legacy-down           Stop a named legacy Compose project (PROJECT=name); keep volumes"
	@echo "hooks / lock / generate / nginx-check / search-rebuild"
	@echo "desktop-check / desktop-lint / desktop-test / desktop-build / desktop-dev / mobile-check"

hooks:
	git config --local core.hooksPath .githooks

tools:
	python3 deploy/kubernetes/install-tools.py

setup:
	./setup.sh $(SETUP_ARGS)

deploy:
	@$(CONFIG_GUARD)
	$(KUBE) deploy $(KUBE_ARGS) $(if $(filter 1 true yes,$(MAINTENANCE)),--maintenance,) $(if $(REVISION),--revision "$(REVISION)",)

status:
	$(KUBE) status $(KUBE_ARGS)

logs:
	$(KUBE) logs $(KUBE_ARGS) $(or $(SERVICE),api)

exec:
	$(KUBE) exec $(KUBE_ARGS) $(or $(SERVICE),api) $(COMMAND)

search-rebuild:
	$(KUBE) exec $(KUBE_ARGS) worker python -m scripts.rebuild_search $(if $(filter 1 true yes,$(RESET)),--reset-index,)

auto-update-enable:
	./deploy/install-auto-update.sh enable

auto-update-disable:
	./deploy/install-auto-update.sh disable

auto-update-status:
	./deploy/install-auto-update.sh status

update-notices:
	python3 deploy/kubernetes/update_notices.py $(KUBE_ARGS)

auto-update-run:
	./deploy/auto-update.sh run-now

auto-update-check:
	./deploy/tests/test_auto_update.sh

env-check:
	@$(CONFIG_GUARD)
	python3 deploy/validate_deploy_env.py --file "$(ENV_FILE)" --file-only

kubernetes-check:
	python3 -m unittest discover -s deploy/tests -p 'test_*.py'
	bash deploy/tests/test_setup_project_name.sh
	bash -n setup.sh deploy/auto-update.sh deploy/kubernetes/import-image.sh

check test audit migration-check identity-check chat-check media-check voice-check release-check federation-check federation-tls-check:
	python3 deploy/kubernetes/check.py $@

migration:
	@test -n "$(m)" || { echo 'usage: make migration m="describe the change"' >&2; exit 2; }
	REVISION_MESSAGE="$(m)" python3 deploy/kubernetes/check.py migration

dev:
	python3 deploy/kubernetes/dev.py up --env-file "$(ENV_FILE)"

dev-logs:
	python3 deploy/kubernetes/dev.py logs

dev-federation-logs:
	python3 deploy/kubernetes/dev.py logs --federation

dev-cluster:
	python3 deploy/kubernetes/dev.py cluster --env-file "$(ENV_FILE)"

dev-down:
	python3 deploy/kubernetes/dev.py down

dev-federation:
	python3 deploy/kubernetes/dev.py up --federation

dev-federation-down:
	python3 deploy/kubernetes/dev.py down --federation

legacy-down:
	python3 deploy/kubernetes/legacy.py stop --project "$(PROJECT)"

lock:
	docker build --target tooling -t kaede-backend-tooling backend
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache -v "$(CURDIR)/backend:/workspace" -w /workspace kaede-backend-tooling uv lock
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e COREPACK_HOME=/tmp/corepack -v "$(CURDIR)/frontend:/workspace" -w /workspace node:22.23.1-bookworm-slim sh -c "corepack pnpm install --lockfile-only"

generate:
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -v "$(CURDIR):/workspace" -w /workspace/backend python:3.12.13-slim python -m scripts.generate_protocol

mobile-check:
	cd mobile && flutter pub get --enforce-lockfile
	cd mobile && flutter gen-l10n
	cd mobile && dart format --output=none --set-exit-if-changed lib test
	cd mobile && flutter analyze
	cd mobile && TZ=America/Los_Angeles flutter test

desktop-check:
	cargo +$(DESKTOP_RUST_VERSION) fmt --all --manifest-path desktop/Cargo.toml -- --check
	test -f frontend/build/index.html || { echo 'frontend/build is missing; run pnpm --dir frontend build' >&2; exit 2; }
	cargo +$(DESKTOP_RUST_VERSION) check --locked --manifest-path desktop/Cargo.toml -p kaede-tauri

desktop-lint:
	cargo +$(DESKTOP_RUST_VERSION) clippy --locked --keep-going --manifest-path desktop/Cargo.toml \
		-p kaede-protocol -p kaede-core -p kaede-platform -p kaede-api \
		-p kaede-cache -p kaede-auth -p kaede-media -p kaede-gateway \
		-p kaede-capture -p kaede-audio -p kaede-voice -p kaede-turnstile \
		-p kaede-e2ee -p kaede-e2ee-ffi \
		-p kaede-tauri --all-targets -- -D warnings

desktop-test:
	cargo +$(DESKTOP_RUST_VERSION) test --locked --manifest-path desktop/Cargo.toml \
		-p kaede-protocol -p kaede-core -p kaede-platform -p kaede-api \
		-p kaede-cache -p kaede-auth -p kaede-media -p kaede-app \
		-p kaede-gateway -p kaede-capture -p kaede-audio -p kaede-voice \
		-p kaede-e2ee -p kaede-e2ee-ffi -p kaede-tauri

desktop-build:
	pnpm --dir frontend install --frozen-lockfile
	pnpm --dir frontend build
	cd desktop/tauri && cargo +$(DESKTOP_RUST_VERSION) tauri build --config src-tauri/tauri.conf.json

desktop-dev:
	pnpm --dir frontend dev --host 127.0.0.1 & \
	frontend_pid=$$!; trap 'kill $$frontend_pid 2>/dev/null || true' EXIT INT TERM; \
	cd desktop/tauri && cargo +$(DESKTOP_RUST_VERSION) tauri dev --config src-tauri/tauri.dev.conf.json

nginx-check:
	@set -eu; \
	tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmp"' EXIT INT TERM; \
	openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
		-subj /CN=chat.example.com \
		-keyout "$$tmp/privkey.pem" -out "$$tmp/fullchain.pem" >/dev/null 2>&1; \
	docker run --rm \
		-v "$(CURDIR)/deploy/nginx/kaede.conf.example:/etc/nginx/conf.d/kaede.conf:ro" \
		-v "$$tmp:/etc/letsencrypt/live/chat.example.com:ro" \
		nginx:1.29.0-alpine nginx -t
