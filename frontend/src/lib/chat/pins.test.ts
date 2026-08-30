import { describe, expect, it, vi } from 'vitest';
import {
  channelSupportsMessagePins,
  loadPinnedMessages,
  messagePinPath,
  reconcileChannelPinsUpdate
} from './pins';

describe('message pin channel eligibility', () => {
  it('allows saved pins in direct and group conversations', () => {
    expect(channelSupportsMessagePins({ guild_id: null, type: 1 })).toBe(true);
    expect(channelSupportsMessagePins({ guild_id: null, type: 3 })).toBe(true);
    expect(channelSupportsMessagePins({ guild_id: null, type: 0 })).toBe(false);
  });

  it('matches the authority guild channel-type set', () => {
    for (const type of [0, 5, 10, 11, 12, 15]) {
      expect(channelSupportsMessagePins({ guild_id: '1', type })).toBe(true);
    }
    for (const type of [1, 2, 3, 4, 13, 17]) {
      expect(channelSupportsMessagePins({ guild_id: '1', type })).toBe(false);
    }
  });

  it('loads modern pin pages and keeps pinned-at order', async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce({
        items: [
          {
            pinned_at: '2026-08-28T12:00:00Z',
            message: { id: '9', origin_domain: 'chat.example' }
          }
        ],
        has_more: true
      })
      .mockResolvedValueOnce({
        items: [
          {
            pinned_at: '2026-08-27T12:00:00Z',
            message: { id: '8', origin_domain: 'chat.example' }
          }
        ],
        has_more: false
      });

    const messages = await loadPinnedMessages('5@chat.example', request);

    expect(messages.map((message) => [message.id, message.pinned, message.pinned_at])).toEqual([
      ['9', true, '2026-08-28T12:00:00Z'],
      ['8', true, '2026-08-27T12:00:00Z']
    ]);
    expect(request).toHaveBeenNthCalledWith(
      2,
      '/channels/5%40chat.example/messages/pins?limit=50&before=2026-08-28T12%3A00%3A00Z'
    );
  });

  it('fails closed on a non-advancing page', async () => {
    const request = async <T>(): Promise<T> => ({ items: [], has_more: true }) as T;
    await expect(loadPinnedMessages('5@chat.example', request)).rejects.toThrow(
      'cursor did not advance'
    );
  });

  it('rejects timezone-less or non-newest-first timestamps', async () => {
    const timezoneLess = async <T>(): Promise<T> =>
      ({
        items: [
          {
            pinned_at: '2026-08-28T12:00:00',
            message: { id: '9', origin_domain: 'chat.example' }
          }
        ],
        has_more: false
      }) as T;
    await expect(loadPinnedMessages('5@chat.example', timezoneLess)).rejects.toThrow(
      'entry is invalid'
    );

    const outOfOrder = async <T>(): Promise<T> =>
      ({
        items: [
          {
            pinned_at: '2026-08-27T12:00:00Z',
            message: { id: '9', origin_domain: 'chat.example' }
          },
          {
            pinned_at: '2026-08-28T12:00:00Z',
            message: { id: '8', origin_domain: 'chat.example' }
          }
        ],
        has_more: false
      }) as T;
    await expect(loadPinnedMessages('5@chat.example', outOfOrder)).rejects.toThrow('newest-first');
  });

  it('allows pins with the same authority timestamp within one page', async () => {
    const request = async <T>(): Promise<T> =>
      ({
        items: [
          {
            pinned_at: '2026-08-28T12:00:00Z',
            message: { id: '9', origin_domain: 'chat.example' }
          },
          {
            pinned_at: '2026-08-28T12:00:00Z',
            message: { id: '8', origin_domain: 'chat.example' }
          }
        ],
        has_more: false
      }) as T;

    await expect(loadPinnedMessages('5@chat.example', request)).resolves.toHaveLength(2);
  });

  it('uses the modern message-scoped mutation path', () => {
    expect(messagePinPath('5@chat.example', '9@chat.example')).toBe(
      '/channels/5%40chat.example/messages/pins/9%40chat.example'
    );
  });

  it('reconciles qualified pin events without guessing standard-only changes', () => {
    const messages = [
      {
        id: '7',
        origin_domain: 'remote.example',
        channel_id: '5',
        channel_domain: 'remote.example',
        author_id: '8',
        author_domain: 'remote.example',
        author: null,
        content: 'saved',
        message_type: 0,
        flags: 0,
        client_nonce: null,
        referenced_message_id: null,
        referenced_message_domain: null,
        mention_user_refs: [],
        edited_at: null,
        deleted_at: null,
        created_at: '2026-08-28T00:00:00Z'
      }
    ];
    expect(
      reconcileChannelPinsUpdate(messages, {
        channel_id: '5',
        channel_domain: 'remote.example',
        message_id: '7',
        message_domain: 'remote.example',
        pinned: true
      })[0]?.pinned
    ).toBe(true);
    expect(
      reconcileChannelPinsUpdate(messages, {
        channel_id: '5',
        channel_domain: 'remote.example',
        last_pin_timestamp: null
      })
    ).toBe(messages);
  });
});
