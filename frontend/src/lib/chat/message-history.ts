import { api } from '$lib/api/client';
import { entityRef } from './refs';
import type { Message } from './types';

export async function loadMessageHistory(
  channelRef: string,
  around: string | null,
  request: <T>(path: string) => Promise<T> = api
): Promise<{ messages: Message[]; hasLater: boolean }> {
  const path = `/channels/${encodeURIComponent(channelRef)}/messages`;
  const messages = await request<Message[]>(
    `${path}${around ? `?around=${encodeURIComponent(around)}` : ''}`
  );
  // Around pages can already contain the latest message, even when full.
  // The API returns newest first; check beyond that boundary before offering pagination.
  const newer =
    around && messages.length
      ? await request<Message[]>(
          `${path}?after=${encodeURIComponent(entityRef(messages[0]))}&limit=1`
        )
      : [];
  return { messages, hasLater: newer.length > 0 };
}
