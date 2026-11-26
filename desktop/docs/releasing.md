# Client releases

Client releases are tagged `vMAJOR.MINOR.PATCH`, or with the older
`desktop-vMAJOR.MINOR.PATCH` format. Pushing either tag starts the release
workflow, which is the only way to publish. Branch pushes, pull requests, and
manual dispatches can't publish a release.

The workflow builds the clients that changed: x86-64 Windows and Linux, ARM64
and Intel macOS, Android APK/AAB, and iOS IPA when enabled. Their files go to
one GitHub Release:

| Client | Release files |
| --- | --- |
| Linux | AppImage, `.deb`, and RPM |
| Windows | NSIS setup executable |
| macOS, per architecture | DMG, a ZIP of the `.app`, and the `.app.tar.gz` updater archive |
| Android | APK and Play AAB |
| iOS | IPA |
| Updater metadata | `latest.json` and desktop `.sig` files |

Each client package has a SHA-256 `.sha256` file beside it. Linux and macOS
updater signatures have checksums too, but `latest.json` and the Windows `.sig`
don't. The publish job downloads only `kaede-chat-*` artifacts. The native
WebRTC build inputs (`webrtc-android.tar`, `webrtc-ios.tar`, and each platform's
`desktop-webrtc.tar`) stay as CI artifacts and aren't uploaded to the release.

## Which clients build

Preflight compares the tagged commit against each client's highest-version
prior published release on the current Git history that has that client's
required assets.
Desktop, Android, and iOS each have their own baseline, so skipping an app in
one tag doesn't hide its changes from the next. Drafts, prereleases, failed
builds, and the current tag don't count as baselines. A client with no prior
build builds once. The Actions summary shows each baseline and decision. API or
Git errors fail preflight instead of quietly skipping builds.

| Changed files | Release builds |
| --- | --- |
| `frontend/` or shipping `desktop/` code | All desktop platforms together |
| `mobile/android/` | Android |
| `mobile/ios/` | iOS, when `IOS_RELEASE_ENABLED=true` |
| Shared `mobile/` files, including Dart, assets, pubspec, and tools | Android and enabled iOS |
| `docs/av1-e2ee/webrtc-build.patch` (mobile native codec/cryptor source) | Android and enabled iOS |
| `desktop/crates/kaede-e2ee/`, `kaede-e2ee-ffi/`, workspace Cargo manifest/lock, Rust toolchain, or Cargo configuration | Desktop and mobile |
| Release workflow or `.github/scripts/` | Desktop and mobile |
| Markdown/docs, retired Slint code, backend, or deployment files alone | No client release |

Matching errs on the side of building: the shared lockfile,
`desktop-release.yml`, and `.github/scripts/` rebuild every consumer. Other CI
files don't select a client. CI still runs for every tag. If nothing is selected
(after applying the iOS flag), no GitHub Release or store upload is created.

Unchanged apps keep their downloads under their earlier release tags. A
mobile-only release carries the previous desktop `latest.json` forward
unchanged, with its original version and download URLs, so desktop update
checks keep working without pointing at a desktop build that doesn't exist.

## Versions

Pick the next version in the tag; no version-bump commit is needed. Before
desktop builds, CI stamps the numeric version into
`desktop/tauri/src-tauri/tauri.conf.json`, `desktop/tauri/src-tauri/Cargo.toml`,
the `kaede-tauri` entry in `desktop/Cargo.lock`, and `frontend/package.json`
in its checkout, without committing back or moving tags. Dependency versions
stay locked, and retries use the same app version.

### Version inventory

| Component | Version source and release behavior |
| --- | --- |
| Desktop app | Tauri JSON, Cargo manifest, and Cargo.lock; stamped from the client tag, including the window title and updater's current version. |
| Embedded frontend | `frontend/package.json`; stamped from the client tag. The private package version does not need a pnpm lockfile change. |
| Android and iOS | `mobile/pubspec.yaml` (`0.1.0+1`) is a development default. Release builds already override it with the tag and CI build number; iOS extensions inherit Flutter's values. |
| Rust libraries / E2EE WASM | `desktop/Cargo.toml` workspace version and generated `frontend/src/lib/e2ee/wasm/package.json` (`0.1.0`); internal library versions, independent of client release tags. Legacy Slint crates are not released. |
| Backend | `backend/pyproject.toml`, its `backend/uv.lock` entry, and FastAPI metadata in `backend/app/main.py` (`0.1.0`); no backend package publishing job exists in these workflows. Client releases do not bump these. |
| Python SDK | `sdk/python/pyproject.toml` and `sdk/python/uv.lock` (`1.0.0` development default); its separate `sdk-vX.Y.Z` workflow stamps both using `uv version`. See [SDK releasing](../../sdk/python/RELEASING.md). |

Protocol, database schema, and encryption format versions are compatibility
identifiers. Change them only when those formats change.

For a local build with release metadata, run
`python3 .github/scripts/check-release-inputs.py --stamp --tag v0.1.107`. It
edits your local metadata files the same way. It doesn't choose the increment or
create tags.

## Before tagging

Every tag first passes the release metadata check and the same reusable CI
workflow used for main and pull requests. Signed builds start only after CI
succeeds. CI also checks the Podfile checksum and pinned Rust toolchain, lints
workflow syntax, and compiles an unsigned iOS release with the release Xcode
SDK. Dependency installation keeps the committed Cargo, Dart, and Pod locks.

Push the code to main and wait for its CI to pass. Then run:

```sh
python3 .github/scripts/check-release-inputs.py
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend lint
pnpm --dir frontend check
pnpm --dir frontend test
pnpm --dir frontend build
make desktop-check desktop-lint desktop-test
```

Test the Linux AppImage, Debian, and RPM packages on a clean supported
distribution. Release testing must cover:

- login and adaptive Turnstile, secure session restart, upload, and gateway
  resume;
- native device selection, PTT while minimized, VAD, echo reference, and
  two-user voice;
- camera/screen permissions, sleep/resume, device unplug, and a federated
  remote guild.

## Platform builds

Both macOS architectures build whenever desktop is selected (there's no
`APPLE_RELEASE_ENABLED` variable), and each must produce a nonempty DMG before
the release can publish.

The RPM job builds its builder image from `desktop/rpm/Dockerfile` on AlmaLinux
10 and compiles against that RHEL-compatible ABI. It publishes
`Kaede-Chat-<tag>-linux-x86_64.rpm` with its SHA-256 checksum, and publication
waits on a clean AlmaLinux install and shared-library check. RPM users need
RHEL 10, AlmaLinux 10, or Rocky Linux 10 with EPEL and CodeReady Builder (CRB)
enabled. RHEL 9 isn't supported because its repositories lack WebKitGTK 4.1.
The RPM isn't GPG-signed. Use the adjacent checksum to verify the download.

## Release secrets

Configure these GitHub Actions secrets in the release repository before
pushing a tag:

- `WINDOWS_CERTIFICATE_BASE64` and `WINDOWS_CERTIFICATE_PASSWORD` for an
  Authenticode PFX certificate.
- `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` for the
  long-lived Tauri updater key.
- `APPLE_CERTIFICATE_BASE64`, `APPLE_CERTIFICATE_PASSWORD`, and
  `APPLE_SIGNING_IDENTITY` for Developer ID signing.
- `APP_STORE_CONNECT_KEY_ID`, `APP_STORE_CONNECT_ISSUER_ID`, and
  `APP_STORE_CONNECT_PRIVATE_KEY_BASE64` for notarization. These reuse the team
  API key for iOS TestFlight uploads (Developer access or higher). You don't
  need an Apple account app-specific password.
- `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
  and `ANDROID_KEY_PASSWORD` for the long-lived Android upload key.
- `ANDROID_GOOGLE_SERVICES_JSON_BASE64` for the official `chat.kaede.mobile`
  Firebase client configuration. This public client file is injected at build
  time. It isn't the relay's private service-account credential.

Encode binary certificate and keystore files with standard base64 and store only
the encoded value. Missing signing material fails its build before the GitHub
Release is created.

On Linux, produce single-line secret values with:

```sh
base64 -w0 windows-signing.pfx
base64 -w0 apple-developer-id.p12
base64 -w0 android-upload.jks
```

On macOS, use `base64 < file | tr -d '\n'`. Once the code and release notes are
ready, create and push the signed release tag:

```sh
git tag -s v0.1.107 -m "Kaede Chat 0.1.107"
git push origin v0.1.107
```

## Publishing and retries

The GitHub Release is created only after every selected signed build succeeds,
so a failed build can't produce a partial release. New releases stay drafts
until all assets have uploaded. When Android is selected, it publishes a signed
sideload APK and a Play-ready AAB. Both are built in official-relay mode and
contain no Firebase service-account key.

Google Play and TestFlight uploads run only for apps that were built, and only
after CI, every selected client build, and GitHub Release publication succeed.
Skips for unchanged clients or disabled iOS are fine. Failures, cancellations,
and unexpected skips block publication and store uploads.

A store rejection fails only that delivery job (for Play, `google-play`),
since the GitHub Release is already out. Use **Re-run failed jobs** to retry delivery from the existing
signed artifacts (kept for seven days) without recompiling. Rerunning the
workflow for the same tag replaces that tag's assets instead of creating a
second release. Source or workflow fixes need a new tag, because rerunning an
old tag still uses its original commit.

### Google Play open testing

Every tag that builds Android uploads the signed AAB for `chat.kaede.mobile` to
Play's open testing track (`beta`). The release uses `completed` status and goes
to review automatically instead of being saved as a draft. Google's review and
managed publishing settings still decide when testers get it. Production isn't
updated.

Set the repository Actions secret `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` to the
entire service-account JSON (not base64). Enable the Google Play Android
Developer API in its Cloud project, then invite the service-account email under
Play Console's Users and permissions. For Kaede, grant **View app information
(read-only)** and **Release apps to testing tracks**. The app must already exist
in Play Console with an initial manual upload, and open testing must be set up,
including its countries and required app declarations.

Complete the full-screen intent declaration on Play Console's App content page
before submitting. Kaede requests `USE_FULL_SCREEN_INTENT` for incoming call
notifications, and Google rejects the release commit while that declaration is
missing, even if the AAB upload succeeds. It's a Console setting, so rebuilding
won't fix it.

Android version names come from the numeric release tag. Version codes are
`GITHUB_RUN_NUMBER * 100 + GITHUB_RUN_ATTEMPT`, which allows attempts 1–99. The
APK and AAB share the same version. Before the first automated upload, check
that this code is higher than any version code already uploaded. Release tags in
order, because rerunning an older release after a newer one can fail on a lower
code. If you need a new binary or version code, use a new tag. Don't reupload
an AAB that was already accepted.

Never commit or bundle service-account credentials.

## Updater key

Generate the updater key once, keep the private key and password outside the
repository, and put the public key in `tauri.conf.json`:

```sh
cargo tauri signer generate --write-keys ~/.config/kaede-chat/updater.key
```

Set the whole private key file as `TAURI_SIGNING_PRIVATE_KEY` and its password
as `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` in GitHub Actions, and back both up.
Losing or rotating this key stops installed clients from accepting future
updates. The updater only downloads artifacts whose Tauri signature matches the
public key in the installed app. Platform code signing is still required as a
separate layer.

Each desktop build produces an updater payload and signature: the AppImage on
Linux, an `app.tar.gz` archive on macOS, and the NSIS setup executable on
Windows. The publish job assembles them into `latest.json` at the root of the
GitHub Release. That file is the only update endpoint built into the app.

## Update checks

The desktop client checks `latest.json` after launch and every six hours. It
never installs or restarts on its own. The user has to choose **Update and
restart**, so calls, uploads, and unsent messages aren't interrupted.

The opt-in **Launch at sign-in** setting uses the operating system's startup
registration. Sign-in launches keep the main window hidden in the tray but still
restore the session and run the usual update check. Regular launches open the
main window.

## Windows installer

The Windows artifact is an NSIS setup executable, not a portable binary. It
installs for the current user under `%LOCALAPPDATA%\Kaede Chat`, needs no
elevation, and registers a normal uninstaller in Windows Installed apps. Its
Start menu page lets the user create or skip a shortcut.

Windows doesn't let installers pin apps silently. Kaede offers a **Pin to
taskbar** action in its foreground UI after launch, and Windows shows the final
consent prompt. On Windows versions without the pinning API, users can
right-click Kaede's running taskbar icon instead.

## Release rules

- Don't publish unsigned Windows or macOS packages, or unsigned updater
  artifacts, as production releases.
- Don't publish a release without `latest.json`.
- Keep the Android upload key the same across releases, or users can't install
  updates over an existing copy.
- Don't embed instance credentials, object-store credentials, signing keys, or
  a Turnstile secret in an application bundle.
