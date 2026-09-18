#!/usr/bin/env bash
# Keep dependency resolution locked while allowing transient downloads to recover.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
exec bash "$repo_dir/.github/scripts/retry-network.sh" pod install --deployment --project-directory=ios
