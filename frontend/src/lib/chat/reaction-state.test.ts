import { describe, expect, it } from 'vitest';
import {
  applyReactionClear,
  applyReactionDispatch,
  applyReactionUpdate,
  ownReactionPath,
  reactionClearEmoji,
  reactionClearPath,
  reactionUpdateFromDispatch
} from './reaction-state';
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

  it('reconciles Discord reaction events from their sparse payload and structured emoji', () => {
    const payload = {
      id: '10',
      origin_domain: 'remote.example',
      channel_id: '20',
      channel_domain: 'remote.example',
      reaction: '❤️',
      removed: true,
      user_id: '7',
      user_domain: 'local.example',
      emoji: { id: null, name: '❤', animated: false }
    };
    const added = applyReactionDispatch(
      { ...message(), reaction_counts: { '❤': 1 }, reacted_emoji: [] },
      'MESSAGE_REACTION_ADD',
      payload,
      currentUser
    );

    expect(added.reaction_counts).toEqual({ '❤': 2 });
    expect(added.reacted_emoji).toEqual(['❤']);
    expect(
      applyReactionDispatch(
        added,
        'MESSAGE_REACTION_REMOVE',
        { ...payload, removed: false },
        currentUser
      )
    ).toMatchObject({ reaction_counts: { '❤': 1 }, reacted_emoji: [] });
  });

  it('keeps accepting legacy MESSAGE_UPDATE reaction payloads during transition', () => {
    const update = reactionUpdateFromDispatch('MESSAGE_UPDATE', {
      id: '10',
      origin_domain: 'remote.example',
      reaction: '🔥',
      removed: true,
      user_id: '7',
      user_domain: 'local.example'
    });

    expect(update?.removed).toBe(true);
    expect(
      applyReactionDispatch(
        { ...message(), reacted_emoji: ['🔥'] },
        'MESSAGE_UPDATE',
        update,
        currentUser
      )
    ).toMatchObject({ reaction_counts: { '🔥': 1 }, reacted_emoji: [] });
  });

  it('clears one emoji group without changing other reaction state', () => {
    const original = {
      ...message(),
      reaction_counts: { '🔥': 2, 'party:7@remote.example': 3 },
      reacted_emoji: ['🔥', 'party:7@remote.example']
    };

    const updated = applyReactionClear(original, 'party:7@remote.example');

    expect(updated.reaction_counts).toEqual({ '🔥': 2 });
    expect(updated.reacted_emoji).toEqual(['🔥']);
    expect(original.reaction_counts).toEqual({ '🔥': 2, 'party:7@remote.example': 3 });
  });

  it('prefers the clear event reaction key over structured display metadata', () => {
    expect(
      reactionClearEmoji({
        message_id: '10',
        message_domain: 'remote.example',
        channel_id: '20',
        channel_domain: 'remote.example',
        reaction: '❤️',
        emoji: { id: null, name: '❤', animated: false }
      })
    ).toBe('❤');
    expect(
      reactionClearEmoji({
        message_id: '10',
        message_domain: 'remote.example',
        channel_id: '20',
        channel_domain: 'remote.example',
        emoji: '🔥'
      })
    ).toBe('🔥');
  });

  it('uses distinct Discord-compatible routes for own removal and group clearing', () => {
    const updated = applyReactionClear({ ...message(), reacted_emoji: ['🔥'] });

    expect(updated.reaction_counts).toEqual({});
    expect(updated.reacted_emoji).toEqual([]);
    expect(reactionClearPath('20@remote.example', '10@remote.example')).toBe(
      '/channels/20%40remote.example/messages/10%40remote.example/reactions'
    );
    expect(
      reactionClearPath('20@remote.example', '10@remote.example', 'party:7@remote.example')
    ).toBe(
      '/channels/20%40remote.example/messages/10%40remote.example/reactions/party%3A7%40remote.example'
    );
    expect(ownReactionPath('20@remote.example', '10@remote.example', '❤')).toBe(
      '/channels/20%40remote.example/messages/10%40remote.example/reactions/%E2%9D%A4/@me'
    );
  });
});
