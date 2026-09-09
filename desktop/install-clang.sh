#!/bin/sh
# Run as root on Debian/Ubuntu; libwebrtc requires Clang 21 or newer.
set -eu
. /etc/os-release
apt-get update
apt-get install -y --no-install-recommends ca-certificates wget
wget -qO /usr/share/keyrings/apt.llvm.org.asc https://apt.llvm.org/llvm-snapshot.gpg.key
echo "deb [signed-by=/usr/share/keyrings/apt.llvm.org.asc] https://apt.llvm.org/${VERSION_CODENAME}/ llvm-toolchain-${VERSION_CODENAME}-21 main" > /etc/apt/sources.list.d/llvm-21.list
apt-get update
apt-get install -y --no-install-recommends clang-21
