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

/// An inherited E2EE thread must be created without a starter, activated, and
/// only then receive its first encrypted message.
bool deferThreadStarterUntilE2eeActive(KaedeChannel parent) =>
    parent.encryptionMode == 'e2ee' || (parent.isForum && parent.e2eeRequired);

bool canStartThreadFromMessage(KaedeChannel parent) =>
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

bool canPinMessages(KaedeChannel channel) {
  if (channel.archived) return false;
  if (channel.guildRef == null) {
    return channel.type == ChannelType.dm ||
        channel.type == ChannelType.groupDm;
  }
  const guildPinTypes = <ChannelType>{
    ChannelType.text,
    ChannelType.announcement,
    ChannelType.announcementThread,
    ChannelType.publicThread,
    ChannelType.privateThread,
    ChannelType.forum,
    ChannelType.tracker,
  };
  return guildPinTypes.contains(channel.type) &&
      channel.allows(Permission.pinMessages);
}

bool canSendVoiceMessage(KaedeChannel channel) {
  if (channel.archived) return false;
  if (channel.guildRef == null) {
    return channel.type == ChannelType.dm ||
        channel.type == ChannelType.groupDm;
  }
  return channel.allows(Permission.attachFiles) &&
      channel.allows(Permission.sendVoiceMessages);
}

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
