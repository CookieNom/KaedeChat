# Client releases

Client releases use `vMAJOR.MINOR.PATCH` or the older
`desktop-vMAJOR.MINOR.PATCH` tag format. Pushing either tag starts the only
release workflow. It builds x86-64 Windows and Linux clients, ARM64 and Intel
macOS clients, and a signed Android APK. All artifacts and their SHA-256
checksums are published to one GitHub Release. A branch push, pull request, or
manual workflow dispatch cannot publish a release.

The desktop version in `desktop/tauri/src-tauri/tauri.conf.json` and
`desktop/tauri/src-tauri/Cargo.toml` must match the numeric part of the tag.
Each desktop build also produces a Tauri updater payload and signature: an
AppImage on Linux, an `app.tar.gz` archive on macOS, and an `nsis.zip` archive
on Windows. The publish job assembles those into `latest.json` at the root of
the GitHub Release, which is the only update endpoint embedded in the app.

Before tagging, run:

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
Test Linux AppImage and Debian packages on a clean supported distribution.
Release testing must cover:

- login and adaptive Turnstile, secure session restart, upload, and gateway
  resume;
- native device selection, PTT while minimized, VAD, echo reference, and
  two-user voice;
- camera/screen permissions, sleep/resume, device unplug, and a federated
  remote guild.

The release repository must configure these GitHub Actions secrets before a tag
is pushed:

- `WINDOWS_CERTIFICATE_BASE64` and `WINDOWS_CERTIFICATE_PASSWORD` for an
  Authenticode PFX certificate;
- `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` for the
  long-lived Tauri updater key;
- `APPLE_CERTIFICATE_BASE64`, `APPLE_CERTIFICATE_PASSWORD`,
  `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, and `APPLE_TEAM_ID`
  for Developer ID signing and notarization;
- `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
  and `ANDROID_KEY_PASSWORD` for the long-lived Android upload key;
- `ANDROID_GOOGLE_SERVICES_JSON_BASE64` for the official
  `chat.kaede.mobile` Firebase client configuration. This public client
  configuration is injected at build time and is not the relay's private
  service-account credential.

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

The workflow publishes both a signed sideload APK and a Play-ready AAB. Both
are built in official-relay mode and contain no Firebase service-account key.
The workflow creates the GitHub Release only after every signed build succeeds.
Rerunning the workflow for the same tag replaces that tag's assets rather than
creating a second release.

Generate the updater key once, keep the private key and password outside the
repository, and put the public key in `tauri.conf.json`:

```sh
cargo tauri signer generate --write-keys ~/.config/kaede-chat/updater.key
```

Set the entire private key file as `TAURI_SIGNING_PRIVATE_KEY` and its password
as `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` in GitHub Actions. Back both up. Losing
or rotating this key prevents installed clients from accepting future updates.
The updater downloads only artifacts whose Tauri signature matches the public
key embedded in the installed app. Platform code signing remains required as a
separate trust layer.

On Windows the release artifact is an NSIS setup executable, not a portable
binary. It installs for the current user under `%LOCALAPPDATA%\Kaede Chat`,
requires no elevation, and registers a normal uninstaller in Windows Installed
apps. Its Start menu page allows the user to create or skip a Start menu
shortcut. Windows does not permit installers to pin applications silently, so
Kaede offers a foreground **Pin to taskbar** action after launch and Windows
shows the final consent prompt. On Windows versions that do not expose the
pinning API, users can right-click Kaede's running taskbar icon instead.

The desktop client checks `latest.json` after launch and every six hours. It
never installs or restarts automatically: the user must choose **Update and
restart**. This avoids interrupting calls, uploads, or unsent messages.

Do not publish unsigned Windows or macOS packages, unsigned updater artifacts,
or a release without `latest.json` as a production release. Keep
the Android upload key stable across releases or users will be unable to install
updates over an existing copy. Do not embed instance credentials, object-store
credentials, signing keys, or a Turnstile secret in an application bundle.
