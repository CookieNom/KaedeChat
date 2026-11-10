# Desktop platform support

## Windows

Windows 10 and later use WASAPI through CPAL and Windows Credential Manager.
Screen audio requires Windows build 20348 or later (including Windows 11);
older builds support video-only sharing.
Camera discovery combines Media Foundation and DirectShow devices, including
DirectShow virtual cameras such as OBS. Matching device paths produce one camera
entry; DirectShow is also a capture fallback when Media Foundation cannot open it.
Native camera capture, desktop capture, global shortcuts, tray integration, and
Windows notifications are all supported. Signed NSIS packages are produced
on Windows runners. Production releases ship a per-user NSIS installer. It
installs under `%LOCALAPPDATA%\Kaede Chat`, registers a normal Windows
uninstaller, and lets the user opt in to a Start menu shortcut. Supported
Windows builds can also show the system taskbar-pin consent prompt from
Kaede's foreground UI.

## macOS

macOS 11 and later use CoreAudio, Keychain, ScreenCaptureKit-compatible LiveKit
capture, notifications, and global shortcuts. The bundle contains microphone,
camera, and screen-capture usage descriptions. Distribution builds require a
Developer ID, hardened runtime, notarization, and stapling. Screen audio uses
the system picker and requires macOS 14 or later.

## Linux

Linux supports PipeWire/PulseAudio/ALSA as exposed through CPAL and the desktop
session, plus Secret Service credentials, notifications, tray integration, and
portal-compatible screen capture. Building requires WebKitGTK 4.1, GTK 3,
Ayatana AppIndicator, ALSA, PulseAudio client headers (`libpulse-dev`), XCB
headers (`libxcb1-dev`), udev, D-Bus, OpenSSL, and Clang 21 or newer for
libwebrtc's bundled libc++. On Debian/Ubuntu, run `sudo sh desktop/install-clang.sh`
from the repository root, then build with `CC=clang-21 CXX=clang++-21`.
The tooling image and Linux CI jobs configure this automatically.
Global shortcuts use the X11 backend on Linux, so Wayland sessions may not receive
push to talk. Voice activity remains available in that case, and the UI reports
the limitation.

Windows, macOS, and Linux AppImage builds poll the latest GitHub Release for a
signed Tauri update. Checking is automatic; installation and restart always require a
user action. The updater signature is independent of Windows Authenticode and
Apple code signing, and all applicable signatures are required in production.
On Linux, in-app checks and installation only work for the AppImage build. Debian
and RPM installs don't check for updates and won't overwrite a system-owned
executable. No apt or dnf repository is published, so download new packages from
GitHub Releases.

macOS and Windows may gate the microphone through operating-system privacy
settings the first time the user explicitly joins or tests voice; Linux depends on
the desktop session. This is an OS privacy prompt, not a
browser device chooser. Devices can disappear or change IDs. When that happens,
Kaede rescans and falls back to the system default with a visible status
instead of opening an unrelated device.
