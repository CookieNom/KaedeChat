# Desktop implementation milestones

The desktop client is implemented in dependency order so protocol and security
decisions are settled before platform-specific polish.

## 1. Contracts and native authentication

- Composite identifiers, permissions, gateway operations, close codes, and
  event names are generated from the Python authority into `kaede-protocol`.
- `X-Kaede-Client: desktop` returns rotating body tokens; refresh tokens live in
  the operating-system credential vault.
- Adaptive Cloudflare Turnstile runs in an isolated, short-lived embedded system
  web view. The chat application itself remains native Slint and no external
  browser is required.

## 2. State, cache, and realtime

- One account runtime combines REST snapshots, an exhaustive gateway reducer,
  bounded SQLite cache, resume/gap recovery, optimistic nonces, and read state.
- Access revocation removes cached entities and decoded media immediately.
- Presence, typing, and voice occupancy are periodically reconciled because
  ephemeral federation events cannot be replayed indefinitely.

## 3. Native application parity

- Authentication, guild and DM navigation, message history and composition,
  friends, profiles, settings, moderation, roles, permissions, invites, emoji,
  GIFs, media, webhooks, and audit views use the same home-instance contracts as
  the web client.
- Server effective permissions gate actions locally while every mutation remains
  server-authorized. Versioned settings use conditional requests and surface
  conflicts instead of overwriting newer state.
- The overlay controller keeps context menus, profiles, pickers, and dialogs
  mutually exclusive.

## 4. Voice, video, and desktop capture

- CPAL owns selectable input/output devices and feeds raw frames through a
  replaceable processor chain into LiveKit.
- Voice activity and global push-to-talk are supported, with explicit Wayland
  limitations. Camera, display, and window sources are selectable where the OS
  permits enumeration.
- LiveKit grants remain the server authority for connect, speak, stream, move,
  mute, and deafen operations.

## 5. Platform lifecycle and releases

- Secret-backed single-instance forwarding, `kaede://` links, system tray,
  notifications, device hotplug, and signed update staging are implemented.
- CI builds Linux, Windows, and macOS packages. Public releases still require
  the platform signing credentials and physical-device validation listed in
  `releasing.md`.

## Contract-deferred surfaces

Message search, group DMs, stickers, notifications while the application is
fully closed, and E2EE device/key management have no backend protocol yet. The
desktop boundaries reserve these capabilities, but inventing a desktop-only
wire format would break federation and is intentionally avoided. They are
tracked explicitly in `parity.toml` rather than presented as completed features.
