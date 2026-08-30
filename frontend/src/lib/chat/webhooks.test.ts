import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: apiMock }));

import { Permission } from '$lib/generated/permissions';
import type { Channel, Guild } from './types';
import {
  createGuildWebhook,
  deleteGuildWebhook,
  isChannelFollowerWebhook,
  listGuildWebhooks,
  manageableWebhookChannels,
  rotateGuildWebhook,
  updateGuildWebhook,
  type WebhookSummary
} from './webhooks';

function channel(
  id: string,
  type: number,
  permissions: bigint,
  encryptionMode: 'plaintext' | 'e2ee' = 'plaintext'
): Channel {
  return {
    id,
    origin_domain: 'remote.example',
    guild_id: '1',
    guild_domain: 'remote.example',
    type,
    name: `channel-${id}`,
    topic: null,
    position: Number(id),
    parent_id: null,
    parent_domain: null,
    permissions: permissions.toString(),
    encryption_mode: encryptionMode,
    rate_limit_per_user: 0,
    last_message_id: null,
    last_message_domain: null
  };
}

const webhook: WebhookSummary = {
  id: '80',
  ref: '80@remote.example',
  guild_id: '1',
  guild_domain: 'remote.example',
  channel_id: '2',
  channel_domain: 'remote.example',
  name: 'Builds',
  avatar_hash: null,
  revoked: false
};

describe('guild webhook management', () => {
  beforeEach(() => apiMock.mockReset().mockResolvedValue({}));

  it('uses qualified authority routes for list, create, edit, rotate, and delete', async () => {
    await listGuildWebhooks('1@remote.example');
    await createGuildWebhook('1@remote.example', '2@remote.example', 'Builds');
    await updateGuildWebhook('1@remote.example', webhook, {
      name: 'Deploys',
      channel_id: '3@remote.example'
    });
    await rotateGuildWebhook('1@remote.example', webhook);
    await deleteGuildWebhook('1@remote.example', webhook);

    expect(apiMock.mock.calls).toEqual([
      ['/guilds/1%40remote.example/webhooks', { signal: undefined }],
      [
        '/guilds/1%40remote.example/channels/2%40remote.example/webhooks',
        { method: 'POST', body: JSON.stringify({ name: 'Builds' }) }
      ],
      [
        '/webhooks/80%40remote.example?guild_ref=1%40remote.example',
        {
          method: 'PATCH',
          body: JSON.stringify({ name: 'Deploys', channel_id: '3@remote.example' })
        }
      ],
      ['/webhooks/80%40remote.example/rotate?guild_ref=1%40remote.example', { method: 'POST' }],
      ['/webhooks/80%40remote.example?guild_ref=1%40remote.example', { method: 'DELETE' }]
    ]);
  });

  it('offers only manageable plaintext message channels and recognizes channel follows', () => {
    const guild: Guild = {
      id: '1',
      origin_domain: 'remote.example',
      name: 'Remote',
      description: null,
      icon_hash: null,
      owner_id: '9',
      permission_generation: '1',
      unavailable: false,
      channels: [
        channel('2', 0, Permission.MANAGE_WEBHOOKS),
        channel('3', 5, Permission.ADMINISTRATOR),
        channel('4', 15, Permission.MANAGE_WEBHOOKS),
        channel('5', 2, Permission.MANAGE_WEBHOOKS),
        channel('6', 0, Permission.VIEW_CHANNEL),
        channel('7', 0, Permission.MANAGE_WEBHOOKS, 'e2ee')
      ]
    };

    expect(manageableWebhookChannels(guild).map((item) => item.id)).toEqual(['2', '3', '4']);
    expect(isChannelFollowerWebhook({ ...webhook, type: 2 })).toBe(true);
    expect(isChannelFollowerWebhook(webhook)).toBe(false);
  });
});
