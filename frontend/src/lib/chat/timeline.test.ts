import { describe, expect, it } from 'vitest';
import type { Message } from './types';
import { buildTimeline } from './timeline';

function message(id: string, author = '1', createdAt = '2026-07-20T10:00:00Z'): Message {
  return {
    id,
    origin_domain: 'chat.example',
    channel_id: '1',
    channel_domain: 'chat.example',
    author_id: author,
    author_domain: 'chat.example',
    author: null,
    content: 'hello',
    message_type: 0,
    flags: 0,
    client_nonce: null,
    referenced_message_id: null,
    referenced_message_domain: null,
    mention_user_refs: [],
    edited_at: null,
    deleted_at: null,
    created_at: createdAt
  };
}

describe('buildTimeline', () => {
  it('groups nearby messages while separating days and unread content', () => {
    const items = buildTimeline(
      [
        message('10'),
        message('11', '1', '2026-07-20T10:05:00Z'),
        message('12', '2', '2026-07-21T10:00:00Z')
      ],
      { id: '10', origin_domain: 'chat.example' }
    );
    expect(items.map((item) => item.kind)).toEqual([
      'day',
      'message',
      'new',
      'message',
      'day',
      'message'
    ]);
    expect(items.filter((item) => item.kind === 'message').map((item) => item.compact)).toEqual([
      false,
      false,
      false
    ]);
  });

  it('does not treat optimistic identifiers as an unread boundary', () => {
    const items = buildTimeline([message('pending-one')], {
      id: '10',
      origin_domain: 'chat.example'
    });
    expect(items.some((item) => item.kind === 'new')).toBe(false);
  });

  it('preserves domain-qualified mention references from API messages', () => {
    const mentioned = {
      ...message('10'),
      mention_user_refs: [{ id: '42', origin_domain: 'remote.example' }]
    };
    const item = buildTimeline([mentioned]).find((candidate) => candidate.kind === 'message');

    expect(item?.kind === 'message' ? item.message.mention_user_refs : []).toEqual([
      { id: '42', origin_domain: 'remote.example' }
    ]);
  });
});
