import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

bool canCreateForumPost(KaedeChannel forum) =>
    forum.isForum &&
    forum.allows(Permission.viewChannel) &&
    forum.allows(Permission.sendMessages);

bool canCreatePublicThread(KaedeChannel parent) =>
    parent.allows(Permission.viewChannel) &&
    parent.allows(Permission.createPublicThreads);

bool canStartThreadFromMessage(KaedeChannel parent) =>
    parent.encryptionMode != 'e2ee' &&
    (parent.type == ChannelType.text ||
        parent.type == ChannelType.announcement) &&
    canCreatePublicThread(parent);

bool canCreatePrivateThread(KaedeChannel parent) =>
    parent.allows(Permission.viewChannel) &&
    parent.allows(Permission.createPrivateThreads);

bool hasSendMessagesInThreads(KaedeChannel channel) =>
    channel.allows(Permission.sendMessagesInThreads);

bool canSendInThread(KaedeChannel thread) =>
    thread.isThread && hasSendMessagesInThreads(thread);

bool canManageThreads(KaedeChannel channel) =>
    channel.allows(Permission.manageThreads);

bool canUseApplicationCommands(KaedeChannel channel) =>
    channel.guildRef == null ||
    channel.allows(Permission.useApplicationCommands);

bool canPinMessages(KaedeChannel channel) =>
    !channel.archived &&
    (channel.guildRef == null || channel.allows(Permission.pinMessages));

bool canBypassSlowmode(KaedeChannel channel) =>
    channel.allows(Permission.bypassSlowmode);

bool canAddMessageReaction(KaedeChannel channel, {required bool emojiExists}) =>
    !channel.archived &&
    (channel.guildRef == null ||
        emojiExists ||
        channel.allows(Permission.addReactions));

bool canRemoveThreadMember(KaedeChannel thread, EntityRef? actor) =>
    thread.isThread &&
    !thread.archived &&
    (canManageThreads(thread) ||
        (actor != null &&
            thread.type == ChannelType.privateThread &&
            thread.ownerRef == actor));

bool canAddThreadMember(KaedeChannel thread) {
  if (!thread.isThread || thread.archived) return false;
  if (thread.type == ChannelType.privateThread) {
    return hasSendMessagesInThreads(thread) &&
        (canManageThreads(thread) || (thread.followed && thread.invitable));
  }
  return (thread.type == ChannelType.publicThread ||
          thread.type == ChannelType.announcementThread) &&
      hasSendMessagesInThreads(thread);
}
