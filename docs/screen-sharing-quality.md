# Screen sharing and media quality

Kaede presents media quality choices before starting a screen share. The
selection is stored locally on the device and is applied to both capture and
the LiveKit publication; it is not an account-level or server-controlled
setting.

## Presets

| Preset | Capture ceiling | Frame rate | Video bitrate ceiling | Encoder priority |
| --- | ---: | ---: | ---: | --- |
| Data saver | 1280×720 | 15 FPS | 1.2 Mbps | Detail |
| Smooth | 1280×720 | 30 FPS | 2.5 Mbps | Motion/frame rate |
| Sharp | 1920×1080 | 30 FPS | 4.5 Mbps | Detail/resolution |
| Source | Source resolution (8K defensive browser bound; 4K native bound) | 30 FPS | 8 Mbps | Detail/resolution |

The ceilings are targets, not reservations. WebRTC can send less during static
content or congestion, and may reduce resolution or frame rate according to the
selected encoder priority. Native desktop capture preserves aspect ratio and
uses even output dimensions required by I420 encoders.

Outgoing microphone audio offers 24, 48, 96, and 128 kbps Opus ceilings. The
browser's 128 kbps Studio option requests stereo and disables discontinuous
transmission. Flutter's current LiveKit API does not expose its stereo switch,
so mobile keeps the native microphone channel layout while disabling DTX for
Studio on its next publication negotiation; its sender bitrate changes in place
without disturbing camera or screen tracks. Native desktop keeps its mono
processed voice graph. RED remains enabled where the SDK supports it. Changing
audio quality during a call does not reacquire microphone permission.

## Platform behavior and privacy

### Browsers

The browser owns the tab/window/display chooser through `getDisplayMedia`.
Kaede cannot enumerate or preview sources before consent. Browser support and
the selected surface determine whether computer audio is available. The
browser may further constrain a requested resolution, especially for a tab or
small window.

### Windows and X11 desktop

The native client can enumerate windows and displays. The chooser creates
bounded, one-frame thumbnails for at most 24 listed sources with three capture
workers; full-resolution frames are not retained or persisted. The selected
source ID is checked against a fresh trusted enumeration before it is stored.

Native desktop system-audio loopback is disabled in the chooser
until the Windows WASAPI, macOS ScreenCaptureKit audio, and Linux PipeWire
paths have a tested common mixer contract. This does not affect microphone
bitrate selection.

### macOS desktop

ScreenCaptureKit's system picker owns source disclosure and selection. Kaede
does not enumerate windows or produce application-owned thumbnails on macOS.
The first share prompts for Screen Recording access; denial or cancellation is
reported and no blank LiveKit publication is left behind. A production build
must be signed with the screen-recording usage/signing configuration expected
by the target macOS release.

### Wayland desktop

When `WAYLAND_DISPLAY` is set or `XDG_SESSION_TYPE=wayland`, Kaede uses the
generic PipeWire/XDG Desktop Portal capturer. It returns no
XWayland source list, previews, or cached IDs, leaving disclosure and consent
to the compositor's portal. X11 sessions continue to use explicit source
enumeration.

### Android

Kaede requests the user-owned MediaProjection grant before capture, then
promotes its existing voice foreground service with the `mediaProjection`
type before LiveKit consumes the grant. The manifest includes Android's
foreground-service media-projection permission and the persistent notification
states that the screen is being shared. Audio is not included in the
screen capture. Android may revoke the grant when the user stops sharing from
system UI; the user can start a new share to obtain a fresh token.

### iOS and iPadOS

Full-device sharing uses a ReplayKit Broadcast Upload extension, not in-app
ReplayKit capture. Runner and `BroadcastExtension` share the
`group.chat.kaede.mobile` app group. The extension sends bounded-backpressure
JPEG frames over flutter-webrtc's private app-group socket and drops new frames
while one is pending instead of accumulating memory. App Store signing must
enable the same App Group capability for both bundle IDs:

- `chat.kaede.mobile`
- `chat.kaede.mobile.BroadcastExtension`

The system broadcast picker and red recording indicator remain authoritative.
The extension ignores ReplayKit app/microphone audio because Kaede publishes
call audio through its normal LiveKit microphone track.

## Release validation

Before a store or desktop release, validate every preset on a physical receiver
and verify permission denial, chooser cancellation, source closure, orientation
changes, network throttling, and stop-from-system-UI behavior. Android 14+
testing must confirm MediaProjection foreground-service ordering. iOS testing
must use a signed physical device because the app-group extension path cannot
be validated by Flutter's Linux CI or treated as proven by project-file
presence alone.

## Voice and call routing

Guild voice runs on the guild's home instance. Human two-person DM calls use
the caller's instance as call authority; bot calls use their DM capability's
conversation authority. A remote client obtains a grant through the relevant
API and connects to the returned LiveKit server. Existing media can continue
through a federation outage, but new joins require the authority. There is no
automatic SFU failover to another instance.

Grants name one room and participant. `CONNECT`, `SPEAK`, and `STREAM` control
joining, microphone publication, and camera/screen publication separately.
LiveKit webhooks and periodic reconciliation remove stale or unauthorized
participants; persisted moderator mute/deafen state applies on every join.
Occupancy is temporary state, and stale remote snapshots display as unknown.

A two-person call begins ringing and needs the other participant's acceptance
before media tokens are issued. End/decline retries are idempotent. Terminal
state commits independently of best-effort LiveKit room removal; a scheduled
sweep cleans up orphaned rooms.

Enable LiveKit in the deployment wizard and open the RTC/TURN ports in the
[operator guide](operator.md#hosts-certificates-and-ports). Configured
`KAEDE_VOICE_REGIONS` values form the selectable region catalog; `rtc_region:
null` leaves selection automatic. `make voice-check` exercises disposable
LiveKit and Dragonfly services; it does not replace physical-device checks.

For bot playback, receiving, video publication, and calls, use the
[SDK recipes](bot-sdk-recipes.md#play-audio-in-a-voice-channel).
Encrypted media requires the room's current MLS state; see [E2EE](e2ee.md).

## Codec defaults

Plaintext camera and screen sharing prefer AV1 when supported, with H.264 as
the compatibility choice and VP8 where H.264 is unavailable. Desktop can
publish a backup codec on demand; web and Flutter follow their SDK fallback
behavior. CPU load and thermal throttling do not trigger an application-level
codec switch.

Encrypted calls keep VP8. AV1 decoding support alone does not establish
compatibility with Kaede's experimental encrypted AV1 format. Keep encryption
enabled and use the supported codec; [the AV1 notes](av1-e2ee/README.md)
record the separate interoperability investigation.
