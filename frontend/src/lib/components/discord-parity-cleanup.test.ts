// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import ThreadsPanel from './ThreadsPanel.svelte';
import ThreadHeader from './ThreadHeader.svelte';
import MessageRow from './MessageRow.svelte';
import type { Channel, Guild, Message } from '$lib/chat/types';

const media = vi.hoisted(() => ({ copy: vi.fn().mockResolvedValue(undefined), load: vi.fn() }));
vi.mock('$lib/media/authenticated', async (original) => ({
  ...(await original<typeof import('$lib/media/authenticated')>()),
  authenticatedMedia: media.load,
  copyAuthenticatedImage: media.copy
}));
let component: ReturnType<typeof createClassComponent> | undefined;
afterEach(() => {
  component?.$destroy();
  component = undefined;
  document.body.replaceChildren();
  vi.restoreAllMocks();
});
const guild = { id: '1', origin_domain: 'chat.example', name: 'Guild' } as Guild;
const parent = {
  id: '2',
  origin_domain: 'chat.example',
  guild_id: '1',
  guild_domain: 'chat.example',
  type: 15,
  name: 'Forum'
} as Channel;
const outside = () =>
  document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
const escape = () =>
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
const button = (label: string) =>
  Array.from(document.querySelectorAll<HTMLButtonElement>('button')).find(
    (item) => item.textContent?.trim() === label
  )!;

describe('Discord parity interactions', () => {
  it('dismisses the Threads panel on outside pointer presses and Escape', async () => {
    component = createClassComponent({
      component: ThreadsPanel,
      target: document.body,
      props: {
        guild,
        parent,
        activeThreads: [],
        archivedThreads: [],
        onOpen: vi.fn(),
        onCreate: vi.fn()
      }
    });
    flushSync();
    const summary = document.querySelector<HTMLElement>('[aria-label="Threads"]')!;
    const menu = summary.parentElement as HTMLDetailsElement;
    summary.click();
    await tick();
    expect(menu.open).toBe(true);
    summary.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    await tick();
    expect(menu.open).toBe(true);
    outside();
    await tick();
    expect(menu.open).toBe(false);
    summary.click();
    await tick();
    expect(menu.open).toBe(true);
    escape();
    await tick();
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(summary);
  });

  it('closes forum posts and dismisses the post actions menu', async () => {
    const lock = vi.fn();
    const thread = {
      ...parent,
      id: '3',
      type: 11,
      parent_id: '2',
      parent_domain: 'chat.example',
      name: 'Post',
      archived: false,
      locked: false
    } as Channel;
    component = createClassComponent({
      component: ThreadHeader,
      target: document.body,
      props: {
        guild,
        parent,
        thread,
        canManage: true,
        onMembership: vi.fn(),
        onNotifications: vi.fn(),
        onRename: vi.fn(),
        onEncryption: vi.fn(),
        onInvitable: vi.fn(),
        onArchive: vi.fn(),
        onLock: lock,
        onMemberChange: vi.fn(),
        onPin: vi.fn(),
        onTagsChange: vi.fn(),
        onDelete: vi.fn()
      }
    });
    flushSync();
    const summary = document.querySelector<HTMLElement>('[aria-label="Thread actions"]')!;
    const menu = summary.parentElement as HTMLDetailsElement;
    for (const dismiss of [outside, escape]) {
      summary.click();
      expect(menu.open).toBe(true);
      summary.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
      expect(menu.open).toBe(true);
      dismiss();
      expect(menu.open).toBe(false);
    }
    summary.click();
    button('Close Post').click();
    expect(lock).toHaveBeenCalledExactlyOnceWith(true);
    expect(menu.open).toBe(false);
    component.$set({ thread: { ...thread, archived: true, locked: true } });
    await tick();
    expect(document.querySelector('.thread-title')?.textContent).toContain('Closed');
    summary.click();
    button('Reopen Post').click();
    expect(lock.mock.calls).toEqual([[true], [false]]);
    expect(menu.open).toBe(false);
  });

  it('uses the full message menu for mobile images and exposes image copying', async () => {
    vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(390);
    const message = {
      id: '3',
      origin_domain: 'chat.example',
      channel_id: '2',
      channel_domain: 'chat.example',
      content: 'A photo',
      flags: 0,
      message_type: 0,
      created_at: '2026-01-01T00:00:00Z',
      edited_at: null,
      author: {
        id: '7',
        origin_domain: 'chat.example',
        username: 'author',
        display_name: null,
        avatar_hash: null
      },
      attachments: [
        {
          id: '8',
          origin_domain: 'chat.example',
          filename: 'photo.png',
          content_type: 'image/png',
          size: 40,
          scan_status: 'clean',
          encryption_mode: 'plaintext',
          variants: {}
        }
      ],
      embeds: [],
      components: [],
      sticker_items: [],
      reactions: []
    } as unknown as Message;
    component = createClassComponent({
      component: MessageRow,
      target: document.body,
      props: { message }
    });
    flushSync();
    document
      .querySelector('[aria-label="Open photo.png"]')!
      .dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
    await tick();
    const menu = document.querySelector('[role="menu"]')!;
    expect(menu).not.toBeNull();
    expect(menu.textContent).toContain('Report message');
    expect(menu.textContent).toContain('Copy message link');
    button('Copy image').click();
    await tick();
    expect(media.copy).toHaveBeenCalledExactlyOnceWith({
      path: '/media/chat.example/8/original',
      contentType: 'image/png'
    });
    expect(document.querySelector('[role="menu"]')).toBeNull();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('Image copied to clipboard.')
    );
  });
});
