# Kaede mobile

Kaede's Android and iOS apps use Flutter, with native integrations for secure
credentials, biometrics, notifications, capture, and LiveKit calls.

The app talks only to the account's home instance, which handles federation,
remote media authorization, presence, history import, direct messages, and
voice grants. A mobile client must never call peer federation endpoints
directly.

## Included functionality

- Accounts and security: Registration, email verification, password recovery,
  MFA, rotating sessions, and an optional biometric/PIN device lock.
- Chat: Home-instance discovery, local and federated guilds, DMs, friends, and
  blocks. Paginated history with replies, pins, reactions, mentions, and custom
  emoji. Attachments, image viewing, video playback, GIFs, typing indicators,
  read state, and offline snapshots.
- Guild administration: Overview, channels and categories, synchronized
  permission overwrites, roles and hierarchy, and members. Moderation, bans, and
  instance bans. Invites, emoji, webhooks, integrations, audit records, and
  scheduled events. Ownership transfer, leaving, and deletion.
- Voice and video: LiveKit guild voice and DM calls with native WebRTC echo
  cancellation, noise suppression, and automatic gain control. Push to talk,
  voice activity, mute/deafen, camera, screen sharing (see [platform
  notes](#platform-notes)), and audio route selection.
- Notifications and network: Authenticated, content-free push wakes for
  messages, calls, moderation, and relationship events, using Android
  notification channels and matching iOS categories. Cached navigation and
  recent messages, small first pages, retryable writes, and deferred
  full-resolution media.

Snowflakes and permission masks stay decimal strings on the wire. Entity caches
are keyed by the composite `id@origin-domain` identity.

## Development

Flutter is pinned to `3.41.4` in `.fvmrc`. Fetch dependencies and run the
checks:

```sh
cd mobile
flutter pub get
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
```

Before `flutter run` or `flutter build`, put this revision's patched WebRTC
artifacts in `native/webrtc`. The stock native library can't handle Kaede's
encrypted AV1 format. Download the artifact from CI, or build it with
`python3 tool/build_webrtc.py android --work-dir /tmp/kaede-webrtc-android` on
Linux (`ios` on macOS). CI and release builds use the same artifacts. See the
[native codec build notes](../docs/av1-e2ee/README.md#mobile-native-artifacts)
for extraction and prerequisites.

Android builds need JDK 21 to read the patched WebRTC library's Java classes.
Point `JAVA_HOME` at your JDK 21, select it for Flutter, and build an
unsigned debug APK:

```sh
flutter config --jdk-dir="$JAVA_HOME"
flutter build apk --debug
```

iOS builds need macOS, the matching Xcode release, an Apple developer team,
and a provisioning profile:

```sh
flutter build ios --release
```

## Android release signing

Create or obtain an upload key outside the repository. Copy
`android/key.properties.example` to `android/key.properties`, fill in the four
values, and keep both files private. Release builds never fall back to the
shared debug signing key.

```sh
flutter build appbundle --release
```

The application ID and iOS bundle ID are both `chat.kaede.mobile`.

Tagged releases build a signed APK and Play AAB when Android or shared mobile
files change. Before pushing a tag, configure the four Android signing secrets
and the official Firebase client-configuration secret listed in
[`desktop/docs/releasing.md`](../desktop/docs/releasing.md). Without them the
workflow fails rather than publishing an unsigned build, dropping push support,
or generating a throwaway key that would break updates.

## Background notification builds

Official releases use the Kaede relay at `push.kaede.chat`, with the Firebase
client configuration injected by protected release CI. Community builds need
their own app identity and Firebase/APNs project (see
[delivery choices](../docs/mobile-push.md#delivery-choices)) and these Dart
definitions:

```sh
flutter build apk \
  --dart-define=KAEDE_PUSH_TRANSPORT=relay \
  --dart-define=KAEDE_PUSH_RELAY_URL=https://push.example.com \
  --dart-define=KAEDE_PUSH_RELAY_ORIGIN=example.com \
  --dart-define=KAEDE_PUSH_APP_ID=org.example.kaede
```

For a single-home custom build, `KAEDE_PUSH_TRANSPORT=direct_fcm` keeps the
legacy authenticated token registration, and that home sets
`KAEDE_PUSH_ENABLED=true` with its own service account. Direct mode can't
notify `chat.kaede.mobile`.

Android still needs `android/app/google-services.json` for the chosen app ID.
iOS needs `GoogleService-Info.plist`, the Push Notifications and Background
Modes entitlements, APNs credentials in Firebase, and valid Apple signing. The
provider service-account JSON is private server material. Never put it under
`mobile/`, bundle it, log it, or commit it.

Server setup, the wake protocol, and the privacy model are in
[`docs/mobile-push.md`](../docs/mobile-push.md).

## Security boundaries

- Access and refresh tokens are stored in Android Keystore-backed encrypted storage or iOS Keychain and are never written to SQLite or logs.
- Presigned object uploads never receive the Kaede bearer token. Cross-host redirects from protected media must strip authorization.
- Link previews come from the home instance's SSRF-protected endpoint; the client does not scrape arbitrary pages.
- Channel access revocation must remove local message snapshots and decoded media for that channel.
- TLS validation uses the operating system trust store. Production builds do not expose an insecure-certificate override.
- Turnstile is rendered in a restricted in-app challenge view and only returns the one-time response token to the authentication flow.

## Platform notes

- Android camera capture works only in the foreground.
- Screen sharing uses MediaProjection on Android and the bundled ReplayKit
  Broadcast Upload extension on iOS. Runner and the extension need the
  `group.chat.kaede.mobile` App Group in their signed provisioning profiles.
  Presets, capture details, and the device checklist are in
  [`docs/screen-sharing-quality.md`](../docs/screen-sharing-quality.md).
- Android background voice uses a microphone/media-playback foreground service.
  iOS uses a dedicated APNs VoIP relay subscription, PushKit, and CallKit, so
  calls to a closed app reach the native incoming-call screen.
- Flutter's LiveKit client uses native WebRTC audio processing. Any future
  neural voice isolation goes after echo cancellation and before the
  publication gate. It must not replace acoustic echo cancellation.

## Generated protocol

`backend/scripts/generate_protocol.py` generates `lib/src/protocol/generated.dart`
from the Python registries. CI regenerates it, along with the TypeScript and
Rust protocol modules, and fails on any drift.

## Cross-client release checks

Sign in to the same account on mobile and web and compare. Test the minimum
supported OS, a current physical Android phone, a signed physical iPhone, a
320–390 logical-pixel screen with large text, and a tablet or foldable in both
orientations. Native media and background behavior need real hardware.

- Create/edit/reorder channels and roles; both clients should update without
  reopening the screen. Test owner, moderator, ordinary, and remote members.
- Test task/lane changes and stale-version recovery from a second client.
- Send replies, Markdown, concealed spoilers, mentions, emoji, stickers,
  attachments, reactions, and pins in both directions. Check moderation controls
  with and without permission, and confirm reaction counts reconcile.
- Follow and publish announcements to local and remote guilds, then remove
  follows. Search DMs, guilds, and channels with filters and pagination while
  the software keyboard is open; results must jump to the selected message.
- Test offline history, send retry/discard, reconnect, and access-revocation
  cleanup. Equal numeric IDs from different domains must remain distinct.
- Complete registration, verification, recovery, MFA, email change, session
  revocation, E2EE backup/restore, and app-lock timeout.
- Test push opt-in/out, DND, disabled previews, cold launch, expired sessions,
  revoked access, and generic E2EE notifications. See [iOS release setup](../docs/ios-release.md).
- Keep a voice call active for at least ten minutes while locked/backgrounded.
  Test Wi-Fi/cellular transitions, Bluetooth changes, interruptions, and camera.
  Confirm remote participants and the resumed UI agree about room membership.
- Test screen-share denial, cancellation, rotation, source closure, system stop,
  and reconnect. Android needs MediaProjection foreground-service ordering;
  iOS needs signed ReplayKit/App Group validation. See
  [media quality](../docs/screen-sharing-quality.md#release-validation).

Mobile uses sheets and OS controls instead of desktop hover menus, tray,
autostart, and updater settings. Developer Portal and Instance Administration
have native screens, but owner grants are CLI-only. The interface is available
in English and Japanese, and the locale also affects dates and localized command
labels.

Record real device results with each release. Automated tests alone don't show
that background calls, capture, or push work on hardware.
