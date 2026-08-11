#!/usr/bin/env bash

# Install, remove, or inspect Kaede's per-user systemd update timer.

set -Eeuo pipefail
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
ENV_FILE=${AUTO_UPDATE_ENV_FILE:-$ROOT/.env}
SYSTEMD_DIR=${AUTO_UPDATE_SYSTEMD_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
SERVICE_FILE="$SYSTEMD_DIR/kaede-auto-update.service"
TIMER_FILE="$SYSTEMD_DIR/kaede-auto-update.timer"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

safe_env_file() {
  [[ -f $ENV_FILE && ! -L $ENV_FILE ]] || die ".env must be a regular, non-symlink file: $ENV_FILE"
  [[ $(stat -c '%h' "$ENV_FILE") == 1 ]] || die '.env must not be hard-linked'
}

read_env() {
  local wanted=$1 fallback=${2-} line value
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line == "$wanted="* ]] || continue
    value=${line#*=}
    printf '%s' "$value"
    return 0
  done < "$ENV_FILE"
  printf '%s' "$fallback"
}

set_env() {
  local wanted=$1 value=$2 temporary
  safe_env_file
  temporary=$(mktemp "$ROOT/.env.auto-update.XXXXXX")
  awk -v wanted="$wanted" -v replacement="$wanted=$value" '
    BEGIN { found = 0 }
    index($0, wanted "=") == 1 { if (!found) print replacement; found = 1; next }
    { print }
    END { if (!found) print replacement }
  ' "$ENV_FILE" > "$temporary"
  chmod 600 "$temporary"
  mv -fT "$temporary" "$ENV_FILE"
}

validate_duration() {
  [[ $1 =~ ^[1-9][0-9]*(s|m|min|h|d|w)$ ]] || die "invalid systemd duration: $1"
}

systemctl_user() {
  systemctl --user "$@"
}

rollback_enable() {
  set_env AUTO_UPDATE_ENABLED false
  systemctl_user disable --now kaede-auto-update.timer >/dev/null 2>&1 || true
  rm -f -- "$SERVICE_FILE" "$TIMER_FILE"
  systemctl_user daemon-reload >/dev/null 2>&1 || true
}

enable_timer() {
  safe_env_file
  command -v systemctl >/dev/null || die 'systemd is required; use the documented cron fallback on this host'
  [[ $ROOT =~ ^/[A-Za-z0-9_./-]+$ && $ROOT != *..* ]] || die 'repository path is unsafe for a systemd unit'
  local interval jitter calendar temporary_service temporary_timer
  interval=$(read_env AUTO_UPDATE_INTERVAL 6h)
  jitter=$(read_env AUTO_UPDATE_JITTER 30m)
  validate_duration "$jitter"
  case $interval in
    6h) calendar='*-*-* 00/6:00:00' ;;
    12h) calendar='*-*-* 00/12:00:00' ;;
    1d) calendar='*-*-* 03:00:00' ;;
    1w) calendar='Mon *-*-* 03:00:00' ;;
    *) die 'AUTO_UPDATE_INTERVAL must be 6h, 12h, 1d, or 1w' ;;
  esac
  set_env AUTO_UPDATE_REMOTE "$(read_env AUTO_UPDATE_REMOTE origin)"
  set_env AUTO_UPDATE_BRANCH "$(read_env AUTO_UPDATE_BRANCH main)"
  set_env AUTO_UPDATE_INTERVAL "$interval"
  set_env AUTO_UPDATE_JITTER "$jitter"
  set_env AUTO_UPDATE_WAIT_TIMEOUT_SECONDS "$(read_env AUTO_UPDATE_WAIT_TIMEOUT_SECONDS 300)"
  set_env AUTO_UPDATE_ENABLED true
  if ! "$ROOT/deploy/auto-update.sh" status >/dev/null; then
    set_env AUTO_UPDATE_ENABLED false
    die 'automatic-update configuration is invalid; configuration was left disabled'
  fi
  local remote branch checked_out
  remote=$(read_env AUTO_UPDATE_REMOTE origin)
  branch=$(read_env AUTO_UPDATE_BRANCH main)
  if ! git -C "$ROOT" remote get-url "$remote" >/dev/null 2>&1; then
    set_env AUTO_UPDATE_ENABLED false
    die "Git remote does not exist: $remote"
  fi
  checked_out=$(git -C "$ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
  if [[ $checked_out != "$branch" ]]; then
    set_env AUTO_UPDATE_ENABLED false
    die "check out the configured branch before enabling updates (expected $branch, found ${checked_out:-detached HEAD})"
  fi

  mkdir -p "$SYSTEMD_DIR"
  chmod 700 "$SYSTEMD_DIR"
  temporary_service=$(mktemp "$SYSTEMD_DIR/.kaede-auto-update.service.XXXXXX")
  temporary_timer=$(mktemp "$SYSTEMD_DIR/.kaede-auto-update.timer.XXXXXX")
  cat > "$temporary_service" <<EOF
[Unit]
Description=Safely update Kaede Chat from its configured Git branch
Documentation=file://$ROOT/docs/operator.md
ConditionPathExists=$ROOT/.env

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=$ROOT/deploy/auto-update.sh run
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TimeoutStartSec=3h
UMask=0077
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
EOF
  cat > "$temporary_timer" <<EOF
[Unit]
Description=Check for Kaede Chat updates

[Timer]
OnCalendar=$calendar
RandomizedDelaySec=$jitter
Persistent=true
Unit=kaede-auto-update.service

[Install]
WantedBy=timers.target
EOF
  chmod 600 "$temporary_service" "$temporary_timer"
  mv -fT "$temporary_service" "$SERVICE_FILE"
  mv -fT "$temporary_timer" "$TIMER_FILE"
  if ! systemctl_user daemon-reload || ! systemctl_user enable --now kaede-auto-update.timer; then
    rollback_enable
    die 'could not enable the user timer; configuration was left disabled'
  fi
  printf 'Kaede automatic updates are enabled (%s with up to %s jitter).\n' "$interval" "$jitter"
  printf 'For updates while logged out, verify user lingering as documented in docs/operator.md.\n'
}

disable_timer() {
  [[ ! -e $ENV_FILE ]] || { safe_env_file; set_env AUTO_UPDATE_ENABLED false; }
  if command -v systemctl >/dev/null 2>&1; then
    systemctl_user disable --now kaede-auto-update.timer >/dev/null 2>&1 || true
  fi
  rm -f -- "$SERVICE_FILE" "$TIMER_FILE"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl_user daemon-reload >/dev/null 2>&1 || true
    systemctl_user reset-failed kaede-auto-update.service >/dev/null 2>&1 || true
  fi
  printf 'Kaede automatic updates are disabled.\n'
}

show_status() {
  "$ROOT/deploy/auto-update.sh" status
  if command -v systemctl >/dev/null 2>&1; then
    printf '\nsystemd timer:\n'
    systemctl_user status --no-pager kaede-auto-update.timer || true
  else
    printf '\nsystemd is not available on this host.\n'
  fi
}

case ${1:-status} in
  enable) enable_timer ;;
  disable) disable_timer ;;
  status) show_status ;;
  *) die 'usage: deploy/install-auto-update.sh {enable|disable|status}' ;;
esac
