// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import VoiceQuickControls from './VoiceQuickControls.svelte';
const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock('$lib/platform/native', () => ({ isNativeDesktop: () => true, nativeInvoke: invoke }));
it('selects microphone and output independently and restores the previous selection on failure', async () => {
  for (const [method, newState] of [
    ['showPopover', 'open'],
    ['hidePopover', 'closed']
  ]) {
    Object.defineProperty(HTMLElement.prototype, method, {
      configurable: true,
      value() {
        this.dispatchEvent(Object.assign(new Event('toggle'), { newState }));
      }
    });
  }
  const target = document.createElement('div');
  document.body.append(target);
  const preferences = { input_device: null, output_device: null };
  invoke.mockImplementation(async (command: string) => {
    if (command === 'native_audio_devices')
      return {
        inputs: [{ id: 'mic', label: 'Studio microphone' }],
        outputs: [{ id: 'out', label: 'USB headphones' }]
      };
    if (command === 'native_preferences_get') return { ...preferences };
    if (command === 'native_preferences_set') throw new Error('unavailable');
  });
  const component = mount(VoiceQuickControls, { target });
  try {
    flushSync();
    flushSync(() =>
      target.querySelector<HTMLButtonElement>('[aria-label="Choose microphone"]')!.click()
    );
    await vi.waitFor(() => expect(target.querySelector('input[value="mic"]')).not.toBeNull());
    flushSync(() => target.querySelector<HTMLInputElement>('input[value="mic"]')!.click());
    await vi.waitFor(() =>
      expect(target.querySelector('[role="alert"]')?.textContent).toContain('previous device')
    );
    expect(target.querySelector<HTMLInputElement>('input[value=""]')!.checked).toBe(true);
    expect(invoke).toHaveBeenCalledWith('native_preferences_set', {
      preferences: { input_device: { id: 'mic', label: 'Studio microphone' }, output_device: null }
    });
    flushSync(() =>
      target.querySelector<HTMLButtonElement>('[aria-label="Choose speakers"]')!.click()
    );
    await vi.waitFor(() => expect(target.querySelector('input[value="out"]')).not.toBeNull());
    expect(target.querySelector('input[value="mic"]')).toBeNull();
    expect(
      target.querySelector('[aria-label="Choose speakers"]')!.getAttribute('aria-expanded')
    ).toBe('true');
  } finally {
    await unmount(component);
    target.remove();
  }
});
