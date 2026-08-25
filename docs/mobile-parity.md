# Mobile parity

Last audited: 2026-08-23 against the current repository working tree.

This is the living parity contract between the Flutter client and the web/
desktop client. A repository method by itself does not count as parity: the
capability must be reachable and usable from the mobile interface. The web
routes and components are the behavioral reference; mobile may use a sheet,
stacked route, native permission prompt, or operating-system service when that
is the better interaction for a phone.

| Status                 | Meaning                                                                            |
| ---------------------- | ---------------------------------------------------------------------------------- |
| **Aligned**            | Mobile exposes the same user outcome and authoritative API behavior.               |
| **Mobile equivalent**  | Mobile intentionally uses a platform-appropriate interaction for the same outcome. |
| **Known gap**          | The web/desktop outcome has no complete, reachable mobile path.                    |
| **Platform exclusion** | The desktop behavior is not a mobile parity target; the replacement is named.      |

## Auth and onboarding

| Capability                                                                                      | Status                | Repository-backed finding                                                                                                                                                                                                             |
| ----------------------------------------------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Home-instance discovery, login, registration, Turnstile, MFA challenge, and session restoration | **Aligned**           | `auth_screen.dart` and `turnstile_challenge.dart` cover the same home-auth APIs and password-KDF flow as the web auth routes.                                                                                                         |
| Email verification and password recovery                                                        | **Mobile equivalent** | Mobile can resend and paste verification/reset tokens in an in-app dialog. Web also accepts links on dedicated `/verify` and `/reset-password` routes.                                                                                |
| Invite acceptance                                                                               | **Mobile equivalent** | Mobile joins from a pasted invite code or URL in the guild action sheet; web has `/invite/[code]`.                                                                                                                                    |
| Account and invite deep links                                                                   | **Mobile equivalent** | Android App Links, iOS associated domains, and the custom scheme route invite, verification/email-change, password-reset, and copied guild/DM message links through signed-in and signed-out continuation.                                  |
| Device lock                                                                                     | **Mobile equivalent** | Mobile adds biometric/device-credential locking and a configurable background timeout; this is intentionally not copied from the browser UI.                                                                                          |

## Navigation, DMs, and friends

| Capability                                                                                 | Status                | Repository-backed finding                                                                                                                                                               |
| ------------------------------------------------------------------------------------------ | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Guild/DM browsing, unread and mention counts, categories, guild ordering, and guild groups | **Mobile equivalent** | `mobile_shell.dart` uses a phone-first rail/drawer and stacked conversation route while retaining composite federated identities.                                                       |
| Friends, inbound/outbound requests, blocks, profiles, new DMs, and group DMs               | **Aligned**           | Mobile exposes add, accept, decline, cancel, remove, block/unblock, message, group creation, membership, rename, leave, and ownership state.                                            |
| Create/join guild, invite shortcut, guild settings, and channel creation                   | **Aligned**           | Authorized users have a create-channel action directly in the guild header as well as the settings screen. Create/update responses are reconciled into navigation immediately.          |
| Responsive navigation                                                                      | **Mobile equivalent** | Compact screens use stacked pages and sheets; wider Flutter layouts use panes or navigation rails. Desktop-only hover, context-menu, and multi-window behaviors are not parity targets. |

## Messaging, rendering, composer, search, and pins

| Capability                                                                                                                           | Status                | Repository-backed finding                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------------------------------------------------------------------------------------------------ | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Realtime/paginated history, around-message jumps, read state, typing, drafts, optimistic sends, retry/discard, and offline snapshots | **Aligned**           | `channel_view.dart` and `mobile_controller.dart` implement the normal and degraded message paths, including federated composite references.                                                                                                                                                                                                                             |
| Reply/edit/delete/report, attachments, image/video/file viewing, reactions, and reaction members                                     | **Aligned**           | The mobile message action sheet reaches the same APIs, including explicit E2EE disclosure before reporting decrypted evidence.                                                                                                                                                                                                                                          |
| Markdown and message tokens                                                                                                          | **Aligned**           | Mobile renders practical GFM text, code, links, quotes, tap-to-reveal `\|\|spoilers\|\|`, user and role mentions, channel tokens, and custom emoji instead of displaying raw markup.                                                                                                                                                                                |
| Core composer                                                                                                                        | **Aligned**           | Text, attachments, user mentions, Unicode/custom emoji insertion, reply notification choice, slow mode, and permission gating are present.                                                                                                                                                                                                                              |
| Pinned messages                                                                                                                      | **Aligned**           | Mobile can pin/unpin, browse a pinned-message sheet, unpin from that sheet, and jump to the source message.                                                                                                                                                                                                                                                             |
| Message search                                                                                                                       | **Aligned**           | The scrollable screen supports scope, text, sort, pinned state, author type, dates, content kinds, specific From/Mentions users, paging, history, and `from:`, `mentions:`, and `has:` autocomplete. Candidate users include the current user, DM recipients/cached profiles, and a searchable guild roster. E2EE search remains unavailable by design on every client. |
| Search with the software keyboard                                                                                                    | **Mobile equivalent** | Filters and member pickers account for keyboard insets and remain scrollable on a compact viewport; the prior bottom overflow is covered by a widget regression test.                                                                                                                                                                                                   |
| Link-preview cards                                                                                                                   | **Aligned**           | Plaintext messages fetch the SSRF-protected home preview, cache per URL, retry failures, proxy authenticated media without leaking bearer credentials, and retain an external-open action. E2EE remains excluded.                                                                                                                                                      |
| GIF discovery, paging, and sending                                                                                                   | **Aligned**           | The composer action sheet opens a keyboard-safe picker backed by the home `/gifs` endpoint, supports trending/search, load more, safe KLIPY previews, and sends the selected URL. It is deliberately unavailable in E2EE rooms, matching the server-side feature exclusion.                                                                                             |
| GIF favorites                                                                                                                        | **Aligned**           | The picker and sent-message action sheet share a bounded persistent favorites list; favorites remain available ahead of trending/search results and can be removed from either surface.                                                                                                                                                                                |
| Unicode and custom emoji insertion                                                                                                   | **Aligned**           | The composer picker is searchable, inserts at the current selection, loads the user's custom-emoji catalog, prefers the current guild, and enforces Use External Emojis. Custom tokens render as media in messages.                                                                                                                                                     |
| Guild stickers                                                                                                                       | **Aligned**           | Mobile loads the member's sticker catalog, groups it by guild with the active guild first, enforces external-sticker permission, sends canonical standalone tokens, renders static and animated stickers at sticker size, and refreshes after sticker Gateway events.                                                                                                   |
| Application/slash/context commands                                                                                                   | **Aligned**           | Mobile discovers installed guild commands, autocompletes slash commands, sends their raw option payload through the authoritative interaction endpoint, and exposes message/user commands from the contextual message sheet.                                                                                                                                            |
| Forums and threads                                                                                                                    | **Aligned**           | Mobile exposes forum list/gallery views, title search, tag filters, Discord sort/view controls, atomic post creation, native `/thread`, message-scoped and private thread creation, active/archived discovery, thread membership and moderation, and future-only child-thread E2EE activation.                                                    |
| Reporter case history                                                                                                                | **Aligned**           | Privacy settings link to a refreshable My Reports screen backed by `/reports/@me`, including category, target, status, description, and submitted time.                                                                                                                                                                                                                  |

## Guild administration, permissions, invites, and webhooks

| Capability                                                  | Status                | Repository-backed finding                                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Guild profile/policy, ownership transfer, leave, and delete | **Aligned**           | Mobile exposes name/description, icon/banner, default notification level, history/federation policy, and owner-only lifecycle actions.                                                                                                                                                                                                      |
| Channel/category lifecycle and roles/hierarchy              | **Aligned**           | Create, edit, delete, reorder, parent category, topic, slow mode, E2EE controls, role create/edit/delete/reorder, and hierarchy ceilings are present. Create/update results are locally upserted and then reconciled, so a screen refresh is not required.                                                                                  |
| Per-channel federated history policy                        | **Aligned**           | Text and announcement channel creation/editing exposes inherit, disabled, and full-retained policy choices and sends the same `federated_history_policy` field as web.                                                                                                                                                                    |
| Base and channel permissions                                | **Aligned**           | Both role and channel editors use generated permission metadata. Channel overrides are role/member searchable, use deny/inherit/allow, support category sync/reset, and include applicable text, voice, voice-moderation, and federation permissions rather than empty headers.                                                             |
| Member roster, nicknames, and role assignment               | **Aligned**           | The roster is searchable/paged, overlays current profiles, enforces role ceilings, and supports nickname and role changes.                                                                                                                                                                                                                  |
| Member and instance moderation                              | **Aligned**           | Timeout supports reason, bounded presets, and indefinite state; bans support expiry and history deletion; instance bans support expiry/reason; kick, ban, and removal paths consistently collect optional audit reasons. Voice moderation remains available in-room.                                                                      |
| Guild emoji                                                 | **Aligned**           | List, upload, and delete are reachable with permission gating.                                                                                                                                                                                                                                                                              |
| Invites                                                     | **Aligned**           | Both mobile invite entry points expose intentional expiry and maximum-use choices, including never/unlimited, while list and revoke behavior remains unchanged.                                                                                                                                                                            |
| Webhooks                                                    | **Aligned**           | Mobile and the current web guild UI both list, create, reveal the one-time token, rotate, and delete. The backend update operation is not exposed by either client.                                                                                                                                                                         |
| Sticker management                                          | **Aligned**           | Mobile can select a PNG, JPEG, GIF, or WebP, preview and adjust a normalized crop, optionally request server-side background removal when enabled, create through the scanned upload flow, list usage, and delete guild stickers.                                                                                                            |
| Guild audit log                                             | **Mobile equivalent** | Mobile exposes a refreshable recent-event list. The current web guild settings route writes audit reasons but does not expose a guild-audit reader; cursor paging would be a shared enhancement, not a mobile parity gap.                                                                                                                   |
| Bots and automations                                        | **Aligned**           | Guild settings list installed bots, grants, intents, E2EE mode, status, and audited removal. Bot-install consent remains tracked separately with the developer/application work below.                                                                                                                                                     |

## Voice, video, and background execution

| Capability                                              | Status                 | Repository-backed finding                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Guild voice rooms                                       | **Aligned**            | Mobile uses the same LiveKit grants and exposes join/leave, reconnect state, participant occupancy/video/volume, and voice moderation.                                                                                                                                                                                     |
| DM call lifecycle                                       | **Aligned**            | Incoming calls have a compact caller banner with explicit answer/decline; answer performs the authoritative transition and opens the existing full call room, while terminal events dismiss stale presentation.                                                                                                           |
| In-call media controls                                  | **Aligned**            | Mute/deafen, push-to-talk/voice activity, camera, screen-share request, participant video, and per-participant volume are available in guild voice and active DM calls.                                                                                                                                                    |
| Phone/speaker/Bluetooth routing and runtime permissions | **Mobile equivalent**  | Mobile uses native WebRTC/LiveKit routing and OS microphone/camera permission prompts instead of the desktop device selector. Android requests Nearby Devices before Bluetooth enumeration or selection and falls back to speaker with a clear denial message.                                                             |
| Background voice membership                             | **Mobile equivalent**  | App lifecycle changes no longer clear a healthy room. Android starts a microphone/media-playback foreground service and iOS declares audio background mode; resume reconciles routes/tracks and renews a recoverable connection. This still requires signed physical-device release validation.                            |
| Android camera and full-screen sharing                  | **Mobile equivalent**  | Camera remains foreground-only by OS design. Full-screen sharing obtains an explicit MediaProjection grant, promotes the persistent voice service with the media-projection foreground type before capture, and shows a screen-sharing notification. Signed Android 14+ physical-device validation remains a release gate. |
| iOS full-screen sharing                                 | **Mobile equivalent**  | Full-device capture uses a ReplayKit Broadcast Upload extension and a shared app-group socket compatible with flutter-webrtc. Both targets must carry the App Group capability in the release provisioning profiles, and signed physical-device validation remains a release gate.                                         |
| System call integration                                 | **Mobile equivalent**  | Android uses a self-managed Telecom `ConnectionService`; iOS uses CallKit. Native answer/decline/end actions return to the authoritative REST transition and Android compilation is gated in CI. Signed physical-device lock-screen/audio-interruption validation remains a release gate.                    |
| Desktop app lifecycle controls                          | **Platform exclusion** | Desktop autostart, tray/taskbar, close behavior, and desktop updater settings do not map to mobile. Mobile uses OS lifecycle, store updates, background modes, and notification settings.                                                                                                                                  |

## Notifications, settings, and security

| Capability                                                               | Status                 | Repository-backed finding                                                                                                                                                                                                           |
| ------------------------------------------------------------------------ | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Message notifications and notification taps                              | **Mobile equivalent**  | Mobile uses authenticated, content-free FCM/APNs wakes, redeems details from the signed-in home, applies DM/mention/guild/DND/preview policy, and routes a tap to the composite channel/message. See [mobile push](mobile-push.md). |
| Call, moderation, and relationship notifications                         | **Aligned**            | Server-authored call, moderation, and relationship events now use opaque redeemable wakes over direct or relay transport, with DND/device validation, urgent call delivery, typed local categories, safe generic taps, and foreground gateway equivalents. |
| Profile, presence, DM privacy, email change, MFA, sessions, and sign-out | **Aligned**            | Mobile exposes profile images/text, presence, privacy, email confirmation, authenticator setup/disable/recovery codes, and per-session revocation.                                                                                  |
| E2EE device identity and recovery                                        | **Aligned**            | Mobile initializes the portable identity, lists identity records, resets with disclosure, and exports/restores bounded recovery backups using the shared vault format.                                                              |
| Credential and media boundaries                                          | **Aligned**            | Tokens use secure storage; protected media authorization is origin constrained; public notification avatars are HTTPS/path/size constrained; Turnstile runs in a restricted view.                                                   |
| Appearance, language, and developer mode                                 | **Known gap**          | Web exposes system/light/dark theme, `en-US`/`ja-JP` locale preference, and technical-ID developer mode. Mobile currently uses one Kaede theme and has no corresponding settings.                                                   |
| Browser/desktop notification and device settings                         | **Platform exclusion** | Browser permission prompts, desktop test notifications, autostart, updater, and native desktop device selectors are replaced by OS notification permission/category controls, the mobile app lock, and the in-call route picker.    |

## Advanced application, developer, and instance administration

| Capability                                 | Status                 | Repository-backed finding                                                                                                                                                                                                                                                            |
| ------------------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Developer teams and applications           | **Known gap**          | Mobile has no `/developers` equivalent for teams, applications, scopes/intents, credentials, workers, commands, templates, federation rules, or installations.                                                                                                                       |
| Bot installation consent                   | **Known gap**          | Mobile has no equivalent of `/applications/[applicationRef]/install/[templateSlug]`.                                                                                                                                                                                                 |
| Instance administration and Trust & Safety | **Known gap**          | Mobile has no administration surface for users, applications, reports, instance blocks, delegated operators, or instance audit. These remain responsive web/desktop pages as documented in [instance administration and developer portals](administration-and-developer-portals.md). |
| Owner grant/revoke CLI                     | **Platform exclusion** | Owner grants deliberately remain an operator CLI action and are not a parity target for web, desktop, or mobile.                                                                                                                                                                     |

Advanced portals are not inherently desktop-only. Until native screens are
prioritized, mobile should provide authenticated browser handoffs that preserve
the selected home instance and return path rather than silently hiding these
capabilities.

## Prioritized actionable gaps

### P0 — release gates

1. Validate background guild voice and DM calls on signed physical Android and
   iOS builds across screen lock, app switching, network changes, audio-route
   changes, permission prompts, and OS interruptions. Treat an in-app stale
   state separately from authoritative room membership.
2. Validate Android MediaProjection service ordering and the iOS ReplayKit
   app-group/upload-extension path on signed physical release devices,
   including denial, cancellation, rotation, system-stop, and reconnect flows.
3. Keep compact-screen regressions as ship gates: channel/role creation must
   appear immediately, permission rows must include voice grants, search must
   not overflow with the keyboard, and spoilers must remain concealed until
   activated.

### P2 — administration and preferences

1. Add theme, language, and developer-ID preferences with the same persisted
   settings contract as web.
2. Provide deliberate mobile entry points or authenticated web handoffs for
   developer teams/applications and capability-gated instance administration.

## Verification matrix

Use the same home account on the mobile target and the current web/desktop
reference. Voice, camera, push, biometrics, screen capture, and background
execution require physical devices; emulator-only results are insufficient.

| Target                                             | Required coverage                                                                                                                                                                 |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Android configured minimum SDK                     | Auth, compact layout, navigation, chat, file picker, permission denial, offline restore, and upgrade from a stored session.                                                       |
| Current Android release, physical phone            | FCM cold/warm tap, foreground service, 10+ minute locked/background voice, Wi-Fi/cellular transition, Bluetooth connect/disconnect, camera, and MediaProjection once implemented. |
| iOS 13 deployment target                           | Auth, navigation, chat, Keychain/session restore, notification permission denial, app lock, and layout fallback.                                                                  |
| Current iOS release, signed physical iPhone        | APNs cold/warm tap, background/locked voice, interruptions, route changes, camera, ReplayKit sharing/rotation/system-stop, and CallKit once implemented.                          |
| 320–390 logical-pixel phone at 1.0× and large text | Auth dialogs, composer, search keyboard, member pickers, channel editor, role/permission editor, and destructive confirmations without overflow or clipped actions.               |
| Tablet/foldable in portrait and landscape          | Pane/rail transitions, member pane, guild management, search, media viewer, and voice participant grid without losing selection.                                                  |
| Poor/offline network and federated remote origin   | Cached navigation/history, queued send retry/discard, reconnect, authority/search warnings, access revocation purge, and composite-ID correctness.                                |

### Cross-client acceptance checklist

- Create, edit, reorder, and delete a channel and role on mobile; verify both
  mobile lists update immediately and web receives the same state without
  leaving/re-entering either screen.
- Exercise every role and channel permission group, including Connect, Speak,
  Stream, Use Voice Activity, Mute/Deafen/Move Members, and federation grants;
  verify effective behavior from a second account.
- Send Markdown, a spoiler, mentions, a role token, channel token, custom emoji, a guild sticker,
  attachments, reply, edit, delete, reactions, and pinned messages in both
  directions.
- Search at DM, guild, and channel scope using From, Mentions, dates, pinned,
  author type, content kinds, inline operators, paging, and a software keyboard;
  verify the selected result jumps to the message.
- Background and lock each physical device during guild voice and a DM call;
  verify remote participants still hear/see the correct state and the local UI
  reconciles on resume rather than inventing a leave.
- Test notification opt-in/out, DND, previews disabled, visible-conversation
  suppression, cold launch, revoked access, expired session, and E2EE generic
  content.
- Complete registration, MFA, verification, recovery, email change, session
  revocation, E2EE backup/restore, and app-lock timeout on both platforms.
- Test owner, administrator, moderator, ordinary-member, federated-member, and
  permission-denied views so hidden controls never become the authorization
  boundary.

## Keeping this document current

- Re-audit a row whenever a mobile route/widget, web route/component, protocol
  registry, platform manifest/service, or relevant backend endpoint changes.
- Change a row to **Aligned** only after the mobile UI is reachable, automated
  tests cover its contract where practical, and the physical-device matrix is
  complete for native behavior.
- Run `dart format --output=none --set-exit-if-changed lib test`,
  `flutter analyze`, and `flutter test` from `mobile/`. Current focused
  regressions live in `widget_test.dart`, `guild_management_test.dart`, and
  `voice_lifecycle_test.dart`.
- Record release-specific device results in the release checklist; do not turn
  a code-presence audit into a claim that MediaProjection, ReplayKit, Telecom,
  CallKit, APNs, or FCM was validated on hardware.
