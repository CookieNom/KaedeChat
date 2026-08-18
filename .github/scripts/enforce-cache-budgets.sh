#!/usr/bin/env bash
set -euo pipefail

# The full desktop target tree can exceed GitHub's repository-wide cache
# allowance on its own. Build output is deliberately never cached; these caps
# keep only immutable/download caches bounded before pnpm and uv are counted.

# Every deletion below targets disposable runner caches. Refuse to operate on a
# developer workstation even if this helper is invoked manually by mistake.
if [[ ${GITHUB_ACTIONS:-} != true || -z ${RUNNER_TEMP:-} || -z ${GITHUB_WORKSPACE:-} ]]; then
  echo "This cache-budget helper may run only inside GitHub Actions." >&2
  exit 2
fi

cd "$GITHUB_WORKSPACE"

cache_size_kib() {
  local total=0 path size
  for path in "$@"; do
    if [[ -e "$path" ]]; then
      size="$(du -sk "$path" | cut -f1)"
      total=$((total + size))
    fi
  done
  printf '%s\n' "$total"
}

enforce_cargo_download_budget() {
  local paths=(
    "$HOME/.cargo/registry/cache"
    "$HOME/.cargo/registry/index"
    "$HOME/.cargo/git/db"
  )
  if (( $(cache_size_kib "${paths[@]}") > 524288 )); then
    echo "Cargo download cache exceeded 512 MiB; skipping it for this run."
    rm -rf "${paths[@]}"
  fi
}

enforce_tauri_cli_budget() {
  local paths=(
    "$HOME/.cargo/bin/cargo-tauri"
    "$HOME/.cargo/bin/cargo-tauri.exe"
  )
  if (( $(cache_size_kib "${paths[@]}") > 131072 )); then
    echo "Tauri CLI cache exceeded 128 MiB; skipping it for this run."
    rm -rf "${paths[@]}"
  fi
}

enforce_mobile_budgets() {
  local flutter_cache="$RUNNER_TEMP/flutter/bin/cache"
  if (( $(cache_size_kib "$flutter_cache") > 2359296 )); then
    echo "Flutter cache exceeded 2.25 GiB; skipping it for this run."
    rm -rf "$flutter_cache"
  fi

  local pub_paths=("$HOME/.pub-cache/hosted" "$HOME/.pub-cache/git")
  if (( $(cache_size_kib "${pub_paths[@]}") > 786432 )); then
    echo "Dart package cache exceeded 768 MiB; skipping it for this run."
    rm -rf "${pub_paths[@]}"
  fi

  local gradle_paths=(
    "$HOME/.gradle/caches/modules-2"
    "$HOME/.gradle/wrapper/dists"
  )
  if (( $(cache_size_kib "${gradle_paths[@]}") > 1572864 )); then
    echo "Gradle cache exceeded 1.5 GiB; retaining only wrapper downloads."
    rm -rf "$HOME/.gradle/caches/modules-2"
    if (( $(cache_size_kib "$HOME/.gradle/wrapper/dists") > 1572864 )); then
      echo "Gradle wrapper cache alone exceeded 1.5 GiB; skipping it for this run."
      rm -rf "$HOME/.gradle/wrapper/dists"
    fi
  fi

}

case "${1:-}" in
  cargo)
    enforce_cargo_download_budget
    ;;
  desktop-release)
    enforce_cargo_download_budget
    enforce_tauri_cli_budget
    ;;
  mobile)
    enforce_cargo_download_budget
    enforce_mobile_budgets
    ;;
  *)
    echo "usage: $0 {cargo|desktop-release|mobile}" >&2
    exit 2
    ;;
esac
