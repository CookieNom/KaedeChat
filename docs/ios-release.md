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
above and download all three `.mobileprovision` files. The main app profile
must include Push Notifications and the app group; both extension profiles
must include the same app group.

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

Add the Actions variable `IOS_RELEASE_ENABLED` with value `true`. Tagged GitHub
releases that select iOS changes will then build a signed IPA using the official
`push.kaede.chat` relay and attach it to the release. See
[client release selection](../desktop/docs/releasing.md) for the shared-code rules.

The iOS job explicitly selects Xcode 26.3 on `macos-15` and checks for an
iOS 26 or newer SDK before building. App Store Connect has required this SDK
since April 28, 2026; the runner's default Xcode 16.4 is too old.
The app and extension versions come from the numeric release tag (for example,
`v0.1.38` becomes `0.1.38`), with `GITHUB_RUN_NUMBER.GITHUB_RUN_ATTEMPT` as the
build number so retries produce a new build number. The IPA and checksum are
retained as Actions artifacts before TestFlight upload, even if Apple rejects
the upload. TestFlight delivery runs only when iOS was built and waits for CI,
every selected client build, and GitHub Release publication to succeed. A failed upload
is visible and retryable with **Re-run failed jobs**. The retry downloads the
existing signed IPA instead of rebuilding it.

Workflow fixes must be committed and included in a new release tag. Rerunning
an old tag uses its original workflow and source. Include the matching
`desktop/rust-toolchain.toml` pin: native mobile builds select that toolchain,
which must match the release workflow's `RUST_VERSION` and installed targets.

## 3. Enable optional TestFlight upload

In App Store Connect, create an API key allowed to upload builds. Add:

- `APP_STORE_CONNECT_KEY_ID`
- `APP_STORE_CONNECT_ISSUER_ID`
- `APP_STORE_CONNECT_PRIVATE_KEY_BASE64`

Then add the Actions variable `IOS_TESTFLIGHT_UPLOAD_ENABLED=true`. This is a
different key from the APNs key and should not be reused for push delivery.

## 4. Configure the official relay

Only the `push.kaede.chat` worker receives these values:

```dotenv
KAEDE_PUSH_RELAY_APNS_KEY_B64=<one-line base64 of AuthKey_FC89Z6BV36.p8>
KAEDE_PUSH_RELAY_APNS_KEY_ID=FC89Z6BV36
KAEDE_PUSH_RELAY_APNS_TEAM_ID=6Q44L7WH4U
KAEDE_PUSH_RELAY_APNS_TOPIC=chat.kaede.mobile.voip
```

Deploy the code, apply Alembic migration `6b1f4d8a2c90`, and restart the API,
worker, and scheduler services.

## 5. Test on a physical iPhone

Install a TestFlight build, enable notifications, and verify:

1. A direct message received while Kaede is terminated shows sender/message
   details and opens the correct conversation.
2. Mentions, guild-message preferences, DND, deleted/read messages, and preview
   privacy match Android.
3. A call received while Kaede is terminated opens CallKit immediately.
4. Answer joins the correct call with working two-way audio; decline and remote
   hang-up dismiss CallKit.
5. Repeat on Wi-Fi and cellular, then with the phone locked.

No personal Mac is required for these steps; the GitHub macOS runner performs
the archive, signing, and TestFlight upload. A physical iPhone is required for
meaningful PushKit, CallKit, and APNs testing.
