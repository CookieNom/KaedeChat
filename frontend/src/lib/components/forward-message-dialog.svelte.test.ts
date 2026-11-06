// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ForwardMessageDialog from './ForwardMessageDialog.svelte';
import { chatEntities } from '$lib/stores/entities.svelte';
import type { Channel, Guild, Message } from '$lib/chat/types';

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  chatEntities.guilds.replace([]);
});

it('searches recipient names and preserves hidden selections when forwarding', async () => {
  const channels = [
    {
      id: '1',
      origin_domain: 'chat.example',
      guild_id: null,
      type: 1,
      recipients: [
        {
          username: 'alice',
          display_name: 'Alice',
          origin_domain: 'people.example',
          avatar_hash: 'a'.repeat(64)
        }
      ]
    },
    {
      id: '2',
      origin_domain: 'chat.example',
      guild_id: '10',
      guild_domain: 'chat.example',
      name: 'general'
    }
  ] as unknown as Channel[];
  chatEntities.guilds.upsert({
    id: '10',
    origin_domain: 'chat.example',
    name: 'Community',
    icon_hash: 'b'.repeat(64)
  } as Guild);
  const onForward = vi.fn().mockResolvedValue(undefined);
  component = mount(ForwardMessageDialog, {
    target: document.body,
    props: { channels, message: { content: 'Hello' } as Message, onForward, onClose: vi.fn() }
  });
  flushSync();
  const pictures = [...document.querySelectorAll<HTMLImageElement>('.destinations img')];
  expect(pictures).toHaveLength(2);
  expect(pictures[0].src).toContain('https://people.example/');
  expect(pictures[1].src).toContain('https://chat.example/');
  expect(document.querySelectorAll('.conversation-icon .badge')).toHaveLength(2);
  pictures[0].dispatchEvent(new Event('error'));
  expect(pictures[0].hidden).toBe(true);
  expect(pictures[0].parentElement?.querySelector('svg')).not.toBeNull();
  const first = document.querySelector<HTMLInputElement>('.destinations input')!;
  first.click();
  flushSync();
  const search = document.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'alice';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(document.querySelectorAll('.destinations label')).toHaveLength(1);
  expect(document.querySelector('.destinations label')?.textContent).toContain('Alice');
  search.value = 'general';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(document.querySelectorAll('.destinations label')).toHaveLength(1);
  document.querySelector<HTMLInputElement>('.destinations input')!.click();
  flushSync();
  search.value = 'missing';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(document.querySelector('[role="status"]')?.textContent).toContain('No matching');
  document
    .querySelector('form')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await Promise.resolve();
  expect(onForward).toHaveBeenCalledWith(channels, '');
});
