// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync, tick } from 'svelte';
import Settings from '../../routes/(app)/g/[guildId]/settings/+page.svelte';
import { Permission } from '$lib/generated/permissions';
import { chatEntities } from '$lib/stores/entities.svelte';
import type { Channel, Guild, GuildMemberSummary, Role, UserSummary } from './types';
const network = vi.hoisted(() => ({
  api: vi.fn(),
  page: {
    params: { guildId: '1@chat.example', channelId: '' },
    url: new URL('https://chat.example/settings')
  }
}));
vi.mock('$app/state', () => ({ page: network.page }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
vi.mock('$lib/auth/config', () => ({
  loadAuthConfiguration: vi.fn().mockResolvedValue({ e2ee_activation_enabled: false })
}));
vi.mock('$lib/e2ee/client', () => ({ initializeE2EE: vi.fn().mockResolvedValue(null) }));
const root = '/guilds/1%40chat.example';
const user = (id: string, username: string) =>
  ({
    id,
    origin_domain: 'chat.example',
    username,
    display_name: null,
    avatar_hash: null
  }) as UserSummary;
const role = (id: string, name: string, position: number) =>
  ({
    id,
    origin_domain: 'chat.example',
    guild_id: '1',
    guild_domain: 'chat.example',
    name,
    position,
    permissions: '0',
    color: 0,
    mentionable: false,
    hoist: false
  }) as Role;
const channel = (id: string, name: string, permissions: bigint, type = 0) =>
  ({
    id,
    origin_domain: 'chat.example',
    guild_id: '1',
    guild_domain: 'chat.example',
    name,
    type,
    permissions: permissions.toString(),
    position: Number(id),
    encryption_mode: 'plaintext'
  }) as Channel;
let guild: Guild;
let members: GuildMemberSummary[];
let component: ReturnType<typeof mount> | undefined;
const button = (label: string, within: ParentNode = document) =>
  Array.from(within.querySelectorAll<HTMLButtonElement>('button')).find(
    (item) => item.textContent?.trim() === label
  )!;
const select = (label: string, within: ParentNode = document) =>
  Array.from(within.querySelectorAll('label'))
    .find((item) => item.querySelector('span')?.textContent?.trim() === label)!
    .querySelector('select')!;
const values = (element: HTMLSelectElement) =>
  Array.from(element.options).map((option) => option.value);
async function render(channelId = '', panel = '') {
  network.page.params.channelId = channelId;
  network.page.url = new URL(`https://chat.example/settings?panel=${panel}`);
  component = mount(Settings, { target: document.body });
  flushSync();
  await vi.waitFor(() =>
    expect(document.querySelector('[aria-label="Loading guild settings"]')).toBeNull()
  );
  await tick();
}
beforeEach(() => {
  guild = {
    id: '1',
    origin_domain: 'chat.example',
    name: 'Private Guild',
    description: 'Private description',
    icon_hash: null,
    permission_generation: '1',
    unavailable: false,
    owner_id: '99',
    owner_domain: 'chat.example',
    actor_highest_role_id: '6',
    permissions: Permission.VIEW_CHANNEL.toString(),
    roles: [role('1', '@everyone', 0), role('5', 'Helper', 1), role('6', 'Moderator', 5)],
    channels: [channel('2', 'general', Permission.VIEW_CHANNEL)]
  } as Guild;
  members = [
    { guild_id: '1', guild_domain: 'chat.example', user: user('7', 'Actor'), role_ids: ['6'] },
    { guild_id: '1', guild_domain: 'chat.example', user: user('8', 'Target'), role_ids: ['5'] }
  ] as GuildMemberSummary[];
  network.api.mockReset().mockImplementation(async (path: string) => {
    if (path === root) return structuredClone(guild);
    if (path === '/users/@me') return user('7', 'Actor');
    if (path === `${root}/notification-settings`) return { level: 'all' };
    if (path.startsWith(`${root}/members?`)) return structuredClone(members);
    if (
      path.endsWith('/scheduled-events') ||
      path.endsWith('/invites') ||
      path.endsWith('/webhooks') ||
      path.endsWith('/overwrites') ||
      path.endsWith('/followers')
    )
      return [];
    throw new Error(`Unexpected settings request: ${path}`);
  });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = undefined;
  chatEntities.clearSession();
  document.body.replaceChildren();
});

describe('live settings permission surfaces', () => {
  it('clears guild settings data after a live guild projection is revoked', async () => {
    guild.permissions = (Permission.VIEW_CHANNEL | Permission.MANAGE_GUILD).toString();
    await render();
    expect(document.body.textContent).toContain('Private Guild');
    expect(
      document.querySelector<HTMLInputElement>('input[value="Private Guild"]')?.value ??
        Array.from(document.querySelectorAll('input')).find(
          (item) => item.value === 'Private Guild'
        )?.value
    ).toBe('Private Guild');
    const signal = network.api.mock.calls.find(([path]) => path === root)![1].signal;
    chatEntities.removeGuild(guild);
    await tick();
    expect(signal.aborted).toBe(true);
    expect(document.body.textContent).toContain(
      'This guild is unavailable or you no longer have access.'
    );
    expect(document.body.textContent).not.toContain('Private Guild');
    expect(Array.from(document.querySelectorAll('input')).map((item) => item.value)).not.toContain(
      'Private Guild'
    );
    expect(document.querySelector('#members')).toBeNull();
  });

  it('does not show guild-scoped channel creation for a channel-only grant', async () => {
    guild.channels![0].permissions = (
      Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS
    ).toString();
    await render('2@chat.example');
    expect(document.querySelector('#channels')).not.toBeNull();
    expect(document.querySelector('#channels .quick-create')).toBeNull();
    chatEntities.guilds.update('1@chat.example', (current) => ({
      ...current,
      permissions: (Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS).toString()
    }));
    await tick();
    expect(document.querySelector('#channels .quick-create')).not.toBeNull();
  });

  it('filters full-guild invite destinations by channel-effective permission', async () => {
    guild.permissions = (Permission.VIEW_CHANNEL | Permission.CREATE_INVITE).toString();
    guild.channels = [
      channel('2', 'allowed', Permission.VIEW_CHANNEL | Permission.CREATE_INVITE),
      channel('3', 'denied', Permission.VIEW_CHANNEL),
      channel('4', 'category', Permission.CREATE_INVITE, 4)
    ];
    await render();
    expect(values(select('Destination', document.querySelector('#invites')!))).toEqual([
      '',
      '2@chat.example'
    ]);
    chatEntities.guilds.update('1@chat.example', (current) => ({
      ...current,
      channels: current.channels!.map((item) => ({
        ...item,
        permissions: Permission.VIEW_CHANNEL.toString()
      }))
    }));
    await tick();
    expect(values(select('Destination', document.querySelector('#invites')!))).toEqual(['']);
  });

  it('uses channel-effective permissions for category moves without gating creation parents', async () => {
    guild.permissions = (Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS).toString();
    const manage = Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS | Permission.MANAGE_ROLES;
    guild.channels = [
      channel('2', 'general', manage),
      channel('3', 'allowed category', manage, 4),
      channel('4', 'denied category', Permission.VIEW_CHANNEL, 4)
    ];
    await render('2@chat.example');
    const creation = document.querySelector('#channels .quick-create')!;
    expect(values(select('Category', creation))).toEqual(['', '3@chat.example', '4@chat.example']);
    const categories = Array.from(document.querySelectorAll('label'))
      .filter((item) => item.querySelector('span')?.textContent?.trim() === 'Category')
      .map((item) => item.querySelector('select')!)
      .filter(Boolean);
    expect(categories.map(values)).toContainEqual(['', '3@chat.example']);
  });
});

describe('settings hierarchy and announcement access', () => {
  it('uses selected-channel permissions for overwrite changes and target management', async () => {
    guild.permissions = (Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES).toString();
    guild.channels![0].permissions = (Permission.VIEW_CHANNEL | Permission.MANAGE_ROLES).toString();
    await render('2@chat.example', 'permissions');
    const targets = document.querySelector('[aria-label="Permission targets"]')!;
    expect(button('Moderator', targets).disabled).toBe(true);
    expect(button('Helper', targets).disabled).toBe(false);
    button('Helper', targets).click();
    await tick();
    const row = Array.from(document.querySelectorAll('.overwrite-permission-row')).find(
      (item) => item.querySelector('strong')?.textContent === 'Send messages'
    )!;
    expect(row).toBeDefined();
    expect(Array.from(row.querySelectorAll('button')).map((item) => item.disabled)).toEqual([
      true,
      true,
      true
    ]);
    chatEntities.guilds.update('1@chat.example', (current) => ({
      ...current,
      channels: current.channels!.map((item) => ({
        ...item,
        permissions: (
          Permission.VIEW_CHANNEL |
          Permission.MANAGE_ROLES |
          Permission.SEND_MESSAGES
        ).toString()
      }))
    }));
    await tick();
    expect(Array.from(row.querySelectorAll('button')).map((item) => item.disabled)).toEqual([
      false,
      false,
      false
    ]);
    chatEntities.guilds.update('1@chat.example', (current) => ({
      ...current,
      channels: current.channels!.map((item) => ({
        ...item,
        permissions: Permission.VIEW_CHANNEL.toString()
      }))
    }));
    await tick();
    expect(document.querySelector('[aria-label="Permission targets"]')).toBeNull();
    expect(
      network.api.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')
    ).toBe(true);
  });

  it('fails role assignment controls closed on incomplete target hierarchy', async () => {
    guild.permissions = (Permission.VIEW_CHANNEL | Permission.MANAGE_ROLES).toString();
    members[1].role_ids = ['missing'];
    await render();
    button('Manage members', document.querySelector('#roles')!).click();
    await tick();
    const target = () =>
      Array.from(document.querySelectorAll('.role-member-row'))
        .find((item) => item.querySelector('strong')?.textContent === 'Target')!
        .querySelector('input')!;
    expect(target().disabled).toBe(true);
    chatEntities.members.upsertMany([{ ...members[1], role_ids: ['5'] }]);
    await tick();
    expect(target().disabled).toBe(false);
    chatEntities.members.remove('1@chat.example:8@chat.example');
    await tick();
    expect(
      Array.from(document.querySelectorAll('.role-member-row')).some((item) =>
        item.textContent?.includes('Target')
      )
    ).toBe(false);
  });

  it('keeps settings hierarchy controls on live role and member projections', async () => {
    guild.permissions = (Permission.VIEW_CHANNEL | Permission.MANAGE_ROLES).toString();
    await render();
    button('Manage members', document.querySelector('#roles')!).click();
    await tick();
    const target = () =>
      Array.from(document.querySelectorAll('.role-member-row'))
        .find((item) => item.querySelector('strong')?.textContent === 'Target')!
        .querySelector('input')!;
    expect(target().disabled).toBe(false);
    chatEntities.guilds.update('1@chat.example', (current) => ({
      ...current,
      roles: current.roles!.map((item) => (item.id === '6' ? { ...item, position: 0 } : item))
    }));
    await tick();
    expect(target().disabled).toBe(true);
    chatEntities.guilds.update('1@chat.example', (current) => ({ ...current, roles: guild.roles }));
    await tick();
    expect(target().disabled).toBe(false);
    chatEntities.members.upsertMany([{ ...members[1], role_ids: ['6'] }]);
    await tick();
    expect(target().disabled).toBe(true);
  });

  it('does not over-gate announcement follows on retained message history', async () => {
    guild.channels = [channel('2', 'announcements', Permission.VIEW_CHANNEL, 5)];
    await render('2@chat.example', 'integrations');
    expect(button('Integrations')).toBeDefined();
    expect(document.body.textContent).toContain('Follow');
    expect(network.api.mock.calls.some(([path]) => path.includes('/followers'))).toBe(true);
    chatEntities.guilds.update('1@chat.example', (current) => ({
      ...current,
      channels: current.channels!.map((item) => ({ ...item, permissions: '0' }))
    }));
    await tick();
    expect(button('Integrations')).toBeUndefined();
    expect(document.querySelector('#channels')).toBeNull();
  });
});
