import { describe, expect, it } from 'vitest';

import type { Message } from './types';
import { applyPollVoteDispatch } from './poll-state';

const currentUser = {
  id: '9',
  origin_domain: 'home.example',
  username: 'miko',
  display_name: null,
  avatar_hash: null,
  bot: false,
  handle: 'miko@home.example'
};

const message = {
  id: '1',
  origin_domain: 'guild.example',
  poll: {
    question: { text: 'Pick one', emoji: null },
    answers: [
      { answer_id: 1, poll_media: { text: 'A', emoji: null } },
      { answer_id: 2, poll_media: { text: 'B', emoji: null } }
    ],
    expiry: '2026-08-30T00:00:00Z',
    allow_multiselect: false,
    layout_type: 1,
    results: {
      is_finalized: false,
      answer_counts: [
        { id: 1, count: 2, me_voted: false },
        { id: 2, count: 1, me_voted: false }
      ]
    }
  }
} as Message;

describe('poll vote Gateway reconciliation', () => {
  it('updates totals and the current-user selection with federated identity', () => {
    const payload = {
      message_id: '1',
      message_domain: 'guild.example',
      channel_id: '7',
      channel_domain: 'guild.example',
      user_id: '9',
      user_domain: 'home.example',
      answer_id: 2
    };
    const added = applyPollVoteDispatch(message, 'MESSAGE_POLL_VOTE_ADD', payload, currentUser);
    expect(added.poll?.results.answer_counts[1]).toEqual({ id: 2, count: 2, me_voted: true });
    const removed = applyPollVoteDispatch(added, 'MESSAGE_POLL_VOTE_REMOVE', payload, currentUser);
    expect(removed.poll?.results.answer_counts[1]).toEqual({ id: 2, count: 1, me_voted: false });
  });

  it('changes another federated voter count without changing me_voted', () => {
    const applied = applyPollVoteDispatch(
      message,
      'MESSAGE_POLL_VOTE_ADD',
      {
        message_id: '1',
        message_domain: 'guild.example',
        channel_id: '7',
        channel_domain: 'guild.example',
        user_id: '9',
        user_domain: 'other.example',
        answer_id: 1
      },
      currentUser
    );
    expect(applied.poll?.results.answer_counts[0]).toEqual({ id: 1, count: 3, me_voted: false });
  });
});
