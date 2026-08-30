import { api } from '$lib/api/client';
import { entityKey } from './refs';
import type { Channel, Message } from './types';

const DIRECT_PIN_CHANNEL_TYPES = new Set([1, 3]);
const GUILD_PIN_CHANNEL_TYPES = new Set([0, 5, 10, 11, 12, 15]);

/** Mirrors the authority's channel-type gate for saved/pinned messages. */
export function channelSupportsMessagePins(channel: Pick<Channel, 'guild_id' | 'type'>): boolean {
  return (channel.guild_id === null ? DIRECT_PIN_CHANNEL_TYPES : GUILD_PIN_CHANNEL_TYPES).has(
    channel.type
  );
}

export interface MessagePin {
  pinned_at: string;
  message: Message;
}

export interface MessagePinPage {
  items: MessagePin[];
  has_more: boolean;
}

export interface ChannelPinsUpdate {
  channel_id: string;
  channel_domain: string;
  last_pin_timestamp?: string | null;
  message_id?: string;
  message_domain?: string;
  pinned?: boolean;
}

/** Reconcile Kaede's qualified event extension; standard-only events remain a no-op. */
export function reconcileChannelPinsUpdate(
  messages: Message[],
  update: ChannelPinsUpdate
): Message[] {
  if (
    typeof update.message_id !== 'string' ||
    typeof update.message_domain !== 'string' ||
    typeof update.pinned !== 'boolean'
  ) {
    return messages;
  }
  return messages.map((message) =>
    message.id === update.message_id &&
    message.origin_domain === update.message_domain &&
    message.channel_id === update.channel_id &&
    message.channel_domain === update.channel_domain
      ? { ...message, pinned: update.pinned }
      : message
  );
}

type PinRequest = <T>(path: string, init?: RequestInit) => Promise<T>;

function pinTimestamp(value: unknown): number {
  if (
    typeof value !== 'string' ||
    !/(?:Z|[+-]\d{2}:\d{2})$/u.test(value) ||
    !Number.isFinite(Date.parse(value))
  ) {
    throw new Error('Pinned message entry is invalid.');
  }
  return Date.parse(value);
}

function parsePinPage(value: unknown, before: string | null): MessagePinPage {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('Pinned messages response is invalid.');
  }
  const raw = value as Record<string, unknown>;
  if (!Array.isArray(raw.items) || typeof raw.has_more !== 'boolean' || raw.items.length > 50) {
    throw new Error('Pinned messages response is invalid.');
  }
  const cursorTimestamp = before ? pinTimestamp(before) : null;
  let previousTimestamp = Number.POSITIVE_INFINITY;
  const items = raw.items.map((item) => {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) {
      throw new Error('Pinned message entry is invalid.');
    }
    const pin = item as Record<string, unknown>;
    if (
      typeof pin.pinned_at !== 'string' ||
      typeof pin.message !== 'object' ||
      pin.message === null ||
      Array.isArray(pin.message)
    ) {
      throw new Error('Pinned message entry is invalid.');
    }
    const timestamp = pinTimestamp(pin.pinned_at);
    if (
      (cursorTimestamp !== null && timestamp >= cursorTimestamp) ||
      timestamp > previousTimestamp
    ) {
      throw new Error('Pinned messages response is not newest-first.');
    }
    previousTimestamp = timestamp;
    return { pinned_at: pin.pinned_at, message: pin.message as Message };
  });
  if (raw.has_more && items.length === 0) {
    throw new Error('Pinned messages cursor did not advance.');
  }
  return { items, has_more: raw.has_more };
}

/** Fetch every page (at most Discord's 250-pin channel cap) newest-first. */
export async function loadPinnedMessages(
  channelRef: string,
  request: PinRequest = api
): Promise<Message[]> {
  const messages: Message[] = [];
  const seen = new Set<string>();
  let before: string | null = null;
  for (let pageNumber = 0; pageNumber < 5; pageNumber += 1) {
    const query = new URLSearchParams({ limit: '50' });
    if (before) query.set('before', before);
    const page = parsePinPage(
      await request<unknown>(
        `/channels/${encodeURIComponent(channelRef)}/messages/pins?${query.toString()}`
      ),
      before
    );
    for (const pin of page.items) {
      const key = entityKey(pin.message);
      if (seen.has(key)) throw new Error('Pinned messages response contains a duplicate.');
      seen.add(key);
      messages.push({ ...pin.message, pinned: true, pinned_at: pin.pinned_at });
    }
    if (!page.has_more) return messages;
    const next = page.items.at(-1)?.pinned_at ?? null;
    if (!next) throw new Error('Pinned messages cursor did not advance.');
    before = next;
  }
  throw new Error('Pinned messages response exceeds the 250-pin channel limit.');
}

export function messagePinPath(channelRef: string, messageRef: string): string {
  return `/channels/${encodeURIComponent(channelRef)}/messages/pins/${encodeURIComponent(messageRef)}`;
}
