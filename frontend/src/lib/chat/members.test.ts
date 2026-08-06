import { describe, expect, it } from 'vitest';
import type { GuildMemberSummary, PresenceStatus } from './types';
import { groupGuildMembers } from './members';

function member(id: string, username: string, presence: PresenceStatus): GuildMemberSummary {
  return {
    guild_id: '1',
    guild_domain: 'chat.example',
    nickname: null,
    role_ids: [],
    presence,
    user: {
      id,
      origin_domain: 'chat.example',
      username,
      display_name: null,
      handle: `${username}@chat.example`,
      avatar_hash: null,
      banner_hash: null,
      bio: null,
      custom_status: null
    }
  };
}

describe('groupGuildMembers', () => {
  it('keeps active members above offline members and orders presence states consistently', () => {
    const members = [
      member('4', 'Zed', 'offline'),
      member('2', 'Bea', 'idle'),
      member('1', 'Ari', 'online'),
      member('3', 'Cal', 'dnd'),
      member('5', 'Ada', 'offline')
    ];

    const grouped = groupGuildMembers(members, (item) => item.presence ?? 'offline');

    expect(grouped.online.map((item) => item.user.username)).toEqual(['Ari', 'Bea', 'Cal']);
    expect(grouped.offline.map((item) => item.user.username)).toEqual(['Ada', 'Zed']);
  });

  it('uses nicknames when alphabetizing members with the same presence', () => {
    const zed = member('1', 'zed', 'online');
    zed.nickname = 'Alpha';
    const ari = member('2', 'ari', 'online');

    const grouped = groupGuildMembers([ari, zed], (item) => item.presence ?? 'offline');

    expect(grouped.online.map((item) => item.user.id)).toEqual(['1', '2']);
  });
});
