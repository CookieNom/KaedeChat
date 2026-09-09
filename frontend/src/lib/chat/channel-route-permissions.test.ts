// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync, tick } from 'svelte';
import Route from '../../routes/(app)/g/[guildId]/[channelId]/+page.svelte';
import { Permission } from '$lib/generated/permissions';
import { chatEntities } from '$lib/stores/entities.svelte';
import type { Channel, Guild, Message } from './types';
import { interactionResponses } from './interaction-responses.svelte';
import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
const network = vi.hoisted(() => ({
  api: vi.fn(),
  page: {
    params: { guildId: '1@chat.example', channelId: '2@chat.example' },
    url: new URL('https://chat.example/g/1/2')
  }
}));
vi.mock('$app/state', () => ({ page: network.page }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
vi.mock('$lib/auth/config', () => ({
  loadAuthConfiguration: vi
    .fn()
    .mockResolvedValue({ e2ee_activation_enabled: false, gif_picker_enabled: false })
}));
vi.mock('$lib/e2ee/client', async (original) => ({
  ...(await original<typeof import('$lib/e2ee/client')>()),
  initializeE2EE: vi.fn().mockResolvedValue(null)
}));
vi.mock('$lib/gateway/runtime.svelte', () => ({
  authenticatedGateway: {
    client: Object.assign(new EventTarget(), {
      subscribeMembers: vi.fn(),
      releaseMembers: vi.fn(),
      requestChannelInfo: vi.fn(),
      setPresence: vi.fn(),
      typing: vi.fn()
    })
  }
}));
vi.mock('$lib/voice/session', async (original) => ({
  ...(await original<typeof import('$lib/voice/session')>()),
  VoiceSession: class extends EventTarget {
    connected = true;
    connecting = false;
    encrypted = false;
    microphone = false;
    deafened = false;
    camera = false;
    screen = false;
    canSpeak = true;
    canStream = true;
    pushToTalkRequired = false;
    participants = () => [];
    prioritySpeakers = () => new Set();
    tiles = () => [];
    attachAudio = () => () => undefined;
    reconcileBrowserPermissions = vi.fn().mockResolvedValue(undefined);
    reconcileParticipantPermissions = vi.fn().mockResolvedValue(undefined);
    disconnect = vi.fn().mockResolvedValue(undefined);
  }
}));
let guild: Guild;
let commands: unknown[];
let occupants: Record<string, unknown[]>;
let component: ReturnType<typeof mount> | undefined;
const root = '/guilds/1%40chat.example';
const room = '/channels/2%40chat.example';
const actor = {
  id: '7',
  origin_domain: 'chat.example',
  username: 'Actor',
  display_name: null,
  avatar_hash: null
};
const message = {
  id: '10',
  origin_domain: 'chat.example',
  channel_id: '2',
  channel_domain: 'chat.example',
  author: actor,
  author_id: '7',
  author_domain: 'chat.example',
  content: 'Private channel history',
  flags: 0,
  message_type: 0,
  created_at: '2026-01-01T00:00:00Z',
  edited_at: null,
  attachments: [],
  embeds: [],
  components: [],
  reactions: [],
  sticker_items: []
} as unknown as Message;
const dispatch = (t: string, d: unknown) =>
  authenticatedGateway.client.dispatchEvent(new CustomEvent('dispatch', { detail: { t, d } }));
async function render() {
  component = mount(Route, { target: document.body });
  flushSync();
  await vi.waitFor(() =>
    expect(
      document.querySelector(
        guild.channels![0].type === 2 ? '[aria-label="Open Apps"]' : 'textarea'
      )
    ).not.toBeNull()
  );
  await tick();
}
beforeEach(() => {
  commands = [];
  occupants = {};
  const permissions = (
    Permission.VIEW_CHANNEL |
    Permission.SEND_MESSAGES |
    Permission.READ_MESSAGE_HISTORY
  ).toString();
  guild = {
    id: '1',
    origin_domain: 'chat.example',
    name: 'Private Guild',
    description: null,
    icon_hash: null,
    permission_generation: '1',
    unavailable: false,
    owner_id: '99',
    owner_domain: 'chat.example',
    permissions,
    roles: [],
    channels: [
      {
        id: '2',
        origin_domain: 'chat.example',
        guild_id: '1',
        guild_domain: 'chat.example',
        type: 0,
        name: 'general',
        permissions,
        position: 0,
        encryption_mode: 'plaintext',
        rate_limit_per_user: 0
      } as Channel
    ]
  } as Guild;
  vi.spyOn(window.location, 'assign').mockImplementation(() => {});
  network.api.mockReset().mockImplementation(async (path: string) => {
    if (path.endsWith('/application-commands')) return commands;
    if (path.endsWith('/voice/occupancy')) return { participants: occupants[path] ?? [] };
    if (path.endsWith('/voice/move')) return {};
    if (path.endsWith('/interactions')) return { interaction_ref: '90@chat.example' };
    if (path.startsWith('/applications/directory')) return { items: [], total: 0, has_more: false };
    if (path.startsWith('/users/@me/applications')) return [];
    if (path === root) return structuredClone(guild);
    if (path === room) return structuredClone(guild.channels![0]);
    if (path === '/users/@me') return structuredClone(actor);
    if (path === '/users/@me/guilds') return [structuredClone(guild)];
    if (path === `${room}/messages`) return [structuredClone(message)];
    if (path === `${root}/threads/active`)
      return { threads: [], members: [], has_more: false, next_cursor: null };
    if (path.endsWith('/moderation-status')) return { timed_out: false, timeout_until: null };
    if (
      path === '/users/@me/read-states' ||
      path === '/users/@me/emojis' ||
      path === '/users/@me/stickers' ||
      path.includes('/pins') ||
      path.endsWith('/application-commands') ||
      path.endsWith('/scheduled-events')
    )
      return [];
    if (path.endsWith('/ack')) return {};
    throw new Error(`Unexpected route request: ${path}`);
  });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = undefined;
  chatEntities.clearSession();
  interactionResponses.reset();
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe('live channel route permissions', () => {
  it('keeps history-backed controls behind READ_MESSAGE_HISTORY', async () => {
    guild.channels![0].permissions = (
      Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES
    ).toString();
    await render();
    expect(document.body.textContent).toContain('Message history is unavailable');
    expect(document.querySelector('[aria-label="Show pinned messages"]')).toBeNull();
    expect(document.querySelector('[aria-label="Search messages"]')).toBeNull();
    expect(
      network.api.mock.calls.some(
        ([path]) =>
          path.includes('/messages') || path.includes('/pins') || path.includes('/threads')
      )
    ).toBe(false);
  });

  it('purges and leaves a channel when live access is revoked', async () => {
    await render();
    expect(chatEntities.messages.get('10@chat.example')?.content).toBe('Private channel history');
    dispatch('CHANNEL_ACCESS_REVOKED', {
      guild_id: '1',
      guild_domain: 'chat.example',
      channel_id: '2',
      channel_domain: 'chat.example'
    });
    await tick();
    expect(window.location.assign).toHaveBeenCalledWith('/home');
    expect(chatEntities.channels.get('2@chat.example')).toBeUndefined();
    expect(chatEntities.messages.get('10@chat.example')).toBeUndefined();
    expect(document.body.textContent).not.toContain('Private channel history');
    expect(document.querySelector('[aria-label="Show pinned messages"]')).toBeNull();
  });

  it('purges and leaves active guild routes after normalized access loss', async () => {
    await render();
    expect(chatEntities.messages.get('10@chat.example')?.content).toBe('Private channel history');
    chatEntities.removeGuild(guild);
    await tick();
    expect(window.location.assign).toHaveBeenCalledWith('/home');
    expect(chatEntities.messages.get('10@chat.example')).toBeUndefined();
    expect(document.body.textContent).not.toContain('Private channel history');
    expect(document.querySelector('textarea')).toBeNull();
  });
});

describe('channel menus and voice actions', () => {
  it('discovers invite and webhook-only channel settings with the correct initial panel', async () => {
    guild.channels![0].permissions = (
      Permission.VIEW_CHANNEL |
      Permission.SEND_MESSAGES |
      Permission.CREATE_INVITE
    ).toString();
    await render();
    const openMenu = async () => {
      document
        .querySelector<HTMLElement>('.channel-row a')!
        .dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
      await tick();
    };
    const manage = () =>
      Array.from(document.querySelectorAll<HTMLAnchorElement>('a[role="menuitem"]')).find(
        (item) => item.textContent?.replace(/\s+/g, ' ').trim() === 'Manage channel'
      )!;
    await openMenu();
    expect(new URL(manage().href).searchParams.get('panel')).toBe('invites');
    dispatch('CHANNEL_PERMISSION_UPDATE', {
      channel_id: '2',
      channel_domain: 'chat.example',
      permissions: (
        Permission.VIEW_CHANNEL |
        Permission.SEND_MESSAGES |
        Permission.MANAGE_WEBHOOKS
      ).toString()
    });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await tick();
    await openMenu();
    expect(new URL(manage().href).searchParams.get('panel')).toBe('integrations');
    expect(manage().pathname).toContain('/2%40chat.example/settings');
    dispatch('CHANNEL_PERMISSION_UPDATE', {
      channel_id: '2',
      channel_domain: 'chat.example',
      permissions: (Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES).toString()
    });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await tick();
    await openMenu();
    expect(manage()).toBeUndefined();
  });

  it('requires MOVE_MEMBERS in both voice move channels', async () => {
    guild.actor_highest_role_id = '6';
    guild.roles = [
      { id: '5', origin_domain: 'chat.example', name: 'Member', position: 1, permissions: '0' },
      { id: '6', origin_domain: 'chat.example', name: 'Moderator', position: 5, permissions: '0' }
    ] as Guild['roles'];
    const source = {
      ...guild.channels![0],
      id: '3',
      name: 'Source voice',
      type: 2,
      permissions: (Permission.VIEW_CHANNEL | Permission.MOVE_MEMBERS).toString()
    };
    const destination = {
      ...source,
      id: '4',
      name: 'Destination voice',
      permissions: Permission.VIEW_CHANNEL.toString()
    };
    guild.channels!.push(source, destination);
    occupants['/channels/3%40chat.example/voice/occupancy'] = [
      {
        identity: '8@chat.example',
        user_id: '8',
        user_domain: 'chat.example',
        room: 'g.1.3',
        channel_id: '3',
        guild_id: '1',
        joined_at: 1
      }
    ];
    await render();
    dispatch('GUILD_MEMBERS_CHUNK', {
      guild_id: '1',
      guild_domain: 'chat.example',
      members: [
        {
          guild_id: '1',
          guild_domain: 'chat.example',
          user: { ...actor, id: '8', username: 'Target' },
          role_ids: ['5']
        }
      ]
    });
    await tick();
    const target = () =>
      document.querySelector<HTMLButtonElement>('[aria-label="People in Source voice"] button')!;
    const drop = async () => {
      target().dispatchEvent(
        new DragEvent('dragstart', {
          bubbles: true,
          cancelable: true,
          dataTransfer: new DataTransfer()
        })
      );
      Array.from(document.querySelectorAll<HTMLElement>('.channel-row a'))
        .find((item) => item.textContent?.includes('Destination voice'))!
        .dispatchEvent(
          new DragEvent('drop', {
            bubbles: true,
            cancelable: true,
            dataTransfer: new DataTransfer()
          })
        );
      await tick();
    };
    const moves = () => network.api.mock.calls.filter(([path]) => path.endsWith('/voice/move'));
    await vi.waitFor(() => expect(target()).not.toBeNull());
    expect(target().getAttribute('draggable')).toBe('true');
    await drop();
    expect(moves()).toEqual([]);
    dispatch('CHANNEL_PERMISSION_UPDATE', {
      channel_id: '4',
      channel_domain: 'chat.example',
      permissions: source.permissions
    });
    dispatch('CHANNEL_PERMISSION_UPDATE', {
      channel_id: '3',
      channel_domain: 'chat.example',
      permissions: Permission.VIEW_CHANNEL.toString()
    });
    await tick();
    expect(target().getAttribute('draggable')).toBe('false');
    await drop();
    expect(moves()).toEqual([]);
    dispatch('CHANNEL_PERMISSION_UPDATE', {
      channel_id: '3',
      channel_domain: 'chat.example',
      permissions: source.permissions
    });
    await tick();
    await drop();
    expect(moves()).toHaveLength(1);
    expect(moves()[0][0]).toBe('/guilds/1%40chat.example/members/8%40chat.example/voice/move');
    expect(JSON.parse(moves()[0][1].body)).toEqual({ channel_id: '4@chat.example' });
  });

  it('opens the route-owned launcher and preserves command execution in voice', async () => {
    guild.channels![0].type = 2;
    guild.channels![0].permissions = (
      Permission.VIEW_CHANNEL | Permission.USE_APPLICATION_COMMANDS
    ).toString();
    commands = [
      {
        id: '5',
        application_ref: '6@apps.example',
        application_name: 'Helper',
        integration_type: 'guild_install',
        interaction_context: 'guild',
        name: 'echo',
        type: 'chat_input',
        description: 'Echo a word',
        options: [{ name: 'word', type: 'string', description: 'Word to echo', required: true }]
      }
    ];
    await render();
    document.querySelector<HTMLButtonElement>('[aria-label="Open Apps"]')!.click();
    await tick();
    const command = Array.from(document.querySelectorAll<HTMLButtonElement>('button')).find(
      (item) => item.textContent?.includes('/echo')
    )!;
    expect(command).toBeDefined();
    command.click();
    await tick();
    const dialog = document.querySelector('[aria-labelledby="voice-command-dialog-title"]')!;
    expect(dialog).not.toBeNull();
    const input = dialog.querySelector<HTMLInputElement>('input')!;
    input.value = 'hello';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    dialog
      .querySelector('form')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await vi.waitFor(() =>
      expect(
        network.api.mock.calls.filter(([path]) => path.endsWith('/interactions'))
      ).toHaveLength(1)
    );
    const [path, options] = network.api.mock.calls.find(([path]) =>
      path.endsWith('/interactions')
    )!;
    expect(path).toBe('/channels/2%40chat.example/interactions');
    expect(options.method).toBe('POST');
    expect(JSON.parse(options.body)).toMatchObject({
      application_ref: '6@apps.example',
      command_name: 'echo',
      options: { word: 'hello' }
    });
  });
});
