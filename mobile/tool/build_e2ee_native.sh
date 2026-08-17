#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mobile_dir="$(cd -- "$script_dir/.." && pwd)"
repo_dir="$(cd -- "$mobile_dir/.." && pwd)"
manifest="$repo_dir/desktop/crates/kaede-e2ee-ffi/Cargo.toml"
cargo_bin="${CARGO:-$(command -v cargo || true)}"
rustup_bin="${RUSTUP:-$(command -v rustup || true)}"
if [[ -z "$cargo_bin" ]]; then
  cargo_bin="${CARGO_HOME:-${HOME}/.cargo}/bin/cargo"
fi
if [[ -z "$rustup_bin" ]]; then
  rustup_bin="${CARGO_HOME:-${HOME}/.cargo}/bin/rustup"
fi
if [[ ! -x "$cargo_bin" ]]; then
  echo "Rust cargo was not found; install Rust or set CARGO." >&2
  exit 1
fi
# Rustup selects the pinned toolchain from the current directory rather than
# from --manifest-path. Build from the workspace root so mobile builds cannot
# silently use an older global Cargo.
cd "$repo_dir/desktop"

build_android() {
  local ndk_root="${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}"
  if [[ -z "$ndk_root" ]]; then
    local sdk_root="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
    local ndk_version="${KAEDE_ANDROID_NDK_VERSION:-27.0.12077973}"
    ndk_root="$sdk_root/ndk/$ndk_version"
  fi
  if [[ ! -d "$ndk_root/toolchains/llvm/prebuilt" ]]; then
    echo "Android NDK was not found; set ANDROID_NDK_HOME." >&2
    exit 1
  fi
  local host
  host=""
  local candidate
  for candidate in "$ndk_root"/toolchains/llvm/prebuilt/*; do
    if [[ -d "$candidate" ]]; then
      host="$candidate"
      break
    fi
  done
  if [[ -z "$host" ]]; then
    echo "Android NDK LLVM toolchain was not found." >&2
    exit 1
  fi
  local bin="$host/bin"
  local api="${KAEDE_ANDROID_MIN_SDK:-23}"
  mkdir -p "$mobile_dir/android/app/src/main/jniLibs/arm64-v8a"
  mkdir -p "$mobile_dir/android/app/src/main/jniLibs/armeabi-v7a"
  mkdir -p "$mobile_dir/android/app/src/main/jniLibs/x86_64"

  CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$bin/aarch64-linux-android${api}-clang" \
    "$cargo_bin" build --locked --release --manifest-path "$manifest" --target aarch64-linux-android
  CARGO_TARGET_ARMV7_LINUX_ANDROIDEABI_LINKER="$bin/armv7a-linux-androideabi${api}-clang" \
    "$cargo_bin" build --locked --release --manifest-path "$manifest" --target armv7-linux-androideabi
  CARGO_TARGET_X86_64_LINUX_ANDROID_LINKER="$bin/x86_64-linux-android${api}-clang" \
    "$cargo_bin" build --locked --release --manifest-path "$manifest" --target x86_64-linux-android

  install -m 0644 "$repo_dir/desktop/target/aarch64-linux-android/release/libkaede_e2ee_ffi.so" \
    "$mobile_dir/android/app/src/main/jniLibs/arm64-v8a/libkaede_e2ee_ffi.so"
  install -m 0644 "$repo_dir/desktop/target/armv7-linux-androideabi/release/libkaede_e2ee_ffi.so" \
    "$mobile_dir/android/app/src/main/jniLibs/armeabi-v7a/libkaede_e2ee_ffi.so"
  install -m 0644 "$repo_dir/desktop/target/x86_64-linux-android/release/libkaede_e2ee_ffi.so" \
    "$mobile_dir/android/app/src/main/jniLibs/x86_64/libkaede_e2ee_ffi.so"
}

build_ios() {
  if [[ "$(uname -s)" != Darwin ]]; then
    echo "iOS MLS libraries must be built on macOS." >&2
    exit 1
  fi
  if [[ ! -x "$rustup_bin" ]]; then
    echo "Rustup was not found; install the required iOS targets or set RUSTUP." >&2
    exit 1
  fi
  "$rustup_bin" target add aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios
  "$cargo_bin" build --locked --release --manifest-path "$manifest" --target aarch64-apple-ios
  "$cargo_bin" build --locked --release --manifest-path "$manifest" --target aarch64-apple-ios-sim
  "$cargo_bin" build --locked --release --manifest-path "$manifest" --target x86_64-apple-ios
  local output="$mobile_dir/ios/KaedeE2EE.xcframework"
  local sim="$repo_dir/desktop/target/kaede-e2ee-simulator.a"
  lipo -create \
    "$repo_dir/desktop/target/aarch64-apple-ios-sim/release/libkaede_e2ee_ffi.a" \
    "$repo_dir/desktop/target/x86_64-apple-ios/release/libkaede_e2ee_ffi.a" \
    -output "$sim"
  rm -rf "$output"
  xcodebuild -create-xcframework \
    -library "$repo_dir/desktop/target/aarch64-apple-ios/release/libkaede_e2ee_ffi.a" \
    -library "$sim" \
    -output "$output"
}

case "${1:-}" in
  --android) build_android ;;
  --ios) build_ios ;;
  *) echo "usage: $0 --android|--ios" >&2; exit 2 ;;
esac
