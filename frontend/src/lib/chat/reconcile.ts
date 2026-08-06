import { entityKey } from './refs';
import type { Message } from './types';

export interface MessageDeliveryUpdate {
  message_id: string;
  message_domain: string;
  channel_id: string;
  channel_domain: string;
  status: 'delivered' | 'failed';
  code?: string;
}

export function compareMessages(left: Message, right: Message): number {
  if (/^\d+$/.test(left.id) && /^\d+$/.test(right.id)) {
    if (left.id === right.id) return left.origin_domain.localeCompare(right.origin_domain);
    return BigInt(left.id) < BigInt(right.id) ? -1 : 1;
  }
  return left.created_at.localeCompare(right.created_at);
}

export function reconcileMessage(existing: Message[], incoming: Message, limit = 250): Message[] {
  const pending = existing.findIndex(
    (item) =>
      item.client_nonce &&
      item.client_nonce === incoming.client_nonce &&
      (item.pending || item.queued || item.failed)
  );
  const next = [...existing];
  if (pending >= 0) next[pending] = incoming;
  else if (!next.some((item) => entityKey(item) === entityKey(incoming))) next.push(incoming);
  return next.sort(compareMessages).slice(-limit);
}

export function failPendingMessage(existing: Message[], nonce: string): Message[] {
  return existing.map((item) =>
    item.client_nonce === nonce && item.pending ? { ...item, pending: false, failed: true } : item
  );
}

export function mergeMessageSnapshot(
  existing: Message[],
  snapshot: Message[],
  options: {
    authoritative?: boolean;
    complete?: boolean;
    limit?: number;
    preserveNonces?: ReadonlySet<string>;
  } = {}
): Message[] {
  const {
    authoritative = false,
    complete = false,
    limit = 250,
    preserveNonces = new Set<string>()
  } = options;
  const oldest = snapshot[0];
  let merged = existing.filter((message) => {
    const provisional =
      message.id.startsWith('pending-') || message.pending || message.queued || message.failed;
    if (provisional || (message.client_nonce && preserveNonces.has(message.client_nonce))) {
      return true;
    }
    if (!authoritative) return true;
    if (complete) return false;
    return !oldest || compareMessages(message, oldest) < 0;
  });
  for (const message of snapshot) {
    merged = reconcileMessage(merged, message, Number.MAX_SAFE_INTEGER);
  }
  return [...merged].sort(compareMessages).slice(-limit);
}

export function applyMessageDeliveryUpdate(
  existing: Message[],
  update: MessageDeliveryUpdate
): { messages: Message[]; matched: boolean } {
  let matched = false;
  const messages = existing.map((message) => {
    if (message.id !== update.message_id || message.origin_domain !== update.message_domain) {
      return message;
    }
    matched = true;
    return {
      ...message,
      delivery_status: update.status,
      failed: update.status === 'failed'
    };
  });
  return { messages, matched };
}

export class LoadFence {
  #generation = 0;

  begin(): number {
    this.#generation += 1;
    return this.#generation;
  }

  isCurrent(generation: number): boolean {
    return generation === this.#generation;
  }
}
