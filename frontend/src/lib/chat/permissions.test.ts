import { describe, expect, it } from 'vitest';

import { Permission } from '$lib/generated/permissions';
import type { Channel } from './types';
import {
  canReadChannelHistory,
  hasAllPermissions,
  hasAnyPermission,
  reconcileChannelPermissionProjection
} from './permissions';

function channel(id: string, permissions: string, type = 0): Channel {
  return {
    id,
    origin_domain: 'chat.example',
    guild_id: '1',
    guild_domain: 'chat.example',
    type,
    name: id,
    topic: null,
    position: 0,
    parent_id: null,
    parent_domain: null,
    permissions,
    rate_limit_per_user: 0,
    last_message_id: null,
    last_message_domain: null
  };
}

describe('permission predicates', () => {
  it('distinguishes all-bit requirements from any-bit choices', () => {
    const effective = Permission.MANAGE_GUILD;
    const moderation = Permission.MANAGE_GUILD | Permission.KICK_MEMBERS;

    expect(hasAnyPermission(effective, moderation)).toBe(true);
    expect(hasAllPermissions(effective, moderation)).toBe(false);
    expect(hasAllPermissions(Permission.ADMINISTRATOR, moderation)).toBe(true);
  });

  it('requires both channel visibility and history access for REST-backed read surfaces', () => {
    expect(canReadChannelHistory(Permission.VIEW_CHANNEL)).toBe(false);
    expect(canReadChannelHistory(Permission.READ_MESSAGE_HISTORY)).toBe(false);
    expect(canReadChannelHistory(Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY)).toBe(
      true
    );
    expect(canReadChannelHistory(Permission.ADMINISTRATOR)).toBe(true);
  });

  it('keeps a stable channel projection by identity so reactive route changes can settle', () => {
    const current = [channel('10', '1'), channel('11', '2', 11)];
    const identical = [channel('10', '1')];

    expect(
      reconcileChannelPermissionProjection(current, identical, (item) => item.type === 11)
    ).toBe(current);

    const changed = reconcileChannelPermissionProjection(
      current,
      [channel('10', '4')],
      (item) => item.type === 11
    );
    expect(changed).not.toBe(current);
    expect(changed[0]).toEqual({ ...current[0], permissions: '4' });
    expect(changed[1]).toBe(current[1]);
  });
});
