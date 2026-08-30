import { describe, expect, it } from 'vitest';
import {
  beginMessageSearchOperator,
  MESSAGE_SEARCH_AUTHOR_TYPES,
  messageSearchUserCandidates,
  messageSearchOperator,
  moveSearchSuggestion,
  replaceMessageSearchOperator
} from './message-search';
import type { UserSummary } from './types';

describe('Discord-style message search composer', () => {
  it('offers distinct people, bot, and webhook author filters', () => {
    expect(MESSAGE_SEARCH_AUTHOR_TYPES).toEqual(['user', 'bot', 'webhook']);
  });

  it('recognizes contextual operators at the active end of a query', () => {
    expect(messageSearchOperator('release notes from:co')).toEqual({
      operator: 'from',
      needle: 'co',
      start: 13
    });
    expect(messageSearchOperator('has:IM')).toEqual({
      operator: 'has',
      needle: 'im',
      start: 0
    });
    expect(messageSearchOperator('from:cookie older words')).toBeNull();
  });

  it('adds and removes the active operator without disturbing search text', () => {
    expect(beginMessageSearchOperator('release notes', 'mentions')).toBe('release notes mentions:');
    expect(replaceMessageSearchOperator('release notes mentions:co')).toBe('release notes');
  });

  it('wraps keyboard selection in both directions', () => {
    expect(moveSearchSuggestion(3, 1, 4)).toBe(0);
    expect(moveSearchSuggestion(0, -1, 4)).toBe(3);
  });

  it('keeps remote message authors as exact federated autocomplete candidates', () => {
    const local = {
      id: '7',
      origin_domain: 'home.example',
      username: 'cookie'
    } as UserSummary;
    const remote = {
      id: '9',
      origin_domain: 'remote.example',
      username: 'turtle'
    } as UserSummary;
    const sameSnowflakeElsewhere = {
      id: '9',
      origin_domain: 'another.example',
      username: 'turtle'
    } as UserSummary;

    expect(
      messageSearchUserCandidates([local, remote, null, remote, sameSnowflakeElsewhere]).map(
        (user) => `${user.id}@${user.origin_domain}`
      )
    ).toEqual(['7@home.example', '9@remote.example', '9@another.example']);
  });
});
