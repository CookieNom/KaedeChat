import { describe, expect, it } from 'vitest';

import {
  dmTitle,
  groupDmSubtitle,
  isGroupDm,
  ownsGroupDm,
  promoteDirectMessage
} from './direct-messages';
import type { Channel, UserSummary } from './types';

const self: UserSummary = {
  id: '1',
  origin_domain: 'alpha.example',
  username: 'cookie',
  display_name: 'Cookie',
  avatar_hash: null,
  handle: 'cookie@alpha.example',
  banner_hash: null,
  bio: null,
  custom_status: null,
  profile_resolved: true,
  bot: false
};

const turtle: UserSummary = {
  ...self,
  id: '2',
  origin_domain: 'beta.example',
  username: 'turtle',
  display_name: 'Turtle',
  handle: 'turtle@beta.example'
};

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: '10',
    origin_domain: 'alpha.example',
    guild_id: null,
    guild_domain: null,
    type: 1,
    name: null,
    topic: null,
    position: 0,
    parent_id: null,
    parent_domain: null,
    rate_limit_per_user: 0,
    permissions: '0',
    last_message_id: null,
    last_message_domain: null,
    recipients: [turtle],
    conversation_type: 'direct',
    ...overrides
  };
}

describe('direct-message presentation', () => {
  it('uses the other participant for a direct-message title', () => {
    expect(dmTitle(channel())).toBe('Turtle');
  });

  it('uses a custom group name and exposes owner state', () => {
    const group = channel({
      conversation_type: 'group',
      name: 'Weekend plans',
      owner_id: self.id,
      owner_domain: self.origin_domain,
      recipients: [turtle, { ...turtle, id: '3', username: 'frog', display_name: 'Frog' }]
    });
    expect(isGroupDm(group)).toBe(true);
    expect(dmTitle(group)).toBe('Weekend plans');
    expect(groupDmSubtitle(group)).toBe('3 members');
    expect(ownsGroupDm(group, self)).toBe(true);
  });

  it('falls back to member names without confusing equal snowflakes across domains', () => {
    const group = channel({
      conversation_type: 'group',
      recipients: [turtle, { ...turtle, id: '1', origin_domain: 'gamma.example' }],
      owner_id: '1',
      owner_domain: 'gamma.example'
    });
    expect(dmTitle(group)).toBe('Turtle, Turtle');
    expect(ownsGroupDm(group, self)).toBe(false);
  });

  it('promotes live activity without duplicating the conversation', () => {
    const older = channel({ id: '9', name: 'Older' });
    const active = channel({ id: '10', name: 'Updated' });

    expect(promoteDirectMessage([older, channel()], active)).toEqual([active, older]);
  });
});
