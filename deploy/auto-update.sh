#!/usr/bin/env bash

# Safely fast-forward and deploy a source-built Kaede Chat installation.

set -Eeuo pipefail
umask 077
export GIT_TERMINAL_PROMPT=0

ROOT=${KAEDE_AUTO_UPDATE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}
if [[ ${KAEDE_AUTO_UPDATE_IMMUTABLE_RUN:-false} != true ]]; then
  IMMUTABLE_DIR=$(mktemp -d /tmp/kaede-auto-update.XXXXXX)
  cp -- "${BASH_SOURCE[0]}" "$IMMUTABLE_DIR/auto-update.sh"
  chmod 700 "$IMMUTABLE_DIR/auto-update.sh"
  exec env KAEDE_AUTO_UPDATE_IMMUTABLE_RUN=true \
    KAEDE_AUTO_UPDATE_IMMUTABLE_DIR="$IMMUTABLE_DIR" \
    KAEDE_AUTO_UPDATE_ROOT="$ROOT" \
    "$IMMUTABLE_DIR/auto-update.sh" "$@"
fi
ENV_FILE=${AUTO_UPDATE_ENV_FILE:-$ROOT/.env}
STATE_FILE="$ROOT/.kaede-auto-update-state"
MARKER_FILE="$ROOT/.kaede-auto-update.in-progress"
LOCK_FILE="$ROOT/.kaede-auto-update.lock"
UPDATE_MARKER_CREATED=false

cleanup() {
  [[ $UPDATE_MARKER_CREATED != true ]] || rm -f -- "$MARKER_FILE"
  if [[ -n ${KAEDE_AUTO_UPDATE_IMMUTABLE_DIR:-} && \
        $KAEDE_AUTO_UPDATE_IMMUTABLE_DIR == /tmp/kaede-auto-update.* ]]; then
    rm -rf -- "$KAEDE_AUTO_UPDATE_IMMUTABLE_DIR"
  fi
}
trap cleanup EXIT INT TERM HUP

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

read_env() {
  local wanted=$1 fallback=${2-} line key value
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    [[ -z $line || $line == \#* || $line != *=* ]] && continue
    key=${line%%=*}
    [[ $key == "$wanted" ]] || continue
    value=${line#*=}
    if [[ ${#value} -ge 2 && ${value:0:1} == '"' && ${value: -1} == '"' ]]; then
      value=${value:1:${#value}-2}
    elif [[ ${#value} -ge 2 && ${value:0:1} == "'" && ${value: -1} == "'" ]]; then
      value=${value:1:${#value}-2}
    fi
    printf '%s' "$value"
    return 0
  done < "$ENV_FILE"
  printf '%s' "$fallback"
}

safe_regular_file() {
  local path=$1 label=$2
  [[ -f $path && ! -L $path ]] || die "$label must be a regular, non-symlink file: $path"
  [[ $(stat -c '%h' "$path") == 1 ]] || die "$label must not be hard-linked: $path"
}

validate_config() {
  safe_regular_file "$ENV_FILE" 'operator environment'
  local mode
  mode=$(stat -c '%a' "$ENV_FILE")
  (((8#$mode & 8#077) == 0)) || die "operator environment must not be group/world accessible (mode $mode)"

  ENABLED=$(read_env AUTO_UPDATE_ENABLED false)
  REMOTE=$(read_env AUTO_UPDATE_REMOTE origin)
  BRANCH=$(read_env AUTO_UPDATE_BRANCH main)
  INTERVAL=$(read_env AUTO_UPDATE_INTERVAL 6h)
  JITTER=$(read_env AUTO_UPDATE_JITTER 30m)
  BACKUP_HOOK=$(read_env AUTO_UPDATE_BACKUP_HOOK '')
  WAIT_TIMEOUT=$(read_env AUTO_UPDATE_WAIT_TIMEOUT_SECONDS 300)
  [[ $ENABLED == true || $ENABLED == false ]] || die 'AUTO_UPDATE_ENABLED must be true or false'
  [[ $REMOTE =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die 'AUTO_UPDATE_REMOTE is invalid'
  [[ $BRANCH =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && $BRANCH != */ && $BRANCH != *..* ]] || die 'AUTO_UPDATE_BRANCH is invalid'
  [[ $INTERVAL =~ ^(6h|12h|1d|1w)$ ]] || die 'AUTO_UPDATE_INTERVAL must be 6h, 12h, 1d, or 1w'
  [[ $JITTER =~ ^[1-9][0-9]*(s|m|min|h|d|w)$ ]] || die 'AUTO_UPDATE_JITTER is invalid'
  [[ $WAIT_TIMEOUT =~ ^[0-9]+$ ]] && ((WAIT_TIMEOUT >= 60 && WAIT_TIMEOUT <= 3600)) || \
    die 'AUTO_UPDATE_WAIT_TIMEOUT_SECONDS must be from 60 through 3600'
  if [[ -n $BACKUP_HOOK ]]; then
    [[ $BACKUP_HOOK == /* && $BACKUP_HOOK =~ ^/[A-Za-z0-9_./-]+$ && $BACKUP_HOOK != *..* ]] || \
      die 'AUTO_UPDATE_BACKUP_HOOK must be a safe absolute path'
    safe_regular_file "$BACKUP_HOOK" 'backup hook'
    [[ -x $BACKUP_HOOK ]] || die "backup hook is not executable: $BACKUP_HOOK"
  fi
}

read_deployed_commit() {
  [[ -f $STATE_FILE && ! -L $STATE_FILE ]] || return 0
  local line
  IFS= read -r line < "$STATE_FILE" || true
  [[ $line == DEPLOYED_COMMIT=* ]] || return 0
  printf '%s' "${line#DEPLOYED_COMMIT=}"
}

write_deployed_commit() {
  local commit=$1 temporary
  temporary=$(mktemp "$ROOT/.kaede-auto-update-state.tmp.XXXXXX")
  printf 'DEPLOYED_COMMIT=%s\n' "$commit" > "$temporary"
  chmod 600 "$temporary"
  mv -fT "$temporary" "$STATE_FILE"
}

compose_command() {
  COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT/deploy/compose.yml")
  [[ ! -f $ROOT/deploy/compose.generated.yml ]] || \
    COMPOSE+=(-f "$ROOT/deploy/compose.generated.yml")
  export KAEDE_OPERATOR_ENV_FILE="$ENV_FILE"
}

show_status() {
  validate_config
  local head deployed
  head=$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null || printf unknown)
  deployed=$(read_deployed_commit)
  printf 'enabled: %s\nremote: %s\nbranch: %s\ninterval: %s\njitter: %s\ncheckout: %s\ndeployed: %s\n' \
    "$ENABLED" "$REMOTE" "$BRANCH" "$INTERVAL" "$JITTER" "$head" "${deployed:-not recorded}"
}

mark_current() {
  validate_config
  local head
  head=$(git -C "$ROOT" rev-parse --verify HEAD) || die 'cannot resolve the current Git commit'
  write_deployed_commit "$head"
  log "recorded currently deployed commit $head"
}

run_update() {
  local force=${1:-false}
  validate_config
  if [[ $ENABLED != true && $force != true ]]; then
    log 'automatic updates are disabled; nothing to do'
    return 0
  fi
  command -v git >/dev/null || die 'git is required'
  command -v docker >/dev/null || die 'Docker with the Compose plugin is required'
  command -v python3 >/dev/null || die 'Python 3 is required for deployment validation'
  command -v flock >/dev/null || die 'flock is required for automatic updates'
  [[ ! -e $ROOT/.kaede-setup.in-progress ]] || die 'setup has an incomplete transaction'
  local setup_lock="$ROOT/.kaede-setup.lock"
  [[ ! -L $setup_lock ]] || die 'setup lock must not be a symlink'
  [[ ! -e $setup_lock || $(stat -c '%h' "$setup_lock") == 1 ]] || die 'setup lock must not be hard-linked'
  exec 8>"$setup_lock"
  chmod 600 "$setup_lock"
  flock -n 8 || die 'setup or another deployment operation is running'
  [[ ! -L $LOCK_FILE ]] || die 'update lock must not be a symlink'
  [[ ! -e $LOCK_FILE || $(stat -c '%h' "$LOCK_FILE") == 1 ]] || die 'update lock must not be hard-linked'
  exec 9>"$LOCK_FILE"
  chmod 600 "$LOCK_FILE"
  flock -n 9 || { log 'another automatic update is already running'; return 0; }

  [[ -z $(git -C "$ROOT" status --porcelain --untracked-files=no) ]] || \
    die 'tracked files are modified; commit or restore them before automatic updating'
  local checked_out
  checked_out=$(git -C "$ROOT" symbolic-ref --quiet --short HEAD) || die 'the checkout is detached'
  [[ $checked_out == "$BRANCH" ]] || die "checked-out branch is $checked_out, expected $BRANCH"
  git -C "$ROOT" remote get-url "$REMOTE" >/dev/null || die "Git remote does not exist: $REMOTE"

  printf 'Kaede automatic update started at %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER_FILE"
  chmod 600 "$MARKER_FILE"
  UPDATE_MARKER_CREATED=true

  log "fetching $REMOTE/$BRANCH"
  git -C "$ROOT" fetch --prune "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
  local current target deployed
  current=$(git -C "$ROOT" rev-parse --verify HEAD)
  target=$(git -C "$ROOT" rev-parse --verify "refs/remotes/$REMOTE/$BRANCH^{commit}")
  deployed=$(read_deployed_commit)
  if [[ $current != "$target" ]]; then
    git -C "$ROOT" merge-base --is-ancestor "$current" "$target" || \
      die 'remote history is not a fast-forward; refusing a force-push, downgrade, or local divergence'
    log "fast-forwarding source from $current to $target"
    git -C "$ROOT" merge --ff-only "$target"
  elif [[ $deployed == "$target" ]]; then
    log "already running recorded commit $target"
    return 0
  else
    log "source is at $target but deployed state differs; retrying deployment"
  fi

  [[ -z $(git -C "$ROOT" status --porcelain --untracked-files=no) ]] || \
    die 'the updated checkout unexpectedly contains tracked modifications'
  compose_command
  log 'validating the operator environment and new application image'
  python3 "$ROOT/deploy/validate_deploy_env.py" --file "$ENV_FILE" --file-only
  "${COMPOSE[@]}" run --rm --no-deps --build preflight
  log 'building application images before downtime'
  "${COMPOSE[@]}" build --pull api gateway worker scheduler migrate storage-init frontend-build

  if [[ -n $BACKUP_HOOK ]]; then
    log "running configured backup hook $BACKUP_HOOK"
    KAEDE_UPDATE_FROM="${deployed:-$current}" KAEDE_UPDATE_TO="$target" KAEDE_ROOT="$ROOT" \
      "$BACKUP_HOOK"
  else
    log 'WARNING: no AUTO_UPDATE_BACKUP_HOOK is configured; relying on the operator backup policy'
  fi

  log 'quiescing application writers and the internal edge'
  "${COMPOSE[@]}" stop caddy api gateway worker scheduler
  log 'applying migrations exactly once before application writers restart'
  "${COMPOSE[@]}" run --rm --no-deps migrate
  log 'starting the updated topology and waiting for health checks'
  "${COMPOSE[@]}" up -d --no-build --wait --wait-timeout "$WAIT_TIMEOUT"
  write_deployed_commit "$target"
  log "automatic update completed at $target"
}

case ${1:-run} in
  run) run_update false ;;
  run-now) run_update true ;;
  status) show_status ;;
  mark-current) mark_current ;;
  *) die 'usage: deploy/auto-update.sh {run|run-now|status|mark-current}' ;;
esac
