// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import NativeDevicePicker from './NativeDevicePicker.svelte';

it('recognizes saved backend aliases without adding duplicate camera options', async () => {
  const target = document.createElement('div');
  document.body.append(target);
  const select = vi.fn();
  const component = mount(NativeDevicePicker, {
    target,
    props: {
      label: 'Camera',
      icon: 'video',
      selectedId: 'dshow:physical',
      onSelect: select,
      options: [
        { id: '0', label: 'Webcam', aliases: ['dshow:physical'] },
        { id: 'dshow:obs', label: 'OBS Virtual Camera' },
        { id: 'dshow:two', label: 'Webcam' }
      ]
    }
  });
  try {
    flushSync();
    const trigger = target.querySelector<HTMLButtonElement>('.device-picker-trigger')!;
    expect(trigger.textContent).toContain('Webcam');
    expect(trigger.classList.contains('unavailable')).toBe(false);
    flushSync(() => trigger.click());
    const options = [...target.querySelectorAll<HTMLButtonElement>('[role="option"]')];
    expect(options).toHaveLength(4); // Default plus three distinct cameras.
    expect(options.filter((option) => option.getAttribute('aria-selected') === 'true')).toEqual([
      options[1]
    ]);
    flushSync(() => options[2].click());
    expect(select).toHaveBeenCalledWith('dshow:obs');
  } finally {
    await unmount(component);
    target.remove();
  }
});
