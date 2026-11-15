Runtime sources of flutter_webrtc 1.6.3 from pub.dev (upstream SHA-256
`b98ef03376401e54ff87fc78907b38e562b15ca8424aba589c263eddc3c1ecf9`). See the included LICENSE and NOTICE.

Upstream supplies HKDF and RTP priority support. Kaede retains shared capture
clones, settings-specific hardware probes, hardware-only HEVC selection, DSCP,
and iOS picture-in-picture. Darwin sources now live under
`ios/flutter_webrtc/Sources/flutter_webrtc` and the corresponding macOS directory;
keep their shared changes in step with `common/darwin/Classes`.

Native dependencies pin WebRTC 150.7871.06. CocoaPods and Swift Package Manager
both require the local patched framework in `mobile/native/webrtc`; Android uses
the app's local Maven override. See `mobile/README.md` for artifact setup.
