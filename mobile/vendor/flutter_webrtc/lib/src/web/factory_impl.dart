import 'package:webrtc_interface/webrtc_interface.dart';
import '../desktop_capturer.dart';

export 'package:dart_webrtc/dart_webrtc.dart'
    hide videoRenderer, MediaDevices, MediaRecorder;

DesktopCapturer get desktopCapturer => throw UnimplementedError();

Future<MediaStreamTrack> cloneLocalVideoTrack(MediaStreamTrack track) async =>
    throw UnsupportedError('Native shared capture is Android-only');
