ENV_FILE ?= .env
OPERATOR_ENV_FILE := $(abspath $(ENV_FILE))
GENERATED_COMPOSE := $(if $(wildcard deploy/compose.generated.yml),-f deploy/compose.generated.yml,)
CONFIG_GUARD := test ! -e .kaede-setup.in-progress || { echo 'setup transaction is incomplete; inspect .kaede-setup.in-progress' >&2; exit 2; }
COMPOSE := $(CONFIG_GUARD); KAEDE_OPERATOR_ENV_FILE="$(OPERATOR_ENV_FILE)" docker compose --env-file "$(ENV_FILE)" -f deploy/compose.yml $(GENERATED_COMPOSE)
DEV_COMPOSE := docker compose -f deploy/compose.dev.yml
VALIDATION_RUN_ID := $(shell date +%s%N)
FEDERATION_COMPOSE := docker compose --project-name kaede-federation-validation-$(VALIDATION_RUN_ID) -f deploy/compose.dev.yml
FEDERATION_TLS_COMPOSE := docker compose --project-name kaede-federation-tls-validation-$(VALIDATION_RUN_ID) -f deploy/compose.dev.yml -f deploy/compose.federation-tls.yml
VALIDATION_COMPOSE := ALLOW_NONPRODUCTION_DEPLOYMENT=true KAEDE_OPERATOR_ENV_FILE="$(abspath deploy/.env.schema)" docker compose --env-file deploy/.env.schema -f deploy/compose.yml
CHECK_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-check-validation-$(VALIDATION_RUN_ID)
TEST_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-test-validation-$(VALIDATION_RUN_ID)
MIGRATION_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-migration-validation-$(VALIDATION_RUN_ID)
IDENTITY_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-identity-validation-$(VALIDATION_RUN_ID)
CHAT_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-chat-validation-$(VALIDATION_RUN_ID)
AUDIT_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-audit-validation-$(VALIDATION_RUN_ID)
MEDIA_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-media-validation-$(VALIDATION_RUN_ID)
VOICE_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-voice-validation-$(VALIDATION_RUN_ID)
RELEASE_COMPOSE := $(VALIDATION_COMPOSE) --project-name kaede-release-validation-$(VALIDATION_RUN_ID)

.PHONY: help setup lock generate check test audit desktop-check desktop-lint desktop-test desktop-build desktop-dev env-check migration migration-check identity-check chat-check media-check voice-check release-check federation-check federation-tls-check compose-check generated-compose-check nginx-check dev dev-down
help:
	@echo "setup            Run the interactive, configuration-only deployment wizard"
	@echo "lock             Generate uv and pnpm lockfiles in one-off containers"
	@echo "generate         Regenerate shared TypeScript and Rust protocol constants"
	@echo "check            Run lint, type, codegen, and unit checks in containers"
	@echo "test             Run backend and frontend tests in containers"
	@echo "audit            Check locked Python and JavaScript dependencies for advisories"
	@echo "desktop-check    Format and compile the portable native desktop workspace"
	@echo "desktop-lint     Run strict Clippy checks across all portable desktop targets"
	@echo "desktop-test     Run desktop protocol, state, platform, auth, and media tests"
	@echo "desktop-build    Build the bundled frontend and native Tauri application"
	@echo "desktop-dev      Run the Tauri application against the frontend dev server"
	@echo "env-check        Validate ENV_FILE and run the production preflight in isolation"
	@echo "migration        Generate an Alembic revision with m=\"description\""
	@echo "migration-check  Run Alembic up/down/up against disposable PostgreSQL"
	@echo "identity-check   Exercise M1 identity against disposable PostgreSQL and Dragonfly"
	@echo "chat-check       Exercise the implemented M2 chat and gateway acceptance path"
	@echo "media-check      Exercise M4 Garage, ClamAV, media, and webhook lifecycles"
	@echo "voice-check      Exercise M5 Dragonfly state and an isolated LiveKit server"
	@echo "release-check    Exercise M6 rate limits, warmup, fanout, and amplification"
	@echo "federation-check Exercise the isolated M3 Alpha/Beta federation gate"
	@echo "federation-tls-check Exercise M3 through an isolated nginx/TLS/Caddy edge"
	@echo "compose-check    Render production and development Compose configurations"
	@echo "generated-compose-check Validate wizard output when it is present"
	@echo "nginx-check      Validate the example host-nginx configuration with a temporary certificate"
	@echo "dev              Start both local development instances (requires explicit operator action)"

setup:
	./setup.sh $(SETUP_ARGS)

lock:
	docker build --target tooling -t kaede-backend-tooling backend
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache -v "$(CURDIR)/backend:/workspace" -w /workspace kaede-backend-tooling uv lock
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e COREPACK_HOME=/tmp/corepack -v "$(CURDIR)/frontend:/workspace" -w /workspace node:22.23.1-bookworm-slim sh -c "corepack pnpm install --lockfile-only"

generate:
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -v "$(CURDIR):/workspace" -w /workspace/backend python:3.12.13-slim python -m scripts.generate_protocol

check:
	@set -eu; \
	trap '$(CHECK_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(CHECK_COMPOSE) run --rm --no-deps --build backend-check; \
	$(CHECK_COMPOSE) run --rm --no-deps --build frontend-check

test:
	@set -eu; \
	trap '$(TEST_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(TEST_COMPOSE) run --rm --no-deps --build backend-test; \
	$(TEST_COMPOSE) run --rm --no-deps --build frontend-test

audit:
	@set -eu; \
	trap '$(AUDIT_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(AUDIT_COMPOSE) run --rm --no-deps --build backend-check pip-audit --skip-editable; \
	$(AUDIT_COMPOSE) run --rm --no-deps --build frontend-check pnpm audit --audit-level=moderate

desktop-check:
	cargo +1.92.0 fmt --all --manifest-path desktop/Cargo.toml -- --check
	test -f frontend/build/index.html || { echo 'frontend/build is missing; run pnpm --dir frontend build' >&2; exit 2; }
	cargo +1.92.0 check --locked --manifest-path desktop/Cargo.toml -p kaede-tauri

desktop-lint:
	cargo +1.92.0 clippy --locked --manifest-path desktop/Cargo.toml \
		-p kaede-protocol -p kaede-core -p kaede-platform -p kaede-api \
		-p kaede-cache -p kaede-auth -p kaede-media -p kaede-gateway \
		-p kaede-capture -p kaede-audio -p kaede-voice -p kaede-turnstile \
		-p kaede-tauri --all-targets -- -D warnings

desktop-test:
	cargo +1.92.0 test --locked --manifest-path desktop/Cargo.toml \
		-p kaede-protocol -p kaede-core -p kaede-platform -p kaede-api \
		-p kaede-cache -p kaede-auth -p kaede-media -p kaede-app \
		-p kaede-gateway -p kaede-capture -p kaede-audio -p kaede-voice

desktop-build:
	pnpm --dir frontend install --frozen-lockfile
	pnpm --dir frontend build
	cd desktop/tauri && cargo +1.92.0 tauri build --config src-tauri/tauri.conf.json

desktop-dev:
	pnpm --dir frontend dev --host 127.0.0.1 & \
	frontend_pid=$$!; trap 'kill $$frontend_pid 2>/dev/null || true' EXIT INT TERM; \
	cd desktop/tauri && cargo +1.92.0 tauri dev --config src-tauri/tauri.dev.conf.json

env-check:
	@test -f "$(ENV_FILE)" || { echo 'missing ENV_FILE: $(ENV_FILE)' >&2; exit 2; }
	docker run --rm \
		-v "$(OPERATOR_ENV_FILE):/run/kaede/operator.env:ro" \
		-v "$(CURDIR)/deploy/validate_deploy_env.py:/run/kaede/validate_deploy_env.py:ro" \
		python:3.12.13-slim python /run/kaede/validate_deploy_env.py \
		--file /run/kaede/operator.env --file-only
	$(COMPOSE) run --rm --no-deps --build preflight

migration-check:
	@set -eu; \
	trap '$(MIGRATION_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(MIGRATION_COMPOSE) --profile validation up -d --wait --build postgres; \
	$(MIGRATION_COMPOSE) run --rm --no-deps --build migration-check

migration:
	@test -n "$(m)" || { echo 'usage: make migration m="describe the change"' >&2; exit 2; }
	@set -eu; \
	trap '$(MIGRATION_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(MIGRATION_COMPOSE) --profile validation up -d --wait --build postgres; \
	$(MIGRATION_COMPOSE) run --rm --no-deps --build --user "$$(id -u):$$(id -g)" \
		-e HOME=/tmp -e REVISION_MESSAGE="$(m)" migration-check \
		sh -ec 'alembic upgrade head && alembic revision --autogenerate -m "$$REVISION_MESSAGE"'

identity-check:
	@set -eu; \
	trap '$(IDENTITY_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(IDENTITY_COMPOSE) --profile validation up -d --wait --build postgres dragonfly; \
	$(IDENTITY_COMPOSE) run --rm --no-deps --build identity-check

chat-check:
	@set -eu; \
	trap '$(CHAT_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(CHAT_COMPOSE) --profile validation up -d --wait --build postgres dragonfly garage worker; \
	$(CHAT_COMPOSE) run --rm --no-deps --build chat-check

media-check:
	@set -eu; \
	trap '$(MEDIA_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(MEDIA_COMPOSE) --profile validation up -d --wait --build postgres dragonfly garage storage-init clamav; \
	$(MEDIA_COMPOSE) run --rm --no-deps --build media-check

voice-check:
	@set -eu; \
	trap '$(VOICE_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(VOICE_COMPOSE) --profile validation up -d --wait --build dragonfly livekit-validation; \
	$(VOICE_COMPOSE) run --rm --no-deps --build voice-check

release-check:
	@set -eu; \
	trap '$(RELEASE_COMPOSE) --profile validation down -v' EXIT INT TERM; \
	$(RELEASE_COMPOSE) --profile validation up -d --wait --build postgres dragonfly; \
	$(RELEASE_COMPOSE) run --rm --no-deps --build release-check

federation-check:
	@set -eu; \
	cleanup() { status=$$?; if [ $$status -ne 0 ]; then $(FEDERATION_COMPOSE) logs --no-color --tail=240 alpha-api beta-api || true; fi; $(FEDERATION_COMPOSE) --profile validation down -v; exit $$status; }; \
	trap cleanup EXIT INT TERM; \
	$(FEDERATION_COMPOSE) up -d --wait alpha-postgres alpha-dragonfly beta-postgres beta-dragonfly; \
	$(FEDERATION_COMPOSE) run --rm --no-deps --build alpha-api sh -ec 'alembic upgrade head && kaede bootstrap'; \
	$(FEDERATION_COMPOSE) run --rm --no-deps --build beta-api sh -ec 'alembic upgrade head && kaede bootstrap'; \
	$(FEDERATION_COMPOSE) up -d --wait --build alpha-api beta-api alpha-gateway beta-gateway alpha-worker beta-worker alpha-scheduler beta-scheduler; \
	$(FEDERATION_COMPOSE) --profile validation run --rm --no-deps --build federation-check

federation-tls-check:
	@set -eu; \
	cleanup() { status=$$?; if [ $$status -ne 0 ]; then $(FEDERATION_TLS_COMPOSE) logs --no-color --tail=240 alpha-api beta-api tls-edge || true; fi; $(FEDERATION_TLS_COMPOSE) --profile validation down -v; exit $$status; }; \
	trap cleanup EXIT INT TERM; \
	$(FEDERATION_TLS_COMPOSE) up -d --wait alpha-postgres alpha-dragonfly beta-postgres beta-dragonfly; \
	$(FEDERATION_TLS_COMPOSE) run --rm --no-deps --build tls-init; \
	$(FEDERATION_TLS_COMPOSE) run --rm --no-deps --build alpha-api sh -ec 'alembic upgrade head && kaede bootstrap'; \
	$(FEDERATION_TLS_COMPOSE) run --rm --no-deps --build beta-api sh -ec 'alembic upgrade head && kaede bootstrap'; \
	$(FEDERATION_TLS_COMPOSE) up -d --wait --build alpha-api beta-api alpha-gateway beta-gateway alpha-worker beta-worker alpha-scheduler beta-scheduler alpha-caddy beta-caddy tls-edge; \
	$(FEDERATION_TLS_COMPOSE) --profile validation run --rm --no-deps --build federation-check

compose-check:
	docker run --rm -v "$(CURDIR)/deploy:/deploy:ro" -w /deploy \
		python:3.12.13-slim python -m unittest discover -s tests -p 'test_validate_*.py'
	@docker compose --env-file .env.example -f deploy/compose.yml config --format json | \
		docker run --rm -i -v "$(CURDIR)/deploy/validate_compose.py:/validate_compose.py:ro" \
		python:3.12.13-slim python /validate_compose.py
	@docker compose --profile observability --env-file .env.example -f deploy/compose.yml config --format json | \
		docker run --rm -i -v "$(CURDIR)/deploy/validate_compose.py:/validate_compose.py:ro" \
		python:3.12.13-slim python /validate_compose.py --observability
	@KAEDE_OPERATOR_ENV_FILE="$(abspath deploy/.env.schema)" KAEDE_VOICE_ENABLED=true \
		docker compose --profile voice --env-file deploy/.env.schema -f deploy/compose.yml config --format json | \
		docker run --rm -i --network none \
		-v "$(CURDIR)/deploy/validate_compose.py:/validate_compose.py:ro" \
		python:3.12.13-slim python /validate_compose.py --voice
	@KAEDE_OPERATOR_ENV_FILE="$(abspath deploy/.env.schema)" KAEDE_VOICE_ENABLED=true \
		LIVEKIT_CONTROL_PORT=7890 LIVEKIT_RTC_TCP_PORT=7891 LIVEKIT_RTC_UDP_PORT=7892 \
		LIVEKIT_TURN_TLS_PORT=5350 KAEDE_TURN_UDP_PORT=13489 \
		KAEDE_VOICE_LIVEKIT_URL=http://host.docker.internal:7890 \
		docker compose --profile voice --env-file deploy/.env.schema -f deploy/compose.yml config --format json | \
		docker run --rm -i --network none \
		-v "$(CURDIR)/deploy/validate_compose.py:/validate_compose.py:ro" \
		python:3.12.13-slim python /validate_compose.py --voice
	docker compose --env-file deploy/reference.env.example -f deploy/compose.yml config --quiet
	@docker compose --env-file .env.s3.example -f deploy/compose.yml -f deploy/compose.s3.yml config --format json | \
		docker run --rm -i -v "$(CURDIR)/deploy/validate_compose.py:/validate_compose.py:ro" \
		python:3.12.13-slim python /validate_compose.py --external-s3
	@KAEDE_DEV_HTTPS_PORT=29443 docker compose -f deploy/compose.dev.yml config --format json | \
		docker run --rm -i -v "$(CURDIR)/deploy/validate_compose.py:/validate_compose.py:ro" \
		python:3.12.13-slim python /validate_compose.py --development --dev-https-port 29443
	docker compose -f deploy/compose.dev.yml -f deploy/compose.federation-tls.yml config --quiet
	docker run --rm -e KAEDE_DOMAIN=chat.example.com -e KAEDE_PROXY_SECRET=01234567890123456789012345678901 -e KAEDE_EDGE_SECRET=abcdefghijklmnopqrstuvwxyz012345 -e KAEDE_VOICE_ENABLED=false -v "$(CURDIR)/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.4-alpine caddy validate --config /etc/caddy/Caddyfile
	docker run --rm -e KAEDE_PROXY_SECRET=kaede-development-proxy-secret-00001 -e KAEDE_DEV_HTTPS_PORT=18443 -v "$(CURDIR)/deploy/Caddyfile.dev:/etc/caddy/Caddyfile:ro" caddy:2.11.4-alpine caddy validate --config /etc/caddy/Caddyfile
	@config="$$(docker compose --profile voice --env-file deploy/.env.schema -f deploy/compose.yml config --format json | docker run --rm -i python:3.12.13-slim python -c 'import json, sys; print(json.load(sys.stdin)["services"]["livekit"]["environment"]["LIVEKIT_CONFIG"])')"; \
	docker run --rm livekit/livekit-server:v1.13.3 --config-body "$$config" ports >/dev/null
	docker run --rm \
		-v "$(CURDIR)/deploy/observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
		-v "$(CURDIR)/deploy/observability/alerts.yml:/etc/prometheus/alerts.yml:ro" \
		--entrypoint /bin/promtool prom/prometheus:v3.13.1 \
		check config /etc/prometheus/prometheus.yml
	docker run --rm -v "$(CURDIR)/deploy/observability/grafana/dashboards:/dashboards:ro" \
		python:3.12.13-slim python -c 'import json; json.load(open("/dashboards/kaede-overview.json", encoding="utf-8"))'
	$(MAKE) generated-compose-check
	$(MAKE) nginx-check

generated-compose-check:
	@$(CONFIG_GUARD); \
	if [ ! -f deploy/compose.generated.yml ]; then \
		echo "generated Compose validation skipped (run make setup to create it)"; \
		exit 0; \
	fi; \
	test -f .env || { echo "deploy/compose.generated.yml exists but .env is missing" >&2; exit 2; }; \
	args=""; \
	if grep -q '^KAEDE_MEDIA_STORAGE_BACKEND=s3$$' .env; then args="$$args --external-s3"; fi; \
	case ",$$(grep '^COMPOSE_PROFILES=' .env | cut -d= -f2-)," in \
		*,observability,*) args="$$args --observability" ;; \
	esac; \
	case ",$$(grep '^COMPOSE_PROFILES=' .env | cut -d= -f2-)," in \
		*,voice,*) args="$$args --voice" ;; \
	esac; \
	KAEDE_OPERATOR_ENV_FILE="$(abspath .env)" docker compose --env-file .env \
		-f deploy/compose.yml -f deploy/compose.generated.yml config --format json | \
		docker run --rm -i --network none \
		-v "$(CURDIR)/deploy/validate_compose.py:/validate_compose.py:ro" \
		python:3.12.13-slim python /validate_compose.py $$args

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

dev:
	$(DEV_COMPOSE) up --build

dev-down:
	$(DEV_COMPOSE) down
