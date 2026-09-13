<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { ApiError, api, userErrorMessage } from '$lib/api/client';
  import { entityRef } from '$lib/chat/refs';
  import type { Channel } from '$lib/chat/types';
  import {
    botE2eeHistoryNotice,
    botE2eeParticipationPath,
    type BotE2eeParticipation
  } from '$lib/e2ee/bot-participation';
  import { onMount } from 'svelte';

  let {
    guildRef,
    applicationRef,
    applicationName,
    channels,
    canManage
  }: {
    guildRef: string;
    applicationRef: string;
    applicationName: string;
    channels: Channel[];
    canManage: boolean;
  } = $props();

  const encryptedChannels = $derived(
    channels.filter((channel) => channel.encryption_mode === 'e2ee')
  );
  let selectedChannelRef = $state('');
  let participation = $state<BotE2eeParticipation | null>(null);
  let loading = $state(false);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');

  function path(): string {
    return botE2eeParticipationPath(guildRef, selectedChannelRef, applicationRef);
  }

  async function load() {
    if (!selectedChannelRef) {
      participation = null;
      return;
    }
    loading = true;
    error = '';
    notice = '';
    try {
      participation = await api<BotE2eeParticipation>(path());
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'BOT_E2EE_PARTICIPATION_NOT_FOUND') {
        participation = null;
      } else {
        error = userErrorMessage(
          caught,
          $t('ui_could_not_load_encrypted_app_access_for_this__f15b15b7')
        );
      }
    } finally {
      loading = false;
    }
  }

  async function selectChannel(value: string) {
    if (value === selectedChannelRef) return;
    selectedChannelRef = value;
    await load();
  }

  async function grant() {
    if (
      busy ||
      !selectedChannelRef ||
      !confirm(
        `Allow ${applicationName}'s verified devices to join this encrypted channel? The app can decrypt future messages and messages after each device's displayed history floor. Removing access triggers another room rekey but cannot erase data the app already received.`
      )
    ) {
      return;
    }
    const reason = prompt('Audit-log reason (optional)', '') ?? '';
    busy = true;
    error = '';
    notice = '';
    try {
      participation = await api<BotE2eeParticipation>(path(), {
        method: 'PUT',
        headers: reason.trim() ? { 'X-Audit-Log-Reason': reason.trim() } : undefined
      });
      notice = $t('ui_access_is_staged_pending_devices_become_activ_f3af9b4e');
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_grant_encrypted_channel_access_52744544'));
    } finally {
      busy = false;
    }
  }

  async function revoke() {
    if (
      busy ||
      !selectedChannelRef ||
      !confirm(
        `Revoke ${applicationName}'s access to this encrypted channel? Kaede will rekey the room. This stops future access but cannot erase messages the app already decrypted.`
      )
    ) {
      return;
    }
    const reason = prompt('Audit-log reason (optional)', '') ?? '';
    busy = true;
    error = '';
    notice = '';
    try {
      await api<BotE2eeParticipation>(path(), {
        method: 'DELETE',
        headers: reason.trim() ? { 'X-Audit-Log-Reason': reason.trim() } : undefined
      });
      participation = null;
      notice = $t('ui_encrypted_channel_access_was_revoked_and_the__71b57ff4');
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_revoke_encrypted_channel_access_f3372ade'));
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    selectedChannelRef = encryptedChannels[0] ? entityRef(encryptedChannels[0]) : '';
    void load();
  });
</script>

<section class="bot-e2ee" aria-labelledby={`bot-e2ee-${applicationRef}`}>
  <h3 id={`bot-e2ee-${applicationRef}`}>{$t('ui_encrypted_channel_access_7013fcf4')}</h3>
  <p>{$t('ui_participant_mode_lets_verified_app_devices_jo_02f81dc7')}</p>
  {#if encryptedChannels.length === 0}
    <small>{$t('ui_this_server_has_no_end_to_end_encrypted_chann_a5513693')}</small>
  {:else}
    <label>
      <span>{$t('ui_channel_ce4683e7')}</span>
      <select
        value={selectedChannelRef}
        disabled={loading || busy}
        onchange={(event) => void selectChannel(event.currentTarget.value)}
      >
        {#each encryptedChannels as channel (entityRef(channel))}
          <option value={entityRef(channel)}>#{channel.name ?? 'encrypted-channel'}</option>
        {/each}
      </select>
    </label>
    {#if loading}
      <small role="status">{$t('ui_checking_participant_devices_128aadb8')}</small>
    {:else if participation?.devices.length}
      <div class="devices">
        {#each participation.devices as device (device.device_id)}
          <div>
            <strong>{device.status}</strong>
            <code>{device.device_id}</code>
            <small>
              {$t('ui_value0_consent_generation_value1_joined_epoch_43c8dc59', {
                value0: String(botE2eeHistoryNotice(device)),
                value1: String(device.consent_generation),
                value2: String(device.joined_epoch)
              })}
            </small>
          </div>
        {/each}
      </div>
    {:else}
      <small>{$t('ui_the_app_is_not_a_participant_in_this_channel_807a398e')}</small>
    {/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    {#if notice}<p class="notice" role="status">{notice}</p>{/if}
    {#if canManage}
      <div class="actions">
        {#if participation?.devices.some((device) => device.status !== 'revoked')}
          <button class="danger" type="button" disabled={busy || loading} onclick={revoke}>
            {busy ? $t('ui_revoking_1a36f21b') : $t('ui_revoke_access_ab292ddb')}
          </button>
        {:else}
          <button type="button" disabled={busy || loading} onclick={grant}>
            {busy ? $t('ui_granting_c0668408') : $t('ui_allow_in_channel_07df133d')}
          </button>
        {/if}
      </div>
    {/if}
    <p class="warning">{$t('ui_the_app_receives_plaintext_only_on_its_verifi_d56e599b')}</p>
  {/if}
</section>

<style>
  .bot-e2ee {
    margin-top: 1rem;
    border-top: 1px solid var(--line);
    padding-top: 0.85rem;
  }
  h3,
  p {
    margin: 0.25rem 0;
  }
  label,
  .devices,
  .devices div {
    display: grid;
    gap: 0.35rem;
  }
  label {
    margin-top: 0.75rem;
    font-weight: 750;
  }
  select {
    width: min(28rem, 100%);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.65rem;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
  }
  .devices {
    margin-top: 0.75rem;
  }
  .devices div {
    border-radius: 8px;
    padding: 0.65rem;
    background: var(--surface-hover);
  }
  .devices code {
    overflow-wrap: anywhere;
  }
  .actions {
    margin-top: 0.75rem;
  }
  button {
    border: 0;
    border-radius: 8px;
    padding: 0.65rem 0.8rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 800;
  }
  button.danger {
    border: 1px solid var(--danger, #d84a4a);
    color: var(--danger, #ef6767);
    background: transparent;
  }
  button:disabled {
    opacity: 0.55;
  }
  .warning {
    margin-top: 0.75rem;
    color: var(--text-muted);
    font-size: 0.78rem;
  }
  .error {
    color: var(--danger, #ef6767);
  }
  .notice {
    color: var(--success, #70ba9d);
  }
</style>
