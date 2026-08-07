import { describe, expect, it } from 'vitest';
import { filterDmFriends, friendsWithoutVisibleDm } from './dm-picker';
import type { Channel, Relationship, UserSummary } from './types';

function user(
  id: string,
  username: string,
  originDomain: string,
  displayName: string | null = null
): UserSummary {
  return {
    id,
    origin_domain: originDomain,
    username,
    display_name: displayName,
    avatar_hash: null,
    handle: `${username}@${originDomain}`
  };
}

function relationship(type: Relationship['type'], relatedUser: UserSummary): Relationship {
  return {
    type,
    user: relatedUser,
    created_at: '2026-08-07T00:00:00Z',
    updated_at: '2026-08-07T00:00:00Z'
  };
}

function directMessage(recipient: UserSummary): Channel {
  return {
    id: '100',
    origin_domain: 'chat.example',
    guild_id: null,
    guild_domain: null,
    type: 1,
    name: null,
    topic: null,
    position: 0,
    parent_id: null,
    parent_domain: null,
    rate_limit_per_user: 0,
    last_message_id: null,
    last_message_domain: null,
    recipients: [recipient]
  };
}

describe('friendsWithoutVisibleDm', () => {
  it('returns accepted friends without a visible conversation', () => {
    const alice = user('1', 'alice', 'chat.example');
    const bob = user('2', 'bob', 'remote.example');
    const pending = user('3', 'pending', 'chat.example');

    expect(
      friendsWithoutVisibleDm(
        [
          relationship('friend', bob),
          relationship('pending_in', pending),
          relationship('friend', alice)
        ],
        [directMessage(alice)]
      )
    ).toEqual([bob]);
  });

  it('compares the complete federated identity rather than only the snowflake', () => {
    const local = user('1', 'local', 'chat.example');
    const remote = user('1', 'remote', 'remote.example');

    expect(
      friendsWithoutVisibleDm(
        [relationship('friend', local), relationship('friend', remote)],
        [directMessage(local)]
      )
    ).toEqual([remote]);
  });
});

describe('filterDmFriends', () => {
  const friends = [
    user('1', 'alice', 'chat.example', 'Alice Example'),
    user('2', 'turtle', 'remote.example', 'River Turtle')
  ];

  it('matches display names, usernames, domains, and handles case-insensitively', () => {
    expect(filterDmFriends(friends, 'river')).toEqual([friends[1]]);
    expect(filterDmFriends(friends, '@TURTLE@REMOTE')).toEqual([friends[1]]);
    expect(filterDmFriends(friends, 'chat.example')).toEqual([friends[0]]);
  });

  it('returns every candidate for an empty query', () => {
    expect(filterDmFriends(friends, '  ')).toEqual(friends);
  });
});
