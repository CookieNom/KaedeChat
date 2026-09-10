import { preferredLocale } from '../ui/locale';
import { compareEntityRefs, entityKey } from './refs';
import type { InteractionResponseEvent } from './rich-content';
import type { Message } from './types';

export type TimelineItem =
  | { kind: 'day'; key: string; label: string }
  | { kind: 'new'; key: string; label: string }
  | { kind: 'ephemeral'; key: string; responseRef: string }
  | { kind: 'message'; key: string; message: Message; compact: boolean };

const GROUP_WINDOW_MS = 7 * 60 * 1000;

function dayKey(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value.slice(0, 10) : date.toISOString().slice(0, 10);
}

function isAfterRead(
  message: Message,
  readRef: { id: string; origin_domain: string } | null
): boolean {
  if (!readRef || message.id.startsWith('pending-')) return false;
  return compareEntityRefs(message, readRef) > 0;
}

export function buildTimeline(
  messages: Message[],
  readRef: { id: string; origin_domain: string } | null = null
): TimelineItem[] {
  const items: TimelineItem[] = [];
  let previous: Message | null = null;
  let previousDay = '';
  let addedNewDivider = false;

  for (const message of messages) {
    const currentDay = dayKey(message.created_at);
    if (currentDay !== previousDay) {
      items.push({
        kind: 'day',
        key: `day:${currentDay}`,
        label: new Intl.DateTimeFormat(preferredLocale(), { dateStyle: 'long' }).format(
          new Date(message.created_at)
        )
      });
      previous = null;
      previousDay = currentDay;
    }
    if (!addedNewDivider && isAfterRead(message, readRef)) {
      items.push({ kind: 'new', key: `new:${entityKey(message)}`, label: 'New messages' });
      addedNewDivider = true;
      previous = null;
    }
    const compact =
      previous !== null &&
      previous.author_id === message.author_id &&
      previous.author_domain === message.author_domain &&
      previous.webhook_id === message.webhook_id &&
      new Date(message.created_at).valueOf() - new Date(previous.created_at).valueOf() <=
        GROUP_WINDOW_MS;
    items.push({ kind: 'message', key: `message:${entityKey(message)}`, message, compact });
    previous = message;
  }
  return items;
}

/** Private responses belong only to the local timeline, never shared history. */
export function withInteractionResponses(
  timeline: TimelineItem[],
  responses: InteractionResponseEvent[],
  channelRef: string
): TimelineItem[] {
  const result = [...timeline];
  const visible = responses
    .filter(
      (event) =>
        event.ephemeral &&
        event.channel_ref === channelRef &&
        !event.deleted_at &&
        [4, 5].includes(event.callback_type ?? 0) &&
        event.response_ref &&
        /^[1-9][0-9]*$/.test(event.response_id ?? '')
    )
    .sort((a, b) => (BigInt(a.response_id!) < BigInt(b.response_id!) ? -1 : 1));
  for (const event of visible) {
    const index = result.findIndex(
      (item) =>
        item.kind === 'message' &&
        /^[1-9][0-9]*$/.test(item.message.id) &&
        BigInt(item.message.id) > BigInt(event.response_id!)
    );
    result.splice(index < 0 ? result.length : index, 0, {
      kind: 'ephemeral',
      key: `ephemeral:${event.response_ref}`,
      responseRef: event.response_ref!
    });
  }
  return result;
}
