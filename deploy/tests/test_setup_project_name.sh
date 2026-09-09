#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
# Exercise the wizard's actual prompt block without running deployment setup.
block=$(sed -n '/^  PROJECT_NAME=$(prompt_text /,/^done$/p' "$ROOT/setup.sh")
[[ -n $block ]]
block=$'while true; do\n'"$block"
old() { printf '%s' "$existing"; }
prompt_text() { local answer; read -r answer; printf '%s' "${answer:-$2}"; }
warn() { printf '%s\n' "$*" >&2; }

for existing in '' kaede-chat-dev; do
  result=$(eval "$block" <<< ''; printf '%s' "$PROJECT_NAME")
  [[ $result == "$existing" ]]
done
existing=
result=$(eval "$block" <<< 'kaede-chat'; printf '%s' "$PROJECT_NAME")
[[ $result == kaede-chat ]]
result=$(eval "$block" <<< $'Bad Name\n-invalid\nvalid_2'; printf '%s' "$PROJECT_NAME")
[[ $result == valid_2 ]]
printf 'setup project name tests passed\n'
