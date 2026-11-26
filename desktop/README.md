# Kaede Chat desktop

The desktop app is a Tauri 2 shell around Kaede's static Svelte build, so web
and desktop share the same features. Rust handles what browsers can't do
reliably:

- secure credential storage and a resumable gateway
- native device selection, CPAL audio, and LiveKit
- global push to talk, voice activity, and local speech processing
- notifications, camera capture, screen capture, and safe object uploads

The previous Slint client lives in `legacy-slint/`. The normal Make targets and
release workflow don't build it.

## Development

You'll need Node 22, pnpm 10.34, Rust 1.97.1, the Tauri CLI, and the native
libraries for your platform (see [Linux](#linux) for the longest list).

Desktop builds also need a custom WebRTC archive with HEVC enabled, because
stock WebRTC prebuilts don't include it. Extract the matching
`kaede-desktop-webrtc` CI artifact for your OS and architecture into
`desktop/native/webrtc`, then run this from the repository root:
`python3 desktop/tool/prepare_codecs.py --cuda --artifact desktop/native/webrtc`.
Export the `LK_CUSTOM_WEBRTC` and (on Windows and Linux) `CUDA_HOME` paths it
prints before running Cargo or the desktop Make targets. To rebuild the
archive, see the [native codec instructions](../docs/av1-e2ee/README.md#desktop-native-artifacts).

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
make desktop-check desktop-test
cargo +1.97.1 run --locked --manifest-path desktop/Cargo.toml -p kaede-tauri
```

For live frontend development, run `make desktop-dev`. `make desktop-build`
runs a full `tauri build`, which produces bundles and updater artifacts as well
as the binary. Published installers come from the desktop release workflow and
must pass the signing and notarization steps in [releasing](docs/releasing.md).

## Platform support

### Windows

Windows 10 and later use WASAPI through CPAL and Windows Credential Manager.
Camera capture, desktop capture, global shortcuts, tray integration, and
Windows notifications all work.

Camera discovery combines Media Foundation and DirectShow devices, so DirectShow
virtual cameras such as OBS show up. A camera with the same device path in both
appears once. DirectShow is also the capture fallback when Media Foundation
can't open a device.

Releases ship a signed per-user NSIS installer. [Releasing](docs/releasing.md)
covers where it installs, the Start menu option, and the taskbar-pin prompt.

### macOS

macOS 11 and later use CoreAudio, Keychain, ScreenCaptureKit-compatible LiveKit
capture, notifications, and global shortcuts. The bundle includes microphone,
camera, and screen-capture usage descriptions. Distribution builds need a
Developer ID, the hardened runtime, notarization, and stapling.

### Linux

Audio goes through whatever CPAL and the desktop session expose: PipeWire,
PulseAudio, or ALSA. Credentials use Secret Service. Notifications, tray
integration, and portal-compatible screen capture are supported.

Building needs WebKitGTK 4.1, GTK 3, Ayatana AppIndicator, ALSA, PulseAudio
client headers (`libpulse-dev`), XCB headers (`libxcb1-dev`), udev, D-Bus,
OpenSSL, and Clang 21 or newer for libwebrtc's bundled libc++. On Debian or
Ubuntu, run `sudo sh desktop/install-clang.sh` from the repository root, then
build with `CC=clang-21 CXX=clang++-21`. The tooling image and Linux CI jobs set
this up for you.

Global shortcuts use the X11 backend, so Wayland sessions may not receive push
to talk. Voice activity still works, and the app tells the user about the
limitation.

Screen-audio requirements for each platform are in
[screen sharing and media quality](../docs/screen-sharing-quality.md#platform-behavior-and-privacy).

### Hardware video

| Platform | Hardware path |
| --- | --- |
| Windows | NVIDIA NVENC/NVDEC when its driver supports the codec; Media Foundation/D3D11 across physical adapters for Intel/AMD and other registered hardware |
| macOS | VideoToolbox H.264/HEVC |
| Linux | NVIDIA H.264/HEVC/AV1 encode and H.264/HEVC/VP9/AV1 decode; existing VAAPI H.264 encode |

Support is checked per codec, dimensions, and scalability mode. NVIDIA has no
VP9 encoder, but VP9 can still serve as the compatible track when upload is
limited and there's CPU headroom for software encoding. HEVC never falls back
to software. An installed Windows HEVC extension or a second GPU doesn't prove
there's a usable hardware path. Without hardware, the compatible stream stays
available. The full policy is in [screen sharing and media quality](../docs/screen-sharing-quality.md#codec-defaults).

### Devices and permissions

macOS and Windows may ask for microphone access through their privacy settings
the first time someone joins or tests voice. On Linux this depends on the
desktop session. It's an operating-system prompt, not a browser device chooser.

Devices can disappear or change IDs. Kaede saves a friendly name alongside the
ID, rescans when a device goes missing, and falls back to the system default
with a visible status rather than opening an unrelated device.

## Updates

Windows, macOS, and Linux AppImage builds check GitHub Releases for signed
updates and install them only when the user chooses to; see
[update checks](docs/releasing.md#update-checks). Debian and RPM installs don't
check, and the app won't overwrite a system-owned executable. There's no apt or
dnf repository, so download new packages from GitHub Releases.

## Desktop themes

In **User settings → Appearance**, choose **Default**, **Light**, **OLED**, or
**Cyberpunk Midnight**. Default follows your existing system/light/dark
preference. The theme choice stays on this device.

Use **Open themes folder** to add or edit `.theme.css` files. The app checks for
changes every two seconds, including edits to the active theme. Bundled files
are installed once, and your edits and deletions are kept.

Theme folders:

- Windows: `%LOCALAPPDATA%\Kaede Chat\Data\themes` (`%APPDATA%` is the fallback).
- macOS: `~/Library/Application Support/Kaede Chat/themes`.
- Linux: `$XDG_DATA_HOME/kaede-chat/themes`, or `~/.local/share/kaede-chat/themes`.

Copy a bundled theme or create a UTF-8 file up to 256 KiB:

```css
/**
 * @name My theme
 * @description A midnight palette with a violet accent.
 * @base dark
 */
:root[data-theme="dark"] {
  --app-bg: #101018;
  --accent: #c4a7ff;
}
```

`@base` accepts `dark` (the default), `light`, or `system`. The name defaults to
the filename. Override the variables in `frontend/src/styles.css` or add your
own CSS rules. Selectors that target specific components may need updating when
the UI changes.

Themes are CSS only, and the app's content security policy still applies, so
remote stylesheet imports and remote fonts are blocked. Files linked from
outside the themes folder aren't loaded.

Choose **Default** to remove custom styling. If a theme makes settings unusable,
move its file out of the folder and the app falls back automatically. A missing
or unreadable theme uses the default appearance until the file is back.

## Architecture

### Application boundary

Tauri packages `frontend/build`. The main window loads only the bundled app and
never navigates to an instance or other remote site. Other `http` and `https`
links open in the system browser. The Svelte routes stay authoritative for
chat, guilds, calls, settings, and the rest of the UI. Platform calls go through
the small adapter in `frontend/src/lib/platform/native.ts`, so browser behavior
doesn't change.

Rust takes an HTTPS home-instance domain and owns the API client, the rotating
session, and gateway resume state. The client never talks directly to
federation peers. All entity keys keep both the snowflake and the origin domain.

New web features usually need no desktop changes. Add a native adapter when a
feature needs credentials, OS integration, device access, capture,
notifications, or network access beyond the bundled UI's permissions.

### Accounts

The bottom-left account panel opens the saved-account chooser at `/accounts`.
You can sign in to several accounts on the same or different instances. Only
labels and account keys reach the WebView. Switching checks the saved session
first, disconnects voice, and reloads the chat UI. Gateway events carry an
account generation, so events queued for the old account are dropped.

Adding an account keeps existing sign-ins. Removing an inactive account deletes
its local credential without revoking other devices. The web app has the same
profile panel, but saved-account switching is desktop-only because web sessions
use a single pair of shared HttpOnly cookies.

### Voice and media

Desktop voice has a single owner. CPAL captures the chosen input into a bounded
queue. A worker resamples and downmixes it to 48 kHz mono ten-millisecond
frames, then applies echo, noise, and gain processing. That worker keeps the DSP
state warm while muted, evaluates voice activity or global push to talk, and
feeds a LiveKit native audio source.

Remote LiveKit tracks go into per-participant bounded queues and one normalized
mixer. The exact mixed signal sent to CPAL is also the echo canceller's render
reference. CPAL callbacks only move samples through bounded queues.

Camera and screen capture stay in Rust and use LiveKit native video sources.
Decoded remote frames reach the UI as bounded binary frames. Control messages,
state, meters, and video frames cross IPC, but microphone and speaker PCM never
do.

Uploads use Kaede's ticket, direct-upload, and commit protocol. Rust streams the
object to the presigned URL with its declared content type and length and no
Kaede authorization header. Media redirects follow the API client's rules for
stripping credentials on cross-origin hops.

### Lifecycle

Only one app process runs per user session. Closing the window hides it to the
tray, so calls, gateway events, notifications, and global push to talk keep
working. Quitting is a separate action. Changing a native device or DSP
preference retires the old voice pipeline and reconnects the active room.
Device scans happen on demand.

### Security boundary

Access and refresh tokens stay in Rust and the operating system's credential
vault. The Svelte code can call only a narrow command allowlist and has no shell
or unrestricted filesystem access. Presigned object uploads never carry Kaede
bearer credentials.

The main content security policy blocks objects, arbitrary scripts, and remote
`connect-src`. Frames are allowed only from YouTube's privacy domain and
Spotify. Images and media may load over HTTPS, and API traffic goes through Rust
commands. Tauri capabilities grant the `core:default` set, window-title changes,
notifications, and the app's command handler.

Turnstile runs in a separate short-lived helper window restricted to the home
instance's challenge page and Cloudflare's challenge host. Each request uses an
unpredictable ID and strict origin checks.

Logs and UI errors must never contain tokens, presigned URLs, password
material, or raw media buffers.
