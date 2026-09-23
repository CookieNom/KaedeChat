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
