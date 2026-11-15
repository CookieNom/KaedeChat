Runtime sources of LiveKit Flutter 2.13.2 from pub.dev (upstream SHA-256
`c63211bd5073b308417de2b048809c156f50cb5bda02713150478742be2a1bc1`). See the included Apache-2.0 license.

Kaede retains pre-negotiation sender encryption, separate backup cryptors,
shared capture clones, and exact proven scalability modes. An awaited
`VideoPublishOptions.configureEncodings` hook caps the computed RTP ladder before
negotiation. These changes live in `lib/src/options.dart`,
`lib/src/e2ee/e2ee_manager.dart`, `lib/src/participant/local.dart`, and
`lib/src/track/local/video.dart`.

Native WebRTC is pinned to 150.7871.06 with Kaede's AV1/HEVC cryptor and hardware
patches. Build or download the artifacts described in `mobile/README.md` before
native builds. The published package's stock WebRTC binary is not interchangeable.
