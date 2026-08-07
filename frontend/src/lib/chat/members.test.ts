import { describe, expect, it } from 'vitest';
import type { GuildMemberSummary, PresenceStatus, Role } from './types';
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

function role(id: string, name: string, position: number, hoist = true): Role {
  return {
    id,
    origin_domain: 'chat.example',
    guild_id: '1',
    guild_domain: 'chat.example',
    name,
    color: 0x22c55e,
    permissions: '0',
    position,
    hoist,
    mentionable: false
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

  it('hoists active members under only their highest displayed role', () => {
    const admin = role('20', 'Admin', 3);
    const staff = role('21', 'Staff', 2);
    const plain = member('1', 'plain', 'online');
    const dualRole = member('2', 'dual', 'online');
    dualRole.role_ids = [staff.id, admin.id];
    const staffMember = member('3', 'staff', 'idle');
    staffMember.role_ids = [staff.id];
    const offlineAdmin = member('4', 'sleeping', 'offline');
    offlineAdmin.role_ids = [admin.id];

    const grouped = groupGuildMembers(
      [plain, dualRole, staffMember, offlineAdmin],
      (item) => item.presence ?? 'offline',
      [staff, admin]
    );

    expect(grouped.hoisted.map((group) => group.role.name)).toEqual(['Admin', 'Staff']);
    expect(grouped.hoisted[0].members.map((item) => item.user.username)).toEqual(['dual']);
    expect(grouped.hoisted[1].members.map((item) => item.user.username)).toEqual(['staff']);
    expect(grouped.online.map((item) => item.user.username)).toEqual(['plain']);
    expect(grouped.offline.map((item) => item.user.username)).toEqual(['sleeping']);
  });
});
