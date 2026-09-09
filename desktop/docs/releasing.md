# Client releases

Client releases use `vMAJOR.MINOR.PATCH` or the older
`desktop-vMAJOR.MINOR.PATCH` tag format. Pushing either tag starts the only
release workflow. It builds x86-64 Windows and Linux clients, ARM64 and Intel
macOS clients, and a signed Android APK. All artifacts and their SHA-256
checksums are published to one GitHub Release. A branch push, pull request, or
manual workflow dispatch cannot publish a release.

The desktop version in `desktop/tauri/src-tauri/tauri.conf.json` and
`desktop/tauri/src-tauri/Cargo.toml` and the `kaede-tauri` entry in
`desktop/Cargo.lock` must match the numeric part of the tag.
Each desktop build also produces a Tauri updater payload and signature: an
AppImage on Linux, an `app.tar.gz` archive on macOS, and the NSIS setup executable
on Windows. The publish job assembles those into `latest.json` at the root of
the GitHub Release, which is the only update endpoint embedded in the app.

Every tag first passes the release metadata check and the same reusable CI
workflow used by main and pull requests. Signed builds start only after CI
succeeds. CI also checks the Podfile checksum and pinned Rust toolchain, lints
workflow syntax, and compiles an unsigned iOS release using the release Xcode
SDK. Dependency installation keeps the committed Cargo, Dart, and Pod locks.

Before tagging, push the version bump to main and wait for its CI to pass. Run:

```sh
python3 .github/scripts/check-release-inputs.py --tag vMAJOR.MINOR.PATCH
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
New GitHub Releases remain drafts until all assets have uploaded. Google Play
and TestFlight uploads run as separate jobs after their signed build; a store
rejection fails that delivery job without blocking the downloadable GitHub
Release. Use **Re-run failed jobs** to retry delivery from the existing signed
artifacts (retained for seven days), without recompiling successful clients.
Rerunning the workflow for the same tag replaces that tag's assets rather than
creating a second release. Source or workflow fixes require a new tag: rerunning
an old tag still uses its original commit.

### Google Play open testing

Every release tag also uploads the signed AAB for `chat.kaede.mobile` to Google
Play's open testing track (`beta`). The release uses `completed` status and is
submitted for review automatically, rather than saved as a draft. Google's
review and managed publishing settings still determine when testers receive
it. Production is not updated.

Set the repository Actions secret `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` to the
entire service-account JSON (not base64). Enable the Google Play Android
Developer API in its Cloud project, then invite the service-account email in
Play Console's Users and permissions. For Kaede, grant **View app information
(read-only)** and **Release apps to testing tracks**. The app must already
exist in Play Console with an initial manual upload, and open testing must be
available and configured, including its countries and required app declarations.

Android version names come from the numeric release tag. Version codes use
`GITHUB_RUN_NUMBER * 100 + GITHUB_RUN_ATTEMPT`, allowing attempts 1–99. Both
the APK and AAB use the same version. Before the first automated upload, check
that this code exceeds any previously uploaded version code. Release tags in
order; rerunning an older release after a newer one can fail because its code
is lower. Rerun the Android job to rebuild with a fresh code after an upload
failure; do not reupload an already accepted AAB.

The signed files are retained as Actions artifacts before Play upload. An
upload failure fails the Android job and blocks GitHub Release publication.
Include this workflow change in a new tag; rerunning an old tag uses its old
workflow. Service-account credentials must never be committed or bundled.

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
apps. The installer's Start menu page lets the user create or skip a Start
menu shortcut. Windows doesn't let installers pin an application silently.
Kaede offers a foreground **Pin to taskbar** action after launch, and Windows
shows the final consent prompt. On Windows versions that don't expose the
pinning API, users can right-click Kaede's running taskbar icon instead.

The desktop client checks `latest.json` after launch and every six hours. It
never installs or restarts automatically: the user must choose **Update and
restart**. This avoids interrupting calls, uploads, or unsent messages.

The opt-in **Launch at sign-in** setting uses the operating system's startup
registration. Sign-in launches keep the main window hidden in the system tray,
but still restore the session and run the normal immediate update check.
Regular launches continue to open the main window.

Do not publish unsigned Windows or macOS packages or unsigned updater
artifacts as production releases, and do not publish a release that lacks
`latest.json`. Keep the Android upload key stable across releases or users
will be unable to install updates over an existing copy. Do not embed instance
credentials, object-store credentials, signing keys, or a Turnstile secret in
an application bundle.
