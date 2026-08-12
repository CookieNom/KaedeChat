#!/usr/bin/env bash

set -Eeuo pipefail

SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
TEST_ROOT=$(mktemp -d /tmp/kaede-auto-update-test.XXXXXX)
cleanup() { rm -rf -- "$TEST_ROOT"; }
trap cleanup EXIT INT TERM HUP

mkdir -p "$TEST_ROOT/repo/deploy" "$TEST_ROOT/bin" "$TEST_ROOT/home"
cp "$SOURCE_ROOT/deploy/auto-update.sh" "$TEST_ROOT/repo/deploy/auto-update.sh"
cp "$SOURCE_ROOT/deploy/install-auto-update.sh" "$TEST_ROOT/repo/deploy/install-auto-update.sh"
printf '# test checkout\n' > "$TEST_ROOT/repo/README.md"
printf '%s\n' \
  'AUTO_UPDATE_ENABLED=false' \
  'AUTO_UPDATE_REMOTE=origin' \
  'AUTO_UPDATE_BRANCH=main' \
  'AUTO_UPDATE_INTERVAL=12h' \
  'AUTO_UPDATE_JITTER=30m' \
  'AUTO_UPDATE_WAIT_TIMEOUT_SECONDS=300' > "$TEST_ROOT/repo/.env"
chmod 600 "$TEST_ROOT/repo/.env"
printf '#!/usr/bin/env bash\nexit 0\n' > "$TEST_ROOT/bin/systemctl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$TEST_ROOT/bin/docker"
chmod 755 "$TEST_ROOT/bin/systemctl" "$TEST_ROOT/bin/docker"

git -C "$TEST_ROOT/repo" init -q -b main
git -C "$TEST_ROOT/repo" config user.name 'Kaede updater test'
git -C "$TEST_ROOT/repo" config user.email updater-test@kaede.invalid
git -C "$TEST_ROOT/repo" add README.md deploy
git -C "$TEST_ROOT/repo" commit -qm baseline
git -C "$TEST_ROOT/repo" remote add origin https://example.invalid/kaede.git

TEST_ENV=(
  env
  "HOME=$TEST_ROOT/home"
  "PATH=$TEST_ROOT/bin:$PATH"
)

sed -i 's#AUTO_UPDATE_REMOTE=origin#AUTO_UPDATE_REMOTE=https://operator:do-not-display@example.invalid/repository#' \
  "$TEST_ROOT/repo/.env"
if invalid_remote_output=$("${TEST_ENV[@]}" "$TEST_ROOT/repo/deploy/auto-update.sh" status 2>&1); then
  printf 'credential-bearing update remote was unexpectedly accepted\n' >&2
  exit 1
fi
grep -q 'AUTO_UPDATE_REMOTE must be a Git remote name' <<< "$invalid_remote_output"
if grep -q 'do-not-display' <<< "$invalid_remote_output"; then
  printf 'invalid update remote leaked credentials in its error message\n' >&2
  exit 1
fi
sed -i 's#AUTO_UPDATE_REMOTE=https://operator:do-not-display@example.invalid/repository#AUTO_UPDATE_REMOTE=origin#' \
  "$TEST_ROOT/repo/.env"

"${TEST_ENV[@]}" "$TEST_ROOT/repo/deploy/install-auto-update.sh" enable >/dev/null
grep -qx 'AUTO_UPDATE_ENABLED=true' "$TEST_ROOT/repo/.env"
grep -q '^OnCalendar=\*-\*-\* 00/12:00:00$' \
  "$TEST_ROOT/home/.config/systemd/user/kaede-auto-update.timer"
grep -qx "WorkingDirectory=$TEST_ROOT/repo" \
  "$TEST_ROOT/home/.config/systemd/user/kaede-auto-update.service"
test ! -e "$TEST_ROOT/repo/.kaede-auto-update-state"
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify \
    "$TEST_ROOT/home/.config/systemd/user/kaede-auto-update.service" \
    "$TEST_ROOT/home/.config/systemd/user/kaede-auto-update.timer" >/dev/null
fi

"${TEST_ENV[@]}" "$TEST_ROOT/repo/deploy/install-auto-update.sh" disable >/dev/null
grep -qx 'AUTO_UPDATE_ENABLED=false' "$TEST_ROOT/repo/.env"
test ! -e "$TEST_ROOT/home/.config/systemd/user/kaede-auto-update.timer"
test ! -e "$TEST_ROOT/home/.config/systemd/user/kaede-auto-update.service"

sed -i 's/AUTO_UPDATE_BRANCH=main/AUTO_UPDATE_BRANCH=other/' "$TEST_ROOT/repo/.env"
if branch_output=$("${TEST_ENV[@]}" "$TEST_ROOT/repo/deploy/install-auto-update.sh" enable 2>&1); then
  printf 'wrong checked-out branch was unexpectedly accepted\n' >&2
  exit 1
fi
grep -q "check out the configured branch 'other'" <<< "$branch_output"
grep -q 'or change AUTO_UPDATE_BRANCH' <<< "$branch_output"
grep -qx 'AUTO_UPDATE_ENABLED=false' "$TEST_ROOT/repo/.env"
test ! -e "$TEST_ROOT/home/.config/systemd/user/kaede-auto-update.timer"
sed -i 's/AUTO_UPDATE_BRANCH=other/AUTO_UPDATE_BRANCH=main/' "$TEST_ROOT/repo/.env"

sed -i 's/AUTO_UPDATE_ENABLED=false/AUTO_UPDATE_ENABLED=true/' "$TEST_ROOT/repo/.env"
printf 'dirty\n' >> "$TEST_ROOT/repo/README.md"
if output=$("${TEST_ENV[@]}" "$TEST_ROOT/repo/deploy/auto-update.sh" run-now 2>&1); then
  printf 'dirty checkout was unexpectedly accepted\n' >&2
  exit 1
fi
grep -q 'tracked files are modified' <<< "$output"

printf 'automatic update tests passed\n'
