import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

const stageModeratorPermissions =
    Permission.manageChannels | Permission.muteMembers | Permission.moveMembers;

bool _allowsAll(KaedeChannel channel, int bits) {
  final expected = BigInt.from(bits);
  return channel.permissions & BigInt.from(Permission.administrator) !=
          BigInt.zero ||
      channel.permissions & expected == expected;
}

bool _allowsAny(KaedeChannel channel, int bits) =>
    channel.permissions & BigInt.from(Permission.administrator) !=
        BigInt.zero ||
    channel.permissions & BigInt.from(bits) != BigInt.zero;

bool canManageStageChannel(KaedeChannel channel) =>
    channel.type == ChannelType.stage &&
    _allowsAll(channel, stageModeratorPermissions);

bool canCreateScheduledEventInChannel(KaedeChannel channel) {
  if (channel.type == ChannelType.stage) {
    return _allowsAll(
      channel,
      Permission.createEvents | stageModeratorPermissions,
    );
  }
  return channel.type == ChannelType.voice &&
      _allowsAll(
        channel,
        Permission.createEvents | Permission.viewChannel | Permission.connect,
      );
}

bool canManageScheduledEventInChannel(
  KaedeChannel channel, {
  required bool ownEvent,
}) {
  if (channel.type != ChannelType.voice && channel.type != ChannelType.stage) {
    return false;
  }
  final hasChannelAccess = channel.type == ChannelType.stage
      ? _allowsAll(channel, stageModeratorPermissions)
      : _allowsAll(channel, Permission.viewChannel | Permission.connect);
  if (!hasChannelAccess) return false;
  return ownEvent
      ? _allowsAny(channel, Permission.createEvents | Permission.manageEvents)
      : _allowsAll(channel, Permission.manageEvents);
}

bool canServerDeafenInChannel(KaedeChannel channel) =>
    channel.type != ChannelType.stage &&
    _allowsAll(channel, Permission.deafenMembers);
