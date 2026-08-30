import { entityKey } from './refs';
import type { Message } from './types';

export interface MessageBulkDeleteUpdate {
  ids?: Array<{ id?: unknown; origin_domain?: unknown }>;
  channel_id?: unknown;
  channel_domain?: unknown;
}

/**
 * Remove every client-only copy of a deleted body while retaining the row as
 * a stable timeline tombstone. This is shared by single and aggregate
 * Gateway events so encrypted plaintext and rich-content caches cannot outlive
 * the authority's deletion.
 */
export function tombstoneMessage(message: Message, deletedAt = new Date().toISOString()): Message {
  return {
    ...message,
    content: null,
    sticker_items: [],
    embeds: [],
    components: [],
    forwarded_message: null,
    message_snapshots: [],
    poll: null,
    poll_result: null,
    e2ee: null,
    decrypted_content: null,
    e2ee_verified: false,
    decrypted_attachments: [],
    decrypted_allowed_mentions: undefined,
    decrypted_forward_snapshot: null,
    mention_user_refs: [],
    mention_role_refs: [],
    mention_everyone: false,
    attachments: [],
    reaction_counts: {},
    reacted_emoji: [],
    pinned: false,
    pinned_at: undefined,
    deleted_at: deletedAt
  };
}

/** Extract only complete federated identities from a bulk-delete dispatch. */
export function bulkDeletedMessageKeys(update: MessageBulkDeleteUpdate): ReadonlySet<string> {
  return new Set(
    (Array.isArray(update.ids) ? update.ids : []).flatMap((item) =>
      typeof item?.id === 'string' && typeof item.origin_domain === 'string'
        ? [entityKey({ id: item.id, origin_domain: item.origin_domain })]
        : []
    )
  );
}

export function applyBulkMessageDelete(
  messages: Message[],
  update: MessageBulkDeleteUpdate,
  deletedAt = new Date().toISOString()
): Message[] {
  const deleted = bulkDeletedMessageKeys(update);
  if (!deleted.size) return messages;
  return messages.map((message) =>
    deleted.has(entityKey(message)) ? tombstoneMessage(message, deletedAt) : message
  );
}
