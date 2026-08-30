<script lang="ts">
  import { ApiError, api, userErrorMessage } from '$lib/api/client';
  import {
    listUserApplicationInstallations,
    userApplicationCanParticipateInEncryptedDm,
    type UserApplicationInstallation
  } from '$lib/chat/application-installations';
  import {
    dmBotE2eeParticipationPath,
    type DmBotE2eeParticipation
  } from '$lib/e2ee/bot-participation';

  let {
    open,
    channelRef,
    channelName,
    onClose
  }: {
    open: boolean;
    channelRef: string;
    channelName: string;
    onClose: () => void;
  } = $props();

  let installations = $state<UserApplicationInstallation[]>([]);
  let selectedApplicationRef = $state('');
  let participation = $state<DmBotE2eeParticipation | null>(null);
  let loading = $state(false);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let loadedKey = '';

  const eligible = $derived(installations.filter(userApplicationCanParticipateInEncryptedDm));
  const selected = $derived(
    eligible.find((item) => item.application_ref === selectedApplicationRef) ?? null
  );

  function path(): string {
    return dmBotE2eeParticipationPath(channelRef, selectedApplicationRef);
  }

  async function loadParticipation() {
    participation = null;
    error = '';
    notice = '';
    if (!selectedApplicationRef) return;
    loading = true;
    try {
      participation = await api<DmBotE2eeParticipation>(path());
    } catch (caught) {
      if (!(caught instanceof ApiError && caught.code === 'BOT_E2EE_PARTICIPATION_NOT_FOUND')) {
        error = userErrorMessage(caught, 'Could not load encrypted app consent.');
      }
    } finally {
      loading = false;
    }
  }

  async function initialize() {
    const key = `${channelRef}:${open}`;
    if (!open || key === loadedKey) return;
    loadedKey = key;
    loading = true;
    error = '';
    notice = '';
    try {
      const loaded = await listUserApplicationInstallations();
      installations = loaded;
      selectedApplicationRef =
        loaded.find(userApplicationCanParticipateInEncryptedDm)?.application_ref ?? '';
      await loadParticipation();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load your participant-capable apps.');
      loading = false;
    }
  }

  async function choose(applicationRef: string) {
    selectedApplicationRef = applicationRef;
    await loadParticipation();
  }

  async function consent() {
    if (
      busy ||
      !selected ||
      !confirm(
        `Consent to add ${selected.application_name} to ${channelName}? Every human participant must separately consent. Once all agree, verified app devices can decrypt future messages after the displayed history floor. The app may retain anything it receives.`
      )
    )
      return;
    busy = true;
    error = '';
    notice = '';
    try {
      participation = await api<DmBotE2eeParticipation>(path(), { method: 'PUT' });
      notice =
        participation.consent_state === 'active'
          ? 'Everyone consented. App devices will join after the room rekeys.'
          : 'Your consent was recorded. The app remains blocked until every participant consents.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not record encrypted app consent.');
    } finally {
      busy = false;
    }
  }

  async function revoke() {
    if (
      busy ||
      !selected ||
      !confirm(
        `Remove ${selected.application_name} from this encrypted conversation? Kaede will rekey the room. This stops future access but cannot erase content already delivered to the app.`
      )
    )
      return;
    busy = true;
    error = '';
    notice = '';
    try {
      participation = await api<DmBotE2eeParticipation>(path(), { method: 'DELETE' });
      notice = 'App access was revoked and a room rekey was staged.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not revoke encrypted app access.');
    } finally {
      busy = false;
    }
  }

  $effect(() => {
    void initialize();
  });
</script>

{#if open}
  <div
    class="backdrop"
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && onClose()}
  >
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dm-apps-title">
      <header>
        <div>
          <p>End-to-end encryption</p>
          <h2 id="dm-apps-title">Apps in this conversation</h2>
        </div>
        <button class="close" type="button" aria-label="Close" onclick={onClose}>×</button>
      </header>
      <p class="warning">
        An app becomes another cryptographic participant. Every person must consent separately;
        installation alone grants nothing. Revocation rotates keys but cannot recall data already
        decrypted by the app operator.
      </p>
      {#if eligible.length}
        <label>
          <span>Authorized participant app</span>
          <select
            value={selectedApplicationRef}
            disabled={loading || busy}
            onchange={(event) => void choose(event.currentTarget.value)}
          >
            {#each eligible as installation (installation.id)}
              <option value={installation.application_ref}>{installation.application_name}</option>
            {/each}
          </select>
        </label>
      {:else if !loading}
        <p class="muted">
          No participant-capable app is authorized for your account and private conversations. Add
          one from its reviewed Add App flow, then return here.
        </p>
      {/if}
      {#if loading}<p class="muted" role="status">Checking consent…</p>{/if}
      {#if participation}
        <div class="status">
          <strong>{participation.consent_state}</strong>
          <small>
            {participation.history_floor_message_ref
              ? `No app history before ${participation.history_floor_message_ref}`
              : 'No app access to messages sent before full consent'}
          </small>
          <ul>
            {#each participation.participants as participant (participant.user_ref)}
              <li>
                <code>{participant.user_ref}</code>
                <span>{participant.consented ? 'Consented' : 'Waiting for consent'}</span>
              </li>
            {/each}
          </ul>
          {#if participation.devices.length}
            <details>
              <summary>Verified app devices</summary>
              {#each participation.devices as device (device.device_id)}
                <code>{device.device_id} · {device.status} · epoch {device.joined_epoch}</code>
              {/each}
            </details>
          {/if}
        </div>
      {/if}
      {#if error}<p class="error" role="alert">{error}</p>{/if}
      {#if notice}<p class="notice" role="status">{notice}</p>{/if}
      <footer>
        <button class="secondary" type="button" onclick={onClose}>Done</button>
        {#if selected}
          {#if participation && participation.consent_state !== 'revoked'}
            <button class="danger" type="button" disabled={busy || loading} onclick={revoke}>
              {busy ? 'Removing…' : 'Remove app'}
            </button>
          {:else}
            <button type="button" disabled={busy || loading} onclick={consent}>
              {busy ? 'Recording…' : 'Consent to add'}
            </button>
          {/if}
        {/if}
      </footer>
    </div>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    z-index: 120;
    display: grid;
    place-items: center;
    padding: 1rem;
    background: rgb(0 0 0 / 0.65);
  }
  .dialog {
    width: min(36rem, 100%);
    max-height: min(48rem, 92vh);
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1rem;
    color: var(--text);
    background: var(--surface);
  }
  header,
  footer,
  li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }
  h2,
  header p,
  .warning {
    margin: 0;
  }
  header p,
  .muted,
  small {
    color: var(--text-muted);
  }
  .close {
    color: var(--text);
    background: transparent;
    font-size: 1.5rem;
  }
  .warning,
  .status {
    margin-top: 0.85rem;
    border-radius: 9px;
    padding: 0.75rem;
    background: var(--surface-hover);
  }
  label,
  .status,
  details {
    display: grid;
    gap: 0.4rem;
  }
  label {
    margin-top: 0.85rem;
    font-weight: 750;
  }
  select {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.65rem;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
  }
  ul {
    display: grid;
    gap: 0.35rem;
    margin: 0.25rem 0;
    padding: 0;
    list-style: none;
  }
  code {
    overflow-wrap: anywhere;
  }
  details code {
    display: block;
    margin-top: 0.35rem;
  }
  footer {
    margin-top: 1rem;
    justify-content: flex-end;
  }
  button {
    border: 0;
    border-radius: 8px;
    padding: 0.65rem 0.85rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 800;
  }
  button.secondary,
  button.danger {
    border: 1px solid var(--line);
    color: var(--text);
    background: transparent;
  }
  button.danger {
    border-color: var(--danger, #d84a4a);
    color: var(--danger, #ef6767);
  }
  button:disabled {
    opacity: 0.55;
  }
  .error {
    color: var(--danger, #ef6767);
  }
  .notice {
    color: var(--success, #70ba9d);
  }
</style>
