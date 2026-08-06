import { preferredLocale } from '../ui/locale';
import { compareEntityRefs, entityKey } from './refs';
import type { Message } from './types';

export type TimelineItem =
  | { kind: 'day'; key: string; label: string }
  | { kind: 'new'; key: string; label: string }
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
