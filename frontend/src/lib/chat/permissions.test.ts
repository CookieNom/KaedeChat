import { describe, expect, it } from 'vitest';

import { Permission } from '$lib/generated/permissions';
import { hasAllPermissions, hasAnyPermission } from './permissions';

describe('permission predicates', () => {
  it('distinguishes all-bit requirements from any-bit choices', () => {
    const effective = Permission.MANAGE_GUILD;
    const moderation = Permission.MANAGE_GUILD | Permission.KICK_MEMBERS;

    expect(hasAnyPermission(effective, moderation)).toBe(true);
    expect(hasAllPermissions(effective, moderation)).toBe(false);
    expect(hasAllPermissions(Permission.ADMINISTRATOR, moderation)).toBe(true);
  });
});
