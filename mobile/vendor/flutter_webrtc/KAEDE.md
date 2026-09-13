Runtime sources of flutter_webrtc 1.2.1 from pub.dev (upstream SHA-256
`71a38363a5b50603e405c275f30de2eb90f980b0cc94b0e1e9d8b9d6a6b03bf0`).
See the included LICENSE and NOTICE.

Kaede changes only the Android and Darwin FlutterRTCFrameCryptor bridges to
forward the configured key-derivation algorithm to the pinned native library.
The app selects HKDF to match desktop and browser encryption.
