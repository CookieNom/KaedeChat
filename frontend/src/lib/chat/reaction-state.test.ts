import { describe, expect, it } from 'vitest';
import { applyReactionUpdate } from './reaction-state';
import type { Message, UserSummary } from './types';

const currentUser: UserSummary = {
  id: '7',
  origin_domain: 'local.example',
  username: 'maple',
  display_name: null,
  avatar_hash: null,
  handle: 'maple@local.example'
};

function message(): Message {
  return {
    id: '10',
    origin_domain: 'remote.example',
    channel_id: '20',
    channel_domain: 'remote.example',
    author_id: '8',
    author_domain: 'remote.example',
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
    created_at: '2026-08-13T00:00:00Z',
    reaction_counts: { '🔥': 2 },
    reacted_emoji: []
  };
}

describe('reaction gateway updates', () => {
  it('tracks counts and the current user reaction without replacing the message', () => {
    const updated = applyReactionUpdate(
      message(),
      {
        id: '10',
        origin_domain: 'remote.example',
        reaction: '🔥',
        user_id: '7',
        user_domain: 'local.example'
      },
      currentUser
    );

    expect(updated.reaction_counts).toEqual({ '🔥': 3 });
    expect(updated.reacted_emoji).toEqual(['🔥']);
    expect(updated.content).toBe('hello');
  });

  it('removes an empty reaction and does not mark another user as the viewer', () => {
    const updated = applyReactionUpdate(
      { ...message(), reaction_counts: { '🔥': 1 }, reacted_emoji: ['🔥'] },
      {
        id: '10',
        origin_domain: 'remote.example',
        reaction: '🔥',
        removed: true,
        user_id: '9',
        user_domain: 'remote.example'
      },
      currentUser
    );

    expect(updated.reaction_counts).toEqual({});
    expect(updated.reacted_emoji).toEqual(['🔥']);
  });
});
