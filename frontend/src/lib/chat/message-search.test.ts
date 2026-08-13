import { describe, expect, it } from 'vitest';
import {
  beginMessageSearchOperator,
  messageSearchOperator,
  moveSearchSuggestion,
  replaceMessageSearchOperator
} from './message-search';

describe('Discord-style message search composer', () => {
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
});
