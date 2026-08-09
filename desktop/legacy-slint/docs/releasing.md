# Desktop release requirements

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

Tagging `desktop-vX.Y.Z` runs `.github/workflows/desktop-release.yml` and creates:

- a Debian package and portable Linux archive with desktop entry, icon, and
  `kaede://` URL handler;
- a per-user Windows MSI plus a portable ZIP with URL-protocol registration;
- a macOS application bundle, ZIP, and DMG with URL-scheme and privacy-purpose
  declarations.

Windows and macOS artifacts are signed only when their protected CI credentials
are present. Unsigned workflow artifacts are suitable for testing, not public
distribution. Linux repository signing remains an operator responsibility.
