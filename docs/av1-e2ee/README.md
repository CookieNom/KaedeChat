# Encrypted AV1 validation

These are development validation notes for the experimental AV1 encryption
format. Production encrypted video still uses VP8. Current Rust dependency
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
format. Encrypted publishing therefore remains VP8 until matching mobile binaries
are integrated and compatibility with older clients is addressed.

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

## Remaining integration

Build the mobile WebRTC patch on Android and Apple CI,
then pin those artifacts in Flutter's native plugin and test actual devices.
Validate Windows/macOS Rust builds, and H264 publishing in a browser with an H264
encoder. Only then change the encrypted default, with a way to handle old clients
that support AV1 decoding but do not understand this encrypted frame format.
