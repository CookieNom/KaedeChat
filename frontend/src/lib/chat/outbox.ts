import type { EncryptedFileManifest } from '$lib/e2ee/media';

export interface PendingMessageSend {
  clientNonce: string;
  content: string | null;
  attachmentIds: string[];
  mentionUserIds: string[];
  referencedMessageId: string | null;
  encryptedAttachments: EncryptedFileManifest[];
}

export function pendingMessageSend(
  content: string | null,
  attachmentIds: readonly string[],
  mentionUserIds: readonly string[],
  clientNonce: string = crypto.randomUUID(),
  referencedMessageId: string | null = null,
  encryptedAttachments: readonly EncryptedFileManifest[] = []
): PendingMessageSend {
  return {
    clientNonce,
    content,
    attachmentIds: [...attachmentIds],
    mentionUserIds: [...mentionUserIds],
    referencedMessageId,
    encryptedAttachments: [...encryptedAttachments]
  };
}

export function discardAttachments(send: PendingMessageSend): PendingMessageSend {
  return { ...send, attachmentIds: [], encryptedAttachments: [] };
}

export function withoutSubmittedUploads<T extends { attachmentId?: string }>(
  uploads: readonly T[],
  attachmentIds: readonly string[]
): T[] {
  if (!attachmentIds.length) return [...uploads];
  const submitted = new Set(attachmentIds);
  return uploads.filter((upload) => !upload.attachmentId || !submitted.has(upload.attachmentId));
}
