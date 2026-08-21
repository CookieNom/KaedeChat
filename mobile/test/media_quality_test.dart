import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/voice/media_quality.dart';
import 'package:livekit_client/livekit_client.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('screen profiles bind capture and publish limits', () {
    const quality = MobileMediaQuality(
      screen: ScreenShareQuality.sharp,
      audio: VoiceAudioQuality.high,
    );

    expect(quality.screen.profile.width, 1920);
    expect(quality.screen.profile.height, 1080);
    expect(quality.screen.profile.frameRate, 30);
    expect(
        quality.videoPublishOptions.screenShareEncoding?.maxBitrate, 4500000);
    expect(quality.videoPublishOptions.degradationPreference,
        DegradationPreference.maintainResolution);
  });

  test('audio presets use bounded Opus bitrates and studio disables DTX', () {
    const standard = MobileMediaQuality();
    const studio = MobileMediaQuality(audio: VoiceAudioQuality.studio);

    expect(standard.audioPublishOptions.audioBitrate, 48000);
    expect(standard.audioPublishOptions.dtx, isTrue);
    expect(studio.audioPublishOptions.audioBitrate, 128000);
    expect(studio.audioPublishOptions.dtx, isFalse);
  });

  test('iOS broadcast extension receives validated capture bounds', () async {
    const channel = MethodChannel('chat.kaede.mobile/screen_share');
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    final received = <MethodCall>[];
    debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
    messenger.setMockMethodCallHandler(channel, (call) async {
      received.add(call);
      return null;
    });
    addTearDown(() {
      messenger.setMockMethodCallHandler(channel, null);
      debugDefaultTargetPlatformOverride = null;
    });

    const quality = MobileMediaQuality(screen: ScreenShareQuality.sharp);
    await quality.prepareIosBroadcastExtension();
    await quality.stopIosBroadcastExtension();

    expect(received.first.method, 'setCaptureProfile');
    expect(received.first.arguments, <String, Object?>{
      'width': 1920,
      'height': 1080,
      'frameRate': 30,
    });
    expect(received.last.method, 'stopBroadcast');
  });
}
