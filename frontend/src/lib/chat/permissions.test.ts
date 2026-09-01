import { describe, expect, it } from 'vitest';

import { Permission } from '$lib/generated/permissions';
import { canReadChannelHistory, hasAllPermissions, hasAnyPermission } from './permissions';

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
});
