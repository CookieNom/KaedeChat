// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ShortcutRecorder from './ShortcutRecorder.svelte';
it('records key combinations, cancels without replacing them, and clears the binding', async () => {
  const target = document.createElement('div');
  document.body.append(target);
  const onsave = vi.fn();
  const component = mount(ShortcutRecorder, { target, props: { label: 'Toggle mute', onsave } });
  try {
    flushSync();
    const record = target.querySelector<HTMLButtonElement>('.record')!;
    flushSync(() => record.click());
    expect(target.querySelector<HTMLButtonElement>('.save')!.disabled).toBe(true);
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
    expect(onsave).not.toHaveBeenCalled();
    expect(record.getAttribute('aria-pressed')).toBe('true');
    flushSync(() => target.querySelector<HTMLButtonElement>('.save')!.click());
    expect(onsave).toHaveBeenCalledOnce();
    expect(record.getAttribute('aria-pressed')).toBe('false');
    flushSync(() => record.click());
    flushSync(() =>
      record.dispatchEvent(new KeyboardEvent('keydown', { key: 'p', code: 'KeyP', bubbles: true }))
    );
    expect(target.textContent).toContain('P');
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
