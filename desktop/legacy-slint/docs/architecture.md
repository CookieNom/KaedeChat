# Desktop architecture

## Trust boundary

The client accepts an instance domain, establishes HTTPS using the operating
system trust store, and talks only to that home instance's `/api/v1` and
`/gateway` endpoints. It never signs or sends federation requests. Entity keys
are always the composite `(snowflake, origin domain)` pair.

Opaque access and refresh tokens are returned only to an explicit
`X-Kaede-Client: desktop` client. They are stored in the platform credential
vault. SQLite contains cached entities and UI state but no bearer credentials.
Cross-origin redirects and presigned uploads strip Kaede authorization headers.

## Runtime

Slint owns the main thread. Tokio owns HTTP, gateway, cache coordination, and
background work. Media decoding, database work, audio callbacks, and capture run
outside the UI thread. UI updates cross a bounded command channel and are
coalesced before being dispatched with `slint::invoke_from_event_loop`.

One overlay controller owns context menus, profiles, dialogs, emoji/GIF
pickers, and Turnstile. Opening an overlay closes the previous incompatible
overlay, preventing the stacked-context-menu failures that the web client has
encountered.

## Realtime state

REST snapshots and gateway dispatches feed a single reducer. Dispatch sequence
and session ID are resumable. A missing sequence or rejected resume triggers a
fresh snapshot. Ephemeral presence, typing, and voice occupancy are reconciled
periodically because they are not replayed. Channel access revocation removes
entities, decoded media, thumbnails, and downloads immediately.

## Audio and capture

The native voice graph is:

`CPAL input -> ring buffer -> channel map/resample -> DSP -> PTT/VAD gate -> LiveKit`

`LiveKit participant PCM -> mixer -> CPAL output`

The graph uses 48 kHz, mono, 10 ms capture frames. DSP stages implement a stable
trait boundary so echo cancellation, noise suppression, automatic gain control,
and optional model-based processing can be replaced without changing UI or room
state. The processor boundary also reserves a render-reference input for a
future acoustic echo canceller; until such a processor is installed, the output
mix is not advertised as echo-cancelled. LiveKit's platform audio device remains
a fallback when a native platform cannot expose raw PCM reliably.

Screen capture is provided by LiveKit's native WebRTC capture implementation:
Windows Graphics Capture on Windows, ScreenCaptureKit on macOS, and the XDG
ScreenCast portal/PipeWire where available on Linux. The settings view lists
displays and windows on platforms that permit enumeration and otherwise defers
selection to the secure operating-system picker.

Global push-to-talk uses platform keyboard hooks. Windows, macOS, and X11 are
supported; Wayland compositors may prohibit global key capture. In that case
Kaede reports that the shortcut is unavailable and voice activity remains
usable. A future XDG Global Shortcuts portal implementation can remove that
Wayland limitation without changing the audio input-mode contract.

## Application lifecycle

Only one process owns an account cache at a time. Later launches authenticate a
bounded loopback message with a per-run secret, forward validated `kaede://`
links to the first process, and bring its window forward. Closing the window
hides it in the system tray when tray integration is available; Quit performs a
normal gateway and credential lifecycle shutdown.

Audio, camera, and screen-source lists are reconciled periodically and on an
explicit refresh. A removed idle device falls back to the operating-system
default on the next connection. If an active device disappears, the media
session reports the failure and can be rejoined without tearing down message
state. Suspend/resume and capture revocation use the same recoverable path.

## Encrypted-message readiness

Message storage and rendering distinguish plaintext from an opaque versioned
envelope. Search, link previews, notifications, and indexing operate only on
plaintext supplied by the crypto provider. Device identity and key distribution
remain a future protocol milestone; the desktop client does not invent a local
wire format.

## Updates

Release artifacts use the native package format for each platform. The updater
accepts only HTTPS manifests signed with the compiled Ed25519 public key, checks
the package SHA-256 before staging, and never executes a downloaded file. The
platform installer applies the staged update so Windows and macOS signature and
notarization policy remain authoritative.
