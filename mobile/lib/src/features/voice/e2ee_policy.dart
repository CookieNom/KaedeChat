import 'package:kaede_mobile/src/domain/models.dart';

bool voiceGrantMatchesChannelE2EEPolicy(
  Map<String, Object?> grant,
  KaedeChannel channel,
) {
  final mediaSessionId = '${grant['media_session_id']}';
  return grant['e2ee'] == true &&
      channel.encryptionMode == 'e2ee' &&
      channel.encryptionState == 'active' &&
      '${grant['channel_id']}@${grant['channel_domain']}' == channel.ref.wire &&
      '${grant['encryption_policy_generation']}' ==
          '${channel.encryptionPolicyGeneration}' &&
      '${grant['encryption_epoch']}' == '${channel.encryptionEpoch}' &&
      grant['media_protocol'] == 'livekit-e2ee-v1' &&
      grant['media_suite'] == 'AES-256-GCM' &&
      RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(mediaSessionId) &&
      '${grant['media_epoch']}' == '${grant['encryption_epoch']}';
}
