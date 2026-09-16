#!/usr/bin/env bash
# Runs against an isolated server; never changes the desktop's audio routing.
set -euo pipefail
capture_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/crates/kaede-capture/native"
capture_tmp="$(mktemp -d)"
capture_jobs=()
cleanup() {
  for pid in "${capture_jobs[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf -- "$capture_tmp"
}
trap cleanup EXIT
export PULSE_SERVER="unix:$capture_tmp/pulse.socket"
export XDG_CONFIG_HOME="$capture_tmp/config"
export PULSE_RUNTIME_PATH="$capture_tmp/runtime"
mkdir -m 700 "$PULSE_RUNTIME_PATH"
# Standard pkg-config flags intentionally undergo shell word splitting.
# shellcheck disable=SC2046
"${CXX:-c++}" -std=c++17 "$capture_root/linux.cpp" "$capture_root/linux_test.cpp" \
  $(pkg-config --cflags --libs libpulse libpulse-simple xcb) -pthread -o "$capture_tmp/test"
pulseaudio --daemonize=no --use-pid-file=no --exit-idle-time=-1 --disable-shm=yes -n \
  --load="module-native-protocol-unix socket=$capture_tmp/pulse.socket auth-anonymous=1" \
  --load="module-null-sink sink_name=kaede_test" >"$capture_tmp/server.log" 2>&1 &
capture_jobs+=("$!")
for _ in {1..100}; do
  [[ -S "$capture_tmp/pulse.socket" ]] && break
  sleep 0.05
done
if [[ ! -S "$capture_tmp/pulse.socket" ]]; then cat "$capture_tmp/server.log"; exit 1; fi
"$capture_tmp/test" play 0 &
selected_pid=$!
capture_jobs+=("$selected_pid")
"$capture_tmp/test" play 1 &
capture_jobs+=("$!")
"$capture_tmp/test" capture "$selected_pid"
"$capture_tmp/test" capture 0
"$capture_tmp/test" missing 4294967294
echo 'Screen audio isolation, desktop mixing, missing-app rejection, and cleanup passed.'
