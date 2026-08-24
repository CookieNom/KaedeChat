import { entityKey } from './refs';
import type { Message } from './types';

export interface MessageReferenceTarget {
  id: string;
  origin_domain: string;
  channel_id: string | null;
  channel_domain: string | null;
}

export function messageReferenceTarget(message: Message): MessageReferenceTarget | null {
  const reference = message.message_reference;
  const resolved = message.referenced_message;
  const id = message.referenced_message_id ?? reference?.message_id ?? resolved?.id;
  const originDomain =
    message.referenced_message_domain ?? reference?.message_domain ?? resolved?.origin_domain;
  if (!id || !originDomain) return null;
  return {
    id,
    origin_domain: originDomain,
    channel_id: reference?.channel_id ?? resolved?.channel_id ?? null,
    channel_domain: reference?.channel_domain ?? resolved?.channel_domain ?? null
  };
}

export function resolvedReferencedMessage(
  message: Message,
  candidates: readonly Message[]
): Message | null {
  if (message.referenced_message) return message.referenced_message;
  const target = messageReferenceTarget(message);
  if (!target) return null;
  return (
    candidates.find(
      (candidate) => candidate.id === target.id && candidate.origin_domain === target.origin_domain
    ) ?? null
  );
}

export interface MessageDeliveryUpdate {
  message_id: string;
  message_domain: string;
  channel_id: string;
  channel_domain: string;
  status: 'delivered' | 'failed' | 'retrying';
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
  if (
    update.code === 'FEDERATED_DM_STORAGE_QUOTA_EXCEEDED' ||
    update.code === 'KAED_FED_DM_STORAGE_QUOTA_EXCEEDED'
  ) {
    return {
      reason:
        'The receiving instance is out of direct-message cache capacity. Your message was not delivered; try again later or contact that instance’s administrator.',
      retryable: true
    };
  }
  if (
    update.code === 'FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED' ||
    update.code === 'KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED'
  ) {
    return {
      reason:
        'The receiving instance cannot retain another remote account record, so this message was not delivered. Try again later or contact that instance’s administrator.',
      retryable: true
    };
  }
  if (
    update.code === 'FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED' ||
    update.code === 'KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED'
  ) {
    return {
      reason:
        'The receiving instance cannot retain another remote server record, so this message was not delivered. Try again later or contact that instance’s administrator.',
      retryable: true
    };
  }
  if (update.code === 'KAED_FED_INBOX_QUOTA_EXCEEDED') {
    return {
      reason:
        'The receiving instance is temporarily full of federation events, so this message was not delivered. Try again later.',
      retryable: true
    };
  }
  if (update.code === 'KAED_FED_OUTBOX_CAPACITY_EXCEEDED') {
    return {
      reason:
        'The receiving instance’s outbound federation queue is full. Kaede is retrying automatically.',
      retryable: true
    };
  }
  if (update.code === 'KAED_FED_REPLICA_QUOTA_EXCEEDED') {
    return {
      reason:
        'The receiving instance paused this remote conversation because its replica cache is full. Try again later or contact that instance’s administrator.',
      retryable: true
    };
  }
  if (update.code === 'KAED_FED_DELIVERY_EXPIRED') {
    return {
      reason:
        'The remote instance did not accept this message before the delivery window ended. Try again later.',
      retryable: true
    };
  }
  if (update.code === 'KAED_FED_EVENT_TOO_LARGE') {
    return {
      reason: 'This message is too large to send between instances. Reduce its size and try again.',
      retryable: false
    };
  }
  return { retryable: true };
}

function messageDeliveryRetryingReason(update: MessageDeliveryUpdate): string {
  if (
    update.code === 'FEDERATED_DM_STORAGE_QUOTA_EXCEEDED' ||
    update.code === 'KAED_FED_DM_STORAGE_QUOTA_EXCEEDED'
  ) {
    return 'The receiving instance is out of direct-message cache capacity. Kaede will keep retrying automatically.';
  }
  if (
    update.code === 'FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED' ||
    update.code === 'KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED'
  ) {
    return 'The receiving instance cannot retain another remote account record yet. Kaede will keep retrying automatically.';
  }
  if (
    update.code === 'FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED' ||
    update.code === 'KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED'
  ) {
    return 'The receiving instance cannot retain another remote server record yet. Kaede will keep retrying automatically.';
  }
  if (update.code === 'KAED_FED_INBOX_QUOTA_EXCEEDED') {
    return 'The receiving instance is temporarily full of federation events. Kaede will keep retrying automatically.';
  }
  if (update.code === 'KAED_FED_OUTBOX_CAPACITY_EXCEEDED') {
    return 'The receiving instance’s outbound federation queue is full. Kaede will keep retrying automatically.';
  }
  if (update.code === 'KAED_FED_REPLICA_QUOTA_EXCEEDED') {
    return 'The receiving instance paused this remote conversation because its replica cache is full. Kaede will keep retrying automatically.';
  }
  return 'The receiving instance is temporarily at capacity. Kaede will keep retrying automatically.';
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
      failure_reason:
        update.status === 'failed'
          ? failure.reason
          : update.status === 'retrying'
            ? messageDeliveryRetryingReason(update)
            : undefined,
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
