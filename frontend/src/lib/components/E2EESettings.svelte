<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import type { UserSummary } from '$lib/chat/types';
  import { initializeE2EE, resetE2EEClient } from '$lib/e2ee/client';
  import {
    clearDeviceState,
    exportRecoveryBundle,
    importRecoveryBundle,
    type RecoveryBundle
  } from '$lib/e2ee/store';
  import { onMount } from 'svelte';

  let { user }: { user: UserSummary } = $props();
  interface Device {
    id: string;
    device_name: string;
    platform: string;
    trust_state: string;
    created_at: string;
    last_seen_at: string;
    revoked_at: string | null;
    available_key_packages?: number;
  }
  interface DeviceList {
    generation: string;
    devices: Device[];
  }

  let devices = $state<Device[]>([]);
  let currentDeviceId = $state('');
  let passphrase = $state('');
  let confirmPassphrase = $state('');
  let importFile = $state<HTMLInputElement | null>(null);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  const accountRef = $derived(`${user.id}@${user.origin_domain}`);

  onMount(() => void load());

  async function load() {
    busy = true;
    error = '';
    try {
      const client = await initializeE2EE(user);
      currentDeviceId = client.deviceId;
      devices = (await api<DeviceList>('/e2ee/devices')).devices;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not initialize encryption on this device.');
    } finally {
      busy = false;
    }
  }

  async function exportBackup() {
    if (busy || passphrase.length < 12 || passphrase !== confirmPassphrase) return;
    busy = true;
    error = '';
    notice = '';
    try {
      const bundle = await exportRecoveryBundle(accountRef, passphrase);
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(bundle)], { type: 'application/json' })
      );
      try {
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `kaede-e2ee-recovery-${user.origin_domain}.json`;
        anchor.click();
      } finally {
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      passphrase = '';
      confirmPassphrase = '';
      notice = 'Encrypted recovery backup downloaded. Store the file and passphrase separately.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the encrypted recovery backup.');
    } finally {
      busy = false;
    }
  }

  async function importBackup(file: File) {
    if (busy || passphrase.length < 12) return;
    busy = true;
    error = '';
    notice = '';
    try {
      if (file.size > 48 * 1024 * 1024) throw new Error('Recovery backup is too large.');
      const bundle = JSON.parse(await file.text()) as RecoveryBundle;
      await importRecoveryBundle(accountRef, passphrase, bundle);
      resetE2EEClient();
      const client = await initializeE2EE(user);
      currentDeviceId = client.deviceId;
      devices = (await api<DeviceList>('/e2ee/devices')).devices;
      passphrase = '';
      notice = 'Encryption keys restored on this device.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not restore that recovery backup.');
    } finally {
      busy = false;
      if (importFile) importFile.value = '';
    }
  }

  async function revoke(device: Device) {
    if (busy || device.revoked_at) return;
    if (
      !window.confirm(
        `Revoke “${device.device_name}”? It will immediately lose future encrypted access.`
      )
    )
      return;
    busy = true;
    error = '';
    try {
      await api(`/e2ee/devices/${encodeURIComponent(device.id)}`, { method: 'DELETE' });
      if (device.id === currentDeviceId) {
        await clearDeviceState(accountRef);
        resetE2EEClient();
        currentDeviceId = '';
      }
      devices = (await api<DeviceList>('/e2ee/devices')).devices;
      notice =
        'Encryption device revoked. Encrypted rooms must rotate keys before it is fully excluded.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not revoke that encryption device.');
    } finally {
      busy = false;
    }
  }
</script>

<div class="settings-card security-card e2ee-card">
  <div class="security-label">
    <div>
      <strong>End-to-end encryption devices</strong>
      <p>
        Kaede stores this device’s MLS keys in encrypted browser storage. A recovery backup is the
        only way to restore encrypted history if every enrolled device is lost.
      </p>
    </div>
    <span>{devices.filter((device) => !device.revoked_at).length} active</span>
  </div>

  <div class="e2ee-devices">
    {#each devices as device (device.id)}
      <div class:revoked={Boolean(device.revoked_at)}>
        <span>
          <strong
            >{device.device_name}{device.id === currentDeviceId ? ' · This device' : ''}</strong
          >
          <small>{device.platform} · {device.available_key_packages ?? 0} ready key packages</small>
        </span>
        {#if device.revoked_at}
          <small>Revoked</small>
        {:else}
          <button
            class="secondary-button"
            type="button"
            disabled={busy}
            onclick={() => revoke(device)}>Revoke</button
          >
        {/if}
      </div>
    {/each}
  </div>

  <div class="security-flow">
    <strong>Encrypted recovery backup</strong>
    <p>
      The backup contains private message keys. It is encrypted locally with your passphrase; the
      passphrase is never sent to Kaede. Use at least 12 characters and store both separately.
    </p>
    <label>
      Backup passphrase
      <input type="password" bind:value={passphrase} minlength="12" autocomplete="new-password" />
    </label>
    <label>
      Confirm passphrase
      <input
        type="password"
        bind:value={confirmPassphrase}
        minlength="12"
        autocomplete="new-password"
      />
    </label>
    <div class="form-actions">
      <button
        class="secondary-button"
        type="button"
        disabled={busy || passphrase.length < 12 || passphrase !== confirmPassphrase}
        onclick={exportBackup}>Download backup</button
      >
      <button
        class="secondary-button"
        type="button"
        disabled={busy || passphrase.length < 12}
        onclick={() => importFile?.click()}>Restore backup</button
      >
      <input
        class="visually-hidden"
        bind:this={importFile}
        type="file"
        accept="application/json,.json"
        onchange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) void importBackup(file);
        }}
      />
    </div>
  </div>
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if notice}<p class="form-success" role="status">{notice}</p>{/if}
</div>

<style>
  .e2ee-card,
  .e2ee-devices,
  .security-flow,
  .security-flow label {
    display: grid;
    gap: 0.75rem;
  }
  .security-label,
  .e2ee-devices > div,
  .form-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  p,
  small,
  .revoked {
    color: var(--text-muted);
  }
  p {
    margin: 0.3rem 0 0;
  }
  .e2ee-devices > div {
    border-top: 1px solid var(--line);
    padding-top: 0.75rem;
  }
  .e2ee-devices span {
    display: grid;
    gap: 0.2rem;
  }
  @media (max-width: 640px) {
    .security-label,
    .e2ee-devices > div,
    .form-actions {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
