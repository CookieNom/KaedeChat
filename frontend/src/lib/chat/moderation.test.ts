import { describe, expect, it } from 'vitest';
import { Permission } from '$lib/generated/permissions';
import { guildModerationActions, guildRoleOutranks } from './moderation';
import type { Guild, GuildMemberSummary, UserSummary } from './types';

function user(id: string, domain = 'chat.example'): UserSummary {
  return {
    id,
    origin_domain: domain,
    username: `user-${id}`,
    display_name: null,
    avatar_hash: null,
    handle: `user-${id}@${domain}`
  };
}

function guild(permissions: bigint): Guild {
  return {
    id: '10',
    origin_domain: 'chat.example',
    name: 'Test guild',
    description: null,
    icon_hash: null,
    owner_id: '1',
    owner_domain: 'chat.example',
    permissions: String(permissions),
    actor_highest_role_id: '20',
    permission_generation: '1',
    unavailable: false,
    roles: [
      {
        id: '10',
        origin_domain: 'chat.example',
        guild_id: '10',
        guild_domain: 'chat.example',
        name: '@everyone',
        color: 0,
        permissions: '0',
        position: 0,
        hoist: false,
        mentionable: false
      },
      {
        id: '20',
        origin_domain: 'chat.example',
        guild_id: '10',
        guild_domain: 'chat.example',
        name: 'Moderators',
        color: 0,
        permissions: '0',
        position: 2,
        hoist: false,
        mentionable: false
      },
      {
        id: '30',
        origin_domain: 'chat.example',
        guild_id: '10',
        guild_domain: 'chat.example',
        name: 'Members',
        color: 0,
        permissions: '0',
        position: 1,
        hoist: false,
        mentionable: false
      }
    ]
  };
}

function member(target: UserSummary, roleIds: string[] = ['30']): GuildMemberSummary {
  return {
    guild_id: '10',
    guild_domain: 'chat.example',
    user: target,
    nickname: null,
    role_ids: roleIds
  };
}

describe('guildModerationActions', () => {
  it('exposes only the actions granted by the effective guild permissions', () => {
    const actor = user('2');
    const target = user('3', 'remote.example');
    const actions = guildModerationActions(
      guild(Permission.KICK_MEMBERS | Permission.MODERATE_MEMBERS),
      actor,
      target,
      [member(actor, ['20']), member(target)]
    );

    expect(actions.map((action) => action.id)).toEqual(['timeout', 'kick']);
  });

  it('exposes every moderation action to administrators', () => {
    const actor = user('2');
    const target = user('3');

    expect(
      guildModerationActions(guild(Permission.ADMINISTRATOR), actor, target, [
        member(actor, ['20']),
        member(target)
      ]).map((action) => action.id)
    ).toEqual(['timeout', 'kick', 'ban']);
  });

  it('never offers moderation against oneself, the owner, or a non-member', () => {
    const actor = user('2');
    const owner = user('1');
    const outsider = user('4');
    const configured = guild(Permission.ADMINISTRATOR);

    expect(guildModerationActions(configured, actor, actor, [member(actor)])).toEqual([]);
    expect(guildModerationActions(configured, actor, owner, [member(owner)])).toEqual([]);
    expect(guildModerationActions(configured, actor, outsider, [member(actor)])).toEqual([]);
  });

  it('does not offer actions against an equal or higher-ranked member, even to admins', () => {
    const actor = user('2');
    const target = user('3');
    const configured = guild(Permission.ADMINISTRATOR);

    expect(
      guildModerationActions(configured, actor, target, [
        member(actor, ['20']),
        member(target, ['20'])
      ])
    ).toEqual([]);
    expect(
      guildModerationActions({ ...configured, actor_highest_role_id: '30' }, actor, target, [
        member(actor, ['30']),
        member(target, ['20'])
      ])
    ).toEqual([]);
  });

  it('fails closed when the target role projection is incomplete', () => {
    const actor = user('2');
    const target = user('3');
    const configured = guild(Permission.ADMINISTRATOR);

    expect(
      guildModerationActions(configured, actor, target, [
        member(actor, ['20']),
        member(target, ['999'])
      ])
    ).toEqual([]);
  });
});

describe('guildRoleOutranks', () => {
  it('requires a strictly higher actor role for channel overwrite targets', () => {
    const actor = user('2');
    const configured = guild(Permission.MANAGE_ROLES);
    const everyone = configured.roles?.find((role) => role.id === configured.id);
    const equal = configured.roles?.find((role) => role.id === '20');
    const lower = configured.roles?.find((role) => role.id === '30');
    const roster = [member(actor, ['20'])];

    expect(guildRoleOutranks(configured, actor, lower!, roster)).toBe(true);
    expect(guildRoleOutranks(configured, actor, equal!, roster)).toBe(false);
    expect(guildRoleOutranks(configured, actor, everyone!, roster)).toBe(true);
    expect(
      guildRoleOutranks({ ...configured, actor_highest_role_id: configured.id }, actor, everyone!, [
        member(actor, [])
      ])
    ).toBe(false);
  });

  it('allows the guild owner to target any role', () => {
    const owner = user('1');
    const configured = guild(Permission.MANAGE_ROLES);
    const highest = configured.roles?.find((role) => role.id === '20');

    expect(guildRoleOutranks(configured, owner, highest!, [member(owner, [])])).toBe(true);
  });
});
