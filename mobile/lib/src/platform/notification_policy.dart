enum LocalMessageNotificationDecision {
  none,
  directMessage,
  mention,
  guildMessage,
}

LocalMessageNotificationDecision decideLocalMessageNotification({
  required bool authoredByCurrentUser,
  required bool doNotDisturb,
  required bool conversationIsVisible,
  required bool isDirectMessage,
  required bool mentionsCurrentUser,
  required bool directMessagesEnabled,
  required bool mentionsEnabled,
  required String guildNotificationLevel,
}) {
  if (authoredByCurrentUser || doNotDisturb || conversationIsVisible) {
    return LocalMessageNotificationDecision.none;
  }
  if (isDirectMessage) {
    return directMessagesEnabled
        ? LocalMessageNotificationDecision.directMessage
        : LocalMessageNotificationDecision.none;
  }
  if (mentionsCurrentUser && mentionsEnabled) {
    return LocalMessageNotificationDecision.mention;
  }
  if (!isDirectMessage && guildNotificationLevel == 'all') {
    return LocalMessageNotificationDecision.guildMessage;
  }
  return LocalMessageNotificationDecision.none;
}
