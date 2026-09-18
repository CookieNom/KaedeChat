// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import VoiceQuickControls from './VoiceQuickControls.svelte';
import { activeVoice } from './active.svelte';
import type { VoiceSession } from './session';
const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock('$lib/platform/native', () => ({ isNativeDesktop: () => true, nativeInvoke: invoke }));

it('refreshes call controls through connection, permission and push-to-talk changes', async () => {
  // VoiceSession is a class: its fields are not deeply reactive Svelte state.
  class Session {
    connected = false;
    canSpeak = false;
    microphone = false;
    deafened = false;
    pushToTalkRequired = false;
    toggleMicrophone = vi.fn();
    toggleDeafen = vi.fn();
  }
  const session = new Session();
  const target = document.createElement('div');
  document.body.append(target);
  const component = mount(VoiceQuickControls, { target });
  const update = (values: Partial<Session>) =>
    flushSync(() => {
      Object.assign(session, values);
      activeVoice.revision += 1;
    });
  try {
    flushSync();
    const [microphone, deafen] = target.querySelectorAll<HTMLButtonElement>(
      '.split > button:first-child'
    );
    expect(microphone.disabled).toBe(true);
    expect(microphone.title).toBe('Join a call to use your microphone');
    expect(target.querySelector('.split.off')).toBeNull();

    flushSync(() => {
      activeVoice.session = session as unknown as VoiceSession;
    });
    update({ connected: true, canSpeak: true });
    expect(microphone.disabled).toBe(false);
    expect(deafen.disabled).toBe(false);
    expect(microphone.title).toBe('Unmute microphone');
    microphone.click();
    deafen.click();
    expect(session.toggleMicrophone).toHaveBeenCalledOnce();
    expect(session.toggleDeafen).toHaveBeenCalledOnce();

    update({ microphone: true });
    expect(microphone.title).toBe('Mute microphone');
    expect(microphone.getAttribute('aria-pressed')).toBe('false');
    update({ canSpeak: false, microphone: false });
    expect(microphone.disabled).toBe(true);
    expect(microphone.title).toContain('permission to speak');

    update({ canSpeak: true, pushToTalkRequired: true });
    expect(microphone.disabled).toBe(true);
    expect(microphone.title).toBe('Use push-to-talk in the call controls');
    microphone.click();
    expect(session.toggleMicrophone).toHaveBeenCalledOnce();
    update({ pushToTalkRequired: false });
    expect(microphone.disabled).toBe(false);

    update({ deafened: true });
    expect(deafen.title).toBe('Undeafen');
    update({ connected: false });
    expect(microphone.disabled).toBe(true);
    expect(deafen.disabled).toBe(true);
    expect(target.querySelector('.split.off')).toBeNull();
  } finally {
    await unmount(component);
    target.remove();
    activeVoice.session = null;
  }
});

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
  const preferences = {
    input_device: null,
    output_device: { id: 'unplugged', label: 'Disconnected headphones' }
  };
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
    expect(target.querySelector<HTMLInputElement>('input[value=""]')!.checked).toBe(true);
    flushSync(() => target.querySelector<HTMLInputElement>('input[value="mic"]')!.click());
    await vi.waitFor(() =>
      expect(target.querySelector('[role="alert"]')?.textContent).toContain('previous device')
    );
    expect(target.querySelector<HTMLInputElement>('input[value=""]')!.checked).toBe(true);
    expect(invoke).toHaveBeenCalledWith('native_preferences_set', {
      preferences: { ...preferences, input_device: { id: 'mic', label: 'Studio microphone' } }
    });
    flushSync(() =>
      target.querySelector<HTMLButtonElement>('[aria-label="Choose speakers"]')!.click()
    );
    await vi.waitFor(() => expect(target.querySelector('input[value="out"]')).not.toBeNull());
    expect(target.querySelector<HTMLInputElement>('input[value=""]')!.checked).toBe(true);
    expect(target.querySelector('input[value="mic"]')).toBeNull();
    expect(
      target.querySelector('[aria-label="Choose speakers"]')!.getAttribute('aria-expanded')
    ).toBe('true');
  } finally {
    await unmount(component);
    target.remove();
  }
});
