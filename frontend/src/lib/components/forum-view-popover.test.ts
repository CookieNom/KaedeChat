// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import ForumView from './ForumView.svelte';
import type { Channel, Guild } from '$lib/chat/types';

let component: ReturnType<typeof mount> | undefined;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});

describe('ForumView Sort & View popover', () => {
  it('dismisses on outside pointer presses and Escape', () => {
    const target = document.createElement('div');
    document.body.append(target);
    component = mount(ForumView, {
      target,
      props: {
        guild: { id: '1', origin_domain: 'chat.example', name: 'Guild' } as Guild,
        forum: {
          id: '2',
          origin_domain: 'chat.example',
          guild_id: '1',
          guild_domain: 'chat.example',
          type: 15,
          name: 'Forum'
        } as Channel,
        posts: [],
        onCreate: vi.fn()
      }
    });
    flushSync();
    const summary = Array.from(target.querySelectorAll('summary')).find((item) =>
      item.textContent?.includes('Sort')
    )!;
    const menu = summary.parentElement as HTMLDetailsElement;
    summary.click();
    expect(menu.open).toBe(true);
    summary.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    expect(menu.open).toBe(true);
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    expect(menu.open).toBe(false);
    summary.click();
    expect(menu.open).toBe(true);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(menu.open).toBe(false);
  });
});
