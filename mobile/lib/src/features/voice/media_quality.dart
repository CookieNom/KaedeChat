import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:shared_preferences/shared_preferences.dart';

enum ScreenShareQuality { dataSaver, smooth, sharp, source }

enum VoiceAudioQuality { dataSaver, standard, high, studio }

@immutable
final class ScreenShareQualityProfile {
  const ScreenShareQualityProfile({
    required this.label,
    required this.description,
    required this.width,
    required this.height,
    required this.frameRate,
    required this.maxBitrate,
  });

  final String label;
  final String description;
  final int width;
  final int height;
  final int frameRate;
  final int maxBitrate;

  VideoParameters get parameters => VideoParameters(
        dimensions: VideoDimensions(width, height),
        encoding: VideoEncoding(
          maxBitrate: maxBitrate,
          maxFramerate: frameRate,
        ),
      );
}

extension ScreenShareQualityDetails on ScreenShareQuality {
  ScreenShareQualityProfile get profile => switch (this) {
        ScreenShareQuality.dataSaver => const ScreenShareQualityProfile(
            label: 'Data saver',
            description: '720p · 15 FPS',
            width: 1280,
            height: 720,
            frameRate: 15,
            maxBitrate: 1200000,
          ),
        ScreenShareQuality.smooth => const ScreenShareQualityProfile(
            label: 'Smooth',
            description: '720p · 30 FPS',
            width: 1280,
            height: 720,
            frameRate: 30,
            maxBitrate: 2500000,
          ),
        ScreenShareQuality.sharp => const ScreenShareQualityProfile(
            label: 'Sharp',
            description: '1080p · 30 FPS',
            width: 1920,
            height: 1080,
            frameRate: 30,
            maxBitrate: 4500000,
          ),
        ScreenShareQuality.source => const ScreenShareQualityProfile(
            label: 'Source',
            description: 'Up to 2160p · 30 FPS',
            width: 3840,
            height: 2160,
            frameRate: 30,
            maxBitrate: 8000000,
          ),
      };
}

extension VoiceAudioQualityDetails on VoiceAudioQuality {
  String get label => switch (this) {
        VoiceAudioQuality.dataSaver => 'Data saver',
        VoiceAudioQuality.standard => 'Standard',
        VoiceAudioQuality.high => 'High',
        VoiceAudioQuality.studio => 'Studio',
      };

  int get bitrate => switch (this) {
        VoiceAudioQuality.dataSaver => 24000,
        VoiceAudioQuality.standard => 48000,
        VoiceAudioQuality.high => 96000,
        VoiceAudioQuality.studio => 128000,
      };

  bool get continuousTransmission => this == VoiceAudioQuality.studio;
}

@immutable
final class MobileMediaQuality {
  const MobileMediaQuality({
    this.screen = ScreenShareQuality.smooth,
    this.audio = VoiceAudioQuality.standard,
  });

  static const _screenKey = 'voice.screen_share_quality.v1';
  static const _audioKey = 'voice.audio_quality.v1';

  final ScreenShareQuality screen;
  final VoiceAudioQuality audio;

  static const _iosBroadcastChannel =
      MethodChannel('chat.kaede.mobile/screen_share');

  static Future<MobileMediaQuality> load() async {
    final storage = await SharedPreferences.getInstance();
    return MobileMediaQuality(
      screen: _enumByName(
        ScreenShareQuality.values,
        storage.getString(_screenKey),
        ScreenShareQuality.smooth,
      ),
      audio: _enumByName(
        VoiceAudioQuality.values,
        storage.getString(_audioKey),
        VoiceAudioQuality.standard,
      ),
    );
  }

  Future<void> save() async {
    final storage = await SharedPreferences.getInstance();
    await Future.wait(<Future<bool>>[
      storage.setString(_screenKey, screen.name),
      storage.setString(_audioKey, audio.name),
    ]);
  }

  /// Persists capture bounds into the shared App Group before ReplayKit starts
  /// the upload extension. This keeps 4K devices from JPEG-encoding every
  /// source frame when the user selected a smaller preset.
  Future<void> prepareIosBroadcastExtension() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.iOS) return;
    await _iosBroadcastChannel.invokeMethod<void>(
      'setCaptureProfile',
      <String, Object?>{
        'width': screen.profile.width,
        'height': screen.profile.height,
        'frameRate': screen.profile.frameRate,
      },
    );
  }

  Future<void> stopIosBroadcastExtension() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.iOS) return;
    await _iosBroadcastChannel.invokeMethod<void>('stopBroadcast');
  }

  AudioPublishOptions get audioPublishOptions =>
      audioPublishOptionsForChannel(384000);

  AudioPublishOptions audioPublishOptionsForChannel(int channelBitrate) =>
      AudioPublishOptions(
        audioBitrate:
            audio.bitrate < channelBitrate ? audio.bitrate : channelBitrate,
        dtx: !audio.continuousTransmission,
        red: true,
      );

  VideoPublishOptions get videoPublishOptions =>
      videoPublishOptionsForCameraMode(1);

  VideoPublishOptions videoPublishOptionsForCameraMode(
    int videoQualityMode,
  ) =>
      VideoPublishOptions(
        videoEncoding:
            cameraCaptureOptionsForMode(videoQualityMode).params.encoding,
        screenShareEncoding: screen.profile.parameters.encoding,
        simulcast: true,
        degradationPreference: screen == ScreenShareQuality.smooth
            ? DegradationPreference.maintainFramerate
            : DegradationPreference.maintainResolution,
      );

  ScreenShareCaptureOptions screenCaptureOptions({
    required bool useIosBroadcastExtension,
  }) =>
      ScreenShareCaptureOptions(
        useiOSBroadcastExtension: useIosBroadcastExtension,
        params: screen.profile.parameters,
        maxFrameRate: screen.profile.frameRate.toDouble(),
      );
}

/// Automatic mode uses a smaller adaptive camera working set; full mode uses
/// Discord's explicit 720p camera target. Screen-share capture remains tied to
/// the independent [ScreenShareQuality] preference.
CameraCaptureOptions cameraCaptureOptionsForMode(int videoQualityMode) =>
    CameraCaptureOptions(
      params: videoQualityMode == 2
          ? VideoParametersPresets.h720_169
          : VideoParametersPresets.h360_169,
    );

T _enumByName<T extends Enum>(List<T> values, String? name, T fallback) {
  for (final value in values) {
    if (value.name == name) return value;
  }
  return fallback;
}
