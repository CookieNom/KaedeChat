import { describe, expect, it } from 'vitest';
import { forwardingDestinations, forwardUnavailableReason } from './forwarding';
import type { Channel } from './types';
import { Permission } from '$lib/generated/permissions';

const channel = (overrides: Partial<Channel>): Channel => ({
  id: '1',
  origin_domain: 'one.example',
  guild_id: '10',
  guild_domain: 'one.example',
  type: 0,
  name: 'general',
  topic: null,
  position: 0,
  parent_id: null,
  parent_domain: null,
  permissions: Permission.SEND_MESSAGES.toString(),
  rate_limit_per_user: 0,
  last_message_id: null,
  last_message_domain: null,
  ...overrides
});

describe('forwarding destinations', () => {
  it('matches Discord forwardability for polls, calls, system notices, and app replies', () => {
    expect(forwardUnavailableReason({ message_type: 0, poll: null })).toBeNull();
    expect(forwardUnavailableReason({ message_type: 19, poll: null })).toBeNull();
    expect(forwardUnavailableReason({ message_type: 20, poll: null })).toBeNull();
    expect(forwardUnavailableReason({ message_type: 23, poll: null })).toBeNull();
    expect(forwardUnavailableReason({ message_type: 0, poll: {} as never })).toMatch(/Poll/u);
    expect(forwardUnavailableReason({ message_type: 3, poll: null })).toMatch(/Call/u);
    expect(forwardUnavailableReason({ message_type: 12, poll: null })).toMatch(/System/u);
    const envelope = {
      forward_projection_version: 2,
      forward_projection_digest: 'A'.repeat(43)
    } as NonNullable<import('./types').Message['e2ee']>;
    expect(forwardUnavailableReason({ message_type: 0, poll: null, e2ee: envelope })).toMatch(
      /not verified/u
    );
    expect(
      forwardUnavailableReason({
        message_type: 0,
        poll: null,
        e2ee: envelope,
        e2ee_verified: true
      })
    ).toBeNull();
  });
  it('keeps permitted DMs and channels across local and federated guilds', () => {
    const source = channel({});
    const remote = channel({
      id: '2',
      origin_domain: 'two.example',
      guild_id: '20',
      guild_domain: 'two.example'
    });
    const dm = channel({ id: '3', guild_id: null, guild_domain: null, type: 1 });
    const groupDm = channel({ id: '4', guild_id: null, guild_domain: null, type: 3 });
    const voiceText = channel({ id: '5', type: 2 });
    const stageText = channel({ id: '6', type: 13 });
    expect(
      forwardingDestinations(source, [remote, dm, groupDm, voiceText, stageText]).map(
        (item) => item.id
      )
    ).toEqual(['2', '3', '4', '5', '6']);
  });

  it('keeps mixed encrypted destinations and rejects non-sendable destinations', () => {
    const source = channel({});
    const encrypted = channel({ id: '3', encryption_mode: 'e2ee' });
    expect(
      forwardingDestinations(source, [channel({ id: '2', permissions: '0' }), encrypted])
    ).toEqual([encrypted]);
  });

  it('keeps age-restricted forwards inside age-restricted channels', () => {
    const source = channel({ nsfw: true });
    const adult = channel({ id: '2', nsfw: true });
    const unrestricted = channel({ id: '3', nsfw: false });
    const dm = channel({ id: '4', guild_id: null, guild_domain: null, type: 1 });

    expect(
      forwardingDestinations(source, [adult, unrestricted, dm]).map((item) => item.id)
    ).toEqual(['2']);
  });

  it('resolves thread age restrictions from the parent and fails closed without it', () => {
    const parent = channel({ id: '2', nsfw: true, permissions: '0' });
    const source = channel({
      id: '3',
      type: 11,
      parent_id: parent.id,
      parent_domain: parent.origin_domain,
      nsfw: false
    });
    const adult = channel({ id: '4', nsfw: true });
    const unrestricted = channel({ id: '5' });

    expect(forwardingDestinations(source, [parent, adult, unrestricted])).toEqual([adult]);
    expect(forwardingDestinations(source, [adult, unrestricted])).toEqual([]);
  });
});
