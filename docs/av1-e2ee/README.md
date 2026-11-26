# Premium codec and E2EE validation

Notes for building the patched codec artifacts and running the AV1/HEVC
encryption harness. Encrypted and plaintext calls follow the same
[codec policy](../screen-sharing-quality.md#codec-defaults), including the
[encrypted H.264 capability check](../screen-sharing-quality.md#codec-hints-and-older-clients)
for 0.1.104 phones. Passing these checks in part doesn't establish
interoperability: native CI compilation and successful hardware RTP runs are
separate required evidence, and neither browser cryptor tests nor source
patches substitute for them.

## Wire format

- The existing Kaede AV1 encryption format is unchanged. An upstream client
  advertising AV1 MIME support doesn't prove it implements this format.
- AV1 leaves the required OBU structure clear **and authenticated**, encrypts
  the rest with AES-GCM, and carries the IV/tag in a versioned metadata OBU.
- HEVC uses the H.265 NAL path. H.264/HEVC headers are authenticated, including
  long parameter sets. Native clear-prefix lengths use `size_t`, not `uint8_t`.
- Annex B start codes are normalized before authentication, and escaped
  ciphertext is restored before extracting the IV/trailer.
- All clients keep HKDF and the `kaede-livekit-v1` salt. Native logs must not
  contain key material.
- Only the additive `extendedCodecs` field advertises HEVC/VP9, so old clients
  keep compatible video.

## Browser checks and diagnostics

The browser patch is [livekit-client 2.24.1](../../frontend/patches/livekit-client@2.24.1.patch).
It includes upstream's H.264/H.265 detection fixes and keeps the requested
scalability mode on screen shares. With Node 22:

```sh
cd frontend
pnpm install --frozen-lockfile
pnpm test:livekit-crypto
```

These cover AV1 layered OBUs, HEVC key/delta frames with long parameter sets,
three- and four-byte start codes, header and ciphertext tampering, plaintext,
key rotation, and compatible/premium sender cleanup. The SDK options tests check
that screen shares keep the requested mode and that HEVC can't pick up spatial
layers or simulcast.

For driver differences, download the JSON from
[`/codec-probe`](../screen-sharing-quality.md#capability-diagnostics-and-encryption).
Its synthetic loopback results don't prove encrypted RTP interoperability.

To rebuild the browser patch, create a pnpm patch workspace for livekit-client
2.24.1 and edit its source. From `frontend`, run
`node scripts/rebuild-livekit-patch.mjs /path/to/workspace`, then
`pnpm patch-commit /path/to/workspace`. Re-run the cryptor and RTP checks.

## Desktop native artifacts

Source pins and header download hashes are in
[codec_sources.json](../../desktop/tool/codec_sources.json). The
[preparation script](../../desktop/tool/prepare_codecs.py) applies the
repository patches to an ignored `desktop/native/rust-sdks` checkout, without
touching the Cargo cache or needing a fork. Cargo then uses the patched
`livekit`, `libwebrtc`, and `webrtc-sys` crates, including initial sender
bitrate controls. The custom WebRTC archive must match those sources and enable
`rtc_use_h265`. `LK_CUSTOM_WEBRTC` selects it, and a mismatched or incomplete
archive fails verification.

The native workflow builds the archive on each target OS. Release and CI jobs
use it and fetch pinned CUDA headers without installing a CUDA toolkit. NVIDIA
driver libraries load at runtime. Codec advertisement and hardware probes have
the final say; a GPU model or OS version never proves a codec or configuration.
Per-platform hardware paths are listed in the
[desktop README](../../desktop/README.md#hardware-video).

CI builds these by default. For a local build on the target OS, when you've
been asked for one:

```sh
python3 desktop/tool/prepare_codecs.py --cuda
python3 desktop/tool/build_webrtc.py --work-dir /absolute/empty/build-directory
python3 desktop/tool/prepare_codecs.py --cuda --artifact desktop/native/webrtc
```

Use the printed `LK_CUSTOM_WEBRTC` and `CUDA_HOME` paths for the desktop build.
The SDK and WebRTC pins are an ABI pair carrying Kaede's AV1 wire format and
native extensions, so they aren't interchangeable with the latest SDK release.
Update the pair and rebase its patches together. Don't swap a single prebuilt
or assume an untested upstream revision works.

## RTP interoperability

Use a hardware-capable host and a browser with HEVC WebRTC support. Start
LiveKit 1.13.3 in dev mode on loopback port 17880 with devkey/secret. Prepare
the patched sources and archive above, then build the native fixture in CI or an
authorized local run:

```sh
cargo test --manifest-path desktop/native/rust-sdks/Cargo.toml \
  -p livekit --features __lk-e2e-test --test backup_codec_test --no-run
```

Install Python Playwright and its browser in a disposable environment, then run
from the repository root:

```sh
LIVEKIT_INTEROP_TEST_BINARY=/absolute/path/to/backup_codec_test-HASH \
  python docs/av1-e2ee/interop.py --codecs av1 h265 --modes L1T1 L1T3 L3T3_KEY
```

The matrix covers encrypted and plaintext video in both directions, with and
without a late VP8-only viewer, and skips HEVC spatial modes. It checks decoded
frames, the actual RTP codec and mode, and HEVC hardware evidence. Missing
hardware is a failure, not a skip.

- `PLAYWRIGHT_CHROMIUM_EXECUTABLE` selects an installed Chrome.
- `--headed` helps when the hardware driver needs a visible browser.
- `LIVEKIT_JS_DIST` optionally points at a patch workspace's `dist` directory.

The harness grants access only to Chromium's synthetic camera, to expose
hardware statistics. Keys are synthetic, signaling stays on localhost, and
native logs go to temporary files.

## Mobile native artifacts

The mobile WebRTC workflow builds the patched Android AAR and iOS XCFramework
from WebRTC 150.7871.06 and [webrtc-build.patch](webrtc-build.patch). Exact
source and build-helper commits are in `mobile/tool/build_webrtc.py`. The
vendored Dart packages are LiveKit 2.13.2 and flutter_webrtc 1.6.3, rebased
with Kaede's track and cryptor extensions. M150 includes upstream's TURN/TLS
trust-root removal and current H.264 encryption fixes. Update the native pins,
plugin constraints, and patch together.

App CI uses these artifacts: Gradle takes the generated Maven package and
CocoaPods the local framework. Missing artifacts fail the build rather than
falling back to stock cryptors. The native H.26x header regression vectors run
in the same workflow.

- The vendored Flutter SDK installs AV1, HEVC, and VP9 cryptors before
  negotiation and keeps key rotation and cleanup for all senders.
- Android rejects unknown probe codecs and tests the encoder its hardware
  factory actually picks.
- Apple advertises HEVC only through VideoToolbox and requires hardware
  sessions. HEVC publishing needs iOS 17.4+ and decoding iOS 17+, where the
  hardware requirement and verification properties exist. Earlier versions
  keep compatible video.
- The iOS AV1 software encoder never passes the hardware probe.
- VP9 is enabled in the native build but still follows the compatible-track
  upload and CPU policy.
- Mobile's native bridge proves and publishes `L1T1` only.

For a requested local build (Linux for Android, macOS for iOS):

```sh
python3 mobile/tool/build_webrtc.py android --work-dir /tmp/kaede-webrtc-android
python3 mobile/tool/build_webrtc.py ios --work-dir /tmp/kaede-webrtc-ios
```

Or extract this revision's `kaede-webrtc-android` or `kaede-webrtc-ios` artifact
into `mobile/native/webrtc`. Rebuild after changing source pins or patches. The
manifests record both pins and the patch hash.

## Release validation

Before release, test on physical Android and iOS devices, Windows with
Intel/AMD/NVIDIA (including multiple adapters), macOS, and Linux with NVIDIA.
Cover camera and screen sharing, both encryption modes, AV1/HEVC audience ties,
late VP8 viewers, reconnects, key rotation, driver/session exhaustion, and
upload/encode pressure.
