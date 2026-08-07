import { describe, expect, it } from 'vitest';

import type { Message, ReadStateStatus, UserSummary } from '$lib/chat/types';
import { applyIncomingMessage, applyReadStateDispatch } from './read-state';

const currentUser = {
  id: '1',
  origin_domain: 'home.test'
} as UserSummary;

const initial: ReadStateStatus = {
  channel_id: '10',
  channel_domain: 'home.test',
  guild_id: '5',
  guild_domain: 'home.test',
  last_message_id: '19',
  last_message_domain: 'home.test',
  read_message_id: '19',
  read_message_domain: 'home.test',
  mention_count: 0,
  unread: false
};

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: '20',
    origin_domain: 'home.test',
    channel_id: '10',
    channel_domain: 'home.test',
    author_id: '2',
    author_domain: 'remote.test',
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
    created_at: '2026-08-07T00:00:00Z',
    ...overrides
  };
}

describe('realtime read-state reduction', () => {
  it('marks messages from another user unread and increments direct mentions', () => {
    const [updated] = applyIncomingMessage(
      [initial],
      message({ mention_user_refs: [{ id: '1', origin_domain: 'home.test' }] }),
      currentUser
    );

    expect(updated).toMatchObject({
      last_message_id: '20',
      mention_count: 1,
      unread: true
    });
  });

  it('does not turn the sender’s own message into an unread notification', () => {
    const [unchanged] = applyIncomingMessage(
      [initial],
      message({ author_id: '1', author_domain: 'home.test' }),
      currentUser
    );

    expect(unchanged).toEqual(initial);
  });

  it('treats server mention totals as authoritative without replacing the latest message', () => {
    const unread = { ...initial, last_message_id: '20', mention_count: 1, unread: true };
    const [updated] = applyReadStateDispatch([unread], {
      channel_id: '10',
      channel_domain: 'home.test',
      last_message_id: '20',
      last_message_domain: 'home.test',
      mention_count: 0
    });

    expect(updated).toMatchObject({
      last_message_id: '20',
      read_message_id: '20',
      mention_count: 0,
      unread: false
    });
  });

  it('does not let a late acknowledgement erase a newer unread message', () => {
    const unread = { ...initial, last_message_id: '21', mention_count: 0, unread: true };
    const [updated] = applyReadStateDispatch([unread], {
      channel_id: '10',
      channel_domain: 'home.test',
      last_message_id: '20',
      last_message_domain: 'home.test',
      mention_count: 0
    });

    expect(updated).toMatchObject({
      last_message_id: '21',
      read_message_id: '20',
      unread: true
    });
  });

  it('does not double-count a replayed message dispatch', () => {
    const once = applyIncomingMessage(
      [initial],
      message({ mention_user_refs: [{ id: '1', origin_domain: 'home.test' }] }),
      currentUser
    );
    const twice = applyIncomingMessage(
      once,
      message({ mention_user_refs: [{ id: '1', origin_domain: 'home.test' }] }),
      currentUser
    );

    expect(twice[0].mention_count).toBe(1);
  });
});
