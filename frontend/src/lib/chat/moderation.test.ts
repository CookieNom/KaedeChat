import { describe, expect, it } from 'vitest';
import { Permission } from '$lib/generated/permissions';
import { guildModerationActions } from './moderation';
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
    permission_generation: '1',
    unavailable: false
  };
}

function member(target: UserSummary): GuildMemberSummary {
  return {
    guild_id: '10',
    guild_domain: 'chat.example',
    user: target,
    nickname: null,
    role_ids: []
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
      [member(actor), member(target)]
    );

    expect(actions.map((action) => action.id)).toEqual(['timeout', 'kick']);
  });

  it('exposes every moderation action to administrators', () => {
    const actor = user('2');
    const target = user('3');

    expect(
      guildModerationActions(guild(Permission.ADMINISTRATOR), actor, target, [member(target)]).map(
        (action) => action.id
      )
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
});
