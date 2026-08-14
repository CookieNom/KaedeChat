# Client releases

Client releases use `vMAJOR.MINOR.PATCH` or the older
`desktop-vMAJOR.MINOR.PATCH` tag format. Pushing either tag starts the only
release workflow. It builds x86-64 Windows and Linux clients, ARM64 and Intel
macOS clients, and a signed Android APK, then publishes all artifacts and their
SHA-256 checksums to one GitHub Release. A branch push, pull request, or manual
workflow dispatch cannot publish a release.

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
Linux AppImage and Debian packages should be tested on a clean supported
distribution. Release testing must cover login and adaptive Turnstile, secure
session restart, upload, gateway resume, native device selection, PTT while
minimized, VAD, echo reference, two-user voice, camera/screen permissions,
sleep/resume, device unplug, and a federated remote guild.

The release repository must configure these GitHub Actions secrets before a tag
is pushed:

- `WINDOWS_CERTIFICATE_BASE64` and `WINDOWS_CERTIFICATE_PASSWORD` for an
  Authenticode PFX certificate;
- `APPLE_CERTIFICATE_BASE64`, `APPLE_CERTIFICATE_PASSWORD`,
  `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, and `APPLE_TEAM_ID`
  for Developer ID signing and notarization;
- `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
  and `ANDROID_KEY_PASSWORD` for the long-lived Android upload key.

Encode binary certificate and keystore files with standard base64 and store only
the encoded value in GitHub Secrets. Missing signing material fails its build
before the GitHub Release is created. The publish job depends on every platform,
so a partial release cannot be published accidentally.

On Linux, produce single-line secret values with:

```sh
base64 -w0 windows-signing.pfx
base64 -w0 apple-developer-id.p12
base64 -w0 android-upload.jks
```

On macOS, use `base64 < file | tr -d '\n'`. After the application versions and
release notes are ready, create and push the signed release tag:

```sh
git tag -s v0.1.10 -m "Kaede Chat 0.1.10"
git push origin v0.1.10
```

The workflow creates the GitHub Release only after every signed build succeeds.
Rerunning the workflow for the same tag replaces that tag's assets rather than
creating a second release.

Do not publish unsigned Windows or macOS packages as production releases. Keep
the Android upload key stable across releases or users will be unable to install
updates over an existing copy. Do not embed instance credentials, object-store
credentials, signing keys, or a Turnstile secret in an application bundle.
