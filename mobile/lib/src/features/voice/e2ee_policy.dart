import 'package:kaede_mobile/src/domain/models.dart';

bool voiceGrantMatchesChannelPolicy(
  Map<String, Object?> grant,
  KaedeChannel channel,
) {
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
