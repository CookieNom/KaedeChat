// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import NativeVoiceSettings from './NativeVoiceSettings.svelte';

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock('$lib/platform/native', () => ({ isNativeDesktop: () => true, nativeInvoke: invoke }));
vi.mock('$lib/api/client', () => ({
  userErrorMessage: (_error: unknown, fallback: string) => fallback
}));

async function settle() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
  flushSync();
}

function setup() {
  vi.useFakeTimers();
  invoke.mockReset().mockImplementation(async (command: string) => {
    if (command === 'native_audio_devices')
      return { inputs: [], outputs: [], cameras: [], screens: [] };
    if (command === 'native_preferences_get')
      return {
        input_device: { id: 'mic', label: 'Microphone' },
        input_mode: 'voice_activity',
        vad_threshold: 0.02,
        audio_quality: 'standard',
        screen_share_profile: 'smooth',
        noise_suppression: 'standard',
        opus_dtx: true,
        echo_cancellation: true,
        automatic_gain_control: true
      };
    if (command === 'native_voice_status') return { state: 'disconnected', input_level: 0.42 };
    return '';
  });
  const target = document.createElement('div');
  document.body.append(target);
  const component = mount(NativeVoiceSettings, { target });
  flushSync();
  return { target, component };
}

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = '';
});

it('keeps microphone testing on, meters while disconnected, and stops on request or unmount', async () => {
  const { target, component } = setup();
  await settle();
  const button = target.querySelector<HTMLButtonElement>('.native-device-actions button')!;
  const meter = target.querySelector('meter')!;
  expect(meter.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  flushSync(() => button.click());
  expect(button.disabled).toBe(true);
  button.click();
  await settle();
  expect(invoke.mock.calls.filter(([name]) => name === 'native_test_input')).toEqual([
    ['native_test_input', { deviceId: 'mic' }]
  ]);
  expect(button.textContent).toContain('Stop testing');
  expect(button.disabled).toBe(false);
  await vi.advanceTimersByTimeAsync(5000);
  flushSync();
  expect(button.textContent).toContain('Stop testing');
  expect(meter.value).toBe(0.42);
  flushSync(() => button.click());
  expect(button.disabled).toBe(true);
  await settle();
  expect(button.textContent).toContain('Test microphone');
  expect(button.getAttribute('aria-pressed')).toBe('false');
  flushSync(() => button.click());
  await settle();
  await unmount(component);
  expect(invoke.mock.calls.filter(([name]) => name === 'native_stop_input_test')).toHaveLength(2);
});

it('allows retry after a failed start and cleans up a start that finishes after unmount', async () => {
  const { target, component } = setup();
  await settle();
  const button = target.querySelector<HTMLButtonElement>('.native-device-actions button')!;
  invoke.mockRejectedValueOnce(new Error('Microphone unavailable'));
  flushSync(() => button.click());
  await settle();
  expect(button.disabled).toBe(false);
  expect(button.textContent).toContain('Test microphone');
  expect(target.querySelector('[role="alert"]')).not.toBeNull();
  let finish!: () => void;
  invoke.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      })
  );
  flushSync(() => button.click());
  await unmount(component);
  finish();
  await settle();
  expect(invoke.mock.calls.filter(([name]) => name === 'native_stop_input_test')).toHaveLength(2);
});

it('enables Save only for unsaved changes, blocks duplicate submissions, and keeps failures retryable', async () => {
  const { target, component } = setup();
  await settle();
  const save = target.querySelector<HTMLButtonElement>('.form-actions button')!;
  const quality = target.querySelector<HTMLInputElement>('input[type=range]')!;
  const change = (value: string) => {
    flushSync(() => {
      quality.value = value;
      quality.dispatchEvent(new Event('input', { bubbles: true }));
    });
  };
  expect(save.disabled).toBe(true);
  change('0.04');
  expect(save.disabled).toBe(false);
  change('0.02');
  expect(save.disabled).toBe(true);
  change('0.04');
  let finish!: () => void;
  invoke.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      })
  );
  flushSync(() =>
    target.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  );
  expect(save.disabled).toBe(true);
  target.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
  expect(invoke.mock.calls.filter(([name]) => name === 'native_preferences_set')).toHaveLength(1);
  finish();
  await settle();
  expect(save.disabled).toBe(true);
  expect(target.querySelector('.settings-toast')?.textContent).toContain('settings saved');
  await vi.advanceTimersByTimeAsync(3500);
  flushSync();
  expect(target.querySelector('.settings-toast')).toBeNull();
  change('0.06');
  invoke.mockRejectedValueOnce(new Error('Save failed'));
  flushSync(() =>
    target.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  );
  await settle();
  expect(save.disabled).toBe(false);
  expect(target.querySelector('[role="alert"]')).not.toBeNull();
  flushSync(() =>
    target.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  );
  await settle();
  expect(save.disabled).toBe(true);
  change('0.02');
  expect(save.disabled).toBe(false);
  await unmount(component);
});

it('keeps edits made during a pending save unsaved', async () => {
  const { target, component } = setup();
  await settle();
  const save = target.querySelector<HTMLButtonElement>('.form-actions button')!;
  const quality = target.querySelector<HTMLInputElement>('input[type=range]')!;
  flushSync(() => {
    quality.value = '0.04';
    quality.dispatchEvent(new Event('input', { bubbles: true }));
  });
  let finish!: () => void;
  invoke.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      })
  );
  flushSync(() =>
    target.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  );
  flushSync(() => {
    quality.value = '0.06';
    quality.dispatchEvent(new Event('input', { bubbles: true }));
  });
  finish();
  await settle();
  expect(save.disabled).toBe(false);
  expect(invoke.mock.calls.find(([name]) => name === 'native_preferences_set')?.[1]).toMatchObject({
    preferences: { vad_threshold: 0.04 }
  });
  await unmount(component);
});
