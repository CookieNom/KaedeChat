import { acceptAuthorizedMessage, protectMessage } from '$lib/chat/content-safety.svelte';
import type { Message } from '$lib/chat/types';

const CLIENT_ONLY_FIELDS = new Set([
  'safety_hidden',
  'safety_unavailable',
  'e2ee_verified',
  'decrypted_content',
  'decrypted_attachments',
  'decrypted_allowed_mentions',
  'decrypted_forward_snapshot',
  'encrypted_manifest'
]);

/**
 * Remove fields that can only be created after local authenticated decryption.
 * REST and Gateway peers are never allowed to assert this client state, even
 * when a compromised federated authority includes convincing-looking keys.
 */
export function stripNetworkClientState<T>(value: T, requestRevision?: number): T {
  if (Array.isArray(value)) {
    return value.map((item) => stripNetworkClientState(item, requestRevision)) as T;
  }
  if (!value || typeof value !== 'object') return value;
  const result = Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !CLIENT_ONLY_FIELDS.has(key))
      .map(([key, item]) => [key, stripNetworkClientState(item, requestRevision)])
  );
  if (result.message && typeof result.message === 'object' && 'snippet' in result) {
    const message = result.message as Message;
    if (message.safety_state || message.safety_hidden || message.safety_unavailable)
      result.snippet = '';
  }
  if (
    typeof result.id === 'string' &&
    typeof result.origin_domain === 'string' &&
    typeof result.channel_id === 'string' &&
    typeof result.author_id === 'string'
  ) {
    const message = result as unknown as Message;
    const restored =
      requestRevision !== undefined && acceptAuthorizedMessage(message, requestRevision);
    return protectMessage(restored ? { ...message, safety_unavailable: false } : message) as T;
  }
  return result as T;
}
