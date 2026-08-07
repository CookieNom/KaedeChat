import { entityKey } from './refs';
import type { Message } from './types';

export interface MessageDeliveryUpdate {
  message_id: string;
  message_domain: string;
  channel_id: string;
  channel_domain: string;
  status: 'delivered' | 'failed';
  code?: string;
  reason?: string | null;
  timeout_until?: string | null;
  timeout_indefinite?: boolean;
}

export function messageDeliveryFailure(
  update: Pick<MessageDeliveryUpdate, 'code' | 'reason' | 'timeout_until' | 'timeout_indefinite'>
): { reason?: string; retryable: boolean } {
  if (update.code === 'MEMBER_TIMED_OUT') {
    const until = update.timeout_until ? new Date(update.timeout_until) : null;
    const duration = update.timeout_indefinite
      ? 'indefinitely'
      : until && !Number.isNaN(until.valueOf())
        ? `until ${until.toLocaleString()}`
        : 'in this guild';
    return {
      reason: `You are timed out ${duration}.${update.reason ? ` Reason: ${update.reason}` : ''}`,
      retryable: false
    };
  }
  if (update.code === 'MISSING_PERMISSIONS') {
    return { reason: 'You no longer have permission to send messages here.', retryable: false };
  }
  return { retryable: true };
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

export function failPendingMessage(
  existing: Message[],
  nonce: string,
  failure: { reason?: string; retryable?: boolean } = {}
): Message[] {
  return existing.map((item) =>
    item.client_nonce === nonce && item.pending
      ? {
          ...item,
          pending: false,
          failed: true,
          failure_reason: failure.reason,
          retryable: failure.retryable ?? true
        }
      : item
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
    const failure = messageDeliveryFailure(update);
    return {
      ...message,
      delivery_status: update.status,
      failed: update.status === 'failed',
      failure_reason: update.status === 'failed' ? failure.reason : undefined,
      retryable: update.status === 'failed' ? failure.retryable : undefined
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
