// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import ForumView from './ForumView.svelte';
import type { Channel, Guild } from '$lib/chat/types';

let component: ReturnType<typeof mount> | undefined;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});

describe('ForumView popovers', () => {
  let target: HTMLDivElement;
  beforeEach(() => {
    target = document.createElement('div');
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
        canCreate: true,
        onCreate: vi.fn()
      }
    });
    flushSync();
  });

  it('dismisses Sort & View on outside pointer presses and Escape', () => {
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

  it('opens the composer emoji picker in a popover and inserts a selection', async () => {
    const showPopover = vi.fn();
    const original = HTMLElement.prototype.showPopover;
    HTMLElement.prototype.showPopover = showPopover;
    try {
      flushSync(() => target.querySelector<HTMLButtonElement>('.new-post')!.click());
      const trigger = target.querySelector<HTMLButtonElement>('.forum-emoji-control button')!;
      flushSync(() => trigger.click());
      const panel = target.querySelector<HTMLElement>('[popover="auto"]')!;
      expect(showPopover).toHaveBeenCalledOnce();
      expect(panel.closest('form')).toBeNull();
      expect(trigger.getAttribute('aria-expanded')).toBe('true');
      await vi.waitFor(() => expect(panel.querySelector('.emoji-grid button')).not.toBeNull());
      const emoji = panel.querySelector<HTMLButtonElement>('.emoji-grid button')!;
      const value = emoji.textContent;
      flushSync(() => emoji.click());
      expect(target.querySelector('textarea')!.value).toBe(value);
      expect(target.querySelector('[popover]')).toBeNull();
      flushSync(() => trigger.click());
      flushSync(() =>
        target
          .querySelector('[popover]')!
          .dispatchEvent(Object.assign(new Event('toggle'), { newState: 'closed' }))
      );
      expect(trigger.getAttribute('aria-expanded')).toBe('false');
      expect(target.querySelector('[popover]')).toBeNull();
    } finally {
      HTMLElement.prototype.showPopover = original;
    }
  });
});
