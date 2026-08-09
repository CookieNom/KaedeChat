# Desktop releases

Desktop tags use `desktop-vMAJOR.MINOR.PATCH`. The release workflow builds the
frontend once per platform and invokes the Tauri bundler on Windows, macOS, and
Linux. Artifacts and checksums are uploaded separately for signing and release
approval.

Before tagging:

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend lint
pnpm --dir frontend check
pnpm --dir frontend test
pnpm --dir frontend build
make desktop-check desktop-lint desktop-test
```

Windows releases require the configured code-signing certificate. macOS
releases require a Developer ID Application identity and notarization secrets.
Linux AppImage and Debian/RPM packages should be tested on a clean supported
distribution. Release testing must cover login and adaptive Turnstile, secure
session restart, upload, gateway resume, native device selection, PTT while
minimized, VAD, echo reference, two-user voice, camera/screen permissions,
sleep/resume, device unplug, and a federated remote guild.

Do not publish unsigned Windows or macOS packages as production releases. Do
not embed instance credentials, object-store credentials, signing keys, or a
Turnstile secret in an application bundle.
