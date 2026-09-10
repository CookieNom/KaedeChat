import { describe, expect, it } from 'vitest';
import type { Message } from './types';
import type { InteractionResponseEvent } from './rich-content';
import { buildTimeline, withInteractionResponses } from './timeline';

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
    const adjacent = buildTimeline([message('10'), message('11', '1', '2026-07-20T10:01:00Z')]);
    expect(adjacent.filter((item) => item.kind === 'message').map((item) => item.compact)).toEqual([
      false,
      true
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

it('inserts private replies inline and keeps edits in place without changing shared history', () => {
  const history = buildTimeline([message('10'), message('30')]);
  const response: InteractionResponseEvent = {
    interaction_id: '15',
    ephemeral: true,
    callback_type: 4,
    channel_ref: '1@chat.example',
    response_id: '20',
    response_ref: '20@chat.example',
    data: { content: 'Choose a channel' }
  };
  const merge = (events: InteractionResponseEvent[]) =>
    withInteractionResponses(history, events, '1@chat.example');
  expect(merge([response]).map((item) => item.key)).toEqual([
    'day:2026-07-20',
    'message:10@chat.example',
    'ephemeral:20@chat.example',
    'message:30@chat.example'
  ]);
  expect(merge([{ ...response, data: { content: 'Paired' } }])).toEqual(merge([response]));
  expect(
    merge([
      { ...response, deleted_at: '2026-07-20T10:01:00Z' },
      { ...response, ephemeral: false },
      { ...response, channel_ref: '2@chat.example' }
    ])
  ).toEqual(history);
  expect(history.some((item) => item.kind === 'ephemeral')).toBe(false);
  expect(withInteractionResponses([], [response], '1@chat.example')).toHaveLength(1);
});
