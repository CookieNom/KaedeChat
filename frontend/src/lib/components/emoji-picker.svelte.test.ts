// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import EmojiPicker from './EmojiPicker.svelte';

it('keeps the full catalog in sections and jumps from search without losing selection', async () => {
  const onSelect = vi.fn();
  const component = mount(EmojiPicker, {
    target: document.body,
    props: {
      onSelect,
      onClose: vi.fn(),
      customEmojis: [
        {
          id: '1',
          origin_domain: 'chat.example',
          guild_id: '10',
          guild_domain: 'chat.example',
          guild_name: 'Garden',
          guild_icon_hash: 'a'.repeat(64),
          name: 'flower',
          url: '/flower.png',
          value: '<:flower:1@chat.example>'
        },
        {
          id: '2',
          origin_domain: 'chat.example',
          guild_id: '20',
          guild_domain: 'chat.example',
          guild_name: 'Games',
          name: 'dance',
          url: '/dance.png',
          value: '<:dance:2@chat.example>'
        }
      ]
    }
  });
  try {
    flushSync();
    await vi.waitFor(() => expect(document.querySelector('[data-section="flags"]')).not.toBeNull());
    expect(
      document.querySelectorAll('[data-section="people"] .emoji-grid button').length
    ).toBeGreaterThan(240);
    expect(document.querySelectorAll('.emoji-results section')).toHaveLength(10);
    expect(document.querySelector('.server-shortcut img')?.getAttribute('src')).toContain(
      '/media/assets/'
    );
    expect(document.querySelector('.server-shortcut')?.getAttribute('aria-pressed')).toBe('true');
    const search = document.querySelector<HTMLInputElement>('.emoji-search input')!;
    flushSync(() => {
      search.value = 'garden';
      search.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(document.querySelectorAll('.custom-emoji-group')).toHaveLength(1);
    flushSync(() => document.querySelector<HTMLButtonElement>('.custom-emojis button')!.click());
    expect(onSelect).toHaveBeenLastCalledWith('<:flower:1@chat.example>');
    document.querySelector<HTMLButtonElement>('nav button[aria-label="Flags"]')!.click();
    await tick();
    flushSync();
    expect(search.value).toBe('');
    expect(document.querySelectorAll('.emoji-results section')).toHaveLength(10);
    expect(
      document.querySelector('nav button[aria-label="Flags"]')?.getAttribute('aria-pressed')
    ).toBe('true');
    const flag = document.querySelector<HTMLButtonElement>(
      '[data-section="flags"] .emoji-grid button'
    )!;
    flushSync(() => flag.click());
    expect(onSelect).toHaveBeenLastCalledWith(flag.textContent);
    flushSync(() => {
      search.value = 'no-such-emoji-123';
      search.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(document.querySelector('[role="status"]')?.textContent).toContain('No emoji');
  } finally {
    await unmount(component);
  }
});
