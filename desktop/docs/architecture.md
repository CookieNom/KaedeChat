# Desktop architecture

## Application boundary

Tauri packages `frontend/build`. The main window never navigates to an
instance or another remote site. The existing Svelte routes remain
authoritative for chat, DMs, and friends; guild management, permissions, and
moderation; and media, calls, settings, and federation-aware UI. Platform
calls go through the small adapter in `frontend/src/lib/platform/native.ts`,
so browser behavior is unchanged.

Rust accepts an HTTPS home-instance domain and owns the API client, the
rotating session, and gateway resume state. Secrets live in the platform
credential vault. The client never talks directly to federation peer
endpoints. All entity keys retain both snowflake and origin domain.

## Voice and media

Desktop voice has one owner. CPAL captures the chosen input into a bounded
queue. A worker resamples/downmixes to 48 kHz mono ten-millisecond frames and
applies echo/noise/gain processing. The same worker keeps the DSP state warm
while muted, evaluates voice activity or global push to talk, and feeds a
LiveKit native audio source.
Remote LiveKit tracks use per-participant bounded queues and one normalized
mixer. The exact post-mix signal sent to CPAL is also the echo canceller's render
reference. CPAL callbacks only move samples through bounded queues.

Camera and screen capture stay in Rust and use LiveKit native video sources.
Decoded remote frames cross the bridge as bounded binary frames for the bundled
UI. Control, state, meters, and video frames cross IPC; microphone and speaker
PCM do not.

Media uses Kaede's ticket, direct-upload, and commit protocol. Rust streams the
object to the presigned URL with its declared content type and length but no
Kaede authorization header. API media redirects are subject to the API client's
cross-origin credential stripping rules.

## Lifecycle

One application process is allowed per user session. Closing the window hides
it to the tray so calls, gateway events, notifications, and global push to talk
continue. Quit is explicit. Changing a native device or DSP preference fences
the old voice generation and reconnects the active room. Device scans happen on
demand. Because operating-system device IDs can change, the saved ID has a
friendly-name fallback.

## Trust controls

The main CSP denies frames, objects, arbitrary scripts, and remote connections.
All network access goes through Rust commands. Tauri capabilities expose only
core window operations and the explicit command handler. Turnstile starts a
separate short-lived helper process/window with an unpredictable request ID and
strict origin validation. Logs and UI errors must not contain tokens, presigned
URLs, password material, or raw media buffers.
