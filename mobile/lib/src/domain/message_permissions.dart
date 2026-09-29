import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/thread_permissions.dart';
import 'package:kaede_mobile/src/domain/voice_messages.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

bool canDeleteMessage(
  KaedeChannel channel,
  KaedeMessage message,
  EntityRef? actor,
) =>
    actor != null &&
    message.channelRef == channel.ref &&
    message.deletedAt == null &&
    (channel.guildRef == null || channel.allows(Permission.viewChannel)) &&
    (message.authorRef == actor ||
        (channel.guildRef != null &&
            channel.allows(Permission.manageMessages))) &&
    (!channel.archived || !channel.locked || canManageThreads(channel));

bool canEditMessage(
  KaedeChannel channel,
  KaedeMessage message,
  EntityRef? actor,
) =>
    actor != null &&
    message.authorRef == actor &&
    message.channelRef == channel.ref &&
    message.deletedAt == null &&
    message.clientContentAvailable &&
    !const <int>{6, 12, 46}.contains(message.messageType) &&
    message.poll == null &&
    message.flags & messageFlagIsVoiceMessage == 0 &&
    !channel.recipients.any((user) => user.isSystem) &&
    (channel.guildRef == null || channel.allows(Permission.viewChannel)) &&
    !channel.archived &&
    (!channel.locked || canManageThreads(channel));

bool canReplyInChannel(KaedeChannel channel) =>
    !channel.recipients.any((user) => user.isSystem) &&
    (!channel.locked || canManageThreads(channel)) &&
    (channel.guildRef == null ||
        (channel.allows(Permission.viewChannel) &&
            (channel.isThread
                ? canSendInThread(channel)
                : channel.allows(Permission.sendMessages))));
