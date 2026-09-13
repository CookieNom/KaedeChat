This is the runtime portion of LiveKit Flutter 2.5.4 from pub.dev, under its
included Apache-2.0 license. Upstream package SHA-256:
`4b8ef07d4acbd21e43a1edfd05932c2d71cc0c433607cefe64afdfe87bb53962`. The app pins this local copy so native builds and
tests always use the same patch; the shared pub cache is never modified.

Kaede changes are limited to `lib/src/e2ee/e2ee_manager.dart` and
`lib/src/participant/local.dart`:

- Permit AV1 sender and receiver cryptors with the patched native WebRTC build.
- Attach video sender cryptors before negotiation, then bind the published SID.
- Encrypt backup senders and signal their encryption type.
- Track primary/backup cryptors separately for key changes and unpublish cleanup.

Update all clients together. Native artifacts come from `tool/build_webrtc.py`;
the stock WebRTC 137 binary does not support Kaede's encrypted AV1 wire format.
