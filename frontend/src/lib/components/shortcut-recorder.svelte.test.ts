// @vitest-environment happy-dom
import { expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ShortcutRecorder from './ShortcutRecorder.svelte';
it('records key combinations, cancels without replacing them, and clears the binding', async () => {
  const target = document.createElement('div');
  document.body.append(target);
  const component = mount(ShortcutRecorder, { target, props: { label: 'Toggle mute' } });
  try {
    flushSync();
    const record = target.querySelector<HTMLButtonElement>('.record')!;
    flushSync(() => record.click());
    flushSync(() =>
      record.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'M',
          code: 'KeyM',
          ctrlKey: true,
          shiftKey: true,
          bubbles: true
        })
      )
    );
    expect([...target.querySelectorAll('kbd')].map((key) => key.textContent)).toEqual([
      'Ctrl',
      'Shift',
      'M'
    ]);
    flushSync(() => record.click());
    flushSync(() =>
      record.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true })
      )
    );
    expect(record.getAttribute('aria-pressed')).toBe('false');
    expect(target.textContent).toContain('Ctrl');
    flushSync(() => target.querySelector<HTMLButtonElement>('.clear')!.click());
    expect(target.querySelector('kbd')).toBeNull();
  } finally {
    await unmount(component);
    target.remove();
  }
});
