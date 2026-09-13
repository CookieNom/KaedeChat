import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/voice/media_quality.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('unknown hardware keeps the same single codec in both call modes', () {
    const quality = MobileMediaQuality();
    for (final encrypted in [false, true]) {
      final options = quality.videoPublishOptionsForCameraMode(1,
          encrypted: encrypted,
          supportedCodecs: ['video/AV1', 'video/H264', 'video/VP8']);
      expect(options.videoCodec, 'vp8');
      expect(options.backupVideoCodec.enabled, isFalse);
      final limited = quality.videoPublishOptionsForCameraMode(1,
          encrypted: encrypted, supportedCodecs: ['video/H264']);
      expect(limited.videoCodec, 'h264');
      expect(limited.backupVideoCodec.enabled, isFalse);
    }
    expect(quality.videoPublishOptions.videoCodec, 'vp8');
  });

  test('screen profiles bind capture and publish limits', () {
    const quality = MobileMediaQuality(
      screen: ScreenShareQuality.sharp,
      audio: VoiceAudioQuality.high,
    );

    expect(quality.screen.profile.width, greaterThan(0));
    expect(quality.screen.profile.height, greaterThan(0));
    expect(quality.screen.profile.frameRate, greaterThan(0));
    expect(quality.videoPublishOptions.screenShareEncoding?.maxBitrate,
        quality.screen.profile.maxBitrate);
    expect(quality.videoPublishOptions.degradationPreference,
        DegradationPreference.maintainResolution);
  });

  test('audio presets use bounded Opus bitrates and enable DTX by default', () {
    const standard = MobileMediaQuality();
    const studio = MobileMediaQuality(audio: VoiceAudioQuality.studio);

    expect(standard.audioPublishOptions.audioBitrate, greaterThan(0));
    expect(standard.audioPublishOptions.dtx, isTrue);
    expect(studio.audioPublishOptions.audioBitrate,
        greaterThan(standard.audioPublishOptions.audioBitrate));
    expect(studio.audioPublishOptions.audioBitrate, lessThanOrEqualTo(510000));
    expect(studio.audioPublishOptions.dtx, isTrue);
    expect(
        const MobileMediaQuality(dtx: false).audioPublishOptions.dtx, isFalse);
  });

  test('DTX defaults on for legacy preferences and persists an override',
      () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      'voice.screen_share_quality.v1': 'sharp',
      'voice.audio_quality.v1': 'studio',
    });
    final legacy = await MobileMediaQuality.load();
    expect(legacy.screen, ScreenShareQuality.sharp);
    expect(legacy.audio, VoiceAudioQuality.studio);
    expect((await MobileMediaQuality.load()).dtx, isTrue);

    await const MobileMediaQuality(dtx: false).save();
    expect((await MobileMediaQuality.load()).dtx, isFalse);
  });

  test('caps microphone publish and republish settings to channel bitrate', () {
    const studio = MobileMediaQuality(audio: VoiceAudioQuality.studio);
    const dataSaver = MobileMediaQuality(audio: VoiceAudioQuality.dataSaver);

    expect(studio.audioPublishOptionsForChannel(32000).audioBitrate, 32000);
    expect(dataSaver.audioPublishOptionsForChannel(96000).audioBitrate, 24000);
    expect(studio.audioPublishOptionsForChannel(32000).dtx, isTrue);
  });

  test('camera channel modes do not change screen-share preferences', () {
    const quality = MobileMediaQuality(screen: ScreenShareQuality.sharp);
    final automatic = cameraCaptureOptionsForMode(1);
    final full = cameraCaptureOptionsForMode(2);

    expect(automatic.params.dimensions.width, 640);
    expect(automatic.params.dimensions.height, 360);
    expect(full.params.dimensions.width, 1280);
    expect(full.params.dimensions.height, 720);
    expect(
      quality.videoPublishOptionsForCameraMode(1).videoEncoding?.maxBitrate,
      lessThan(
        quality.videoPublishOptionsForCameraMode(2).videoEncoding?.maxBitrate ??
            0,
      ),
    );
    expect(
      quality
          .videoPublishOptionsForCameraMode(2)
          .screenShareEncoding
          ?.maxBitrate,
      4500000,
    );
  });

  test('iOS capture-bound payload forwarding', () async {
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
