import { describe, expect, it } from 'vitest';
import type { UserSummary } from './types';
import { mergeReactionUsers } from './reaction-viewer';

function user(id: string, origin_domain: string, username: string): UserSummary {
  return {
    id,
    origin_domain,
    username,
    display_name: null,
    avatar_hash: null,
    handle: `${username}@${origin_domain}`
  };
}

describe('reaction viewer pagination', () => {
  it('deduplicates repeated pages by composite identity', () => {
    const local = user('7', 'local.example', 'maple');
    const remote = user('7', 'remote.example', 'cedar');
    const updated = { ...local, display_name: 'Maple' };

    expect(mergeReactionUsers([local, remote], [updated])).toEqual([updated, remote]);
  });
});
