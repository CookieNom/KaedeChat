# Screen sharing and media quality

## Server setup

Enable LiveKit in the deployment wizard and open the RTC/TURN ports listed in
the [operator guide](operator.md#hosts-certificates-and-ports).

- `KAEDE_VOICE_REGIONS` sets the catalog of regions users can choose from.
  `rtc_region: null` leaves the choice automatic.
- `make voice-check` exercises disposable LiveKit and Dragonfly services. It
  doesn't replace the physical-device checks in [release validation](#release-validation).

Bot playback, receiving, video publishing, and calls are covered in the
[SDK recipes](bot-api-quickstart.md#play-audio-in-a-voice-channel). Encrypted
media needs the room's current MLS state; see [E2EE](e2ee.md).

## Presets

People pick a preset before sharing. It's saved on the device, not the
account, and the server doesn't control it. It applies to both capture and the
LiveKit publication.

| Preset | Capture ceiling | Frame rate | Video bitrate ceiling | Encoder priority |
| --- | ---: | ---: | ---: | --- |
| Data saver | 1280×720 | 15 FPS | 1.2 Mbps | Detail |
| Smooth | 1280×720 | 30 FPS | 2.5 Mbps | Motion/frame rate |
| Sharp | 1920×1080 | 30 FPS | 4.5 Mbps | Detail/resolution |
| Source | Source resolution (8K defensive browser bound; 4K native bound) | 30 FPS | 8 Mbps | Detail/resolution |

These are ceilings, not reservations. WebRTC sends less for static content or
under congestion, trimming resolution or frame rate according to the encoder
priority. Native desktop capture keeps the aspect ratio and uses the even
dimensions I420 encoders need.

### Microphone audio

Outgoing microphone audio offers 24, 48, 96, and 128 kbps Opus ceilings.

- In the browser, the 128 kbps Studio option requests stereo.
- Mobile keeps the native channel layout (Flutter's LiveKit API has no stereo
  switch). Its bitrate changes in place without touching camera or screen tracks.
- Native desktop keeps its mono processed voice graph.

Silence suppression (Opus DTX) is a separate setting, on by default. RED stays
enabled where the SDK supports it. Changing quality mid-call doesn't re-request
microphone permission.

## Platform behavior and privacy

### Browsers

The browser owns the source chooser through `getDisplayMedia`, so Kaede can't
list or preview sources before consent. The browser and chosen surface decide
whether computer audio is available, and may cap resolution, especially for a
tab or small window.

### Windows and X11 desktop

The native client lists windows and displays itself. The chooser makes
one-frame thumbnails for at most 24 sources with three capture workers, and
never keeps full-resolution frames. The chosen source ID is checked against a
fresh trusted listing before it's stored.

To include sound, turn on **Share computer audio** (or **Share app audio** for a
window) in the share options:

- A display shares output from other apps.
- A window shares its owning app's output, including that app's other windows
  and child processes.
- Kaede's own call playback is left out, so callers don't hear themselves.

Screen audio is a separate 48 kHz stereo track, unaffected by microphone mute,
voice activation, or processing. Stopping the share or losing the chosen app
stops both screen tracks. A failed app-audio capture never falls back to
whole-computer audio.

Windows uses process-loopback capture, which needs build 20348 or later
(including Windows 11); earlier versions share video only. Linux monitors
individual playback streams through PipeWire-Pulse or PulseAudio, so ALSA-only
sessions can't share audio. X11 matches the window's process ID and its
children, so apps without a usable local process ID can't be matched.

### macOS desktop

ScreenCaptureKit's system picker handles source selection, so Kaede makes no
window list or thumbnails on macOS. The first share asks for Screen Recording
access. Denial or cancellation is reported, with no blank LiveKit publication
left behind. Production builds need the screen-recording usage and signing
setup the target macOS release expects.

Audio sharing needs macOS 14 or later and uses the same ScreenCaptureKit filter
as the video. On older systems, turn off shared audio to use video-only capture.

### Wayland desktop

When `WAYLAND_DISPLAY` is set or `XDG_SESSION_TYPE=wayland`, Kaede uses the
generic PipeWire/XDG Desktop Portal capturer. There's no XWayland source list,
preview, or cached ID; the compositor's portal handles consent. X11 sessions
keep Kaede's own source list.

The portal doesn't reliably reveal which process owns a window. To share audio,
pick **All computer audio** for a display, or the app's name for a window,
before opening the portal. Only apps currently playing sound are listed, so
start playback and refresh if needed. The audio choice isn't saved.

### Android

Kaede requests the user's MediaProjection grant, then promotes its voice
foreground service to the `mediaProjection` type before LiveKit uses the grant.
The manifest declares Android's foreground-service media-projection permission,
and the persistent notification says the screen is being shared. Audio isn't
captured. Stopping from system UI may revoke the grant; a new share gets a
fresh one.

### iOS and iPadOS

The app and its extensions are built for iPhone only
(`TARGETED_DEVICE_FAMILY = 1`); there's no separate iPad build.

Full-device sharing uses a ReplayKit Broadcast Upload extension, not in-app
capture. Runner and `BroadcastExtension` share the `group.chat.kaede.mobile`
app group. The extension sends JPEG frames over flutter-webrtc's private
app-group socket and drops new frames while one is pending, so memory stays
bounded. App Store signing must enable the App Group for both
`chat.kaede.mobile` and `chat.kaede.mobile.BroadcastExtension`
(see [iOS release setup](ios-release.md)).

The system broadcast picker and red recording indicator have the final say.
The extension ignores ReplayKit audio, because call audio already goes through
the normal LiveKit microphone track.

## Call video visibility

On mobile, leaving a video call enters native picture in picture if
**Automatic picture in picture** is on (the default, saved per device) and the
OS allows it: Android 8 and later, or iOS 15 and later. Desktop has an
always-on-top **Picture in picture** window and a normal **Pop out video**
window. Closing either returns the video inline in the same call and encryption
context, without duplicate audio.

Incoming video pauses when nobody can see it:

- Browsers pause it in hidden tabs.
- Desktop pauses it when the active video window is hidden or minimized.
- Mobile pauses it in the background unless native PiP is showing.

Audio, signaling, and outgoing media continue, and video resumes when visible.

## Voice and call routing

Guild voice runs on the guild's home instance. Human DM calls use the
conversation's authority: the lower participant domain for two people, or the
creator's home for a group. Bot calls use their DM capability's conversation
authority. A remote client gets a grant through the relevant API and connects
to the LiveKit server it returns. Existing media survives a federation outage, but
new joins need the authority. There's no automatic SFU failover.

Each grant names one room and one participant. `CONNECT`, `SPEAK`, and `STREAM`
separately control joining, microphone publishing, and camera/screen
publishing. LiveKit webhooks and periodic reconciliation remove stale or
unauthorized participants, and saved moderator mute/deafen state applies on
every join. Occupancy is temporary, and stale remote snapshots show as unknown.

A two-person call rings until the other person accepts; only then are media
tokens issued. End and decline retries are idempotent. The final call state is
saved even if best-effort LiveKit room removal fails, and a scheduled sweep
cleans up orphaned rooms.

## Codec defaults

Camera and screen sharing start with VP8. They switch to H.264 when only H.264
has proven hardware acceleration and every viewer accepts it. Each source has at
most one compatible track and one premium track.

The premium codecs are AV1 and HEVC (H.265). Kaede picks whichever serves the
most viewers, and AV1 wins ties, including when everyone supports both. HEVC is
never used as a compatible or software fallback codec. SDK backup codecs are
disabled on all clients.

Premium encoding must pass a hardware probe whose implementation, codec,
dimensions, and scalability mode match the publication. HEVC also needs
hardware decoding evidence; an advertised MIME type isn't enough. A missing,
redacted, or timed-out result never counts as hardware. The desktop decoder
probe uses a prerecorded HEVC frame and needs no encoder. Browser HEVC decoder
probes need a local HEVC encoder for their loopback, so decode-only browser
devices keep the compatible track.

### Dual-cast

A second track costs another encoder and more upload, so both run only when:

- at least a third of viewers use the selected premium codec,
- there's no encoder pressure, and
- the uplink isn't constrained.

Phones also skip dual-cast on cellular data or in low-power mode. When every
viewer takes the premium track, the compatible stream is dropped or paused
(desktop uses LiveKit dynacast).

A viewer without codec hints counts as VP8-only after eight seconds.
Unchanged hints repeat every four seconds. While hints are pending, the
compatible stream resumes immediately and the premium choice stays warm. A late
VP8-only viewer gets the compatible stream without displacing the premium one.

### Codec hints and older clients

The `kaede.video.v1` message keeps only `vp8`, `h264`, and `av1` in its legacy
`codecs` list. The additive `extendedCodecs` field carries `h265` and `vp9`.
Unknown extension strings are ignored, and the bounded list never changes the
old field. Fallback `demand` values stay within the legacy set too. Older
clients ignore the extension and keep their compatible track, so HEVC needs no
coordinated upgrade.

In encrypted calls, updated clients add `encryptedH264: 2` to their hints to
signal the corrected H.264 frame format, and offer H.264 only once every
participant does. With a 0.1.104 participant, camera and screen sharing fall
back to VP8; leaving H.264 out of the advertised list steers a 0.1.104
publisher there too. AV1 stays available when supported. Calls between older
clients follow their built-in policy.

### VP9 as the compatible track

VP9 can replace the compatible track only when every viewer accepts it, upload
is constrained, and thirty seconds of healthy encoder evidence shows CPU load
under half a core. A viewer asking for VP8 or H.264 blocks the switch. VP9 then
stays while it relieves upload pressure, provided the audience and measured CPU
headroom still qualify.

A replacement encoder has up to ten seconds to report statistics; missing
statistics can't extend the last CPU proof indefinitely. Encoder pressure or an
incompatible late join restores the compatible codec. The choice is per codec,
so a usable Intel encoder isn't excluded because a discrete GPU is present.

### Scalability and pressure

Web and desktop try spatial/temporal modes for AV1 and VP9 (`L3T3_KEY`, then
`L1T3`, then `L1T1`) when the actual encoder proves them. HEVC uses temporal
layers only (`L1T3` or `L1T1`). Unsupported modes fall back to a proven simpler
one. Browser publishing keeps the requested mode instead of LiveKit forcing AV1
screen shares to `L1T3`. The mobile native probe proves only `L1T1`, so mobile
requests that. Compatible VP8/H.264 tracks keep their simulcast ladders.

A struggling receiver first asks for a lower spatial layer if one exists, and
waits an adaptation interval before switching codec. Single-spatial-layer
streams need a compatible encode for lower resolution. Temporal layers reduce
frame rate, not resolution. Encoder load is measured across actual senders; an
SVC publication isn't free.

Encode pressure lowers resolution or frame rate one step at a time, and phone
thermal state counts as encode pressure. Text shares keep their pixels and drop
frames. Upload pressure retires a dual-cast premium track.

Bitrate budgets:

- The selected preset and the shared 8 Mbps video ceiling cap each source.
- Active codec tracks split their source's allowance equally.
- Simulcast layers share their track's allowance proportionally. Inclusive SVC
  layers count as one stream.
- During dual-cast, compatible video fits within 1280×720 (or 720×1280 in
  portrait). Retiring a track gives the remaining track its full allowance and
  size back.

Congestion control can lower rates further. Web and mobile keep
audience-based priorities within these limits. Desktop reserves a paused
dynacast publication's share so it can resume within the ceiling. Budget
changes apply in place.

| Adaptation timing | Value |
| --- | --- |
| Sampling interval | Every 2 seconds |
| Evidence before degrading | 6 seconds |
| Healthy time before recovering | 30 seconds |
| Minimum gap between steps | 20 seconds |
| Backoff for a failed premium codec | 1 minute, then 5, then 15, then the rest of the call |
| Healthy playback that clears failure history | 5 minutes |

All clients share these timings. Each failed premium codec backs off on its
own, and software fallback on a hardware-only HEVC path revokes HEVC. Keep the
browser `VIDEO_POLICY`, mobile `VideoPolicy`, and desktop `video_policy`
constants in step.

### Capability diagnostics and encryption

Open `/codec-probe` on the frontend to test each codec and scalability mode at
720p, 1080p, or 4K. It reports advertised support, observed encode/decode
frames, and actual mode and implementation names, and exports JSON. Tests use
synthetic pixels and local peer connections. **Reveal hardware names** offers
optional camera access, since browsers may hide implementation names without
it; no camera frames are sent, and the camera stops afterward. The page tests
the browser engine, not the desktop app's native encoder factory.

Call setup reuses exact-setting probe results and runs hardware loopbacks one at
a time. Transient negative results expire after one minute. Missing mode
statistics are accepted only for baseline VP8/H.264 `L1T1`, never as proof of a
scalable or premium mode.

Calls stay encrypted through codec changes, late joins, and key rotation. Kaede
keeps its existing AV1 wire format. HEVC uses the H.265 NAL cryptor with
full-width clear-prefix lengths and authenticated headers. The
[codec/E2EE validation notes](av1-e2ee/README.md) cover native artifacts and the
hardware interoperability matrix. Passing the capability probe doesn't replace
those E2EE tests.

## Release validation

For desktop audio, play different sounds in two apps. Confirm a display sends
both and an app window sends only its owning app (including child audio
processes), with no Kaede call echo. Also test:

- microphone mute on its own, app closure, and new playback streams
- output-device changes and capture denial
- stopping the share and leaving the call
- encrypted and plaintext calls with a remote receiver
- both explicit audio choices on Wayland

Protected media may refuse capture.

Linux's automated isolation test runs its own temporary PulseAudio server and
never changes the desktop's routing. Install `pulseaudio`, `libpulse-dev`,
`libxcb1-dev`, a C++ compiler, and `pkg-config`, then run
`bash desktop/test-screen-audio.sh` from the repository root.

Before a store or desktop release, validate every preset on a physical receiver.
Check permission denial, chooser cancellation, source closure, orientation
changes, network throttling, and stopping from system UI. On Android 14 and
later, confirm MediaProjection foreground-service ordering. Test iOS on a signed
physical device: Linux CI can't exercise the app-group extension path.

Also test backgrounding, restoring, closing PiP, and desktop minimize and tray
behavior in plaintext and encrypted calls. Incoming video should pause only when
it's hidden, and audio should keep playing. Leaving or reconnecting a call must
not leave a stale video window behind.
