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

Install Node 22, pnpm 10.34, Rust 1.92, the Tauri CLI, and the native libraries
listed in [platform support](docs/platform-support.md). Then:

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
make desktop-check desktop-test
cargo +1.92.0 run --locked --manifest-path desktop/Cargo.toml -p kaede-tauri
```

For live frontend development, run `make desktop-dev`. Compile a release
binary with `make desktop-build`. Installers come from the desktop release
workflow and must pass the signing/notarization approval described in
[releasing](docs/releasing.md) before publication.

## Security boundary

The application loads only the bundled frontend. The Svelte code invokes a
narrow command allowlist and cannot use a shell or unrestricted filesystem API.
Access and refresh tokens remain in Rust and the operating system credential
vault. Presigned object uploads are performed without Kaede bearer credentials.
Turnstile uses an isolated helper window restricted to Cloudflare's challenge
origin. See [architecture](docs/architecture.md) for the complete boundary.
