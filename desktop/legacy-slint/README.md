# Archived Slint desktop client

This directory preserves the previous Slint client. It is excluded from the
current desktop workspace, Make targets, and release workflow. For current
builds, platform support, and signing instructions, use the
[Tauri desktop README](../README.md).

The archived client used Slint for its UI, Tokio for network/background work,
SQLite for cached state, and CPAL/LiveKit for media. It connected through the
user's home instance and stored credentials in the OS vault. Its old parity
inventory is in [parity.toml](../parity.toml); it describes the archived client,
not current backend support.

Historical design and packaging notes remain in Git history. They should not
be used to configure a current Kaede release.
