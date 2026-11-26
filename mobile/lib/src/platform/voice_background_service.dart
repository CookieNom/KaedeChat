import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// Keeps Android's Flutter/WebRTC process eligible to run while a call is
/// active. iOS uses the `audio` background mode declared in Info.plist.
final class VoiceBackgroundService {
  const VoiceBackgroundService();

  static const _channel = MethodChannel('chat.kaede.mobile/voice_lifecycle');

  Future<void> screenCaptureStopped() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) return;
    try {
      await _channel.invokeMethod<bool>('screenCaptureStopped');
    } on MissingPluginException {
      // Tests and other platforms do not run the Android service.
    }
  }

  Future<bool> setActive(
    bool active, {
    bool microphone = false,
    bool camera = false,
    bool screenShare = false,
  }) async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) return true;
    try {
      return await _channel.invokeMethod<bool>(
            'setCallActive',
            <String, Object?>{
              'active': active,
              'microphone': microphone,
              if (camera) 'camera': true,
              'screenShare': screenShare,
            },
          ) ??
          false;
    } on MissingPluginException {
      return false;
    } on PlatformException {
      return false;
    }
  }
}
