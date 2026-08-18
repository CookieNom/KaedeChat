import type { Message } from '$lib/chat/types';

export type EncryptedReportDisclosure =
  { available: false; content: null } | { available: true; content: string };

/**
 * Preserve the security-significant distinction between an MLS application
 * that decrypted to an empty string and a message that has not decrypted on
 * this device. Attachment-only encrypted messages legitimately use `""`.
 */
export function encryptedReportDisclosure(
  message: Pick<Message, 'decrypted_content'>
): EncryptedReportDisclosure {
  const content = message.decrypted_content;
  return typeof content === 'string'
    ? { available: true, content }
    : { available: false, content: null };
}
