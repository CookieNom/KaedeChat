# Kaede Chat desktop

The supported desktop application is a Tauri 2 shell around Kaede's static
Svelte build. This keeps the web and desktop feature surfaces aligned. Rust
covers the parts that browsers cannot provide reliably:

- secure credential storage and a resumable gateway
- native device selection, CPAL audio, and LiveKit
- global push to talk, voice activity, and local speech processing
- notifications, camera capture, screen capture, and safe object uploads

The previous Slint client is preserved in `legacy-slint/`. It is not built by
the normal Make targets or release workflow.

## Development

Install Node 22, pnpm 10.34, Rust 1.97.1, the Tauri CLI, and the native libraries
listed in [platform support](docs/platform-support.md). Then:

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
make desktop-check desktop-test
cargo +1.97.1 run --locked --manifest-path desktop/Cargo.toml -p kaede-tauri
```

For live frontend development, run `make desktop-dev`. Compile a release
binary with `make desktop-build`. Installers come from the desktop release
workflow and must pass the signing/notarization approval described in
[releasing](docs/releasing.md) before publication.

## Desktop themes

In **User settings → Appearance**, choose **Default**, **Light**, **OLED**, or
**Cyberpunk Midnight**. Default uses your existing system/light/dark preference;
the desktop theme selection stays on this device.

Use **Open themes folder** to add or edit `.theme.css` files. The app checks for
changes every two seconds, including edits to the active theme. Bundled files
are installed once; your edits and deletions are preserved.

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
the filename. Override the variables in `frontend/src/styles.css` or add CSS
rules; selectors targeting specific components may need updating when the UI
changes. Themes contain CSS only, and the app's existing content security
policy still applies: remote stylesheet imports and remote fonts are blocked.
Files linked from outside the themes folder are not loaded.

Choose **Default** to remove custom styling. If a theme makes settings unusable,
move its file out of the folder; the app falls back automatically. Missing or
unreadable themes use the default appearance until the file is available again.

## Security boundary

The application loads only the bundled frontend. The Svelte code invokes a
narrow command allowlist and cannot use a shell or unrestricted filesystem API.
Access and refresh tokens remain in Rust and the operating system credential
vault. Presigned object uploads are performed without Kaede bearer credentials.
Turnstile uses an isolated helper window restricted to Cloudflare's challenge
origin. See [architecture](docs/architecture.md) for the complete boundary.
