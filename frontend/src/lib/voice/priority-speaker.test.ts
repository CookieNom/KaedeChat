// @vitest-environment happy-dom
import { mount, unmount, flushSync, tick } from 'svelte';
import NativeVoiceSettings from '$lib/components/NativeVoiceSettings.svelte';
const native = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock('$lib/platform/native', async (original) => ({
  ...(await original<typeof import('$lib/platform/native')>()),
  isNativeDesktop: () => true,
  nativeInvoke: native.invoke
}));
let component: ReturnType<typeof mount> | undefined;
afterEach(async () => {
  if (component) await unmount(component);
  component = undefined;
  document.body.replaceChildren();
});
import { afterEach, describe, expect, it, vi } from 'vitest';

import { nativePrioritySpeakerIdentities } from '$lib/platform/native';

describe('native Priority Speaker UI', () => {
  it('accepts only concrete native participant identities and deduplicates them', () => {
    expect([
      ...nativePrioritySpeakerIdentities(['42@chat.example', '42@chat.example', '', 42, null])
    ]).toEqual(['42@chat.example']);
    expect(nativePrioritySpeakerIdentities(null).size).toBe(0);
    expect(nativePrioritySpeakerIdentities(['not an identity']).size).toBe(0);
  });
  it('offers a separate priority keybind only in the push-to-talk settings branch', async () => {
    const preferences = {
      input_device: null,
      output_device: null,
      camera_device: null,
      screen_source: null,
      input_mode: 'voice_activity',
      vad_threshold: 0.01,
      push_to_talk_hotkey: 'Ctrl+Space',
      priority_push_to_talk_hotkey: 'Ctrl+Alt+Space',
      noise_suppression: 'off',
      echo_cancellation: true,
      automatic_gain_control: true,
      screen_share_profile: 'smooth',
      audio_quality: 'standard',
      opus_dtx: true,
      share_system_audio: false
    };
    native.invoke.mockImplementation(async (command) =>
      command === 'native_preferences_get'
        ? preferences
        : command === 'native_audio_devices'
          ? { inputs: [], outputs: [], cameras: [], screens: [] }
          : 'Available'
    );
    component = mount(NativeVoiceSettings, { target: document.body });
    flushSync();
    await vi.waitFor(() =>
      expect(document.querySelector('input[value="voice_activity"]')).not.toBeNull()
    );
    const priorityInput = () =>
      Array.from(document.querySelectorAll('label'))
        .find((label) => label.textContent?.includes('Priority push-to-talk shortcut'))
        ?.querySelector('input');
    expect(priorityInput()).toBeUndefined();
    document.querySelector<HTMLInputElement>('input[value="push_to_talk"]')!.click();
    await tick();
    expect(priorityInput()?.value).toBe('Ctrl+Alt+Space');
    expect(
      document.querySelector<HTMLInputElement>('input[placeholder="Ctrl+Shift+Space"]')?.value
    ).toBe('Ctrl+Space');
    priorityInput()!.value = 'Alt+P';
    priorityInput()!.dispatchEvent(new Event('input', { bubbles: true }));
    document
      .querySelector('form')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await tick();
    expect(native.invoke).toHaveBeenCalledWith('native_preferences_set', {
      preferences: expect.objectContaining({
        input_mode: 'push_to_talk',
        push_to_talk_hotkey: 'Ctrl+Space',
        priority_push_to_talk_hotkey: 'Alt+P'
      })
    });
    document.querySelector<HTMLInputElement>('input[value="voice_activity"]')!.click();
    await tick();
    expect(priorityInput()).toBeUndefined();
  });
});
