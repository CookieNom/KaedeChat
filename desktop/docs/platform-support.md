# Desktop platform support

## Windows

Windows 10 and later use WASAPI through CPAL and Windows Credential Manager.
Native camera capture, desktop capture, global shortcuts, tray integration, and
Windows notifications are all supported. Signed MSI/NSIS packages are produced
on Windows runners.

## macOS

macOS 11 and later use CoreAudio, Keychain, ScreenCaptureKit-compatible LiveKit
capture, notifications, and global shortcuts. The bundle contains microphone,
camera, and screen-capture usage descriptions. Distribution builds require a
Developer ID, hardened runtime, notarization, and stapling.

## Linux

Linux supports PipeWire/PulseAudio/ALSA as exposed through CPAL and the desktop
session, plus Secret Service credentials, notifications, tray integration, and
portal-compatible screen capture. Building requires WebKitGTK 4.1, GTK 3,
Ayatana AppIndicator, ALSA, udev, D-Bus, OpenSSL, and the ordinary C/C++ build
toolchain. Global push to talk can be unavailable on Wayland compositors that
don't grant a global-shortcut portal. Voice activity remains available in that
case, and the UI reports the limitation.

All platforms require an operating-system microphone permission the first time
the user explicitly joins or tests voice. This is an OS privacy prompt, not a
browser device chooser. Devices can disappear or change IDs. When that happens,
Kaede rescans and falls back to the system default with a visible status
instead of silently opening an unrelated device.
