// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ForumEmojiField from './ForumEmojiField.svelte';

it('searches guild emoji, saves custom IDs or Unicode, clears and dismisses', async () => {
  const onChange = vi.fn();
  const component = mount(ForumEmojiField, {
    target: document.body,
    props: {
      emojiId: '123',
      guildDomain: 'chat.example',
      label: 'Tag emoji',
      customEmojis: [
        {
          id: '123',
          origin_domain: 'chat.example',
          guild_id: '1',
          guild_domain: 'chat.example',
          guild_name: 'Garden',
          name: 'flower',
          url: '/flower.png',
          value: '<:flower:123@chat.example>'
        }
      ],
      onChange
    }
  });
  try {
    flushSync();
    expect(document.querySelector('img')?.getAttribute('src')).toBe('/flower.png');
    const panel = document.querySelector<HTMLDivElement>('[popover]')!;
    panel.hidePopover = vi.fn();
    const open = () =>
      flushSync(() =>
        panel.dispatchEvent(
          Object.assign(new Event('toggle'), { newState: 'open', oldState: 'closed' })
        )
      );
    open();
    const search = document.querySelector<HTMLInputElement>('.emoji-search input')!;
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    search.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(true);
    flushSync(() => {
      search.value = 'garden';
      search.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(document.querySelector('.custom-emoji-group h3')?.textContent).toBe('Garden');
    flushSync(() => document.querySelector<HTMLButtonElement>('.custom-emojis button')!.click());
    expect(onChange).toHaveBeenLastCalledWith({ emoji_id: '123', emoji_name: null });
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Tag emoji');
    open();
    await vi.waitFor(() => expect(document.querySelector('.emoji-grid button')).not.toBeNull());
    const unicode = document.querySelector<HTMLButtonElement>('.emoji-grid button')!;
    const value = unicode.textContent;
    flushSync(() => unicode.click());
    expect(onChange).toHaveBeenLastCalledWith({ emoji_id: null, emoji_name: value });
    flushSync(() => document.querySelector<HTMLButtonElement>('.clear-emoji')!.click());
    expect(onChange).toHaveBeenLastCalledWith({ emoji_id: null, emoji_name: null });
    open();
    flushSync(() =>
      panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    );
    expect(document.querySelector('.emoji-picker')).toBeNull();
  } finally {
    await unmount(component);
  }
});
