# iOS release setup

The repository is configured for Apple team `6Q44L7WH4U`, App Store Connect
app `6808771548`, and these identifiers:

- `chat.kaede.mobile`
- `chat.kaede.mobile.BroadcastExtension`
- `chat.kaede.mobile.NotificationService`
- `group.chat.kaede.mobile`

## 1. Create signing material

In Apple Developer, create one **Apple Distribution** certificate. Then create
an **App Store Connect** provisioning profile for each of the three App IDs
above and download the `.mobileprovision` files. Manual signing expects these
exact profile names:

- `Kaede Chat App Store`
- `Kaede Broadcast Extension App Store`
- `Kaede Notification Service App Store`

The main app profile needs Push Notifications, the app group, Associated
Domains, and Data Protection. Both extension profiles need the same app group.

Export the distribution certificate and its private key together as a
password-protected `.p12` file. Keep the `.p12`, password, profiles, and keys
out of the repository.

## 2. Add GitHub release values

In the GitHub repository, open **Settings → Secrets and variables → Actions**.
Add these secrets, with each file base64-encoded as one line:

- `IOS_DISTRIBUTION_CERTIFICATE_BASE64`
- `IOS_DISTRIBUTION_CERTIFICATE_PASSWORD`
- `IOS_APP_PROFILE_BASE64`
- `IOS_BROADCAST_PROFILE_BASE64`
- `IOS_NOTIFICATION_PROFILE_BASE64`
- `IOS_GOOGLE_SERVICE_INFO_PLIST_BASE64`

On Linux, encode a file with `base64 -w0 FILE`. On macOS, use
`base64 -i FILE | tr -d '\n'`.

Add the Actions variable `IOS_RELEASE_ENABLED` with value `true`. From then on,
tagged releases that select iOS build a signed IPA for the official
`push.kaede.chat` relay and attach it to the release.
[Client releases](../desktop/docs/releasing.md) explains which changes select
iOS and how failed store uploads are retried.

The iOS job selects Xcode 26.3 on `macos-15` and checks for an iOS 26 or newer
SDK before building. App Store Connect has required this SDK since April 28,
2026, and the runner's default Xcode 16.4 is too old. App and extension
versions come from the numeric tag (`v0.1.38` becomes `0.1.38`). The build
number is `GITHUB_RUN_NUMBER.GITHUB_RUN_ATTEMPT`, so retries get a new one. The
IPA and checksum are kept as Actions artifacts even if Apple rejects the upload,
and a retry reuses that signed IPA.

Keep `desktop/rust-toolchain.toml` in step with the release workflow's
`RUST_VERSION` and installed targets, since native mobile builds use that
toolchain.

## 3. Enable optional TestFlight upload

In App Store Connect, create an API key allowed to upload builds. Add:

- `APP_STORE_CONNECT_KEY_ID`
- `APP_STORE_CONNECT_ISSUER_ID`
- `APP_STORE_CONNECT_PRIVATE_KEY_BASE64`

Then add the Actions variable `IOS_TESTFLIGHT_UPLOAD_ENABLED=true`. This key is
separate from the APNs key; don't reuse it for push delivery.

Only the upload is optional. The macOS desktop build needs the same three
secrets for notarization whenever a release builds desktop, even with iOS and
TestFlight off. Its other signing secrets are listed in
[client releases](../desktop/docs/releasing.md#release-secrets).

The TestFlight job waits up to 30 minutes for Apple to process the IPA. It then
sets the encryption answers through the App Store Connect API:

- **standard third-party encryption**
- **no proprietary encryption**
- **not distributed in France**

It reuses an approved matching declaration when one exists, so the API key also
needs permission to manage encryption declarations and builds. Review these
answers in `.github/scripts/upload-testflight.py` before changing the app's
cryptography or distributing in France. If processing or declaration assignment
fails, rerun the failed job. It picks up the existing matching build.

## 4. Configure the official relay

Only the `push.kaede.chat` worker receives these values:

```dotenv
KAEDE_PUSH_RELAY_APNS_KEY_B64=<one-line base64 of AuthKey_FC89Z6BV36.p8>
KAEDE_PUSH_RELAY_APNS_KEY_ID=FC89Z6BV36
KAEDE_PUSH_RELAY_APNS_TEAM_ID=6Q44L7WH4U
KAEDE_PUSH_RELAY_APNS_TOPIC=chat.kaede.mobile.voip
```

Deploy the code, apply the database migrations, and restart the API, worker,
and scheduler services. The rest of the relay setup is in
[mobile push delivery](mobile-push.md#home-configuration).

## 5. Test on a physical iPhone

Install a TestFlight build, enable notifications, and verify:

1. A direct message received while Kaede is terminated shows sender/message
   details and opens the correct conversation.
2. Mentions, guild-message preferences, DND, deleted/read messages, and preview
   privacy match Android, apart from iOS's generic alert for a suppressed wake.
3. A call received while Kaede is terminated opens CallKit immediately.
4. Answer joins the correct call with working two-way audio; decline and remote
   hang-up dismiss CallKit.
5. Repeat on Wi-Fi and cellular, then with the phone locked.

You don't need a Mac for any of this, since the GitHub macOS runner archives,
signs, and uploads the build. You do need a physical iPhone to test PushKit,
CallKit, and APNs.
