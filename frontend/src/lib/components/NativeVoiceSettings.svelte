<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import Icon from '$lib/components/Icon.svelte';
  import NativeDevicePicker from '$lib/components/NativeDevicePicker.svelte';
  import {
    isNativeDesktop,
    nativeInvoke,
    type NativeDevices,
    type NativePreferences,
    type NativeVoiceStatus
  } from '$lib/platform/native';
  import { onMount } from 'svelte';

  let devices = $state<NativeDevices>({ inputs: [], outputs: [], cameras: [], screens: [] });
  let preferences = $state<NativePreferences | null>(null);
  let loading = $state(true);
  let saving = $state(false);
  let notice = $state('');
  let error = $state('');
  let hotkeyStatus = $state('');
  let inputLevel = $state(0);
  let testingInput = $state(false);
  let testingOutput = $state(false);
  let devicesUpdatedAt = $state<Date | null>(null);
  let deviceSignature = '';

  function signature(available: NativeDevices): string {
    return JSON.stringify({
      inputs: available.inputs.map(({ id, label, is_default }) => [id, label, is_default]),
      outputs: available.outputs.map(({ id, label, is_default }) => [id, label, is_default]),
      cameras: available.cameras.map(({ id, label }) => [id, label]),
      screens: available.screens.map(({ id, label }) => [id, label])
    });
  }

  async function loadDevices(silent = false) {
    if (!silent) error = '';
    try {
      const available = await nativeInvoke<NativeDevices>('native_audio_devices');
      const nextSignature = signature(available);
      if (nextSignature !== deviceSignature) {
        devices = available;
        deviceSignature = nextSignature;
      }
      devicesUpdatedAt = new Date();
      if (error === 'Could not enumerate this computer’s media devices.') error = '';
    } catch (caught) {
      if (!silent) {
        error = userErrorMessage(
          caught,
          'Could not list this computer’s media devices. Check system privacy settings and try again.'
        );
      }
    }
  }

  async function testInput() {
    if (testingInput) return;
    testingInput = true;
    error = '';
    notice = '';
    try {
      const peak = await nativeInvoke<number>('native_test_input', {
        deviceId: preferences?.input_device?.id ?? null
      });
      inputLevel = peak;
      notice =
        peak > 0.005
          ? 'Microphone input received.'
          : 'The microphone opened, but no speech was detected.';
    } catch (caught) {
      error = userErrorMessage(
        caught,
        'Could not open the selected microphone. It may be in use or blocked by system privacy settings.'
      );
    } finally {
      testingInput = false;
    }
  }

  async function testOutput() {
    if (testingOutput) return;
    testingOutput = true;
    error = '';
    notice = '';
    try {
      await nativeInvoke('native_test_output', {
        deviceId: preferences?.output_device?.id ?? null
      });
      notice = 'Played a test tone through the selected output.';
    } catch (caught) {
      error = userErrorMessage(
        caught,
        'Could not open the selected output device. Check that it is connected and try again.'
      );
    } finally {
      testingOutput = false;
    }
  }

  onMount(() => {
    if (!isNativeDesktop()) return;
    void Promise.all([
      nativeInvoke<NativeDevices>('native_audio_devices'),
      nativeInvoke<NativePreferences>('native_preferences_get'),
      nativeInvoke<string>('native_hotkey_status')
    ])
      .then(([available, loaded, shortcutStatus]) => {
        devices = available;
        deviceSignature = signature(available);
        devicesUpdatedAt = new Date();
        preferences = loaded;
        hotkeyStatus = shortcutStatus;
      })
      .catch(
        (caught) =>
          (error = userErrorMessage(
            caught,
            'Could not list this computer’s media devices. Check system privacy settings and try again.'
          ))
      )
      .finally(() => (loading = false));
    const meter = setInterval(() => {
      void nativeInvoke<NativeVoiceStatus>('native_voice_status')
        .then((status) => (inputLevel = status.input_level ?? 0))
        .catch(() => undefined);
    }, 120);
    const deviceWatcher = setInterval(() => void loadDevices(true), 4000);
    const rescanOnFocus = () => void loadDevices(true);
    const rescanWhenVisible = () => {
      if (document.visibilityState === 'visible') void loadDevices(true);
    };
    window.addEventListener('focus', rescanOnFocus);
    document.addEventListener('visibilitychange', rescanWhenVisible);
    return () => {
      clearInterval(meter);
      clearInterval(deviceWatcher);
      window.removeEventListener('focus', rescanOnFocus);
      document.removeEventListener('visibilitychange', rescanWhenVisible);
    };
  });

  function devicePreference(id: string, list: { id: string; label: string }[]) {
    if (!id) return null;
    const device = list.find((candidate) => candidate.id === id);
    return device ? { id: device.id, label: device.label } : null;
  }

  async function save() {
    if (!preferences || saving) return;
    saving = true;
    notice = '';
    error = '';
    try {
      await nativeInvoke('native_preferences_set', { preferences });
      hotkeyStatus = await nativeInvoke<string>('native_hotkey_status');
      notice = 'Native voice settings saved. Active voice was safely reconnected if needed.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save native voice settings. Try again.');
    } finally {
      saving = false;
    }
  }
</script>

{#if isNativeDesktop()}
  <section id="voice-devices" class="settings-section">
    <div class="settings-section-heading">
      <span class="section-icon"><Icon name="volume" /></span>
      <div>
        <h2>Voice & devices</h2>
        <p>Use native audio devices without a browser device chooser.</p>
      </div>
    </div>
    {#if loading}
      <div class="settings-card"><p>Looking for media devices…</p></div>
    {:else if preferences}
      <form
        class="settings-card settings-form"
        onsubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div class="native-device-toolbar">
          <div>
            <strong><span class="native-live-dot"></span>Devices update automatically</strong>
            <p>
              Kaede watches for devices being connected or removed, including while this page is
              open.
            </p>
          </div>
          {#if devicesUpdatedAt}<small
              >Last checked {devicesUpdatedAt.toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit'
              })}</small
            >{/if}
        </div>
        <div class="native-device-grid">
          <NativeDevicePicker
            label="Input device"
            description="Microphone used for voice and calls"
            icon="microphone"
            selectedId={preferences.input_device?.id ?? ''}
            options={devices.inputs}
            onSelect={(id) => (preferences!.input_device = devicePreference(id, devices.inputs))}
          />
          <NativeDevicePicker
            label="Output device"
            description="Speakers or headphones used for call audio"
            icon="volume"
            selectedId={preferences.output_device?.id ?? ''}
            options={devices.outputs}
            onSelect={(id) => (preferences!.output_device = devicePreference(id, devices.outputs))}
          />
          <NativeDevicePicker
            label="Camera"
            description="Video source used when your camera is enabled"
            icon="video"
            selectedId={preferences.camera_device?.id ?? ''}
            options={devices.cameras}
            onSelect={(id) => (preferences!.camera_device = devicePreference(id, devices.cameras))}
          />
          <NativeDevicePicker
            label="Screen or window"
            description="Preferred source for screen sharing"
            icon="screen"
            selectedId={preferences.screen_source?.id ?? ''}
            defaultLabel="Ask when sharing"
            options={devices.screens}
            onSelect={(id) => (preferences!.screen_source = devicePreference(id, devices.screens))}
          />
        </div>
        <div class="native-device-grid">
          <label class="form-field">
            <span>Outgoing audio quality</span>
            <small>Sets the maximum Opus bitrate. Network conditions can reduce it.</small>
            <select bind:value={preferences.audio_quality}>
              <option value="data_saver">Data saver · 24 kbps</option>
              <option value="standard">Standard · 48 kbps</option>
              <option value="high">High · 96 kbps</option>
              <option value="studio">Studio · 128 kbps</option>
            </select>
          </label>
          <label class="form-field">
            <span>Default screen-share quality</span>
            <small>You can change this again before each share.</small>
            <select bind:value={preferences.screen_share_profile}>
              <option value="data_saver">720p · 15 FPS</option>
              <option value="smooth">720p · 30 FPS</option>
              <option value="sharp">1080p · 30 FPS</option>
              <option value="source">Source · 30 FPS</option>
            </select>
          </label>
        </div>
        <div class="native-device-actions">
          <button
            class="secondary-button"
            type="button"
            disabled={testingInput}
            onclick={() => void testInput()}
          >
            {testingInput ? 'Listening…' : 'Test microphone'}
          </button>
          <button
            class="secondary-button"
            type="button"
            disabled={testingOutput}
            onclick={() => void testOutput()}
          >
            {testingOutput ? 'Playing…' : 'Test output'}
          </button>
        </div>
        <fieldset class="native-input-mode">
          <legend>Input mode</legend>
          <label
            ><input type="radio" bind:group={preferences.input_mode} value="voice_activity" /> Voice activity</label
          >
          <label
            ><input type="radio" bind:group={preferences.input_mode} value="push_to_talk" /> Push to talk</label
          >
        </fieldset>
        {#if preferences.input_mode === 'voice_activity'}
          <label class="form-field">
            <span>Voice activity sensitivity</span>
            <small>Higher values ignore more background noise.</small>
            <input
              type="range"
              min="0.003"
              max="0.15"
              step="0.001"
              bind:value={preferences.vad_threshold}
            />
          </label>
        {:else}
          <label class="form-field">
            <span>Push-to-talk shortcut</span>
            <small
              >Global shortcuts may be unavailable under Wayland until the desktop portal grants
              access.</small
            >
            <input bind:value={preferences.push_to_talk_hotkey} placeholder="Ctrl+Shift+Space" />
            <small>{hotkeyStatus}</small>
          </label>
        {/if}
        <div class="native-device-grid">
          <label class="form-field">
            <span>Noise suppression</span>
            <select bind:value={preferences.noise_suppression}>
              <option value="off">Off</option>
              <option value="standard">Standard</option>
              <option value="voice_isolation">Voice isolation</option>
            </select>
          </label>
          <div class="toggle-list">
            <label class="toggle-row"
              ><span
                ><strong>Echo cancellation</strong><small
                  >Uses speaker audio as the far-end reference.</small
                ></span
              ><input type="checkbox" bind:checked={preferences.echo_cancellation} /></label
            >
            <label class="toggle-row"
              ><span
                ><strong>Automatic gain</strong><small>Keeps speech at a consistent level.</small
                ></span
              ><input type="checkbox" bind:checked={preferences.automatic_gain_control} /></label
            >
          </div>
        </div>
        <label class="form-field native-input-meter">
          <span>Input level</span>
          <small>Shown while connected to voice. Audio never crosses the UI bridge.</small>
          <meter min="0" max="1" value={inputLevel}></meter>
        </label>
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        {#if notice}<p class="settings-helper" role="status">{notice}</p>{/if}
        <div class="form-actions">
          <button class="primary-button" disabled={saving}
            >{saving ? 'Saving…' : 'Save voice settings'}</button
          >
        </div>
      </form>
    {/if}
  </section>
{/if}
