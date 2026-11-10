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

for existing in kaede kaede-chat-dev; do
  result=$(eval "$block" <<< ''; printf '%s' "$PROJECT_NAME")
  [[ $result == "$existing" ]]
done
existing=
result=$(eval "$block" <<< 'kaede-chat'; printf '%s' "$PROJECT_NAME")
[[ $result == kaede-chat ]]
result=$(eval "$block" <<< $'Bad Name\n-invalid\nvalid-2'; printf '%s' "$PROJECT_NAME")
[[ $result == valid-2 ]]
printf 'setup project name tests passed\n'

# Exercise actual emission and carry-over code without invoking the wizard.
declare -A OLD=(
  [KAEDE_DOMAIN]=old.example
  [KAEDE_LEGAL_OPERATOR_NAME]='Maple & Co'
  [KAEDE_LANDING_PAGE]=custom
  [CUSTOM_SETTING]='literal$(do-not-run)'
  [KAEDE_SMTP_URL]=obsolete
)
die() { printf '%s\n' "$*" >&2; exit 1; }
eval "$(sed -n '/^emit() {/,/^}/p' "$ROOT/setup.sh")"
result=$(emit KAEDE_DOMAIN new.example
  eval "$(sed -n '/^  # Do not restore credentials/,/^} > /p' "$ROOT/setup.sh" | sed '$d')")
[[ $result == *'KAEDE_DOMAIN=new.example'* ]]
[[ $result != *'KAEDE_DOMAIN=old.example'* ]]
[[ $result == *'KAEDE_LEGAL_OPERATOR_NAME=Maple & Co'* ]]
[[ $result == *'KAEDE_LANDING_PAGE=custom'* ]]
[[ $result == *'CUSTOM_SETTING=literal$(do-not-run)'* ]]
[[ $result != *'KAEDE_SMTP_URL='* ]]
# The relay service instruction must be a dotenv comment.
relay_line=$(sed -n "/^    printf.*Push relay: create/p" "$ROOT/setup.sh")
[[ $(DOMAIN=example.org; eval "$relay_line") == *'# Push relay:'* ]]
printf 'setup setting preservation tests passed\n'

push_defaults=$(sed -n '/^PUSH_DEFAULT=/,/^PUSH_CHOICE=/p' "$ROOT/setup.sh" | sed '$d')
old() { printf '%s' "${OLD[$1]-$2}"; }
OLD=([KAEDE_PUSH_ENABLED]=false [KAEDE_PUSH_RELAY_ENABLED]=false)
eval "$push_defaults"
[[ $PUSH_DEFAULT == Disabled ]]
OLD=([KAEDE_PUSH_ENABLED]=true [KAEDE_PUSH_RELAY_ENABLED]=false)
eval "$push_defaults"
[[ $PUSH_DEFAULT == 'Direct Firebase for a custom/community app build' ]]
OLD=()
eval "$push_defaults"
[[ $PUSH_DEFAULT == 'Official Kaede Push Relay (recommended)' ]]
printf 'setup push defaults tests passed\n'
