import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Channel, Guild, UserSummary } from '$lib/chat/types';
import { chatEntities } from '$lib/stores/entities.svelte';

const request = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', async (original) => ({ ...(await original()), api: request }));
vi.mock('$lib/notifications/browser.svelte', () => ({
  browserNotifications: { notifyMessage: vi.fn() }
}));
vi.mock('./client', () => ({
  GATEWAY_STATUS_EVENT: 'status',
  GatewayClient: class extends EventTarget {
    connect() {}
    close() {}
    rememberPresence() {}
  }
}));
import { authenticatedGateway } from './runtime.svelte';

const user = {
  id: '7',
  origin_domain: 'users.example',
  username: 'member',
  display_name: null,
  avatar_hash: null,
  handle: 'member@users.example'
} satisfies UserSummary;
const channel = {
  id: '2',
  origin_domain: 'guild.example',
  guild_id: '1',
  guild_domain: 'guild.example',
  type: 0,
  name: 'general',
  topic: null,
  position: 0,
  parent_id: null,
  parent_domain: null,
  rate_limit_per_user: 0,
  last_message_id: null,
  last_message_domain: null,
  permissions: '8'
} satisfies Channel;
const guild = {
  id: '1',
  origin_domain: 'guild.example',
  name: 'Guild',
  description: null,
  icon_hash: null,
  owner_id: '9',
  permission_generation: '1',
  permissions: '8',
  unavailable: false,
  channels: [channel],
  roles: []
} satisfies Guild;
function dispatch(t: string, d: unknown) {
  authenticatedGateway.client.dispatchEvent(
    new CustomEvent('dispatch', { detail: { t, d, s: 1 } })
  );
}
const role = {
  id: '3',
  origin_domain: 'guild.example',
  guild_id: '1',
  guild_domain: 'guild.example',
  name: 'member',
  color: 0,
  permissions: '0',
  position: 1,
  hoist: false,
  mentionable: false
};

beforeEach(() => {
  vi.stubGlobal('BroadcastChannel', undefined);
  request.mockReset();
  authenticatedGateway.start();
  chatEntities.ingestCurrentUser(user);
  chatEntities.ingestGuilds([guild]);
});
afterEach(() => {
  authenticatedGateway.stop();
  vi.unstubAllGlobals();
});

describe('gateway permission projection coherence', () => {
  it('updates both normalized and nested channel projections', () => {
    const other = {
      ...channel,
      origin_domain: 'other.example',
      guild_id: '4',
      guild_domain: 'other.example'
    };
    chatEntities.ingestGuilds([
      { ...guild, id: '4', origin_domain: 'other.example', channels: [other] }
    ]);
    dispatch('CHANNEL_PERMISSION_UPDATE', {
      channel_id: '2',
      channel_domain: 'guild.example',
      permissions: '1024'
    });
    expect(chatEntities.channels.get('2@guild.example')?.permissions).toBe('1024');
    expect(chatEntities.guilds.get('1@guild.example')?.channels?.[0].permissions).toBe('1024');
    expect(chatEntities.channels.get('2@other.example')?.permissions).toBe('8');
    expect(chatEntities.guilds.get('4@other.example')?.channels?.[0].permissions).toBe('8');
  });

  it('fails closed and refreshes effective guild permissions after role/member changes', async () => {
    for (const [event, payload] of [
      ['GUILD_ROLE_CREATE', role],
      ['GUILD_ROLE_UPDATE', role],
      ['GUILD_ROLE_DELETE', role],
      [
        'GUILD_MEMBER_UPDATE',
        { guild_id: '1', guild_domain: 'guild.example', user, nickname: null, role_ids: ['3'] }
      ]
    ] as const) {
      chatEntities.ingestGuilds([guild]);
      let complete!: (value: Guild) => void;
      request.mockReturnValueOnce(
        new Promise<Guild>((resolve) => {
          complete = resolve;
        })
      );
      dispatch(event, payload);
      expect(request).toHaveBeenLastCalledWith('/guilds/1%40guild.example');
      expect(chatEntities.guilds.get('1@guild.example')?.permissions, event).toBeUndefined();
      expect(chatEntities.channels.get('2@guild.example')?.permissions, event).toBeUndefined();
      complete({ ...guild, permissions: '1024', channels: [{ ...channel, permissions: '1024' }] });
      await Promise.resolve();
      expect(chatEntities.guilds.get('1@guild.example')?.permissions, event).toBe('1024');
      expect(chatEntities.channels.get('2@guild.example')?.permissions, event).toBe('1024');
    }
    expect(request).toHaveBeenCalledTimes(4);
  });

  it('cancels stale permission refreshes and purges the guild on access loss', async () => {
    let complete!: (value: Guild) => void;
    request.mockReturnValueOnce(
      new Promise<Guild>((resolve) => {
        complete = resolve;
      })
    );
    dispatch('GUILD_ROLE_UPDATE', role);
    dispatch('GUILD_DELETE', { id: '1', origin_domain: 'guild.example' });
    expect(chatEntities.guilds.get('1@guild.example')).toBeUndefined();
    expect(chatEntities.channels.get('2@guild.example')).toBeUndefined();
    complete(guild);
    await Promise.resolve();
    expect(chatEntities.guilds.get('1@guild.example')).toBeUndefined();
    expect(chatEntities.channels.get('2@guild.example')).toBeUndefined();
  });
});
