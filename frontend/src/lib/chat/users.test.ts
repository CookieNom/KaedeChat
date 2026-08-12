import { describe, expect, it } from 'vitest';

import type { Channel, Relationship, UserSummary } from './types';
import { applyUserProfileToHomeProjections } from './users';

function user(overrides: Partial<UserSummary> = {}): UserSummary {
  return {
    id: '42',
    origin_domain: 'remote.example',
    username: 'history_deadbeef',
    display_name: null,
    avatar_hash: null,
    handle: 'history_deadbeef@remote.example',
    profile_resolved: false,
    ...overrides
  };
}

describe('home profile projections', () => {
  it('replaces unresolved DM, relationship, and open-profile copies immediately', () => {
    const placeholder = user();
    const resolved = user({
      username: 'maple',
      display_name: 'Maple',
      avatar_hash: 'avatar',
      handle: 'maple@remote.example',
      profile_version: '2',
      profile_resolved: true
    });
    const directMessage = {
      id: '7',
      origin_domain: 'local.example',
      recipients: [placeholder]
    } as Channel;
    const relationship = {
      type: 'friend',
      user: placeholder,
      created_at: '2026-08-12T00:00:00Z',
      updated_at: '2026-08-12T00:00:00Z'
    } as Relationship;

    const projected = applyUserProfileToHomeProjections(
      [directMessage],
      [relationship],
      placeholder,
      resolved
    );

    expect(projected.directMessages[0].recipients?.[0]).toMatchObject({
      username: 'maple',
      profile_resolved: true
    });
    expect(projected.relationships[0].user).toMatchObject({
      display_name: 'Maple',
      avatar_hash: 'avatar',
      profile_resolved: true
    });
    expect(projected.selectedUser).toMatchObject({
      handle: 'maple@remote.example',
      profile_resolved: true
    });
  });

  it('uses the full composite reference and leaves colliding numeric IDs alone', () => {
    const otherHome = user({ origin_domain: 'other.example' });
    const resolved = user({ username: 'maple', profile_resolved: true });
    const relationship = {
      type: 'friend',
      user: otherHome,
      created_at: '2026-08-12T00:00:00Z',
      updated_at: '2026-08-12T00:00:00Z'
    } as Relationship;

    const projected = applyUserProfileToHomeProjections([], [relationship], otherHome, resolved);

    expect(projected.relationships[0].user).toBe(otherHome);
    expect(projected.selectedUser).toBe(otherHome);
  });
});
