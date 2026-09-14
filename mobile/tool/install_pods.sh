#!/usr/bin/env bash
# Retry transient downloads, while preserving deployment-mode lockfile checks.
set -euo pipefail

log_file="$(mktemp)"
trap 'rm -f "$log_file"' EXIT
for attempt in 1 2 3 4; do
  if pod install --deployment --project-directory=ios 2>&1 | tee "$log_file"; then
    exit 0
  else
    status=${PIPESTATUS[0]}
  fi
  if [[ "$attempt" == 4 ]] || ! grep -Eiq \
    'Could not resolve host|Could not resolve hostname|Temporary failure in name resolution|Connection (timed out|reset)|Failed to connect|HTTP[^[:cntrl:]]* (502|503|504)|requested URL returned error: (502|503|504)' "$log_file"; then
    exit "$status"
  fi
  echo "CocoaPods download failed; retrying in $((attempt * 10)) seconds ($attempt/4)." >&2
  sleep "$((attempt * 10))"
done
