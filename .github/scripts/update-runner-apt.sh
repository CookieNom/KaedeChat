#!/usr/bin/env bash
set -euo pipefail

# Hosted runners include this unrelated feed; its 403s block Ubuntu package installs.
# Keep Ubuntu and LLVM sources, and their signature verification, intact.
rm -f /etc/apt/sources.list.d/microsoft-prod.list /etc/apt/sources.list.d/microsoft-prod.sources
apt-get update
