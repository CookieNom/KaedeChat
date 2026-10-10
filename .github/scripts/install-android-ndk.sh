#!/usr/bin/env bash
# Install Flutter's exact NDK before Gradle runs, so failed downloads can retry.
set -euo pipefail
flutter_root="${1:?Pass the installed Flutter SDK directory}"
sdk_root="${ANDROID_HOME:-${ANDROID_SDK_ROOT:?Android SDK directory is required}}"
version="$(sed -n 's/.*val ndkVersion: String = "\([0-9.]*\)".*/\1/p' "$flutter_root/packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt")"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Cannot determine Flutter NDK version" >&2; exit 1; }
ndk="$sdk_root/ndk/$version"
installed() {
  [[ -s "$ndk/source.properties" && -x "$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/clang" ]]
}
installed && exit 0
sdkmanager="$sdk_root/cmdline-tools/latest/bin/sdkmanager"
[[ -x "$sdkmanager" ]] || { echo "sdkmanager is missing: $sdkmanager" >&2; exit 1; }
for attempt in 1 2 3; do
  if "$sdkmanager" --sdk_root="$sdk_root" --install "ndk;$version" && installed; then
    exit 0
  fi
  echo "NDK $version installation incomplete (attempt $attempt/3)." >&2
  if [[ "$attempt" != 3 ]]; then
    sleep "$((attempt * 10))"
  fi
done
exit 1
