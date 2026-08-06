#!/usr/bin/env bash

# Interactive deployment configuration for Kaede Chat.
# This script only writes configuration. It never starts containers, installs
# host proxy files, requests certificates, changes firewall rules, or reloads
# nginx/Caddy.

set -Eeuo pipefail
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ENV_FILE="$ROOT/.env"
OVERLAY_FILE="$ROOT/deploy/compose.generated.yml"
GENERATED_DIR="$ROOT/deploy/generated"
LOCK_FILE="$ROOT/.kaede-setup.lock"
MARKER_FILE="$ROOT/.kaede-setup.in-progress"
DRY_RUN=false
PLAIN=${KAEDE_SETUP_PLAIN:-false}
PUBLISHING=false
LOCK_CREATED=false
BACKUP_DIR=
STAGE_DIR=

OUTPUTS=(
  ".env"
  "deploy/compose.generated.yml"
  "deploy/generated/kaede.nginx.conf"
  "deploy/generated/README.txt"
)

usage() {
  cat <<'EOF'
Usage: ./setup.sh [--dry-run] [--plain]

Interactively generate Kaede's production .env, Compose override, and optional
host nginx configuration.

  --dry-run  collect and validate answers without writing files
  --plain    use the built-in ANSI interface even when gum is installed
  --help     show this help

The script never starts Docker Compose or changes host services.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    --plain) PLAIN=true ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -t 0 && -t 1 ]] || {
  printf 'setup.sh is interactive and requires a terminal.\n' >&2
  exit 2
}
command -v openssl >/dev/null || {
  printf 'OpenSSL is required to generate deployment secrets.\n' >&2
  exit 2
}
[[ -f "$ROOT/deploy/compose.yml" ]] || {
  printf 'Run this script from an intact Kaede Chat repository.\n' >&2
  exit 2
}

if command -v gum >/dev/null 2>&1 && [[ $PLAIN != true ]]; then
  USE_GUM=true
else
  USE_GUM=false
fi

if [[ $USE_GUM == false ]]; then
  C_CYAN=$'\033[36m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
  C_BOLD=$'\033[1m'
  C_RESET=$'\033[0m'
else
  C_CYAN= C_GREEN= C_YELLOW= C_RED= C_BOLD= C_RESET=
fi

die() {
  printf '%sError:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
  exit 1
}

note() {
  printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET" >&2
}

warn() {
  printf '%sWarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2
}

cleanup() {
  local status=$?
  [[ -z $STAGE_DIR || ! -d $STAGE_DIR ]] || rm -rf -- "$STAGE_DIR"
  if ((status != 0)) && [[ $PUBLISHING == true ]]; then
    warn 'Setup was interrupted; attempting to restore the previous generated files.'
    if declare -F restore_previous >/dev/null && restore_previous; then
      rm -f -- "$MARKER_FILE"
      warn 'Previous configuration restored.'
    else
      warn 'Rollback was incomplete. Do not run Compose until .kaede-setup.in-progress is resolved.'
    fi
  fi
  [[ $LOCK_CREATED != true ]] || rm -f -- "$LOCK_FILE"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

section() {
  local title=$1 body=$2
  if [[ $USE_GUM == true ]]; then
    gum style --border rounded --border-foreground 45 --padding "0 1" \
      --bold "$title" "$body" >&2
  else
    printf '\n%s%s── %s ──%s\n%s\n' "$C_BOLD" "$C_CYAN" "$title" "$C_RESET" "$body" >&2
  fi
}

prompt_text() {
  local prompt=$1 default=${2-} answer
  if [[ $USE_GUM == true ]]; then
    answer=$(gum input --prompt "$prompt: " --value "$default") || exit 130
  else
    if [[ -n $default ]]; then
      read -r -p "$prompt [$default]: " answer
      answer=${answer:-$default}
    else
      read -r -p "$prompt: " answer
    fi
  fi
  printf '%s' "$answer"
}

prompt_secret() {
  local prompt=$1 answer
  if [[ $USE_GUM == true ]]; then
    answer=$(gum input --password --prompt "$prompt: ") || exit 130
  else
    IFS= read -r -s -p "$prompt: " answer
    printf '\n' >&2
  fi
  printf '%s' "$answer"
}

choose() {
  local prompt=$1 default=$2
  shift 2
  local option answer index=1
  local -a ordered=("$default")
  for option in "$@"; do
    [[ $option == "$default" ]] || ordered+=("$option")
  done
  if [[ $USE_GUM == true ]]; then
    answer=$(gum choose --header "$prompt" "${ordered[@]}") || exit 130
  else
    printf '%s\n' "$prompt" >&2
    for option in "${ordered[@]}"; do
      printf '  %d) %s%s\n' "$index" "$option" "$([[ $index == 1 ]] && printf ' (default)')" >&2
      ((index += 1))
    done
    while :; do
      read -r -p "Choice [1]: " answer
      answer=${answer:-1}
      [[ $answer =~ ^[0-9]+$ ]] && ((answer >= 1 && answer <= ${#ordered[@]})) && break
      warn "Enter a number from 1 to ${#ordered[@]}."
    done
    answer=${ordered[answer-1]}
  fi
  printf '%s' "$answer"
}

confirm() {
  local prompt=$1 default=${2:-false} answer
  if [[ $USE_GUM == true ]]; then
    local -a flags=()
    [[ $default == true ]] && flags+=(--default=true)
    gum confirm "${flags[@]}" "$prompt"
    return
  fi
  local suffix='[y/N]'
  [[ $default == true ]] && suffix='[Y/n]'
  while :; do
    read -r -p "$prompt $suffix " answer
    answer=${answer,,}
    if [[ -z $answer ]]; then
      [[ $default == true ]]
      return
    fi
    case "$answer" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) warn 'Answer yes or no.' ;;
    esac
  done
}

declare -A OLD=()
read_existing_env() {
  [[ -e $ENV_FILE || -L $ENV_FILE ]] || return 0
  [[ -f $ENV_FILE && ! -L $ENV_FILE ]] || die ".env must be a regular, non-symlink file"
  local links
  links=$(stat -c '%h' "$ENV_FILE")
  [[ $links == 1 ]] || die ".env must not be hard-linked"
  local line key value
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] && continue
    [[ $line == *=* ]] || die "invalid line in existing .env"
    key=${line%%=*}
    value=${line#*=}
    [[ $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid key in existing .env: $key"
    [[ -v "OLD[$key]" ]] && die "duplicate key in existing .env: $key"
    [[ $value != \"* && $value != \'* ]] || die 'quoted existing .env values are not supported'
    OLD[$key]=$value
  done < "$ENV_FILE"
}

old() {
  local key=$1 default=${2-}
  printf '%s' "${OLD[$key]-$default}"
}

old_uint() {
  local key=$1 default=$2 value
  value=$(old "$key" "$default")
  [[ $value =~ ^[0-9]+$ ]] || die "existing $key must be an unsigned integer"
  printf '%s' "$value"
}

random_hex() {
  openssl rand -hex "$1"
}

random_base64url() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n'
}

preserve_or_generate() {
  local key=$1 kind=$2 value
  value=${OLD[$key]-}
  if [[ -n $value && $value != *replace* && $value != *change-me* ]]; then
    printf '%s' "$value"
    return
  fi
  case "$kind" in
    b64) random_base64url ;;
    hex16) random_hex 16 ;;
    hex24) random_hex 24 ;;
    hex32) random_hex 32 ;;
    garage_access) printf 'GK%s' "$(random_hex 16)" ;;
    livekit) printf 'LK%s' "$(random_hex 8)" ;;
    *) die "internal secret generator error" ;;
  esac
}

valid_domain() {
  local domain=$1 label
  ((${#domain} >= 3 && ${#domain} <= 253)) || return 1
  [[ $domain == *.* && $domain != .* && $domain != *. && $domain != *..* ]] || return 1
  [[ $domain =~ ^[a-z0-9.-]+$ ]] || return 1
  IFS='.' read -r -a labels <<< "$domain"
  for label in "${labels[@]}"; do
    ((${#label} >= 1 && ${#label} <= 63)) || return 1
    [[ $label =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || return 1
  done
  case "$domain" in
    localhost|example|example.com|example.net|example.org|*.localhost|*.test|*.invalid|*.example|*.example.com|*.example.net|*.example.org) return 1 ;;
  esac
}

ask_domain() {
  local value
  while :; do
    value=$(prompt_text 'Public instance domain' "$(old KAEDE_DOMAIN '')")
    value=${value,,}
    value=${value%.}
    valid_domain "$value" && { printf '%s' "$value"; return; }
    warn 'Enter a valid, non-reserved fully-qualified domain.'
  done
}

valid_port() {
  local value=$1 minimum=${2:-1024}
  [[ $value =~ ^[0-9]+$ ]] && ((10#$value >= minimum && 10#$value <= 65535))
}

port_in_use() {
  local port=$1 protocol=$2 output
  command -v ss >/dev/null 2>&1 || return 1
  if [[ $protocol == udp ]]; then
    output=$(ss -H -lun "sport = :$port" 2>/dev/null || true)
  else
    output=$(ss -H -ltn "sport = :$port" 2>/dev/null || true)
  fi
  [[ -n $output ]]
}

available_port() {
  local port=$1 protocol=${2:-tcp}
  while ((port <= 65535)); do
    port_in_use "$port" "$protocol" || { printf '%s' "$port"; return; }
    ((port += 1))
  done
  die 'no available host port found'
}

ask_port() {
  local prompt=$1 default=$2 protocol=${3:-tcp} minimum=${4:-1024} value
  while :; do
    value=$(prompt_text "$prompt" "$default")
    valid_port "$value" "$minimum" || { warn "Use a port from $minimum to 65535."; continue; }
    printf '%s' "$value"
    return
  done
}

valid_origin() {
  local value=$1 authority
  [[ $value == https://* && $value != *' '* && $value != *'?'* && $value != *'#'* && $value != *@* ]]
  authority=${value#https://}
  [[ -n $authority && $authority != :* && $authority != */* ]]
}

ask_origin() {
  local prompt=$1 default=${2-} value
  while :; do
    value=$(prompt_text "$prompt" "$default")
    value=${value%/}
    valid_origin "$value" && { printf '%s' "$value"; return; }
    warn 'Enter an HTTPS origin without credentials, query, fragment, or path.'
  done
}

valid_bucket() {
  [[ $1 =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]
}

ask_bucket() {
  local prompt=$1 default=$2 value
  while :; do
    value=$(prompt_text "$prompt" "$default")
    valid_bucket "$value" && { printf '%s' "$value"; return; }
    warn 'Use a 3-63 character lower-case S3 bucket name.'
  done
}

portable_secret() {
  [[ $1 =~ ^[-A-Za-z0-9_./:@%+,=~{}]+$ ]]
}

ask_provider_secret() {
  local prompt=$1 minimum=$2 value
  while :; do
    value=$(prompt_secret "$prompt")
    if ((${#value} >= minimum)) && portable_secret "$value"; then
      printf '%s' "$value"
      return
    fi
    warn "Use at least $minimum portable credential characters (letters, digits, and standard token punctuation)."
  done
}

urlencode() {
  local LC_ALL=C input=$1 output= char hex i
  for ((i=0; i<${#input}; i++)); do
    char=${input:i:1}
    case "$char" in
      [a-zA-Z0-9.~_-]) output+=$char ;;
      *) printf -v hex '%02X' "'$char"; output+="%$hex" ;;
    esac
  done
  printf '%s' "$output"
}

safe_path() {
  [[ $1 == /* && $1 != *'..'* && $1 =~ ^/[A-Za-z0-9_./-]+$ ]]
}

ask_path() {
  local prompt=$1 default=$2 value
  while :; do
    value=$(prompt_text "$prompt" "$default")
    safe_path "$value" && { printf '%s' "$value"; return; }
    warn 'Use an absolute path containing only letters, digits, dot, underscore, dash, and slash.'
  done
}

read_existing_env

if command -v flock >/dev/null 2>&1; then
  [[ ! -L $LOCK_FILE ]] || die '.kaede-setup.lock must not be a symlink'
  if [[ -e $LOCK_FILE ]]; then
    [[ -f $LOCK_FILE && $(stat -c '%h' "$LOCK_FILE") == 1 ]] || die 'unsafe setup lock file'
  else
    LOCK_CREATED=true
  fi
  exec 9>"$LOCK_FILE"
  chmod 600 "$LOCK_FILE"
  flock -n 9 || die 'another setup wizard is already running'
else
  warn 'flock is unavailable; do not run another setup wizard concurrently.'
fi

[[ ! -e $MARKER_FILE && ! -L $MARKER_FILE ]] || die \
  'an earlier setup was interrupted; inspect .kaede-setup.in-progress and .kaede-backups first'

section 'Kaede Chat setup' \
  'Configuration only: no containers, packages, certificates, firewall rules, or host services will be changed.'

DOMAIN=$(ask_domain)
if [[ -n ${OLD[KAEDE_DOMAIN]-} && ${OLD[KAEDE_DOMAIN]%.} != "$DOMAIN" && ${OLD[KAEDE_DOMAIN]} != *.example.com ]]; then
  die 'an established instance domain cannot be changed safely by this script'
fi

FEDERATION_DEFAULT=$(old KAEDE_FEDERATION_MODE open)
[[ $FEDERATION_DEFAULT == open || $FEDERATION_DEFAULT == allowlist ]] || FEDERATION_DEFAULT=open
FEDERATION=$(choose 'Federation admission mode' "$FEDERATION_DEFAULT" open allowlist)
if confirm 'Generate or retain an administrator API token?' "$([[ $FEDERATION == allowlist || -n ${OLD[KAEDE_ADMIN_TOKEN]-} ]] && printf true || printf false)"; then
  ADMIN_ENABLED=true
else
  ADMIN_ENABLED=false
fi

section 'Optional services' 'Voice uses host-networked LiveKit. Observability enables Prometheus, Loki, and Grafana.'
if confirm 'Enable LiveKit voice/video?' "$([[ $(old KAEDE_VOICE_ENABLED false) == true ]] && printf true || printf false)"; then
  VOICE=true
else
  VOICE=false
fi
OLD_PROFILES=$(old COMPOSE_PROFILES '')
if confirm 'Enable the observability profile?' "$([[ ,$OLD_PROFILES, == *,observability,* ]] && printf true || printf false)"; then
  OBSERVABILITY=true
else
  OBSERVABILITY=false
fi

section 'Reverse proxy' \
  'Kaede uses loopback-only Caddy for internal routing. Setup can write a host nginx config for ports 80/443, but it will not create or manage TLS certificates.'
HOST_NGINX_DEFAULT=false
[[ -f $GENERATED_DIR/kaede.nginx.conf ]] && HOST_NGINX_DEFAULT=true
[[ ${OLD[SETUP_HOST_NGINX]-} == true ]] && HOST_NGINX_DEFAULT=true
if [[ ! -e $ENV_FILE ]]; then HOST_NGINX_DEFAULT=true; fi
if confirm 'Generate a host nginx configuration referencing an existing TLS certificate?' "$HOST_NGINX_DEFAULT"; then
  HOST_NGINX=true
else
  HOST_NGINX=false
fi

EDGE_PREFERRED=$(old_uint KAEDE_CADDY_HOST_PORT 18081)
valid_port "$EDGE_PREFERRED" || die 'existing KAEDE_CADDY_HOST_PORT is outside 1024-65535'
[[ -e $ENV_FILE ]] || EDGE_PREFERRED=$(available_port "$EDGE_PREFERRED")
EDGE_PORT=$(ask_port 'Loopback edge port' "$EDGE_PREFERRED")
API_PREFERRED=$(old_uint KAEDE_API_HOST_PORT 18082)
valid_port "$API_PREFERRED" || die 'existing KAEDE_API_HOST_PORT is outside 1024-65535'
[[ -e $ENV_FILE ]] || API_PREFERRED=$(available_port "$API_PREFERRED")
while :; do
  API_PORT=$(ask_port 'Loopback API diagnostics port' "$API_PREFERRED")
  [[ $API_PORT != "$EDGE_PORT" ]] && break
  warn 'The edge and API diagnostics ports must differ.'
done

GRAFANA_PORT=$(old_uint KAEDE_GRAFANA_HOST_PORT 18084)
valid_port "$GRAFANA_PORT" || die 'existing KAEDE_GRAFANA_HOST_PORT is outside 1024-65535'
if [[ $OBSERVABILITY == true ]]; then
  [[ -e $ENV_FILE ]] || GRAFANA_PORT=$(available_port "$GRAFANA_PORT")
  while :; do
    GRAFANA_PORT=$(ask_port 'Loopback Grafana port' "$GRAFANA_PORT")
    [[ $GRAFANA_PORT != "$EDGE_PORT" && $GRAFANA_PORT != "$API_PORT" ]] && break
    warn 'Grafana must use a distinct port.'
  done
fi

TURN_PORT=$(old_uint KAEDE_TURN_UDP_PORT 13478)
valid_port "$TURN_PORT" || die 'existing KAEDE_TURN_UDP_PORT is outside 1024-65535'
if [[ $VOICE == true ]]; then
  [[ -e $ENV_FILE ]] || TURN_PORT=$(available_port "$TURN_PORT" udp)
  while :; do
    TURN_PORT=$(ask_port 'TURN UDP port' "$TURN_PORT" udp)
    [[ $TURN_PORT != 7882 ]] && break
    warn 'UDP 7882 is reserved for LiveKit RTC.'
  done
  for fixed in 5349 7880 7881; do
    [[ $EDGE_PORT != "$fixed" && $API_PORT != "$fixed" ]] || \
      die "a selected TCP port conflicts with LiveKit's fixed port $fixed"
    [[ $OBSERVABILITY != true || $GRAFANA_PORT != "$fixed" ]] || \
      die "the Grafana port conflicts with LiveKit's fixed port $fixed"
  done
fi

check_selected_port() {
  local label=$1 port=$2 protocol=$3 old_key=${4-} old_port=
  port_in_use "$port" "$protocol" || return 0
  [[ -z $old_key ]] || old_port=$(old "$old_key" '')
  if [[ $old_port == "$port" ]]; then
    note "$label $port/$protocol is already in use; the preserved value may belong to the current Kaede deployment."
    return 0
  fi
  warn "$label $port/$protocol is already in use system-wide."
  confirm 'Continue with this port anyway?' false || die 'setup cancelled because a selected port is occupied'
}

check_selected_port 'Loopback edge' "$EDGE_PORT" tcp KAEDE_CADDY_HOST_PORT
check_selected_port 'API diagnostics' "$API_PORT" tcp KAEDE_API_HOST_PORT
[[ $OBSERVABILITY != true ]] || check_selected_port 'Grafana' "$GRAFANA_PORT" tcp KAEDE_GRAFANA_HOST_PORT
if [[ $VOICE == true ]]; then
  check_selected_port 'LiveKit control' 7880 tcp
  check_selected_port 'LiveKit RTC' 7881 tcp
  check_selected_port 'LiveKit RTC' 7882 udp
  check_selected_port 'TURN TLS' 5349 tcp
  check_selected_port 'TURN UDP' "$TURN_PORT" udp KAEDE_TURN_UDP_PORT
fi

if [[ $HOST_NGINX == true || $VOICE == true ]]; then
  if [[ $HOST_NGINX == true && $VOICE == true ]]; then
    TLS_CONSUMERS='The generated host nginx configuration and LiveKit TURN will reference these files.'
  elif [[ $HOST_NGINX == true ]]; then
    TLS_CONSUMERS='The generated host nginx configuration will reference these files.'
  else
    TLS_CONSUMERS='LiveKit TURN will reference these files.'
  fi
  section 'TLS certificate references' \
    "No certificate will be generated. $TLS_CONSUMERS Enter paths to files already managed on this host (for example by Certbot)."
  CERT_PATH=$(ask_path 'Existing TLS certificate path' "$(old LIVEKIT_TURN_CERT_PATH "/etc/letsencrypt/live/$DOMAIN/fullchain.pem")")
  KEY_PATH=$(ask_path 'Existing TLS private-key path' "$(old LIVEKIT_TURN_KEY_PATH "/etc/letsencrypt/live/$DOMAIN/privkey.pem")")
else
  CERT_PATH= KEY_PATH=
fi

section 'Object storage' 'Use bundled Garage or a private AWS S3, Backblaze B2, Cloudflare R2, or generic S3-compatible service.'
STORAGE_DEFAULT=$(old SETUP_STORAGE_PROVIDER "$(old KAEDE_MEDIA_STORAGE_BACKEND garage)")
case "$STORAGE_DEFAULT" in
  aws) STORAGE_DEFAULT_LABEL='AWS S3' ;;
  backblaze) STORAGE_DEFAULT_LABEL='Backblaze B2' ;;
  cloudflare) STORAGE_DEFAULT_LABEL='Cloudflare R2' ;;
  s3) STORAGE_DEFAULT_LABEL='Generic S3-compatible' ;;
  *) STORAGE_DEFAULT_LABEL='Bundled Garage' ;;
esac
STORAGE_LABEL=$(choose 'Storage provider' "$STORAGE_DEFAULT_LABEL" \
  'Bundled Garage' 'AWS S3' 'Backblaze B2' 'Cloudflare R2' 'Generic S3-compatible')
case "$STORAGE_LABEL" in
  'Bundled Garage') STORAGE=garage ;;
  'AWS S3') STORAGE=aws ;;
  'Backblaze B2') STORAGE=backblaze ;;
  'Cloudflare R2') STORAGE=cloudflare ;;
  *) STORAGE=s3 ;;
esac

ATTACHMENTS_BUCKET=$(old KAEDE_MEDIA_ATTACHMENTS_BUCKET kaede-attachments)
DERIVED_BUCKET=$(old KAEDE_MEDIA_DERIVED_BUCKET kaede-derived)
CACHE_BUCKET=$(old KAEDE_MEDIA_REMOTE_CACHE_BUCKET kaede-remote-cache)
S3_SESSION=
if [[ $STORAGE == garage ]]; then
  ((${#DOMAIN} <= 247)) || die 'the domain is too long to derive media.<domain> for Garage'
  STORAGE_BACKEND=garage
  S3_ENDPOINT=http://garage:3900
  MEDIA_PUBLIC="https://media.$DOMAIN"
  S3_REGION=kaede
  S3_STYLE=path
  S3_CREATE=true
  if [[ $(old KAEDE_MEDIA_STORAGE_BACKEND garage) == garage ]]; then
    S3_ACCESS=$(preserve_or_generate KAEDE_MEDIA_S3_ACCESS_KEY garage_access)
    S3_SECRET=$(preserve_or_generate KAEDE_MEDIA_S3_SECRET_KEY hex32)
    GARAGE_RPC=$(preserve_or_generate GARAGE_RPC_SECRET hex32)
    GARAGE_ADMIN=$(preserve_or_generate GARAGE_ADMIN_TOKEN hex32)
  else
    S3_ACCESS="GK$(random_hex 16)"
    S3_SECRET=$(random_hex 32)
    GARAGE_RPC=$(random_hex 32)
    GARAGE_ADMIN=$(random_hex 32)
  fi
else
  STORAGE_BACKEND=s3
  case "$STORAGE" in
    aws) DEFAULT_REGION=us-east-1 ;;
    backblaze) DEFAULT_REGION=us-west-004 ;;
    cloudflare) DEFAULT_REGION=auto ;;
    *) DEFAULT_REGION=us-east-1 ;;
  esac
  S3_REGION=$(prompt_text 'SigV4 region' "$(old KAEDE_MEDIA_S3_REGION "$DEFAULT_REGION")")
  [[ $S3_REGION =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die 'invalid SigV4 region'
  case "$STORAGE" in
    aws) DEFAULT_ENDPOINT="https://s3.$S3_REGION.amazonaws.com"; DEFAULT_STYLE=virtual ;;
    backblaze) DEFAULT_ENDPOINT="https://s3.$S3_REGION.backblazeb2.com"; DEFAULT_STYLE=path ;;
    cloudflare)
      ACCOUNT_ID=$(prompt_text 'Cloudflare account ID' '')
      [[ $ACCOUNT_ID =~ ^[A-Fa-f0-9]{32}$ ]] || die 'Cloudflare account ID must be 32 hexadecimal characters'
      DEFAULT_ENDPOINT="https://$ACCOUNT_ID.r2.cloudflarestorage.com"; DEFAULT_STYLE=path
      ;;
    *) DEFAULT_ENDPOINT=''; DEFAULT_STYLE=path ;;
  esac
  S3_ENDPOINT=$(ask_origin 'S3 API origin' "$(old KAEDE_MEDIA_S3_ENDPOINT "$DEFAULT_ENDPOINT")")
  MEDIA_PUBLIC=$(ask_origin 'Browser-visible S3 origin' "$(old KAEDE_MEDIA_PUBLIC_BASE_URL "$S3_ENDPOINT")")
  S3_STYLE=$(choose 'S3 addressing style' "$(old KAEDE_MEDIA_S3_ADDRESSING_STYLE "$DEFAULT_STYLE")" path virtual)
  [[ $S3_STYLE == path || $S3_STYLE == virtual ]] || die 'S3 addressing style must be path or virtual'
  EXISTING_EXTERNAL=false
  [[ $(old KAEDE_MEDIA_STORAGE_BACKEND '') == s3 ]] && EXISTING_EXTERNAL=true
  if [[ $EXISTING_EXTERNAL == true ]] && confirm 'Reuse existing S3 credentials?' true; then
    S3_ACCESS=$(old KAEDE_MEDIA_S3_ACCESS_KEY '')
    S3_SECRET=$(old KAEDE_MEDIA_S3_SECRET_KEY '')
    S3_SESSION=$(old KAEDE_MEDIA_S3_SESSION_TOKEN '')
  else
    S3_ACCESS=$(ask_provider_secret 'S3 access key' 16)
    S3_SECRET=$(ask_provider_secret 'S3 secret key' 16)
    if confirm 'Use a temporary S3 session token?' false; then
      S3_SESSION=$(ask_provider_secret 'S3 session token' 16)
    fi
  fi
  ((${#S3_ACCESS} >= 16 && ${#S3_SECRET} >= 16)) || die 'S3 credentials must contain at least 16 characters'
  portable_secret "$S3_ACCESS" && portable_secret "$S3_SECRET" || die 'existing S3 credentials contain unsupported dotenv characters'
  [[ -z $S3_SESSION ]] || portable_secret "$S3_SESSION" || die 'S3 session token contains unsupported dotenv characters'
  ATTACHMENTS_BUCKET=$(ask_bucket 'Attachments bucket' "$ATTACHMENTS_BUCKET")
  DERIVED_BUCKET=$(ask_bucket 'Derived-media bucket' "$DERIVED_BUCKET")
  CACHE_BUCKET=$(ask_bucket 'Federated-cache bucket' "$CACHE_BUCKET")
  [[ $ATTACHMENTS_BUCKET != "$DERIVED_BUCKET" && $ATTACHMENTS_BUCKET != "$CACHE_BUCKET" && $DERIVED_BUCKET != "$CACHE_BUCKET" ]] || \
    die 'the three S3 buckets must have distinct names'
  if [[ $S3_STYLE == virtual && ( $ATTACHMENTS_BUCKET == *.* || $DERIVED_BUCKET == *.* || $CACHE_BUCKET == *.* ) ]]; then
    die 'virtual-hosted S3 bucket names cannot contain dots with normal wildcard TLS certificates'
  fi
  if confirm 'Allow Kaede to create missing buckets?' "$([[ $(old KAEDE_MEDIA_S3_CREATE_BUCKETS false) == true ]] && printf true || printf false)"; then
    S3_CREATE=true
  else
    S3_CREATE=false
  fi
  GARAGE_RPC= GARAGE_ADMIN=
fi

section 'Email' \
  'Choose a delivery provider, or disable email for username-and-password-only registration.'
EMAIL_DEFAULT=$(old SETUP_EMAIL_PROVIDER "$(old KAEDE_EMAIL_BACKEND mailtrap_api)")
case "$EMAIL_DEFAULT" in
  disabled) EMAIL_DEFAULT_LABEL='Disabled (no email at signup)' ;;
  mailtrap_smtp) EMAIL_DEFAULT_LABEL='Mailtrap SMTP' ;;
  ses) EMAIL_DEFAULT_LABEL='AWS SES SMTP' ;;
  smtp) EMAIL_DEFAULT_LABEL='Generic SMTP' ;;
  *) EMAIL_DEFAULT_LABEL='Mailtrap API' ;;
esac
EMAIL_LABEL=$(choose 'Email provider' "$EMAIL_DEFAULT_LABEL" \
  'Mailtrap API' 'Mailtrap SMTP' 'AWS SES SMTP' 'Generic SMTP' \
  'Disabled (no email at signup)')
case "$EMAIL_LABEL" in
  'Disabled (no email at signup)') EMAIL=disabled ;;
  'Mailtrap API') EMAIL=mailtrap_api ;;
  'Mailtrap SMTP') EMAIL=mailtrap_smtp ;;
  'AWS SES SMTP') EMAIL=ses ;;
  *) EMAIL=smtp ;;
esac
SMTP_URL= MAILTRAP_TOKEN=
if [[ $EMAIL == disabled ]]; then
  warn 'Email verification, email changes, and self-service password recovery will be unavailable.'
  confirm 'Continue without email-based account recovery?' false || die 'email configuration cancelled'
  FROM_ADDRESS="no-reply@$DOMAIN"
elif [[ $EMAIL == mailtrap_api ]]; then
  FROM_ADDRESS=$(prompt_text 'From address' "$(old KAEDE_EMAIL_FROM_ADDRESS "no-reply@$DOMAIN")")
  [[ $FROM_ADDRESS =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || die 'invalid From address'
  if [[ -n ${OLD[KAEDE_MAILTRAP_API_TOKEN]-} ]] && confirm 'Reuse existing Mailtrap API token?' true; then
    MAILTRAP_TOKEN=${OLD[KAEDE_MAILTRAP_API_TOKEN]}
  else
    MAILTRAP_TOKEN=$(ask_provider_secret 'Mailtrap API token' 16)
  fi
else
  FROM_ADDRESS=$(prompt_text 'From address' "$(old KAEDE_EMAIL_FROM_ADDRESS "no-reply@$DOMAIN")")
  [[ $FROM_ADDRESS =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || die 'invalid From address'
  REUSE_SMTP=false
  if [[ $(old KAEDE_EMAIL_BACKEND '') == smtp && -n ${OLD[KAEDE_SMTP_URL]-} ]] && confirm 'Reuse existing SMTP URL and credentials?' true; then
    REUSE_SMTP=true
  fi
  if [[ $REUSE_SMTP == true ]]; then
    SMTP_URL=${OLD[KAEDE_SMTP_URL]}
  else
    case "$EMAIL" in
      mailtrap_smtp) SMTP_HOST_DEFAULT=live.smtp.mailtrap.io ;;
      ses)
        SES_REGION=$(prompt_text 'AWS SES region' us-east-1)
        SMTP_HOST_DEFAULT="email-smtp.$SES_REGION.amazonaws.com"
        ;;
      *) SMTP_HOST_DEFAULT='' ;;
    esac
    SMTP_HOST=$(prompt_text 'SMTP host' "$SMTP_HOST_DEFAULT")
    [[ $SMTP_HOST =~ ^[A-Za-z0-9.-]+$ ]] || die 'invalid SMTP hostname'
    SMTP_PORT=$(ask_port 'SMTP port' 587 tcp 1)
    if confirm 'Use implicit TLS (SMTPS, normally port 465)?' false; then SMTP_SCHEME=smtps; else SMTP_SCHEME=smtp; fi
    SMTP_AUTH=true
    if [[ $EMAIL == smtp ]] && ! confirm 'Authenticate to the SMTP server?' true; then SMTP_AUTH=false; fi
    if [[ $SMTP_AUTH == true ]]; then
      SMTP_USER=$(prompt_secret 'SMTP username')
      SMTP_PASSWORD=$(prompt_secret 'SMTP password')
      [[ -n $SMTP_USER && -n $SMTP_PASSWORD ]] || die 'SMTP username and password are required'
      SMTP_URL="$SMTP_SCHEME://$(urlencode "$SMTP_USER"):$(urlencode "$SMTP_PASSWORD")@$SMTP_HOST:$SMTP_PORT"
    else
      SMTP_URL="$SMTP_SCHEME://$SMTP_HOST:$SMTP_PORT"
    fi
  fi
fi

section 'Runtime sizing' 'Defaults are appropriate for a small instance and can be changed now.'
if confirm 'Customize worker counts and media limits?' false; then
  API_WORKERS=$(prompt_text 'API workers (1-64)' "$(old KAEDE_API_WORKERS 4)")
  GATEWAY_WORKERS=$(prompt_text 'Gateway replicas (1-64)' "$(old KAEDE_GATEWAY_WORKERS 2)")
  ATTACHMENT_MIB=$(prompt_text 'Maximum attachment size in MiB' "$(( $(old_uint KAEDE_MEDIA_MAX_ATTACHMENT_BYTES 104857600) / 1048576 ))")
  USER_QUOTA_MIB=$(prompt_text 'Per-user media quota in MiB' "$(( $(old_uint KAEDE_MEDIA_USER_QUOTA_BYTES 5368709120) / 1048576 ))")
else
  API_WORKERS=$(old KAEDE_API_WORKERS 4)
  GATEWAY_WORKERS=$(old KAEDE_GATEWAY_WORKERS 2)
  ATTACHMENT_MIB=$(( $(old_uint KAEDE_MEDIA_MAX_ATTACHMENT_BYTES 104857600) / 1048576 ))
  USER_QUOTA_MIB=$(( $(old_uint KAEDE_MEDIA_USER_QUOTA_BYTES 5368709120) / 1048576 ))
fi
[[ $API_WORKERS =~ ^[0-9]+$ ]] && ((API_WORKERS >= 1 && API_WORKERS <= 64)) || die 'API workers must be 1-64'
[[ $GATEWAY_WORKERS =~ ^[0-9]+$ ]] && ((GATEWAY_WORKERS >= 1 && GATEWAY_WORKERS <= 64)) || die 'gateway replicas must be 1-64'
[[ $ATTACHMENT_MIB =~ ^[0-9]+$ ]] && ((ATTACHMENT_MIB >= 1 && ATTACHMENT_MIB <= 10240)) || die 'attachment size must be 1-10240 MiB'
[[ $USER_QUOTA_MIB =~ ^[0-9]+$ ]] && ((USER_QUOTA_MIB >= ATTACHMENT_MIB)) || die 'user quota must be at least one maximum attachment'

MASTER_KEY=$(preserve_or_generate KAEDE_SECRET_KEY b64)
GATEWAY_KEY=$(preserve_or_generate KAEDE_GATEWAY_SECRET_KEY b64)
[[ $MASTER_KEY != "$GATEWAY_KEY" ]] || GATEWAY_KEY=$(random_base64url)
PROXY_SECRET=$(preserve_or_generate KAEDE_PROXY_SECRET hex32)
EDGE_SECRET=$(preserve_or_generate KAEDE_EDGE_SECRET hex32)
[[ $PROXY_SECRET != "$EDGE_SECRET" ]] || EDGE_SECRET=$(random_hex 32)
POSTGRES_PASSWORD=$(preserve_or_generate POSTGRES_PASSWORD hex24)
DRAGONFLY_PASSWORD=$(preserve_or_generate DRAGONFLY_PASSWORD hex32)
if [[ $ADMIN_ENABLED == true ]]; then ADMIN_TOKEN=$(preserve_or_generate KAEDE_ADMIN_TOKEN hex32); else ADMIN_TOKEN=; fi
if [[ $VOICE == true ]]; then
  LIVEKIT_KEY=$(preserve_or_generate LIVEKIT_API_KEY livekit)
  LIVEKIT_SECRET=$(preserve_or_generate LIVEKIT_API_SECRET hex32)
else
  LIVEKIT_KEY= LIVEKIT_SECRET=
fi
if [[ $OBSERVABILITY == true ]]; then
  GRAFANA_USER=$(old GRAFANA_ADMIN_USER admin)
  GRAFANA_PASSWORD=$(preserve_or_generate GRAFANA_ADMIN_PASSWORD b64)
else
  GRAFANA_USER=admin GRAFANA_PASSWORD=
fi
LOG_LEVEL=${OLD[KAEDE_LOG_LEVEL]-INFO}
LOG_LEVEL=${LOG_LEVEL^^}
case "$LOG_LEVEL" in DEBUG|INFO|WARNING|ERROR) ;; *) die 'existing KAEDE_LOG_LEVEL is invalid' ;; esac

PROFILES=()
[[ $VOICE == true ]] && PROFILES+=(voice)
[[ $OBSERVABILITY == true ]] && PROFILES+=(observability)
COMPOSE_PROFILES=
if ((${#PROFILES[@]})); then
  COMPOSE_PROFILES=$(IFS=,; printf '%s' "${PROFILES[*]}")
fi

STAGE_DIR=$(mktemp -d "$ROOT/.kaede-setup-stage.XXXXXX")
chmod 700 "$STAGE_DIR"
mkdir -p "$STAGE_DIR/generated"

emit() {
  local key=$1 value=$2
  [[ $key =~ ^[A-Z][A-Z0-9_]*$ ]] || die "internal invalid environment key: $key"
  [[ $value != *$'\n'* && $value != *$'\r'* ]] || die "invalid control character in $key"
  [[ $value =~ ^[-A-Za-z0-9_./:@%+,=~{}]*$ ]] || die "$key contains characters unsafe for a Compose dotenv file"
  printf '%s=%s\n' "$key" "$value"
}

{
  printf '# Generated by ./setup.sh. Keep this file private (mode 0600).\n'
  emit SETUP_STORAGE_PROVIDER "$STORAGE"
  emit SETUP_EMAIL_PROVIDER "$EMAIL"
  emit SETUP_HOST_NGINX "$HOST_NGINX"
  emit OPERATOR_ENV_UID "$(id -u)"
  emit OPERATOR_ENV_GID "$(id -g)"
  emit KAEDE_DOMAIN "$DOMAIN"
  emit KAEDE_ENVIRONMENT production
  emit KAEDE_SECRET_KEY "$MASTER_KEY"
  emit KAEDE_GATEWAY_SECRET_KEY "$GATEWAY_KEY"
  emit KAEDE_PROXY_SECRET "$PROXY_SECRET"
  emit KAEDE_EDGE_SECRET "$EDGE_SECRET"
  [[ -z $ADMIN_TOKEN ]] || emit KAEDE_ADMIN_TOKEN "$ADMIN_TOKEN"
  emit KAEDE_LOG_LEVEL "$LOG_LEVEL"
  emit KAEDE_FEDERATION_MODE "$FEDERATION"
  emit KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED true
  emit KAEDE_FEDERATION_HISTORY_EXPORT_TTL_MINUTES 1440
  emit KAEDE_FEDERATION_HISTORY_PAGE_MESSAGES 100
  emit KAEDE_FEDERATION_HISTORY_MAX_MESSAGES 250000
  emit POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
  emit KAEDE_DATABASE_URL "postgresql+asyncpg://kaede:$POSTGRES_PASSWORD@postgres:5432/kaede"
  emit DRAGONFLY_PASSWORD "$DRAGONFLY_PASSWORD"
  emit KAEDE_DRAGONFLY_URL "redis://:$DRAGONFLY_PASSWORD@dragonfly:6379/0"
  emit KAEDE_MEDIA_STORAGE_BACKEND "$STORAGE_BACKEND"
  emit KAEDE_MEDIA_S3_ENDPOINT "$S3_ENDPOINT"
  emit KAEDE_MEDIA_PUBLIC_BASE_URL "$MEDIA_PUBLIC"
  emit KAEDE_MEDIA_S3_REGION "$S3_REGION"
  emit KAEDE_MEDIA_S3_ADDRESSING_STYLE "$S3_STYLE"
  emit KAEDE_MEDIA_S3_CREATE_BUCKETS "$S3_CREATE"
  emit KAEDE_MEDIA_S3_INIT_TIMEOUT_SECONDS 120
  emit KAEDE_MEDIA_S3_ACCESS_KEY "$S3_ACCESS"
  emit KAEDE_MEDIA_S3_SECRET_KEY "$S3_SECRET"
  [[ -z $S3_SESSION ]] || emit KAEDE_MEDIA_S3_SESSION_TOKEN "$S3_SESSION"
  emit KAEDE_MEDIA_ATTACHMENTS_BUCKET "$ATTACHMENTS_BUCKET"
  emit KAEDE_MEDIA_DERIVED_BUCKET "$DERIVED_BUCKET"
  emit KAEDE_MEDIA_REMOTE_CACHE_BUCKET "$CACHE_BUCKET"
  [[ -z $GARAGE_RPC ]] || emit GARAGE_RPC_SECRET "$GARAGE_RPC"
  [[ -z $GARAGE_ADMIN ]] || emit GARAGE_ADMIN_TOKEN "$GARAGE_ADMIN"
  emit KAEDE_MEDIA_MAX_ATTACHMENT_BYTES "$((ATTACHMENT_MIB * 1048576))"
  emit KAEDE_MEDIA_USER_QUOTA_BYTES "$((USER_QUOTA_MIB * 1048576))"
  emit KAEDE_MEDIA_SCAN_ENABLED true
  emit KAEDE_MEDIA_INFLIGHT_LIMIT "$(old_uint KAEDE_MEDIA_INFLIGHT_LIMIT 10)"
  emit KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES "$(old_uint KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES 524288000)"
  emit KAEDE_MEDIA_UPLOAD_TTL_SECONDS "$(old_uint KAEDE_MEDIA_UPLOAD_TTL_SECONDS 900)"
  emit KAEDE_MEDIA_REMOTE_CACHE_BYTES "$(old_uint KAEDE_MEDIA_REMOTE_CACHE_BYTES 21474836480)"
  emit KAEDE_MEDIA_REMOTE_CACHE_TTL_DAYS "$(old_uint KAEDE_MEDIA_REMOTE_CACHE_TTL_DAYS 30)"
  emit KAEDE_MEDIA_EMOJI_LIMIT "$(old_uint KAEDE_MEDIA_EMOJI_LIMIT 100)"
  if [[ $EMAIL == disabled ]]; then
    emit KAEDE_EMAIL_BACKEND disabled
  elif [[ $EMAIL == mailtrap_api ]]; then
    emit KAEDE_EMAIL_BACKEND mailtrap_api
    emit KAEDE_MAILTRAP_API_TOKEN "$MAILTRAP_TOKEN"
  else
    emit KAEDE_EMAIL_BACKEND smtp
    emit KAEDE_SMTP_URL "$SMTP_URL"
  fi
  [[ $EMAIL == disabled ]] || emit KAEDE_EMAIL_FROM_ADDRESS "$FROM_ADDRESS"
  emit KAEDE_APP_URL "https://$DOMAIN"
  emit KAEDE_API_WORKERS "$API_WORKERS"
  emit KAEDE_GATEWAY_WORKERS "$GATEWAY_WORKERS"
  emit KAEDE_CADDY_HOST_PORT "$EDGE_PORT"
  emit KAEDE_API_HOST_PORT "$API_PORT"
  emit KAEDE_GRAFANA_HOST_PORT "$GRAFANA_PORT"
  emit KAEDE_VOICE_ENABLED "$VOICE"
  emit KAEDE_TURN_UDP_PORT "$TURN_PORT"
  if [[ $VOICE == true ]]; then
    emit KAEDE_VOICE_PUBLIC_URL "wss://$DOMAIN/livekit"
    emit KAEDE_VOICE_LIVEKIT_URL http://host.docker.internal:7880
    emit LIVEKIT_API_KEY "$LIVEKIT_KEY"
    emit LIVEKIT_API_SECRET "$LIVEKIT_SECRET"
    emit LIVEKIT_TURN_CERT_PATH "$CERT_PATH"
    emit LIVEKIT_TURN_KEY_PATH "$KEY_PATH"
  fi
  if [[ $OBSERVABILITY == true ]]; then
    emit GRAFANA_ADMIN_USER "$GRAFANA_USER"
    emit GRAFANA_ADMIN_PASSWORD "$GRAFANA_PASSWORD"
  fi
  [[ -z $COMPOSE_PROFILES ]] || emit COMPOSE_PROFILES "$COMPOSE_PROFILES"
} > "$STAGE_DIR/env"
chmod 600 "$STAGE_DIR/env"

render_overlay() {
  local output=$1
  {
    printf '# Generated by ./setup.sh; rerun the script instead of editing.\nservices:'
    if [[ $STORAGE == garage ]]; then
      printf ' {}\n'
      return
    fi
    printf '\n'
    if [[ $STORAGE != garage ]]; then
      cat <<'YAML'
  garage:
    profiles: [disabled-garage-generated]
YAML
    fi
  } > "$output"
  chmod 644 "$output"
}
render_overlay "$STAGE_DIR/compose.yml"

remove_marked_blocks() {
  local source=$1 output=$2 remove_upstream=$3
  awk -v remove_upstream="$remove_upstream" '
    /# KAEDE_SETUP_GARAGE_MEDIA_BEGIN/ { skip=1; next }
    /# KAEDE_SETUP_GARAGE_MEDIA_END/ { skip=0; next }
    remove_upstream == "true" && /# KAEDE_SETUP_GARAGE_UPSTREAM_BEGIN/ { skip=1; next }
    remove_upstream == "true" && /# KAEDE_SETUP_GARAGE_UPSTREAM_END/ { skip=0; next }
    !skip && $0 !~ /^# KAEDE_SETUP_/ { print }
  ' "$source" > "$output"
}

sed_escape() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

if [[ $HOST_NGINX == true ]]; then
  [[ -f $ROOT/deploy/nginx/kaede.conf.example ]] || die 'missing deploy/nginx/kaede.conf.example'
  HOST_STAGE="$STAGE_DIR/generated/kaede.nginx.conf.raw"
  if [[ $STORAGE == garage ]]; then
    awk '$0 !~ /^# KAEDE_SETUP_/' "$ROOT/deploy/nginx/kaede.conf.example" > "$HOST_STAGE"
  else
    remove_marked_blocks "$ROOT/deploy/nginx/kaede.conf.example" "$HOST_STAGE" false
  fi
  sed \
    -e "s|chat\.example\.com|$(sed_escape "$DOMAIN")|g" \
    -e "s|server 127\.0\.0\.1:18081;|server 127.0.0.1:$EDGE_PORT;|g" \
    -e "s|/etc/letsencrypt/live/$DOMAIN/fullchain\.pem|$(sed_escape "$CERT_PATH")|g" \
    -e "s|/etc/letsencrypt/live/$DOMAIN/privkey\.pem|$(sed_escape "$KEY_PATH")|g" \
    -e "s|replace-with-kaede-edge-secret|$EDGE_SECRET|g" \
    -e "s|client_max_body_size 101m;|client_max_body_size $((ATTACHMENT_MIB + 1))m;|g" \
    "$HOST_STAGE" > "$STAGE_DIR/generated/kaede.nginx.conf"
  rm -f "$HOST_STAGE"
  chmod 600 "$STAGE_DIR/generated/kaede.nginx.conf"
fi

{
  printf 'Kaede deployment configuration generated by ./setup.sh.\n\n'
  printf 'Nothing was started, installed, or reloaded.\n\n'
  printf 'Validate:\n  make env-check\n  make generated-compose-check\n\n'
  printf 'Internal Caddy edge: 127.0.0.1:%s\n' "$EDGE_PORT"
  printf 'Selected storage: %s\nSelected email: %s\n' "$STORAGE" "$EMAIL"
  if [[ $HOST_NGINX == true ]]; then
    printf '\nHost nginx: review deploy/generated/kaede.nginx.conf, which references your existing TLS files. No certificate was generated. Install the config manually, run nginx -t, then reload nginx yourself.\n'
  fi
  if [[ $STORAGE != garage ]]; then
    printf '\nKeep all S3 buckets private and configure browser CORS for PUT/GET/HEAD from https://%s.\n' "$DOMAIN"
  fi
  if [[ $VOICE == true ]]; then
    printf '\nVoice requires TCP 7880/7881/5349 and UDP 7882/%s plus valid TURN certificate files.\n' "$TURN_PORT"
  fi
  printf '\nAfter review, start explicitly with:\n'
  printf '  KAEDE_OPERATOR_ENV_FILE="$PWD/.env" docker compose --env-file .env -f deploy/compose.yml -f deploy/compose.generated.yml up -d --build --wait\n'
  printf '\nStartup automatically applies pending Alembic migrations in revision order, then bootstraps the instance. Application services start only after that one-shot step succeeds.\n'
} > "$STAGE_DIR/generated/README.txt"
chmod 644 "$STAGE_DIR/generated/README.txt"

if [[ $USE_GUM == true ]]; then
  gum style --border double --border-foreground 42 --padding "0 1" --bold \
    'Ready to write' \
    "Domain: $DOMAIN" \
    "Internal Caddy: 127.0.0.1:$EDGE_PORT" \
    "Storage: $STORAGE" \
    "Email: $EMAIL" \
    "Host nginx file: $HOST_NGINX" \
    "Voice: $VOICE" \
    "Observability: $OBSERVABILITY" \
    'Secrets: generated or preserved; never displayed'
else
  printf '\n%s%sReady to write%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
  printf '  Domain:          %s\n  Internal Caddy:  127.0.0.1:%s\n' "$DOMAIN" "$EDGE_PORT"
  printf '  Storage:         %s\n  Email:           %s\n' "$STORAGE" "$EMAIL"
  printf '  Host nginx file: %s\n  Voice:           %s\n  Observability:   %s\n' "$HOST_NGINX" "$VOICE" "$OBSERVABILITY"
  printf '  Secrets:         generated or preserved; never displayed\n'
fi

if [[ $DRY_RUN == true ]]; then
  note 'Dry run complete; no files were written.'
  rm -rf "$STAGE_DIR"
  STAGE_DIR=
  exit 0
fi
confirm 'Write this configuration?' false || { note 'Cancelled; no files were written.'; exit 0; }

safe_target() {
  local path=$1
  [[ -e $path || -L $path ]] || return 0
  [[ -f $path && ! -L $path ]] || die "refusing unsafe output target: ${path#"$ROOT/"}"
  [[ $(stat -c '%h' "$path") == 1 ]] || die "refusing hard-linked output target: ${path#"$ROOT/"}"
}

atomic_install() {
  local source=$1 destination=$2 mode=$3 temporary
  safe_target "$destination"
  mkdir -p "$(dirname -- "$destination")"
  temporary=$(mktemp "${destination}.tmp.XXXXXX")
  install -m "$mode" "$source" "$temporary"
  mv -fT "$temporary" "$destination"
}

for relative in "${OUTPUTS[@]}"; do safe_target "$ROOT/$relative"; done
[[ ! -L $GENERATED_DIR ]] || die 'deploy/generated must not be a symlink'
[[ ! -L $ROOT/deploy ]] || die 'deploy must not be a symlink'
mkdir -p "$GENERATED_DIR"
chmod 700 "$GENERATED_DIR"

BACKUP_DIR="$ROOT/.kaede-backups/setup-$(date -u +%Y%m%dT%H%M%SZ)-$(random_hex 4)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$ROOT/.kaede-backups" "$BACKUP_DIR"
for relative in "${OUTPUTS[@]}"; do
  if [[ -f $ROOT/$relative ]]; then
    mkdir -p "$BACKUP_DIR/$(dirname -- "$relative")"
    install -m 600 "$ROOT/$relative" "$BACKUP_DIR/$relative"
  fi
done

restore_previous() {
  local relative target mode
  for relative in "${OUTPUTS[@]}"; do
    target="$ROOT/$relative"
    if [[ -f $BACKUP_DIR/$relative ]]; then
      case "$relative" in
        deploy/compose.generated.yml|deploy/generated/README.txt) mode=644 ;;
        *) mode=600 ;;
      esac
      atomic_install "$BACKUP_DIR/$relative" "$target" "$mode" || return 1
    else
      rm -f -- "$target" || return 1
    fi
  done
}

printf 'Kaede setup is publishing configuration. Backup: %s\n' "${BACKUP_DIR#"$ROOT/"}" > "$MARKER_FILE"
chmod 600 "$MARKER_FILE"
PUBLISHING=true

if [[ -f $STAGE_DIR/generated/kaede.nginx.conf ]]; then
  atomic_install "$STAGE_DIR/generated/kaede.nginx.conf" "$GENERATED_DIR/kaede.nginx.conf" 600
else
  rm -f -- "$GENERATED_DIR/kaede.nginx.conf"
fi
atomic_install "$STAGE_DIR/generated/README.txt" "$GENERATED_DIR/README.txt" 644
atomic_install "$STAGE_DIR/compose.yml" "$OVERLAY_FILE" 644
atomic_install "$STAGE_DIR/env" "$ENV_FILE" 600
rm -f -- "$MARKER_FILE"
PUBLISHING=false
rm -rf -- "$STAGE_DIR"
STAGE_DIR=

printf '\n%sConfiguration ready.%s\n' "$C_GREEN" "$C_RESET"
printf 'Review deploy/generated/README.txt, then run: make env-check\n'
printf 'A private rollback copy is in %s\n' "${BACKUP_DIR#"$ROOT/"}"
