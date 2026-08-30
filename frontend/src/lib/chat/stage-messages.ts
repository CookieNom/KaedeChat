export function stageSystemMessageText(
  messageType: number,
  author: string,
  topic: string | null
): string | null {
  const resolvedTopic = topic?.trim() || 'Untitled Stage';
  switch (messageType) {
    case 27:
      return `${author} started a Stage: ${resolvedTopic}`;
    case 28:
      return `${author} ended the Stage: ${resolvedTopic}`;
    case 29:
      return `${author} became a speaker.`;
    case 31:
      return `${author} changed the Stage topic: ${resolvedTopic}`;
    default:
      return null;
  }
}
