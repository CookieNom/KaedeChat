import type { Message } from './types';
import { protectMessage } from './content-safety.svelte';

export function searchLoadedMessages(messages: Message[], needle: string) {
  if (!needle) return [];
  return messages.map(protectMessage).flatMap((message) => {
    if (
      message.deleted_at ||
      message.safety_hidden ||
      message.safety_locked ||
      message.safety_unavailable ||
      message.safety_state ||
      message.pending ||
      message.failed ||
      message.queued
    )
      return [];
    const body = message.e2ee
      ? message.e2ee_verified && message.decrypted_content
      : message.content;
    if (!body) return [];
    const snippet = body.replace(/\|\|[\s\S]*?(?:\|\||$)/g, '…');
    return snippet.toLocaleLowerCase().includes(needle) ? [{ message, snippet }] : [];
  });
}
