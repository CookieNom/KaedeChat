import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
        ScreenShareQuality.dataSaver => ScreenShareQualityProfile(
            label: L10n.current.ui_data_saver_2e98ac08,
            description: L10n.current.ui_720p_15_fps_6a315c4c,
            width: 1280,
            height: 720,
            frameRate: 15,
            maxBitrate: 1200000,
          ),
        ScreenShareQuality.smooth => ScreenShareQualityProfile(
            label: L10n.current.ui_smooth_4a02d9cf,
            description: L10n.current.ui_720p_30_fps_37676aed,
            width: 1280,
            height: 720,
            frameRate: 30,
            maxBitrate: 2500000,
          ),
        ScreenShareQuality.sharp => ScreenShareQualityProfile(
            label: L10n.current.ui_sharp_64858ee7,
            description: L10n.current.ui_1080p_30_fps_b784c1f5,
            width: 1920,
            height: 1080,
            frameRate: 30,
            maxBitrate: 4500000,
          ),
        ScreenShareQuality.source => ScreenShareQualityProfile(
            label: L10n.current.ui_source_61e2a3f8,
            description: L10n.current.ui_up_to_2160p_30_fps_dc228b99,
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
}

@immutable
final class MobileMediaQuality {
  const MobileMediaQuality({
    this.screen = ScreenShareQuality.smooth,
    this.audio = VoiceAudioQuality.standard,
    this.dtx = true,
  });

  static const _screenKey = 'voice.screen_share_quality.v1';
  static const _audioKey = 'voice.audio_quality.v1';
  static const _dtxKey = 'voice.opus_dtx.v1';

  final ScreenShareQuality screen;
  final VoiceAudioQuality audio;
  final bool dtx;

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
      dtx: storage.getBool(_dtxKey) ?? true,
    );
  }

  Future<void> save() async {
    final storage = await SharedPreferences.getInstance();
    await Future.wait(<Future<bool>>[
      storage.setString(_screenKey, screen.name),
      storage.setString(_audioKey, audio.name),
      storage.setBool(_dtxKey, dtx),
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
        dtx: dtx,
        red: true,
      );

  VideoPublishOptions get videoPublishOptions =>
      videoPublishOptionsForCameraMode(1);

  VideoPublishOptions videoPublishOptionsForCameraMode(
    int videoQualityMode, {
    bool encrypted = false,
    Iterable<String> supportedCodecs = const [],
  }) {
    final codecs = supportedCodecs.map((codec) => codec.toLowerCase()).toSet();
    // RTP capabilities prove compatibility, not hardware acceleration at the
    // requested capture settings. Native SDKs currently expose no such query;
    // keep the common codec until that evidence and safe capture cloning exist.
    final fallback = codecs.isNotEmpty &&
            !codecs.contains('video/vp8') &&
            !codecs.contains('video/av1') &&
            codecs.contains('video/h264')
        ? 'h264'
        : 'vp8';
    return VideoPublishOptions(
      videoCodec: fallback,
      backupVideoCodec: const BackupVideoCodec(enabled: false),
      videoEncoding:
          cameraCaptureOptionsForMode(videoQualityMode).params.encoding,
      screenShareEncoding: screen.profile.parameters.encoding,
      simulcast: true,
      degradationPreference: screen == ScreenShareQuality.smooth
          ? DegradationPreference.maintainFramerate
          : DegradationPreference.maintainResolution,
    );
  }

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
