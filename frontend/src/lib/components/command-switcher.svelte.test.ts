// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommandSwitcher from './CommandSwitcher.svelte';

const network = vi.hoisted(() => ({ api: vi.fn(), goto: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: network.goto, afterNavigate: vi.fn() }));
vi.mock('$app/paths', () => ({ resolve: (path: string) => `/${path}` }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  vi.clearAllMocks();
});

it('mixes recent DMs and channels before limiting results and preserves search and keyboard navigation', async () => {
  const channels = Array.from({ length: 16 }, (_, index) => ({
    id: String(index + 1),
    origin_domain: 'chat.example',
    type: 0,
    name: `general-${index}`,
    last_message_id: '9007199254740992'
  }));
  network.api.mockImplementation(async (path: string) =>
    path.endsWith('/guilds')
      ? [
          {
            id: '100',
            origin_domain: 'chat.example',
            name: 'Community',
            channels: [
              ...channels,
              {
                id: '99',
                origin_domain: 'chat.example',
                type: 4,
                name: 'Category',
                last_message_id: '9007199254740999'
              }
            ]
          }
        ]
      : [
          {
            id: '1',
            origin_domain: 'remote.example',
            type: 3,
            guild_id: null,
            conversation_type: 'group',
            name: 'Weekend plans',
            recipients: [
              { origin_domain: 'people.example', avatar_hash: 'a'.repeat(64) },
              { origin_domain: 'other.example', avatar_hash: 'b'.repeat(64) }
            ],
            last_message_id: '9007199254740993'
          }
        ]
  );
  component = mount(CommandSwitcher, { target: document.body });
  flushSync();
  window.dispatchEvent(new Event('kaede:open-command-switcher'));
  await vi.waitFor(() => expect(document.querySelectorAll('[role="option"]')).toHaveLength(15));
  const labels = () =>
    [...document.querySelectorAll('[role="option"] strong')].map((element) => element.textContent);
  expect(labels()[0]).toBe('Weekend plans');
  expect(document.querySelectorAll('[role="option"] .conversation-icon')).toHaveLength(15);
  expect(document.querySelectorAll('[role="option"] .stacked img')).toHaveLength(2);
  expect(labels()).not.toContain('# Category');
  const input = document.querySelector<HTMLInputElement>('[role="combobox"]')!;
  input.value = 'general-9';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(labels()).toEqual(['# general-9']);
  input.value = '';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  input.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
  );
  expect(network.goto).toHaveBeenCalledWith('/home/1%40remote.example');
});
