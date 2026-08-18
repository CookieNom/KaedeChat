<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import type { UserSummary } from '$lib/chat/types';
  import { initializeE2EE, resetE2EEClient } from '$lib/e2ee/client';
  import {
    isCanonicalRecoveryAuthorization,
    recoveryRestoreAvailability,
    restoreRecoveredIdentity
  } from '$lib/e2ee/recovery-restore';
  import {
    clearVaultCheckpoint,
    clearDeviceState,
    exportRecoveryBundle,
    importRecoveryBundle,
    saveDeviceState,
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
  interface EncryptionResetResult {
    status: 'encryption_reset';
    account_ref: string;
    recovery_authorization: string;
    recovery_authorization_expires_in: number;
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
  const restoreAvailability = $derived(
    recoveryRestoreAvailability(busy, passphrase, currentDeviceId)
  );

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
      try {
        devices = (await api<DeviceList>('/e2ee/devices')).devices;
      } catch {
        devices = [];
      }
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

  async function resetRemoteEncryption(): Promise<string> {
    const result = await api<EncryptionResetResult>('/e2ee/reset', {
      method: 'POST',
      body: JSON.stringify({ confirmation: 'RESET ENCRYPTED HISTORY' })
    });
    if (
      !result ||
      Object.keys(result).sort().join('\0') !==
        'account_ref\0recovery_authorization\0recovery_authorization_expires_in\0status' ||
      result.status !== 'encryption_reset' ||
      result.account_ref !== accountRef ||
      !isCanonicalRecoveryAuthorization(result.recovery_authorization) ||
      result.recovery_authorization_expires_in !== 300
    ) {
      throw new Error('The encryption-reset response was invalid. Local keys were not changed.');
    }
    // A rollback checkpoint may be lowered only after this authenticated reset
    // response confirms that the remote vault and digest ledger were cleared.
    await clearVaultCheckpoint(accountRef);
    return result.recovery_authorization;
  }

  async function importBackup(file: File) {
    if (!restoreAvailability.enabled) return;
    busy = true;
    error = '';
    notice = '';
    try {
      if (file.size > 48 * 1024 * 1024) throw new Error('Recovery backup is too large.');
      const bundle = JSON.parse(await file.text()) as RecoveryBundle;
      const recovered = await importRecoveryBundle(accountRef, passphrase, bundle);
      if (
        !window.confirm(
          'Restore this backup? Kaede will discard the encryption identity currently loaded here, replace the remote encrypted vault, and revoke the current portable identity record before re-enrolling the recovered identity. Rooms may pause for key rotation.'
        )
      ) {
        return;
      }
      const client = await restoreRecoveredIdentity(user, recovered, {
        resetClient: resetE2EEClient,
        authorizeReset: resetRemoteEncryption,
        clearLocalState: clearDeviceState,
        saveRecoveredState: saveDeviceState,
        initializeRecoveredIdentity: (profile, recoveryAuthorization) =>
          initializeE2EE(profile, { recoveryAuthorization })
      });
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

  async function startFresh() {
    if (busy) return;
    if (
      !window.confirm(
        'Start a new encryption identity? Any encrypted history that is not available in local storage or a recovery backup will be permanently unreadable.'
      )
    )
      return;
    busy = true;
    error = '';
    notice = '';
    try {
      await resetE2EEClient();
      await resetRemoteEncryption();
      await clearDeviceState(accountRef);
      const client = await initializeE2EE(user);
      currentDeviceId = client.deviceId;
      devices = (await api<DeviceList>('/e2ee/devices')).devices;
      notice = 'A new encryption identity was created. Encrypted rooms must rotate their keys.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not reset the encryption identity.');
    } finally {
      busy = false;
    }
  }
</script>

<div class="settings-card security-card e2ee-card">
  <div class="security-label">
    <div>
      <strong>End-to-end encryption identity</strong>
      <p>
        Your account has one portable MLS identity. Your password unlocks its encrypted keys on each
        signed-in client; Kaede syncs only ciphertext and cannot read those keys.
      </p>
    </div>
    <span>{devices.some((device) => !device.revoked_at) ? 'Active' : 'Not active'}</span>
  </div>

  <div class="e2ee-devices">
    {#each devices as device (device.id)}
      <div class:revoked={Boolean(device.revoked_at)}>
        <span>
          <strong
            >Portable account identity{device.id === currentDeviceId
              ? ' · Loaded here'
              : ''}</strong
          >
          <small
            >Last enrolled from {device.device_name} ({device.platform}) ·
            {device.available_key_packages ?? 0} ready key packages</small
          >
        </span>
        {#if device.revoked_at}
          <small>Revoked</small>
        {:else}
          <button class="secondary-button" type="button" disabled={busy} onclick={startFresh}
            >Rotate identity…</button
          >
        {/if}
      </div>
    {/each}
  </div>

  <div class="security-flow">
    <strong>Encrypted recovery backup</strong>
    <p>
      The backup contains private message keys. It is encrypted locally with your passphrase; the
      passphrase is never sent to Kaede. Keep it for password recovery or loss of the synchronized
      vault, and store the file and passphrase separately. Automatic sync keeps the newest 2,000
      decrypted messages or 8 MiB; older plaintext may require an existing trusted client or this
      recovery backup.
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
        disabled={!restoreAvailability.enabled}
        onclick={() => importFile?.click()}
        >{restoreAvailability.replacesActiveIdentity
          ? 'Replace identity from backup…'
          : 'Restore backup'}</button
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
    <small>
      A restore is available even when an identity is loaded. After explicit confirmation, it
      replaces that identity and the synchronized encrypted vault with the backup.
    </small>
  </div>
  <div class="security-flow danger-flow">
    <strong>Start a new encryption identity</strong>
    <p>
      Use this only when neither automatic vault recovery nor a recovery backup is available. It
      revokes the current encryption identity and permanently abandons unavailable encrypted
      history. Encrypted rooms will pause until their keys are rotated.
    </p>
    <div class="form-actions">
      <button
        class="secondary-button danger-button"
        type="button"
        disabled={busy}
        onclick={startFresh}>Start fresh…</button
      >
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
