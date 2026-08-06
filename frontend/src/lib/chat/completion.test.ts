import { describe, expect, it } from 'vitest';
import { completionAt, memberCompletions, replaceCompletion } from './completion';

describe('composer completion', () => {
  it('finds the token touching the caret and replaces only that token', () => {
    const query = completionAt('hello @ali', 10);
    expect(query).toEqual({ marker: '@', query: 'ali', start: 6, end: 10 });
    expect(replaceCompletion('hello @ali', query!, '@alice@chat.example')).toBe(
      'hello @alice@chat.example '
    );
  });

  it('does not complete a marker embedded in a word', () => {
    expect(completionAt('email@example', 13)).toBeNull();
  });

  it('stores user completions as immutable federated mention tokens', () => {
    const [completion] = memberCompletions(
      [
        {
          guild_id: '1',
          guild_domain: 'chat.example',
          nickname: null,
          role_ids: [],
          user: {
            id: '75512661369970688',
            origin_domain: 'chat.example',
            username: 'alice',
            display_name: 'Alice',
            avatar_hash: null,
            handle: 'alice@chat.example'
          }
        }
      ],
      'ali'
    );
    expect(completion).toEqual({
      value: '<@75512661369970688@chat.example>',
      label: 'Alice',
      detail: '@alice@chat.example'
    });
  });
});
