import 'package:kaede_mobile/src/domain/models.dart';

final class VoiceMediaPolicy {
  const VoiceMediaPolicy({
    required this.bitrate,
    required this.userLimit,
    required this.rtcRegion,
    required this.videoQualityMode,
  });

  static const defaults = VoiceMediaPolicy(
    bitrate: 64000,
    userLimit: 0,
    rtcRegion: null,
    videoQualityMode: 1,
  );

  final int bitrate;
  final int userLimit;
  final String? rtcRegion;
  final int videoQualityMode;

  bool matches(VoiceMediaPolicy other) =>
      bitrate == other.bitrate &&
      userLimit == other.userLimit &&
      rtcRegion == other.rtcRegion &&
      videoQualityMode == other.videoQualityMode;
}

VoiceMediaPolicy? voiceMediaPolicyFromGrant(Map<String, Object?> grant) {
  final bitrate = grant['bitrate'];
  final userLimit = grant['user_limit'];
  final rtcRegion = grant['rtc_region'];
  final videoQualityMode = grant['video_quality_mode'];
  if (!grant.containsKey('rtc_region')) return null;
  if (bitrate is! int || bitrate < 8000 || bitrate > 384000) return null;
  // The grant does not carry the channel type. Authority-side channel
  // validation caps ordinary voice at 99 and Stage at Discord's 10,000, so the
  // client must accept the larger signed Stage policy here.
  if (userLimit is! int || userLimit < 0 || userLimit > 10000) return null;
  if (rtcRegion != null &&
      (rtcRegion is! String ||
          rtcRegion.runes.isEmpty ||
          rtcRegion.runes.length > 64)) {
    return null;
  }
  if (videoQualityMode != 1 && videoQualityMode != 2) return null;
  return VoiceMediaPolicy(
    bitrate: bitrate,
    userLimit: userLimit,
    rtcRegion: rtcRegion as String?,
    videoQualityMode: videoQualityMode as int,
  );
}

VoiceMediaPolicy? _voiceMediaPolicyFromChannel(KaedeChannel channel) {
  final policy = <String, Object?>{
    'bitrate': channel.bitrate,
    'user_limit': channel.userLimit,
    'rtc_region': channel.rtcRegion,
    'video_quality_mode': channel.videoQualityMode,
  };
  final media = voiceMediaPolicyFromGrant(policy);
  if (media == null || !channel.type.isVoiceLike) return null;
  final maximumUserLimit = channel.type == ChannelType.stage ? 10000 : 99;
  return media.userLimit <= maximumUserLimit ? media : null;
}

bool voiceGrantMatchesChannelPolicy(
  Map<String, Object?> grant,
  KaedeChannel channel,
) {
  final grantMedia = voiceMediaPolicyFromGrant(grant);
  final channelMedia = _voiceMediaPolicyFromChannel(channel);
  if (grantMedia == null ||
      channelMedia == null ||
      !grantMedia.matches(channelMedia)) {
    return false;
  }
  final grantEncryptionMode = grant['e2ee'];
  final channelId = grant['channel_id'];
  final channelDomain = grant['channel_domain'];
  if (grantEncryptionMode is! bool ||
      (channel.encryptionMode != 'plaintext' &&
          channel.encryptionMode != 'e2ee') ||
      grantEncryptionMode != (channel.encryptionMode == 'e2ee') ||
      channelId is! String ||
      channelDomain is! String ||
      '$channelId@$channelDomain' != channel.ref.wire) {
    return false;
  }

  if (!grantEncryptionMode) {
    return grant['encryption_policy_generation'] == null &&
        grant['encryption_epoch'] == null &&
        grant['media_protocol'] == null &&
        grant['media_suite'] == null &&
        grant['media_session_id'] == null &&
        grant['media_epoch'] == null;
  }

  final policyGeneration = grant['encryption_policy_generation'];
  final encryptionEpoch = grant['encryption_epoch'];
  final mediaEpoch = grant['media_epoch'];
  final mediaSessionId = grant['media_session_id'];
  return channel.encryptionState == 'active' &&
      policyGeneration is String &&
      policyGeneration == '${channel.encryptionPolicyGeneration}' &&
      encryptionEpoch is String &&
      encryptionEpoch == '${channel.encryptionEpoch}' &&
      grant['media_protocol'] == 'livekit-e2ee-v1' &&
      grant['media_suite'] == 'AES-256-GCM' &&
      mediaSessionId is String &&
      RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(mediaSessionId) &&
      mediaEpoch is String &&
      mediaEpoch == encryptionEpoch;
}
