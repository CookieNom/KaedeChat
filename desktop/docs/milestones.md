# Desktop delivery milestones

The Tauri implementation is organized as independently testable layers even
though they ship together:

1. Bundled Svelte shell, narrow IPC capabilities, native auth, restricted
   Turnstile, secure session storage, API and gateway ownership.
2. Native device settings, CPAL capture/playback, Rust LiveKit, bounded mixing,
   mute/deafen, push to talk, voice activity, camera, and screen sharing.
3. Echo cancellation, standard suppression, optional local voice isolation,
   automatic gain, meters, device tests, hot swap and reconnect fencing.
4. Native notifications, tray lifecycle, single instance behavior, upload
   streaming, binary video IPC, and cross-platform packaging.
5. CI checks for shared protocol drift, frontend parity, Rust tests, and native
   compilation on Windows, macOS, and Linux.

New web features should normally require no Tauri view work. A feature needs a
native adapter only when it touches credentials, operating-system integration,
devices, media capture, notifications, or unrestricted network destinations.
