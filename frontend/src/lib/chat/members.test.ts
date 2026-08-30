import { describe, expect, it } from 'vitest';
import type { GuildMemberSummary, PresenceStatus, Role } from './types';
import {
  groupGuildMembers,
  guildMemberSearchPath,
  highestColoredRole,
  highestIconRole,
  memberRoleColor,
  mergeGuildMemberPage
} from './members';

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
    icon_hash: null,
    color: 0x22c55e,
    permissions: '0',
    position,
    hoist,
    mentionable: false
  };
}

describe('groupGuildMembers', () => {
  it('builds a bounded authority member search without preloading the full guild', () => {
    expect(guildMemberSearchPath('1@guild.example', '  @ari  ')).toBe(
      '/guilds/1%40guild.example/members?limit=26&query=%40ari'
    );
    expect(guildMemberSearchPath('1@guild.example', '', 500)).toBe(
      '/guilds/1%40guild.example/members?limit=100'
    );
    expect(guildMemberSearchPath('1@guild.example', 'ari', 26, '55@remote.example')).toBe(
      '/guilds/1%40guild.example/members?limit=26&after=55%40remote.example&query=ari'
    );
  });

  it('merges paged member matches without duplicates and advances by composite cursor', () => {
    const first = member('1', 'one', 'online');
    const replaced = member('2', 'old', 'offline');
    const replacement = member('2', 'new', 'online');
    const next = member('3', 'next', 'online');
    next.user.origin_domain = 'remote.example';
    const overflow = member('4', 'overflow', 'online');

    const page = mergeGuildMemberPage([first, replaced], [replacement, next, overflow], 2);

    expect(page.members.map((item) => item.user.username)).toEqual(['one', 'new', 'next']);
    expect(page.cursor).toBe('3@remote.example');
    expect(page.hasMore).toBe(true);
  });

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

describe('highestIconRole', () => {
  it('uses the highest assigned role with an icon independently of role color', () => {
    const lower = role('20', 'Lower', 2);
    lower.icon_hash = 'a'.repeat(64);
    const higherWithoutIcon = role('19', 'Higher', 5);
    const higherWithIcon = role('18', 'Icon', 4);
    higherWithIcon.icon_hash = 'b'.repeat(64);
    higherWithIcon.color = 0;
    const item = member('1', 'Ari', 'online');
    item.role_ids = [lower.id, higherWithoutIcon.id, higherWithIcon.id];

    expect(highestIconRole(item, [lower, higherWithoutIcon, higherWithIcon])).toBe(higherWithIcon);
  });
});

describe('memberRoleColor', () => {
  it('uses the highest assigned role that has a color', () => {
    const lower = role('20', 'Green', 2);
    lower.color = 0x22c55e;
    const colorless = role('19', 'Moderator', 5);
    colorless.color = 0;
    const higher = role('18', 'Purple', 4);
    higher.color = 0x8b5cf6;
    const item = member('1', 'Ari', 'online');
    item.role_ids = [lower.id, colorless.id, higher.id];

    expect(highestColoredRole(item, [lower, colorless, higher])).toBe(higher);
    expect(memberRoleColor(item, [lower, colorless, higher])).toBe('#8b5cf6');
  });

  it('uses the lower snowflake as the deterministic winner at the same position', () => {
    const older = role('20', 'Older', 3);
    const newer = role('21', 'Newer', 3);
    const item = member('1', 'Ari', 'online');
    item.role_ids = [newer.id, older.id];

    expect(highestColoredRole(item, [newer, older])).toBe(older);
  });
});
