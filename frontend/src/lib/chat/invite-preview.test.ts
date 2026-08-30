import { describe, expect, it } from 'vitest';

import { hasJoinedGuild, invitedChannel, invitePreviewDetails } from './invite-preview';
import type { Channel, Guild } from './types';

function channel(id: string, position: number): Channel {
  return {
    id,
    origin_domain: 'home.test',
    guild_id: '1',
    guild_domain: 'home.test',
    type: 0,
    name: id,
    topic: null,
    position,
    parent_id: null,
    parent_domain: null,
    rate_limit_per_user: 0,
    last_message_id: null,
    last_message_domain: null
  };
}

function guild(channels: Channel[] = []): Guild {
  return {
    id: '1',
    origin_domain: 'home.test',
    name: 'Home',
    description: null,
    icon_hash: null,
    owner_id: '2',
    permission_generation: '1',
    unavailable: false,
    channels
  };
}

describe('invite destination', () => {
  it('opens the invited channel instead of the first channel', () => {
    const general = channel('10', 0);
    const invited = channel('11', 1);
    expect(invitedChannel(guild([general, invited]), '11')).toBe(invited);
  });

  it('falls back safely when the invited channel is no longer visible', () => {
    const general = channel('10', 0);
    expect(invitedChannel(guild([general]), '11')).toBe(general);
  });

  it('matches membership using the composite federated identity', () => {
    expect(hasJoinedGuild([guild()], guild())).toBe(true);
    expect(hasJoinedGuild([{ ...guild(), origin_domain: 'other.test' }], guild())).toBe(false);
  });

  it('summarizes role, allowlist, stream, and event context without leaking identities', () => {
    expect(
      invitePreviewDetails({
        code: 'Ab12Cd34',
        guild: guild(),
        channel_id: null,
        expires_at: null,
        uses: 2,
        max_uses: 10,
        role_ids: ['7@home.test'],
        target_user_count: 1,
        target_type: 'stream',
        guild_scheduled_event: { name: 'Town hall' }
      })
    ).toEqual([
      '8 uses remain',
      'Grants 1 role',
      'Limited invitation',
      'Opens a Go Live stream',
      'Event: Town hall'
    ]);
  });
});
