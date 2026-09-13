<script lang="ts">
  import { t } from '$lib/ui/locale';

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
          $t('ui_could_not_list_this_computer_s_media_devices__6f982224')
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
        $t('ui_could_not_open_the_selected_microphone_it_may_f44c7506')
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
      notice = $t('ui_played_a_test_tone_through_the_selected_outpu_385288ad');
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_open_the_selected_output_device_che_e50f00fb')
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
            $t('ui_could_not_list_this_computer_s_media_devices__6f982224')
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
      notice = $t('ui_native_voice_settings_saved_active_voice_was__e3210080');
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_save_native_voice_settings_try_agai_4aa37dbb')
      );
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
        <h2>{$t('ui_voice_devices_48691508')}</h2>
        <p>{$t('ui_use_native_audio_devices_without_a_browser_de_ac1d174d')}</p>
      </div>
    </div>
    {#if loading}
      <div class="settings-card"><p>{$t('ui_looking_for_media_devices_f466425d')}</p></div>
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
            <strong
              ><span class="native-live-dot"></span>{$t(
                'ui_devices_update_automatically_12cc3ba8'
              )}</strong
            >
            <p>{$t('ui_kaede_watches_for_devices_being_connected_or__8cf01103')}</p>
          </div>
          {#if devicesUpdatedAt}<small
              >{$t('ui_last_checked_value0_331f2cb8', {
                value0: String(
                  devicesUpdatedAt.toLocaleTimeString([], {
                    hour: '2-digit',
                    minute: '2-digit'
                  })
                )
              })}</small
            >{/if}
        </div>
        <div class="native-device-grid">
          <NativeDevicePicker
            label={$t('ui_input_device_42b6a9e8')}
            description={$t('ui_microphone_used_for_voice_and_calls_8bd190b9')}
            icon="microphone"
            selectedId={preferences.input_device?.id ?? ''}
            options={devices.inputs}
            onSelect={(id) => (preferences!.input_device = devicePreference(id, devices.inputs))}
          />
          <NativeDevicePicker
            label={$t('ui_output_device_b2fad20f')}
            description={$t('ui_speakers_or_headphones_used_for_call_audio_b54bd7d3')}
            icon="volume"
            selectedId={preferences.output_device?.id ?? ''}
            options={devices.outputs}
            onSelect={(id) => (preferences!.output_device = devicePreference(id, devices.outputs))}
          />
          <NativeDevicePicker
            label={$t('ui_camera_03494b0d')}
            description={$t('ui_video_source_used_when_your_camera_is_enabled_90462417')}
            icon="video"
            selectedId={preferences.camera_device?.id ?? ''}
            options={devices.cameras}
            onSelect={(id) => (preferences!.camera_device = devicePreference(id, devices.cameras))}
          />
          <NativeDevicePicker
            label={$t('ui_screen_or_window_34a4df8a')}
            description={$t('ui_preferred_source_for_screen_sharing_31e49e29')}
            icon="screen"
            selectedId={preferences.screen_source?.id ?? ''}
            defaultLabel="Ask when sharing"
            options={devices.screens}
            onSelect={(id) => (preferences!.screen_source = devicePreference(id, devices.screens))}
          />
        </div>
        <div class="native-device-grid">
          <label class="form-field">
            <span>{$t('ui_outgoing_audio_quality_c8cb11c2')}</span>
            <small>{$t('ui_sets_the_maximum_opus_bitrate_network_conditi_8557a159')}</small>
            <select bind:value={preferences.audio_quality}>
              <option value="data_saver">{$t('ui_data_saver_24_kbps_5d1d7d97')}</option>
              <option value="standard">{$t('ui_standard_48_kbps_0d132cbe')}</option>
              <option value="high">{$t('ui_high_96_kbps_757036d5')}</option>
              <option value="studio">{$t('ui_studio_128_kbps_11fb3fa3')}</option>
            </select>
          </label>
          <label class="form-field">
            <span>{$t('ui_default_screen_share_quality_ab2b758d')}</span>
            <small>{$t('ui_you_can_change_this_again_before_each_share_07cf89c9')}</small>
            <select bind:value={preferences.screen_share_profile}>
              <option value="data_saver">{$t('ui_720p_15_fps_cc7985b4')}</option>
              <option value="smooth">{$t('ui_720p_30_fps_b1baa0ae')}</option>
              <option value="sharp">{$t('ui_1080p_30_fps_e352ff7c')}</option>
              <option value="source">{$t('ui_source_30_fps_36ae2494')}</option>
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
            {testingInput ? $t('ui_listening_bbb4106e') : $t('ui_test_microphone_4875bbe1')}
          </button>
          <button
            class="secondary-button"
            type="button"
            disabled={testingOutput}
            onclick={() => void testOutput()}
          >
            {testingOutput ? $t('ui_playing_a9fcbd2b') : $t('ui_test_output_e6e383e6')}
          </button>
        </div>
        <fieldset class="native-input-mode">
          <legend>{$t('ui_input_mode_3fa818f2')}</legend>
          <label
            ><input type="radio" bind:group={preferences.input_mode} value="voice_activity" />
            {$t('ui_voice_activity_5a16bda1')}</label
          >
          <label
            ><input type="radio" bind:group={preferences.input_mode} value="push_to_talk" />
            {$t('ui_push_to_talk_03fc694c')}</label
          >
        </fieldset>
        {#if preferences.input_mode === 'voice_activity'}
          <label class="form-field">
            <span>{$t('ui_voice_activity_sensitivity_053e149f')}</span>
            <small>{$t('ui_higher_values_ignore_more_background_noise_40a2b263')}</small>
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
            <span>{$t('ui_push_to_talk_shortcut_ea17fe61')}</span>
            <small>{$t('ui_global_shortcuts_may_be_unavailable_under_way_546d7a8a')}</small>
            <input
              bind:value={preferences.push_to_talk_hotkey}
              placeholder={$t('ui_ctrl_shift_space_e7302a99')}
            />
          </label>
          <label class="form-field">
            <span>{$t('ui_priority_push_to_talk_shortcut_25230d9b')}</span>
            <small> {$t('ui_available_while_using_push_to_talk_when_your__5cbdb3ca')} </small>
            <input
              bind:value={preferences.priority_push_to_talk_hotkey}
              placeholder={$t('ui_ctrl_alt_space_8857c46c')}
            />
            <small>{hotkeyStatus}</small>
          </label>
        {/if}
        <div class="native-device-grid">
          <label class="form-field">
            <span>{$t('ui_noise_suppression_a5c124c1')}</span>
            <select bind:value={preferences.noise_suppression}>
              <option value="off">{$t('ui_off_ca7981b4')}</option>
              <option value="standard">{$t('ui_standard_ef669154')}</option>
              <option value="voice_isolation">{$t('ui_voice_isolation_bbd8f7c2')}</option>
            </select>
          </label>
          <div class="toggle-list">
            <label class="toggle-row"
              ><span
                ><strong>{$t('ui_opus_discontinuous_transmission_d7a0e7e0')}</strong><small
                  >{$t('ui_reduces_outgoing_bandwidth_while_you_are_not__7eec99b3')}</small
                ></span
              ><input type="checkbox" bind:checked={preferences.opus_dtx} /></label
            >
            <label class="toggle-row"
              ><span
                ><strong>{$t('ui_echo_cancellation_311baa5f')}</strong><small
                  >{$t('ui_uses_speaker_audio_as_the_far_end_reference_33a60461')}</small
                ></span
              ><input type="checkbox" bind:checked={preferences.echo_cancellation} /></label
            >
            <label class="toggle-row"
              ><span
                ><strong>{$t('ui_automatic_gain_e076a1de')}</strong><small
                  >{$t('ui_keeps_speech_at_a_consistent_level_bbf4e81d')}</small
                ></span
              ><input type="checkbox" bind:checked={preferences.automatic_gain_control} /></label
            >
          </div>
        </div>
        <label class="form-field native-input-meter">
          <span>{$t('ui_input_level_17b694d7')}</span>
          <small>{$t('ui_shown_while_connected_to_voice_audio_never_cr_1d34668a')}</small>
          <meter min="0" max="1" value={inputLevel}></meter>
        </label>
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        {#if notice}<p class="settings-helper" role="status">{notice}</p>{/if}
        <div class="form-actions">
          <button class="primary-button" disabled={saving}
            >{saving ? $t('ui_saving_23e39291') : $t('ui_save_voice_settings_aa805aa2')}</button
          >
        </div>
      </form>
    {/if}
  </section>
{/if}
