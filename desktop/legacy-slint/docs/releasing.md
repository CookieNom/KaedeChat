# Desktop release requirements

> This document describes the archived Slint client. It is retained for
> historical reference and does not control the current release workflow. See
> [the Tauri client release guide](../../docs/releasing.md) for tag formats,
> signing secrets, packages, and GitHub Releases.

Release builds are reproducible from the locked Cargo workspace. CI can build
unsigned artifacts, but public distribution additionally requires:

- a Windows code-signing certificate for MSI artifacts;
- an Apple Developer ID certificate and notarization credentials for the macOS
  application and DMG;
- a signing key for Linux repository metadata and update manifests;
- a final Slint licensing decision before distributing proprietary binaries;
- per-platform validation of microphones, output devices, screen capture,
  global shortcuts, suspend/resume, and hotplug behavior.

Update manifests must be signed independently from transport TLS. The client
verifies the signature and package hash before offering an update and never runs
an unsigned downloaded binary.

The canonical signed bytes are the UTF-8 sequence
`version + "\\n" + package_url + "\\n" + lowercase_sha256 + "\\n"`. The
manifest carries those three fields plus a base64 Ed25519 signature. Its package
URL must use HTTPS and the package must fit the client's bounded download limit.

These historical package requirements do not imply that the archived client is
built or published by `.github/workflows/desktop-release.yml`.
