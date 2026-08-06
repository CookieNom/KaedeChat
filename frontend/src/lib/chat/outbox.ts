export interface PendingMessageSend {
  clientNonce: string;
  content: string | null;
  attachmentIds: string[];
  mentionUserIds: string[];
}

export function pendingMessageSend(
  content: string | null,
  attachmentIds: readonly string[],
  mentionUserIds: readonly string[],
  clientNonce: string = crypto.randomUUID()
): PendingMessageSend {
  return {
    clientNonce,
    content,
    attachmentIds: [...attachmentIds],
    mentionUserIds: [...mentionUserIds]
  };
}

export function discardAttachments(send: PendingMessageSend): PendingMessageSend {
  return { ...send, attachmentIds: [] };
}

export function withoutSubmittedUploads<T extends { attachmentId?: string }>(
  uploads: readonly T[],
  attachmentIds: readonly string[]
): T[] {
  if (!attachmentIds.length) return [...uploads];
  const submitted = new Set(attachmentIds);
  return uploads.filter((upload) => !upload.attachmentId || !submitted.has(upload.attachmentId));
}
