# Kaede mobile

Kaede's Android and iOS clients use one Flutter presentation and domain layer while retaining native platform integrations for credentials, biometrics, notifications, media capture, and LiveKit voice/video. The layouts are mobile-first rather than scaled versions of the desktop shell.

The client connects only to the account's home instance. That instance brokers federation, remote media authorization, presence, history import, direct messages, and voice grants. A mobile client must never call peer federation endpoints directly.

## Included functionality

- registration, email verification, password recovery, MFA, rotating sessions, optional biometric/PIN device lock;
- home-instance discovery, guilds, federated guilds, direct messages, friends and blocks;
- paginated chat history, replies, pins, reactions, mentions, custom emoji, attachments, image viewing, video playback, GIFs, typing, read state, and offline snapshots;
- complete guild administration for overview, channels/categories, synchronized permission overwrites, roles and hierarchy, members, moderation, bans, instance bans, invites, emoji, webhooks, audit records, ownership transfer, leave, and deletion;
- LiveKit guild voice and DM calls with native WebRTC echo cancellation, noise suppression, automatic gain control, push-to-talk, voice activity, mute/deafen, camera, screen sharing, and route selection;
- Android notification channels for DMs, mentions, guild messages, calls, and account/moderation events, with equivalent iOS notification categories;
- adaptive network behavior: cached navigation and recent messages, small first pages, retryable writes, and deferred full-resolution media.

All snowflakes and permission masks remain decimal strings on the wire. Entity caches are keyed by the composite `id@origin-domain` identity.

## Development

Flutter is pinned to `3.41.4` in `.fvmrc`.

```sh
cd mobile
flutter pub get
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter run
```

Build an unsigned/debug Android artifact with:

```sh
flutter build apk --debug
```

iOS builds require macOS, the matching Xcode release, an Apple developer team, and a provisioning profile:

```sh
flutter build ios --release
```

## Android release signing

Generate or provision an upload key outside the repository. Copy `android/key.properties.example` to `android/key.properties`, fill in the four values, and keep both files private. Release builds deliberately do not fall back to the shared debug signing key.

```sh
flutter build appbundle --release
```

The application ID and iOS bundle ID are both `chat.kaede.mobile`.

## Firebase Cloud Messaging setup

Firebase is optional. Foreground gateway notifications and unread indicators
continue to work without it; reliable delivery after Android suspends or
terminates Kaede requires a platform push provider.

FCM is a no-cost Firebase product and does not require a billing account or
Google Analytics. To configure Android delivery:

1. Create a Firebase project and register an Android application whose package
   name is exactly `chat.kaede.mobile`.
2. Download the Android client configuration to
   `android/app/google-services.json`. From the repository root, the same path
   is `mobile/android/app/google-services.json`.
3. In Google Cloud IAM, create a dedicated service account with only **Firebase
   Cloud Messaging API Admin** (`roles/firebasecloudmessaging.admin`), generate
   a JSON key, and download it somewhere outside the repository. Revoke and
   replace any key that is ever pasted into chat, logs, or an issue tracker.
4. From the repository root, run `make setup` and choose to enable Firebase
   Cloud Messaging. Either provide the private service-account JSON file path or
   paste the complete JSON into the hidden multiline prompt, ending it with
   `KAEDE_FIREBASE_JSON_END` on a line by itself. The wizard base64-encodes it
   into the private operator `.env` as
   `KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64` and sets `KAEDE_PUSH_ENABLED=true`.
5. Run the environment validators, rebuild the APK/app bundle, and restart the
   API and worker services so both device registration and delivery use the new
   configuration.

Do not confuse the two JSON documents:

- `google-services.json` contains non-secret Android client/project identifiers
  and is bundled into the application. It is ignored by this repository so
  operators can inject the correct project at build time.
- The service-account JSON contains a private key. Never place it under
  `mobile/`, bundle it in an application, commit it, or paste it into logs. Only
  the Kaede backend/worker environment may receive it.

Both files must belong to the same Firebase project. The Gradle build applies
the Google Services plugin only when `android/app/google-services.json` exists,
so credential-free community builds remain valid. After installation, Android
13 and later also require the user to grant the notification permission.

For iOS, additionally inject `ios/Runner/GoogleService-Info.plist`, enable Push
Notifications and Background Modes in the signing profile, upload the APNs key
to the same Firebase project, and satisfy Apple's signing requirements.

### FCM privacy boundary

FCM connections are protected in transit but are not end-to-end encrypted by
default. Kaede therefore sends FCM a data-only wake containing only a protocol
version and a short-lived random token. It does not send sender names, message
text, notification kind, or channel/message references through FCM. The app
redeems the single-use token over the authenticated Kaede API, where device
ownership, current channel access, read state, do-not-disturb, and notification
preferences are rechecked before the app creates a local notification. If that
direct fetch fails, a content-free local fallback asks the user to open Kaede.

FCM still receives the app's provider token and delivery metadata such as the
Firebase project, target device, timing, platform, and network address. Message
previews affect only the authenticated Kaede response and local notification;
they do not change the FCM payload.

The app creates distinct user-controllable notification categories. The
repository includes authenticated, encrypted device registration and an FCM
HTTP v1 worker for notifications while the process is terminated. Device tokens
are encrypted at rest by the backend.

Do-not-disturb and per-guild `all`, `mentions`, or `none` settings are checked
both when queuing the wake and when redeeming it.

## Security boundaries

- Access and refresh tokens are stored in Android Keystore-backed encrypted storage or iOS Keychain and are never written to SQLite or logs.
- Presigned object uploads never receive the Kaede bearer token. Cross-host redirects from protected media must strip authorization.
- Link previews come from the home instance's SSRF-protected endpoint; the client does not scrape arbitrary pages.
- Channel access revocation must remove local message snapshots and decoded media for that channel.
- TLS validation uses the operating system trust store. Production builds do not expose an insecure-certificate override.
- Turnstile is rendered in a restricted in-app challenge view and only returns the one-time response token to the authentication flow.

## Platform notes

- Android screen sharing requires a user-approved MediaProjection session and an active foreground media-projection service.
- iOS screen sharing uses ReplayKit and requires an app-group/upload extension in the signed release target.
- Background voice/call presentation should be completed with Android Telecom/foreground service integration and iOS CallKit for store releases.
- Flutter's LiveKit client uses native WebRTC audio processing. A future neural isolation model belongs after echo cancellation and before the publication gate; it must not replace acoustic echo cancellation.

## Generated protocol

`lib/src/protocol/generated.dart` is generated from the authoritative Python registries by `backend/scripts/generate_protocol.py`. CI regenerates it and rejects drift alongside the TypeScript and Rust protocol modules.
