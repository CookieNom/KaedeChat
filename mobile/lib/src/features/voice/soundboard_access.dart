import 'package:kaede_mobile/src/core/refs.dart';

bool soundboardChannelSupported({
  required int channelType,
  required bool directCall,
}) =>
    !directCall && channelType == 2;

String? soundboardPlaybackUnavailableReason({
  required bool connected,
  required bool canSpeak,
  required bool selfMuted,
  required bool selfDeafened,
  bool serverMuted = false,
  bool serverDeafened = false,
  bool suppressed = false,
}) {
  if (!connected) return 'Join this voice channel before using Soundboard.';
  if (serverDeafened) {
    return 'A moderator must undeafen you before you can use Soundboard.';
  }
  if (selfDeafened) return 'Undeafen before using Soundboard.';
  if (serverMuted) {
    return 'A moderator must unmute you before you can use Soundboard.';
  }
  if (suppressed) return 'Join the Stage speakers before using Soundboard.';
  if (!canSpeak) {
    return 'You need permission to speak before using Soundboard.';
  }
  if (selfMuted) return 'Unmute before using Soundboard.';
  return null;
}

bool soundboardSourceAllowed({
  required EntityRef? targetGuildRef,
  required EntityRef? sourceGuildRef,
  required bool canUseExternalSounds,
}) =>
    sourceGuildRef == null ||
    sourceGuildRef == targetGuildRef ||
    canUseExternalSounds;
