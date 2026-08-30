<script lang="ts">
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
        error = userErrorMessage(caught, 'Could not load encrypted app access for this channel.');
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
      notice = 'Access is staged. Pending devices become active after the encrypted room rekeys.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not grant encrypted channel access.');
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
      notice = 'Encrypted channel access was revoked and the room rekey was staged.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not revoke encrypted channel access.');
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
  <h3 id={`bot-e2ee-${applicationRef}`}>Encrypted channel access</h3>
  <p>
    Participant mode lets verified app devices join a channel's MLS room. Consent is per channel,
    auditable, and always triggers a room rekey.
  </p>
  {#if encryptedChannels.length === 0}
    <small>This server has no end-to-end encrypted channels.</small>
  {:else}
    <label>
      <span>Channel</span>
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
      <small role="status">Checking participant devices…</small>
    {:else if participation?.devices.length}
      <div class="devices">
        {#each participation.devices as device (device.device_id)}
          <div>
            <strong>{device.status}</strong>
            <code>{device.device_id}</code>
            <small>
              {botE2eeHistoryNotice(device)} · consent generation {device.consent_generation} · joined
              epoch {device.joined_epoch}
            </small>
          </div>
        {/each}
      </div>
    {:else}
      <small>The app is not a participant in this channel.</small>
    {/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    {#if notice}<p class="notice" role="status">{notice}</p>{/if}
    {#if canManage}
      <div class="actions">
        {#if participation?.devices.some((device) => device.status !== 'revoked')}
          <button class="danger" type="button" disabled={busy || loading} onclick={revoke}>
            {busy ? 'Revoking…' : 'Revoke access'}
          </button>
        {:else}
          <button type="button" disabled={busy || loading} onclick={grant}>
            {busy ? 'Granting…' : 'Allow in channel'}
          </button>
        {/if}
      </div>
    {/if}
    <p class="warning">
      The app receives plaintext only on its verified participant devices. Revocation prevents new
      decryptions; it cannot recall content already delivered or override the displayed history
      floor.
    </p>
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
