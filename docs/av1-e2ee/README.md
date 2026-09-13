# Encrypted AV1 validation

Encrypted camera and screen sharing now prefer AV1 when the sender supports it,
with an encrypted VP8 backup. This rollout requires coordinated updates of all
desktop, browser and mobile installations. Current Rust dependency
pins are in [desktop/Cargo.toml](../../desktop/Cargo.toml); the browser patch is
[the LiveKit patch](../../frontend/patches/livekit-client@2.20.1.patch), and the
mobile build changes are in [webrtc-build.patch](webrtc-build.patch).

The original failures came from encrypting AV1 OBU structure needed by RTP,
browser cryptors rejecting AV1, and missing cryptors on backup senders. Desktop
and mobile also used PBKDF2 while the browser's raw key provider used HKDF; the
application now explicitly selects HKDF on all three platforms with its existing
`kaede-livekit-v1` salt.

The patch keeps necessary AV1 structure clear **and authenticated**, encrypts the
remaining bytes using the existing AES-GCM implementation, and carries the IV/tag
in a versioned metadata OBU. This is a Kaede-specific experimental wire format.
See the Rust fork's `webrtc-sys/src/av1_e2ee/README.md` for details and ABI limits.
An upstream client advertising AV1 does not imply support for this encryption
format. Kaede ships matching native mobile binaries and patched Flutter packages
from `mobile/vendor/livekit_client` and `mobile/vendor/flutter_webrtc`. The latter
forwards the HKDF setting through the Android/iOS native bridges.
Older clients must update before joining
this rollout; there is no version-negotiation layer.

## Current rollout checks

The codec-selection tests, Flutter encryption lifecycle test (including a backup
created after key rotation and a failed native cryptor setup), browser cryptor
suite, and app/SDK static checks pass. A fresh local SFU run passed encrypted
Rust -> Chromium, Chromium -> Rust, and Chromium -> a late VP8-only Rust receiver
using Playwright 1.62's Chromium 151. Native plaintext/encrypted AV1 L3T3_KEY
controls also pass. A separately cached Chromium 153 binary failed both plaintext
and encrypted AV1 receive in this environment. Validate the browsers used for the
rollout; these checks also do not establish Android/iOS device behavior.

The patched Android AAR builds for ARMv7, ARM64, x86 and x86_64 and passes archive
integrity checks. The Android debug APK builds successfully and contains the
exact patched WebRTC binaries for its three supported ABIs. Gradle resolves both
Flutter plugins to that local artifact. The full Flutter suite passes (517
passed, one skipped), and the eight focused AV1 tests pass after the native bridge
update. The iOS native build
and real-device calls remain pending.

## Recorded investigation results

| Check | Result |
| --- | --- |
| Native encrypted AV1 L1T1, L1T3, L3T3_KEY | Passed |
| Native plaintext AV1, encrypted VP8/H264 controls | Passed |
| Native late join + reconnect, H264/VP8, plaintext/encrypted, single/dual PC | 8 passed |
| Rust SDK unit tests | 79 passed |
| Desktop voice crate against pinned SDK | 14 passed |
| Rust FFI compile | Passed |
| Browser cryptor, key rotation, malformed frames, authentication | 24 passed |
| C++ shared WebCrypto vector, mutations, truncation, random parser inputs with ASan/UBSan | Passed |
| Rust -> Chromium encrypted AV1 | Passed |
| Chromium -> Rust encrypted AV1 | Passed |
| Chromium -> late Rust receiver, encrypted VP8 backup | Passed |
| Browser H264 publishing | Unverified: headless Chromium has no H264 encoder |
| Flutter quality tests | 7 passed |
| Frontend type check and production build | Passed |
| Full frontend suite | 698 passed |
| Flutter analyzer on changed voice files | Passed |
| Android/iOS/macOS device calls and rebuilt mobile binaries | Pending |

These are historical functional-test results, not a fresh run against the
current checkout or hardware-performance measurements. No LiveKit server changes or encryption bypass were used.

## Reproduce

Browser cryptor tests (Node 22):

```sh
cd frontend
pnpm install --frozen-lockfile
pnpm test:livekit-crypto
```

For real RTP interoperability, start LiveKit 1.13.6 in dev mode on loopback
port 17880, using its devkey/secret credentials, and build the Rust fork tests:

```sh
cargo test -p livekit --features __lk-e2e-test --test backup_codec_test --no-run
```

Install Python Playwright and its Chromium browser in a disposable environment,
then from the app repository:

```sh
LIVEKIT_INTEROP_TEST_BINARY=/absolute/path/to/target/debug/deps/backup_codec_test-HASH \
  python docs/av1-e2ee/interop.py
```

The harness uses synthetic keys, localhost only, and checks decoded frames plus
actual RTP codec statistics. It runs AV1 in both directions and a late-joining VP8
receiver while another receiver uses AV1. `LIVEKIT_JS_DIST` optionally selects a
patch workspace's `dist` directory. Native logs go to the temporary directory.

To rebuild the browser patch, create a pnpm patch workspace for livekit-client
2.20.1, edit its source, then run `node scripts/rebuild-livekit-patch.mjs /path/to/workspace`
from `frontend` and `pnpm patch-commit /path/to/workspace`. Re-run both cryptor and
interop tests. The rebuild script uses the existing Vite/esbuild dependency.

## Mobile native builds and rollout

The mobile WebRTC CI workflow builds and caches the patched Android AAR and iOS
XCFramework from pinned source revisions. Main CI consumes those artifacts before
building either app. Gradle substitutes the hosted Android library with the local
Maven artifact; CocoaPods resolves both plugins' WebRTC dependency to the local
framework. Missing artifacts fail the build instead of using the stock cryptor.

For local builds, run from the repository root (Linux for Android, macOS for iOS):

```sh
python3 mobile/tool/build_webrtc.py android --work-dir /tmp/kaede-webrtc-android
python3 mobile/tool/build_webrtc.py ios --work-dir /tmp/kaede-webrtc-ios
```

These are substantial Chromium/WebRTC source builds requiring native build tools,
network access and ample disk space. Alternatively, download the matching
`kaede-webrtc-android` or `kaede-webrtc-ios` artifact from this revision's CI and
extract its tar file into `mobile/native/webrtc`. Rebuild after changing the source
pins or native patch. The generated build manifest records both pins and patch hash.

Flutter queries sender codec capabilities for encrypted calls as well as plaintext
calls. Encrypted publishing selects AV1 with VP8 backup, or VP8 alone if AV1 is
unavailable. Encryption remains enabled in either case. The vendored SDK installs
video encryptors before negotiation, attaches AV1 receiver decryptors, and covers
backup senders in key rotation and unpublish cleanup.

Before distributing the coordinated update, validate Android/iOS device calls,
Windows/macOS native builds, mixed AV1/VP8 receivers, camera and screen sharing,
late joins and reconnects. The historical results above do not replace those
platform checks. Refresh existing browser tabs and replace older test builds;
ordinary AV1 capability does not make those older builds compatible.
