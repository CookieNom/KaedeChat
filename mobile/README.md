# Kaede mobile

Kaede's Android and iOS clients share one Flutter presentation and domain layer. Native platform integrations remain for credentials, biometrics, notifications, media capture, and LiveKit voice/video. The layouts are mobile-first rather than scaled versions of the desktop shell.

The implementation-level web/desktop comparison, known gaps, and release-device
checklist live in [`docs/mobile-parity.md`](../docs/mobile-parity.md).

The client connects only to the account's home instance. That instance brokers federation, remote media authorization, presence, history import, direct messages, and voice grants. A mobile client must never call peer federation endpoints directly.

## Included functionality

- registration, email verification, password recovery, MFA, rotating sessions, optional biometric/PIN device lock;
- home-instance discovery, guilds, federated guilds, direct messages, friends and blocks;
- paginated chat history with replies, pins, reactions, mentions, and custom emoji; attachments, image viewing, video playback, and GIFs; typing, read state, and offline snapshots;
- core guild administration: overview, channels/categories, synchronized permission overwrites, and roles and hierarchy; members, moderation, bans, and instance bans; invites, emoji, webhooks, and audit records; ownership transfer, leave, and deletion; see the parity matrix for the remaining advanced controls;
- LiveKit guild voice and DM calls with native WebRTC echo cancellation, noise suppression, automatic gain control, push-to-talk, voice activity, mute/deafen, camera, screen-share controls subject to the platform notes below, and route selection;
- Android notification channels for DMs, mentions, guild messages, calls, and account/moderation events, with equivalent iOS notification categories; only message-event delivery is currently wired end to end;
- adaptive network behavior: cached navigation and recent messages, small first pages, retryable writes, and deferred full-resolution media.

All snowflakes and permission masks remain decimal strings on the wire. Entity caches are keyed by the composite `id@origin-domain` identity.

## Development

Flutter is pinned to `3.41.4` in `.fvmrc`. Fetch dependencies and run the
checks:

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

Generate or provision an upload key outside the repository. Copy `android/key.properties.example` to `android/key.properties`, fill in the four values, and keep both files private. Release builds never fall back to the shared debug signing key.

```sh
flutter build appbundle --release
```

The application ID and iOS bundle ID are both `chat.kaede.mobile`.

Tag-triggered GitHub Releases build a signed APK and Play AAB together with the
desktop clients. Configure the four Android signing secrets and the official
Firebase client-configuration secret listed in
[`desktop/docs/releasing.md`](../desktop/docs/releasing.md) before pushing a
release tag. The workflow deliberately fails instead of publishing an unsigned
artifact, omitting push support, or generating a throwaway key that would break
application updates.

## Background notification builds

Official releases use the Kaede relay at `push.kaede.chat`. The official
Firebase client configuration is injected by protected release CI. Federated
home operators receive neither it nor the provider service account. The app
pins the relay and registers its provider token directly there.

Community builds use a distinct application/bundle ID, signing identity,
Firebase/APNs project, and update lineage. Configure these Dart definitions:

```sh
flutter build apk \
  --dart-define=KAEDE_PUSH_TRANSPORT=relay \
  --dart-define=KAEDE_PUSH_RELAY_URL=https://push.example.com \
  --dart-define=KAEDE_PUSH_RELAY_ORIGIN=example.com \
  --dart-define=KAEDE_PUSH_APP_ID=org.example.kaede
```

For a single-home custom build, `KAEDE_PUSH_TRANSPORT=direct_fcm` retains the
legacy authenticated token registration. That home sets
`KAEDE_PUSH_ENABLED=true` and supplies its service account. Direct mode cannot
notify `chat.kaede.mobile` unless it owns the matching official Firebase
project, which third-party operators do not.

Android still requires `android/app/google-services.json` for the selected app
ID. iOS requires `GoogleService-Info.plist`, the Push Notifications/Background
Modes entitlements, APNs credentials in Firebase, and valid Apple signing. The
provider service-account JSON is private server material and must never be
placed under `mobile/`, bundled, logged, or committed.

Relay wakes are content-free and authenticated by a device/home secret that the
relay does not possess. The app verifies that MAC before redemption or fallback.
Notification details are fetched from the signed-in home, which rechecks access
and preferences. Encrypted messages use a generic notification unless the
device can decrypt locally. Full protocol and privacy details are in
[`docs/mobile-push.md`](../docs/mobile-push.md).

## Security boundaries

- Access and refresh tokens are stored in Android Keystore-backed encrypted storage or iOS Keychain and are never written to SQLite or logs.
- Presigned object uploads never receive the Kaede bearer token. Cross-host redirects from protected media must strip authorization.
- Link previews come from the home instance's SSRF-protected endpoint; the client does not scrape arbitrary pages.
- Channel access revocation must remove local message snapshots and decoded media for that channel.
- TLS validation uses the operating system trust store. Production builds do not expose an insecure-certificate override.
- Turnstile is rendered in a restricted in-app challenge view and only returns the one-time response token to the authentication flow.

## Platform notes

- Android camera capture is foreground-only. Screen sharing requests a user-approved MediaProjection session and promotes the active voice service to the media-projection foreground type before capture.
- iOS screen sharing uses the bundled ReplayKit Broadcast Upload extension. Runner and the extension require the `group.chat.kaede.mobile` App Group in their signed provisioning profiles.
- The shared preset behavior, privacy boundaries, and physical-device release checklist are documented in [`docs/screen-sharing-quality.md`](../docs/screen-sharing-quality.md).
- Android background voice uses a microphone/media-playback foreground service. System call presentation still requires Android Telecom and iOS CallKit/VoIP push integration for store releases.
- Flutter's LiveKit client uses native WebRTC audio processing. A future neural isolation model belongs after echo cancellation and before the publication gate; it must not replace acoustic echo cancellation.

## Generated protocol

`lib/src/protocol/generated.dart` is generated from the authoritative Python registries by `backend/scripts/generate_protocol.py`. CI regenerates it and rejects drift alongside the TypeScript and Rust protocol modules.
