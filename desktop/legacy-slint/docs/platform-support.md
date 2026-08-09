# Desktop platform support

Kaede Desktop targets 64-bit Windows, macOS, and Linux. Release packages are
built on the three operating systems instead of cross-compiling native media
and web-view dependencies.

## Windows

- Audio input and output use CPAL/WASAPI.
- Screen and window capture use the native LiveKit/libwebrtc capture path.
- Global push-to-talk uses a low-level keyboard hook and can be disabled at any
  time. It does not run while signed out.
- Credentials are stored in Windows Credential Manager.
- Camera, microphone, notification, and screen-capture consent remain under
  Windows privacy controls.

Production installers must be Authenticode-signed. CI can build unsigned test
artifacts, but SmartScreen behavior is not representative until a release is
signed by the project certificate.

## macOS

- Audio uses CPAL/CoreAudio.
- Screen and window selection uses the operating-system capture picker.
- Global push-to-talk requires Accessibility/Input Monitoring consent. The app
  remains usable with voice activity or an in-window key when consent is denied.
- Credentials are stored in Keychain.
- Camera, microphone, screen recording, and notification permissions are
  declared in the application bundle and requested by macOS at point of use.

Production applications must be Developer ID signed, hardened, notarized, and
stapled. Test unsigned bundles may require a local Gatekeeper override and must
not be distributed as releases.

## Linux

- Audio uses CPAL/ALSA. Distribution packages declare the corresponding runtime
  libraries.
- X11 can support a global push-to-talk key. Wayland intentionally does not
  promise an unrestricted global keyboard hook; desktop-environment portals and
  compositor policy decide what is available.
- Wayland screen sharing uses PipeWire through the desktop portal when the
  native LiveKit build supports it. The user chooses the source in the portal.
- Credentials use Secret Service. A session without an available secret-service
  collection cannot persist a refresh token and must not fall back to a plain
  text file.
- Tray support depends on Ayatana AppIndicator on packaged Linux systems.

The packaged desktop file registers the `kaede` URL scheme. Individual desktop
environments may require logging out once after the first installation before a
new scheme handler is visible everywhere.

## Shared behavior

Only the user's home instance receives Kaede credentials. Presigned object-store
uploads and cross-host media redirects use a separate client that never carries
the bearer token. Remote guild, DM, history, media, presence, and voice traffic
continues to be brokered by the home server.

The embedded Turnstile window is restricted to the home-instance challenge and
Cloudflare origins, has no general navigation controls, and is destroyed after
the one-time challenge result. The rest of the application is native Slint.

Device identifiers are treated as hints because operating systems can replace
them after sleep, reconnect, or driver updates. Kaede periodically reconciles
available microphones, speakers, cameras, displays, and windows and falls back
to the platform default when a saved device disappears.
