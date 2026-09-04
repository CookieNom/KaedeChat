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
releases will then build a signed IPA using the official `push.kaede.chat`
relay and attach it to the release.

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
