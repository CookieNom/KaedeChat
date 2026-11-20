# Premium codec and E2EE validation

Encrypted and plaintext calls share the [codec policy](../screen-sharing-quality.md#codec-defaults):
one compatible stream and at most one hardware-encoded AV1 or HEVC stream per
source. AV1 wins equal audiences. HEVC requires hardware decoding too. VP9 may
replace the compatible stream under upload pressure with a fully capable audience
and measured CPU headroom. Encryption stays enabled through every transition.

The `kaede.video.v1` legacy fields remain compatible with existing Kaede clients.
Only the additive `extendedCodecs` field advertises HEVC/VP9; old clients ignore
it and retain compatible video. Clients without hints become VP8-only after the
eight-second grace period. The existing Kaede AV1 encryption format is unchanged.
An upstream client's AV1 MIME support alone is still not evidence that it
implements that format.

The AV1 cryptor leaves required OBU structure clear **and authenticated**, encrypts
the remaining bytes with AES-GCM, and carries the IV/tag in a versioned metadata
OBU. HEVC uses the H.265 NAL path. H.264/HEVC headers are authenticated, including
long parameter sets; native clear-prefix lengths use `size_t`, not `uint8_t`.
The [encrypted H.264 capability check](../screen-sharing-quality.md#codec-defaults)
keeps 0.1.104 phones on a compatible stream when they call an updated client.
Annex B start codes are normalized before authentication, and escaped ciphertext
is restored before extracting the IV/trailer. All clients retain HKDF and the
existing `kaede-livekit-v1` salt. No key material should appear in native logs.

The browser patch is [livekit-client 2.24.1](../../frontend/patches/livekit-client@2.24.1.patch),
including upstream's H.264/H.265 detection fixes. It preserves the explicitly
requested scalability mode on screen shares. AV1/VP9 spatial modes and HEVC
temporal modes are used only after the exact mode passes the encoder probe;
mobile's native bridge currently proves and publishes `L1T1` only.

## Browser checks and diagnostics

With Node 22:

```sh
cd frontend
pnpm install --frozen-lockfile
pnpm test:livekit-crypto
```

Tests cover AV1 layered OBUs, HEVC key/delta frames with long parameter sets,
three/four-byte start codes, header/ciphertext tampering, plaintext, key rotation,
and compatible/premium sender cleanup. The SDK options tests ensure screen
shares preserve the requested mode and HEVC cannot acquire spatial layers or
simulcast implicitly.

Open `/codec-probe` to inspect advertised and observed encode/decode support at
720p, 1080p or 4K. Download its JSON when investigating driver differences.
Hardware names can be hidden without active camera permission; the optional
**Reveal hardware names** control explains and requests that access. Synthetic
loopback results do not prove encrypted RTP interoperability.

To rebuild the browser patch, create a pnpm patch workspace for livekit-client
2.24.1, edit its source, then run
`node scripts/rebuild-livekit-patch.mjs /path/to/workspace` from `frontend`, followed
by `pnpm patch-commit /path/to/workspace`. Re-run the cryptor and RTP checks.

## Desktop native artifacts

Desktop source pins and header download hashes live in
[codec_sources.json](../../desktop/tool/codec_sources.json). The
[preparation script](../../desktop/tool/prepare_codecs.py) applies repository
patches to an ignored `desktop/native/rust-sdks` checkout without editing the
Cargo cache or requiring a fork push. Cargo uses the patched `livekit`,
`libwebrtc` and `webrtc-sys` crates, including initial sender bitrate controls.
The custom WebRTC archive must match those sources and enable
`rtc_use_h265`; `LK_CUSTOM_WEBRTC` selects it. A mismatched or incomplete artifact
fails verification.

The native workflow builds this archive on each target OS; release/CI jobs
consume it and obtain pinned CUDA headers without installing a CUDA toolkit.
NVIDIA driver libraries are loaded at runtime. Windows machines without NVIDIA
can use their registered hardware Media Foundation transforms; macOS uses
VideoToolbox. Codec advertisement and hardware probes remain authoritative:
NVIDIA's NVENC has no VP9 encoder, and a GPU model or OS version alone never
proves a particular codec/configuration.

Media Foundation enumerates hardware transforms across adapters, including an
Intel iGPU alongside a discrete GPU. Microsoft's HEVC codec is an
[optional codec installation](https://support.microsoft.com/en-us/windows/apps/windowsmediaplayer/codecs-faq);
vendor transforms may be registered independently. Its
[decoder can fall back to software](https://learn.microsoft.com/en-us/windows/win32/medfound/h-265---hevc-video-decoder),
so installing the extension does not establish Kaede hardware support.

For an explicitly requested local native build, prepare sources and build on the
target OS (the default workflow is CI):

```sh
python3 desktop/tool/prepare_codecs.py --cuda
python3 desktop/tool/build_webrtc.py --work-dir /absolute/empty/build-directory
python3 desktop/tool/prepare_codecs.py --cuda --artifact desktop/native/webrtc
```

Use the printed `LK_CUSTOM_WEBRTC` and `CUDA_HOME` paths for the desktop build.
The SDK/WebRTC pins are an ABI pair, not interchangeable with the latest SDK
release: the fork carries Kaede's AV1 wire format and native extensions. Update
the pair and rebase its patches together; do not replace a single prebuilt or
claim an untested upstream revision is compatible.

## RTP interoperability

Use a hardware-capable host and a browser with HEVC WebRTC support. Start
LiveKit 1.13.3 in dev mode on loopback port 17880 with devkey/secret. Prepare the
patched native sources/artifact above, then build the native fixture in CI or an
explicitly authorized local native validation:

```sh
cargo test --manifest-path desktop/native/rust-sdks/Cargo.toml \
  -p livekit --features __lk-e2e-test --test backup_codec_test --no-run
```

Install Python Playwright and its browser in a disposable environment, then run
from the application repository:

```sh
LIVEKIT_INTEROP_TEST_BINARY=/absolute/path/to/backup_codec_test-HASH \
  python docs/av1-e2ee/interop.py --codecs av1 h265 --modes L1T1 L1T3 L3T3_KEY
```

HEVC spatial-mode cases are excluded. The remaining matrix covers encrypted and
plaintext video in both directions, with and without a late VP8-only viewer.
It checks decoded frames, actual RTP codec/mode, and HEVC hardware evidence.
Missing hardware is a failed requirement, not a successful skip. Select a
suitable installed Chrome through `PLAYWRIGHT_CHROMIUM_EXECUTABLE`; use
`--headed` when the hardware driver needs a visible browser. The harness grants
access only to Chromium's synthetic camera to expose hardware statistics.
`LIVEKIT_JS_DIST` optionally selects a patch workspace's `dist` directory.
Keys are synthetic, signaling is localhost-only, and native logs use temporary
files.

## Mobile native artifacts

The mobile WebRTC workflow builds the patched Android AAR and iOS XCFramework
from WebRTC 150.7871.06 and [webrtc-build.patch](webrtc-build.patch), with exact
source and build-helper commits in `mobile/tool/build_webrtc.py`. The vendored
Dart packages are LiveKit 2.13.2 and flutter_webrtc 1.6.3, rebased with Kaede's
track/cryptor extensions. The M150 update includes upstream's TURN/TLS trust-root
removal and current H.264 encryption fixes. Update the native pins, plugin
constraints and patch together. App CI consumes these artifacts. Gradle uses the generated Maven package and CocoaPods uses the
local framework; missing artifacts fail the build instead of selecting stock
cryptors. The native H.26x header regression vectors run in that workflow.

The vendored Flutter SDK installs cryptors for AV1, HEVC and VP9 before
negotiation and retains key rotation/cleanup for all senders. Android rejects
unknown probe codecs and tests the encoder selected by its actual hardware
factory. Apple advertises HEVC only through its VideoToolbox path and requires
hardware sessions. HEVC publishing requires iOS 17.4+, and decoding requires iOS
17+, because those releases expose the hardware requirement and verification
properties; earlier iOS versions retain compatible video. The iOS AV1 software
encoder never passes the hardware probe. VP9 is explicitly enabled in the native
build but remains subject to the compatible-track upload and CPU policy.

For explicitly requested local builds (Linux for Android, macOS for iOS):

```sh
python3 mobile/tool/build_webrtc.py android --work-dir /tmp/kaede-webrtc-android
python3 mobile/tool/build_webrtc.py ios --work-dir /tmp/kaede-webrtc-ios
```

Alternatively, extract this revision's `kaede-webrtc-android` or
`kaede-webrtc-ios` artifact into `mobile/native/webrtc`. Rebuild after changing
source pins or patches; manifests record both pins and the patch hash.

Before release, validate physical Android/iOS devices, Windows Intel/AMD/NVIDIA
(including multiple adapters), macOS and Linux/NVIDIA. Include camera and screen
sharing, both encryption modes, AV1/HEVC audience ties, late VP8 viewers,
reconnects, key rotation, driver/session exhaustion, and upload/encode pressure.
Native compilation in CI and successful hardware RTP runs are separate required
evidence; source patches and browser cryptor tests alone do not establish them.
