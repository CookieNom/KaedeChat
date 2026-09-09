// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync, tick } from 'svelte';
import Integrations from '../../routes/(app)/g/[guildId]/integrations/+page.svelte';
import { Permission } from '$lib/generated/permissions';
import { chatEntities } from '$lib/stores/entities.svelte';
import type { Channel, Guild } from './types';
import type { WebhookSummary } from './webhooks';
const network = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$app/state', () => ({ page: { params: { guildId: '1@chat.example' } } }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
const root = '/guilds/1%40chat.example';
const botPath = `${root}/integrations/bots/9%40apps.example`;
const hookPath = '/webhooks/8%40chat.example?guild_ref=1%40chat.example';
const permissions = (
  Permission.MANAGE_GUILD |
  Permission.MANAGE_WEBHOOKS |
  Permission.VIEW_CHANNEL
).toString();
const channel = (id: string, type: number, name: string): Channel =>
  ({
    id,
    origin_domain: 'chat.example',
    guild_id: '1',
    guild_domain: 'chat.example',
    type,
    name,
    permissions,
    position: Number(id),
    encryption_mode: 'plaintext'
  }) as Channel;
const guild = {
  id: '1',
  origin_domain: 'chat.example',
  name: 'Guild',
  description: null,
  icon_hash: null,
  permission_generation: '1',
  unavailable: false,
  owner_id: '7',
  owner_domain: 'chat.example',
  permissions,
  roles: [],
  channels: [
    channel('2', 0, 'general'),
    channel('3', 5, 'announcements'),
    channel('4', 4, 'category'),
    channel('5', 11, 'thread')
  ]
} as Guild;
const installation = {
  id: '6',
  status: 'active',
  scopes: [],
  intents: [],
  permissions: '0',
  channel_restrictions: [],
  e2ee_mode: 'disabled',
  grant_revision: '1',
  installed_at: '2026-01-01T00:00:00Z',
  application: {
    ref: '9@apps.example',
    name: 'Helper Bot',
    description: null,
    origin_domain: 'apps.example',
    bot_user: { username: 'helper', display_name: null, handle: 'helper@apps.example' }
  }
};
const hook: WebhookSummary = {
  id: '8',
  guild_id: '1',
  guild_domain: 'chat.example',
  channel_id: '2',
  channel_domain: 'chat.example',
  name: 'News',
  avatar_hash: null,
  revoked: false,
  execution_url: 'https://chat.example/api/v1/webhooks/8/kwh_original'
};
let hooks: WebhookSummary[];
let component: ReturnType<typeof mount> | undefined;
const button = (label: string, within: ParentNode = document) =>
  Array.from(within.querySelectorAll('button')).find((b) => b.textContent?.trim() === label)!;
async function render() {
  component = mount(Integrations, { target: document.body });
  flushSync();
  await vi.waitFor(() => expect(document.body.textContent).toContain('Helper Bot'));
  await vi.waitFor(() => expect(button('Create webhook')).toBeDefined());
}
beforeEach(() => {
  hooks = [];
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true)
  );
  vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
  network.api.mockReset().mockImplementation(async (path: string, options: RequestInit = {}) => {
    const method = options.method ?? 'GET';
    if (path === root) return structuredClone(guild);
    if (path === `${root}/integrations/bots`) return [structuredClone(installation)];
    if (path === botPath && method === 'PATCH')
      return { status: 'active', ...JSON.parse(String(options.body)), grant_revision: '2' };
    if (path === botPath && method === 'DELETE') return undefined;
    if (path === `${root}/webhooks`) return structuredClone(hooks);
    if (path === `${root}/channels/2%40chat.example/webhooks` && method === 'POST') {
      hooks = [{ ...hook, ...JSON.parse(String(options.body)) }];
      return structuredClone(hooks[0]);
    }
    if (path === hookPath && method === 'PATCH') {
      const changes = JSON.parse(String(options.body));
      hooks[0] = { ...hooks[0], name: changes.name, channel_id: changes.channel_id.split('@')[0] };
      return structuredClone(hooks[0]);
    }
    if (path === '/webhooks/8%40chat.example/rotate?guild_ref=1%40chat.example') {
      hooks[0] = {
        ...hooks[0],
        execution_url: 'https://chat.example/api/v1/webhooks/8/kwh_rotated'
      };
      return structuredClone(hooks[0]);
    }
    if (path === hookPath && method === 'DELETE') {
      hooks = [];
      return undefined;
    }
    if (path.includes('/followers')) return [];
    throw new Error(`Unexpected request ${method} ${path}`);
  });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = undefined;
  document.body.replaceChildren();
  chatEntities.clearSession();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
describe('server integrations', () => {
  it('keeps bots, webhooks, and channel following reachable from one server route', async () => {
    await render();
    const links = Array.from(
      document.querySelectorAll('nav[aria-label="Integration types"] a')
    ) as HTMLAnchorElement[];
    expect(links.map((a) => a.textContent?.trim())).toEqual([
      'Bots & Apps',
      'Webhooks',
      'Channels Followed'
    ]);
    for (const link of links) {
      link.click();
      expect(document.querySelector(link.hash)).not.toBeNull();
    }
    expect(button('Create webhook').disabled).toBe(true);
    expect(button('Follow')).toBeDefined();
    button('Remove bot').click();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('No bots or apps installed')
    );
    expect(network.api).toHaveBeenCalledWith(
      botPath,
      expect.objectContaining({ method: 'DELETE' })
    );
  });
  it('keeps target-owned bot channel ceilings in Server Settings Integrations', async () => {
    await render();
    const menu = Array.from(document.querySelectorAll('summary')).find(
      (s) => s.textContent === 'Channel access'
    )!;
    menu.click();
    const fieldset = menu.parentElement!.querySelector('fieldset')!;
    expect(fieldset.textContent).not.toContain('thread');
    const label = Array.from(fieldset.querySelectorAll('label')).find((l) =>
      l.textContent?.includes('general')
    )!;
    label.querySelector('input')!.click();
    await tick();
    button('Save channel access').click();
    await vi.waitFor(() =>
      expect(network.api).toHaveBeenCalledWith(
        botPath,
        expect.objectContaining({
          method: 'PATCH',
          body: JSON.stringify({ channel_restrictions: ['2@chat.example'] })
        })
      )
    );
    await vi.waitFor(() => expect(button('Allow all').disabled).toBe(false));
    button('Allow all').click();
    await tick();
    button('Save channel access').click();
    await vi.waitFor(() =>
      expect(network.api).toHaveBeenLastCalledWith(
        botPath,
        expect.objectContaining({ method: 'PATCH', body: '{"channel_restrictions":[]}' })
      )
    );
  });
  it('offers full webhook CRUD and reload-safe copyable execution URLs', async () => {
    await render();
    const form = button('Create webhook').closest('form')!;
    const input = form.querySelector('input')!;
    input.value = 'News';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(button('Copy webhook URL')).toBeDefined());
    expect(network.api).toHaveBeenCalledWith(`${root}/channels/2%40chat.example/webhooks`, {
      method: 'POST',
      body: '{"name":"News"}'
    });
    await unmount(component!);
    component = undefined;
    document.body.replaceChildren();
    await render();
    const list = document.querySelector('[aria-label="Server webhooks"]')!;
    await vi.waitFor(() => expect(button('Copy webhook URL', list)).toBeDefined());
    button('Copy webhook URL', list).click();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(hook.execution_url);
    const name = list.querySelector<HTMLInputElement>('input[minlength]')!;
    name.value = 'Updated';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    button('Save', list).click();
    await vi.waitFor(() =>
      expect(network.api).toHaveBeenCalledWith(hookPath, {
        method: 'PATCH',
        body: '{"name":"Updated","channel_id":"2@chat.example"}'
      })
    );
    await vi.waitFor(() => expect(button('Rotate token', list).disabled).toBe(false));
    button('Rotate token', list).click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('kwh_rotated'));
    button('Copy webhook URL', list).click();
    expect(navigator.clipboard.writeText).toHaveBeenLastCalledWith(
      'https://chat.example/api/v1/webhooks/8/kwh_rotated'
    );
    await vi.waitFor(() => expect(button('Delete', list).disabled).toBe(false));
    button('Delete', list).click();
    await vi.waitFor(() => expect(list.querySelector('article')).toBeNull());
    expect(network.api).toHaveBeenCalledWith(hookPath, { method: 'DELETE' });
  });
  it('clears guild integrations after a live guild projection is revoked', async () => {
    hooks = [{ ...hook }];
    await render();
    const navigate = vi.spyOn(window.location, 'assign').mockImplementation(() => undefined);
    chatEntities.removeGuild({ id: '1', origin_domain: 'chat.example' });
    await tick();
    expect(navigate).toHaveBeenCalledWith('/home');
    expect(document.querySelector('[aria-label="Server webhooks"]')).toBeNull();
    expect(document.body.textContent).not.toContain('Helper Bot');
    expect(document.body.textContent).not.toContain('kwh_original');
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      'no longer have access'
    );
    const load = network.api.mock.calls.find(([path]) => path === `${root}/integrations/bots`)!;
    expect(load[1].signal.aborted).toBe(true);
  });
});
