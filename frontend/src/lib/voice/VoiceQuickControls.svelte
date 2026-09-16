<script lang="ts">
  import { tick } from 'svelte';
  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';
  import {
    isNativeDesktop,
    nativeInvoke,
    type NativeDevices,
    type NativePreferences
  } from '$lib/platform/native';
  import { activeVoice } from './active.svelte';
  const id = $props.id();
  let devices = $state<{ id: string; label: string }[]>([]);
  let selected = $state('');
  let kind = $state<'audioinput' | 'audiooutput'>('audioinput');
  let error = $state('');
  let busy = $state(false);
  let menu = $state<HTMLElement>();
  let menuOpen = $state(false);
  let menuLeft = $state(16);
  let menuBottom = $state(80);
  let request = 0;
  const session = $derived((activeVoice.revision, activeVoice.session));
  const muted = $derived((activeVoice.revision, !session?.microphone));
  const deafened = $derived((activeVoice.revision, session?.deafened ?? false));

  async function control(deafen: boolean) {
    error = '';
    try {
      if (deafen) await session?.toggleDeafen();
      else await session?.toggleMicrophone();
    } catch {
      kind = deafen ? 'audiooutput' : 'audioinput';
      error = 'Could not change the call controls. Try again.';
      document.getElementById(id)?.showPopover();
    }
  }

  async function open(next: typeof kind, event: MouseEvent) {
    if (menuOpen && kind === next) {
      menu?.hidePopover();
      return;
    }
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
    menuLeft = Math.max(12, Math.min(bounds.right - 288, window.innerWidth - 300));
    menuBottom = Math.max(12, window.innerHeight - bounds.top + 10);
    menu?.showPopover();
    const current = ++request;
    devices = [];
    kind = next;
    error = '';
    busy = true;
    try {
      if (isNativeDesktop()) {
        const [available, preferences] = await Promise.all([
          nativeInvoke<NativeDevices>('native_audio_devices'),
          nativeInvoke<NativePreferences>('native_preferences_get')
        ]);
        if (current !== request) return;
        devices = next === 'audioinput' ? available.inputs : available.outputs;
        selected =
          (next === 'audioinput' ? preferences.input_device : preferences.output_device)?.id ?? '';
      } else {
        const available = await navigator.mediaDevices.enumerateDevices();
        if (current !== request) return;
        devices = available
          .filter((device) => device.kind === next && !!device.deviceId)
          .map((device, i) => ({
            id: device.deviceId,
            label: device.label || `${next === 'audioinput' ? 'Microphone' : 'Speaker'} ${i + 1}`
          }));
        selected = session?.room.getActiveDevice(next) ?? '';
      }
    } catch {
      if (current === request)
        error = 'Could not list audio devices. Check device permissions and try again.';
    } finally {
      if (current === request) {
        busy = false;
        await tick();
        const height = menu?.getBoundingClientRect().height ?? 240;
        menuBottom =
          bounds.top >= height + 12
            ? window.innerHeight - bounds.top + 10
            : Math.max(12, window.innerHeight - bounds.bottom - height - 10);
      }
    }
  }

  async function select(value: string) {
    busy = true;
    error = '';
    const selectedKind = kind;
    const previous = selected;
    const device = devices.find((item) => item.id === value) ?? null;
    selected = value;
    try {
      if (isNativeDesktop()) {
        const preferences = await nativeInvoke<NativePreferences>('native_preferences_get');
        if (selectedKind === 'audioinput') preferences.input_device = device;
        else preferences.output_device = device;
        await nativeInvoke('native_preferences_set', { preferences });
      } else if (session) {
        await session.room.switchActiveDevice(selectedKind, value || 'default');
      }
      if (kind === selectedKind) selected = value;
    } catch {
      if (kind === selectedKind) selected = previous;
      error = 'Could not switch audio devices. Your previous device is still selected.';
    } finally {
      busy = false;
    }
  }
</script>

<div class="quick-controls" aria-label="Voice controls">
  <div class:off={muted} class="split">
    <button
      disabled={!session?.connected || !session?.canSpeak}
      aria-label={muted ? 'Unmute microphone' : 'Mute microphone'}
      title={muted ? 'Unmute microphone' : 'Mute microphone'}
      aria-pressed={muted}
      onclick={() => control(false)}
      ><Icon name={muted ? 'microphone-off' : 'microphone'} size={19} /></button
    >
    <button
      aria-label="Choose microphone"
      title="Choose microphone"
      aria-controls={id}
      aria-expanded={menuOpen && kind === 'audioinput'}
      onclick={(event) => open('audioinput', event)}><Icon name="chevron-down" size={14} /></button
    >
  </div>
  <div class:off={deafened} class="split">
    <button
      disabled={!session?.connected}
      aria-label={deafened ? 'Undeafen' : 'Deafen'}
      title={deafened ? 'Undeafen' : 'Deafen'}
      aria-pressed={deafened}
      onclick={() => control(true)}
      ><Icon name={deafened ? 'headphones-off' : 'headphones'} size={19} /></button
    >
    <button
      aria-label="Choose speakers"
      title="Choose speakers"
      aria-controls={id}
      aria-expanded={menuOpen && kind === 'audiooutput'}
      onclick={(event) => open('audiooutput', event)}><Icon name="chevron-down" size={14} /></button
    >
  </div>
</div>
<div
  {id}
  bind:this={menu}
  popover="auto"
  class="device-menu"
  role="dialog"
  aria-label="Audio devices"
  style:left={`${menuLeft}px`}
  style:bottom={`${menuBottom}px`}
  ontoggle={(event) => (menuOpen = event.newState === 'open')}
>
  <header>
    <Icon name={kind === 'audioinput' ? 'microphone' : 'headphones'} size={18} /><strong
      >{kind === 'audioinput' ? 'Input device' : 'Output device'}</strong
    >
  </header>
  {#if busy}<p class="device-status" role="status">Loading audio devices…</p>{/if}
  <fieldset
    disabled={busy || (!isNativeDesktop() && !session?.connected)}
    aria-label={kind === 'audioinput' ? 'Microphone' : 'Speakers'}
  >
    {#each [{ id: '', label: 'System default' }, ...devices.filter((device) => device.id && device.id !== 'default')] as device (device.id)}
      <label class="device-option"
        ><input
          type="radio"
          name={`${id}-device`}
          value={device.id}
          checked={selected === device.id || (device.id === '' && selected === 'default')}
          onchange={() => select(device.id)}
        /><span>{device.label}</span><Icon name="check" size={16} /></label
      >
    {/each}
  </fieldset>
  {#if !isNativeDesktop() && !session?.connected}<p class="device-status">
      Join a call to select an audio device.
    </p>{/if}
  {#if error}<p class="device-status error" role="alert">{error}</p>{/if}
  <a href={resolve('/settings#voice-devices')}
    ><Icon name="settings" size={16} />Voice settings &amp; shortcuts<Icon
      name="chevron-right"
      size={16}
    /></a
  >
</div>

<style>
  .quick-controls {
    display: flex;
    align-items: center;
    gap: 3px;
  }
  .split {
    display: flex;
    border-radius: 9px;
    background: transparent;
  }
  .split.off {
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 12%, var(--surface));
  }
  button {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 38px;
    min-width: 22px;
    padding: 4px;
    border: 0;
    border-radius: 7px;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }
  button:first-child {
    width: 30px;
  }
  button:hover {
    background: var(--surface-hover);
  }
  button:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .device-menu {
    position: fixed;
    inset: auto;
    margin: 0;
    box-sizing: border-box;
    width: min(288px, calc(100vw - 24px));
    max-height: min(420px, calc(100dvh - 100px));
    overflow-y: auto;
    padding: 8px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface-raised);
    color: var(--text);
    box-shadow: 0 8px 32px #0006;
  }
  header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 8px 12px;
    font-size: 12px;
    color: var(--text-muted);
  }
  fieldset {
    border: 0;
    padding: 0;
    margin: 0;
    min-width: 0;
  }
  .device-option {
    position: relative;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }
  .device-option span {
    flex: 1;
    overflow-wrap: anywhere;
  }
  .device-option input {
    position: absolute;
    opacity: 0;
    width: 1px;
    height: 1px;
  }
  .device-option :global(svg) {
    opacity: 0;
    flex-shrink: 0;
  }
  .device-option:has(input:checked) {
    background: color-mix(in srgb, var(--accent) 14%, var(--surface-raised));
    color: var(--accent);
  }
  .device-option:has(input:checked) :global(svg) {
    opacity: 1;
  }
  .device-option:hover {
    background: var(--surface-hover);
  }
  .device-option:has(input:focus-visible),
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  fieldset:disabled {
    opacity: 0.5;
  }
  .device-status {
    padding: 8px;
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-muted);
  }
  .error {
    color: var(--danger);
  }
  a {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 8px 6px;
    margin-top: 8px;
    border-top: 1px solid var(--line);
    text-decoration: none;
    color: var(--text);
    font-size: 12px;
  }
  a:hover {
    color: var(--accent);
  }
</style>
