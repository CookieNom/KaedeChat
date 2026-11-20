#!/usr/bin/env bash
set -euo pipefail

# Inspect the device binary's dyld exports, which back DynamicLibrary.process().
if [[ "$(uname -s)" == Darwin ]]; then
  objdump=(xcrun objdump)
else
  objdump=(llvm-objdump)
fi
exports=$("${objdump[@]}" --macho --arch=arm64 --exports-trie "${1:?Expected the Runner executable}")
missing=0
for symbol in _kaede_e2ee_invoke _kaede_e2ee_close _kaede_e2ee_buffer_free; do
  if ! awk -v symbol="$symbol" '$2 == symbol { found = 1 } END { exit !found }' <<< "$exports"; then
    echo "Missing iOS MLS export $symbol" >&2
    missing=1
  fi
done
exit "$missing"
