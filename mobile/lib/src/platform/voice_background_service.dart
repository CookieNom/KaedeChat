import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// Keeps Android's Flutter/WebRTC process eligible to run while a call is
/// active. iOS uses the `audio` background mode declared in Info.plist.
final class VoiceBackgroundService {
  const VoiceBackgroundService();

  static const _channel = MethodChannel('chat.kaede.mobile/voice_lifecycle');

  Future<bool> setActive(
    bool active, {
    bool microphone = false,
    bool screenShare = false,
  }) async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) return true;
    try {
      return await _channel.invokeMethod<bool>(
            'setCallActive',
            <String, Object?>{
              'active': active,
              'microphone': microphone,
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
