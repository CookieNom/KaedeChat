# Kaede Desktop

Kaede Desktop is the native Slint client for Kaede Chat. It connects only to a
user's home instance; federation, history replication, remote media, and voice
authorization remain server responsibilities.

The workspace targets Windows 10/11, macOS 14 or newer, and current Linux
desktops using Wayland or X11. The application UI is native Slint. A restricted
system web view is used only when an instance requires a Cloudflare Turnstile
challenge; it is not used to render the application.

Development status and the web-to-desktop coverage contract are tracked in
[`parity.toml`](parity.toml). Architecture and platform constraints are in
[`docs/architecture.md`](docs/architecture.md).

```sh
cargo fmt --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace
cargo run -p kaede-desktop
```

Platform packaging, signing, and notarization require the credentials described
in [`docs/releasing.md`](docs/releasing.md).

