import { describe, expect, it } from 'vitest';

import type { Guild, ReadStateStatus } from '$lib/chat/types';
import {
  channelUnreadPresentation,
  compactBadgeCount,
  directMessageUnreadCount,
  guildMentionCount
} from './counts';

const guild = { id: '1', origin_domain: 'home.test' } as Guild;

function state(overrides: Partial<ReadStateStatus>): ReadStateStatus {
  return {
    channel_id: '10',
    channel_domain: 'home.test',
    guild_id: '1',
    guild_domain: 'home.test',
    last_message_id: '20',
    last_message_domain: 'home.test',
    read_message_id: null,
    read_message_domain: null,
    mention_count: 0,
    unread: true,
    ...overrides
  };
}

describe('rail notification counts', () => {
  it('uses a dot for ordinary unread activity and reserves numbers for mentions', () => {
    expect(channelUnreadPresentation(state({ mention_count: 0 }))).toEqual({
      unread: true,
      mentionCount: 0,
      showUnreadDot: true
    });
    expect(channelUnreadPresentation(state({ mention_count: 3 }))).toEqual({
      unread: true,
      mentionCount: 3,
      showUnreadDot: false
    });
    expect(channelUnreadPresentation(state({ unread: false, mention_count: 3 }))).toEqual({
      unread: false,
      mentionCount: 0,
      showUnreadDot: false
    });
  });

  it('counts only actual guild mentions', () => {
    expect(
      guildMentionCount(
        [
          state({ mention_count: 2 }),
          state({ channel_id: '11', mention_count: 0 }),
          state({ guild_id: '2', mention_count: 7 }),
          state({ guild_domain: 'remote.test', mention_count: 9 })
        ],
        guild
      )
    ).toBe(2);
  });

  it('counts unread DM conversations even without an explicit mention', () => {
    expect(
      directMessageUnreadCount([
        state({ guild_id: null, guild_domain: null }),
        state({ channel_id: '11', guild_id: null, guild_domain: null, mention_count: 3 }),
        state({
          channel_id: '12',
          guild_id: null,
          guild_domain: null,
          unread: false,
          mention_count: 8
        }),
        state({ mention_count: 6 })
      ])
    ).toBe(4);
  });

  it('caps crowded badges', () => {
    expect(compactBadgeCount(1)).toBe('1');
    expect(compactBadgeCount(98)).toBe('98');
    expect(compactBadgeCount(99)).toBe('99');
    expect(compactBadgeCount(100)).toBe('99+');
    expect(compactBadgeCount(125)).toBe('99+');
  });
});
