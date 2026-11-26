<script lang="ts">
  import Toast from '$lib/components/Toast.svelte';
  import VoiceQuickControls from '$lib/voice/VoiceQuickControls.svelte';
  import { tick } from 'svelte';
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import type { UserSummary } from '$lib/chat/types';
  import { assetUrl } from '$lib/media/assets';
  import { isNativeDesktop } from '$lib/platform/native';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import {
    presenceControl,
    type PresencePreference,
    type PresenceStatePayload
  } from '$lib/stores/presence.svelte';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import PresencePicker from './PresencePicker.svelte';
  import PresenceSchedulesPanel from './PresenceSchedulesPanel.svelte';
  import Icon from './Icon.svelte';

  let {
    user,
    presence = 'online',
    onPresenceChange
  }: {
    user: UserSummary | null;
    presence?: PresencePreference;
    onPresenceChange?: (value: PresencePreference) => void;
  } = $props();
  const id = $props.id();
  let panel = $state<HTMLElement | null>(null);
  let trigger = $state<HTMLButtonElement | null>(null);
  let profile = $state<UserSummary | null>(null);
  let open = $state(false);
  let schedulesOpen = $state(false);
  let schedulesDialog = $state<HTMLDialogElement | null>(null);
  $effect(() => {
    if (schedulesDialog && !schedulesDialog.open) schedulesDialog.showModal();
  });
  let editing = $state(false);
  let status = $state('');
  let preference = $state<PresencePreference>('online');
  let busy = $state(false);
  let loading = $state(false);
  let error = $state('');
  let notice = $state('');
  let left = $state(12);
  let bottom = $state(80);
  let generation = 0;
  const displayed = $derived(profile?.id === user?.id ? profile : user);
  const effectivePreference = $derived(presenceControl.state?.effective_status ?? preference);

  async function opened(event: Event) {
    open = (event as ToggleEvent).newState === 'open';
    const current = ++generation;
    if (!open) return;
    const bounds = trigger?.getBoundingClientRect();
    left = Math.max(8, Math.min(bounds?.left ?? 12, window.innerWidth - 368));
    bottom = Math.min(window.innerHeight - 100, window.innerHeight - (bounds?.top ?? 80) + 10);
    error = '';
    notice = '';
    editing = false;
    preference = presence;
    loading = true;
    profile = user;
    status = user?.custom_status ?? '';
    try {
      const [loaded, settings] = await Promise.all([
        api<UserSummary>('/users/@me'),
        api<{
          presence_preference: PresencePreference;
          presence_state?: PresenceStatePayload;
        }>('/users/@me/settings')
      ]);
      if (current !== generation) return;
      profile = loaded;
      status = loaded.custom_status ?? '';
      preference = settings.presence_preference;
      if (settings.presence_state) {
        presenceControl.apply(settings.presence_state);
      } else {
        presenceControl.applyFallbackPreference(settings.presence_preference);
      }
    } catch (caught) {
      if (current === generation) error = userErrorMessage(caught, 'Could not load your profile.');
    } finally {
      if (current === generation) loading = false;
    }
  }

  async function saveStatus() {
    if (busy || loading || status.trim() === (displayed?.custom_status ?? '')) return;
    busy = true;
    error = '';
    try {
      profile = await api<UserSummary>('/users/@me', {
        method: 'PATCH',
        body: JSON.stringify({ custom_status: status.trim() || null })
      });
      editing = false;
      notice = 'Status saved.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save your status.');
    } finally {
      busy = false;
    }
  }

  async function changePresence(value: PresencePreference, durationMinutes?: number | null) {
    if (busy || loading) return;
    busy = true;
    error = '';
    try {
      const payload: Record<string, unknown> = { presence_preference: value };
      if (durationMinutes !== undefined) {
        payload.presence_duration_minutes = durationMinutes;
      }
      const updated = await api<{
        presence_preference?: PresencePreference;
        presence_state?: PresenceStatePayload;
      } | null>('/users/@me/settings', {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
      const resolved = updated?.presence_state?.effective_status ?? value;
      preference = resolved;
      if (updated?.presence_state) {
        presenceControl.apply(updated.presence_state);
      } else {
        presenceControl.applyFallbackPreference(resolved);
      }
      authenticatedGateway.client.rememberPresence(resolved);
      if (user) chatEntities.setPresence(user, resolved === 'invisible' ? 'offline' : resolved);
      onPresenceChange?.(resolved);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not change your presence.');
    } finally {
      busy = false;
    }
  }

  async function resetPresenceOverride() {
    if (busy || loading) return;
    busy = true;
    error = '';
    try {
      const updated = await api<{
        presence_preference: PresencePreference;
        presence_state?: PresenceStatePayload;
      }>('/users/@me/settings', {
        method: 'PATCH',
        body: JSON.stringify({ presence_reset_override: true })
      });
      preference = updated.presence_preference;
      if (updated.presence_state) {
        presenceControl.apply(updated.presence_state);
      } else {
        presenceControl.applyFallbackPreference(updated.presence_preference);
      }
      authenticatedGateway.client.rememberPresence(updated.presence_preference);
      if (user) {
        chatEntities.setPresence(
          user,
          updated.presence_preference === 'invisible' ? 'offline' : updated.presence_preference
        );
      }
      onPresenceChange?.(updated.presence_preference);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not reset your presence.');
    } finally {
      busy = false;
    }
  }

  function openSchedulesModal() {
    panel?.hidePopover?.();
    schedulesOpen = true;
  }

  async function editStatus() {
    editing = true;
    await tick();
    panel?.querySelector('input')?.focus();
  }

  async function copyId() {
    if (!user) return;
    try {
      await navigator.clipboard.writeText(user.id);
      notice = 'User ID copied.';
    } catch {
      error = 'Could not copy your user ID. Try again.';
    }
  }
</script>

<Toast message={notice} onDismiss={() => (notice = '')} />

<button
  bind:this={trigger}
  class="account-trigger"
  popovertarget={id}
  aria-expanded={open}
  aria-label="Open your profile and account menu"
  disabled={!user}
>
  <span class="avatar avatar-small">
    {#if displayed?.avatar_hash}<img
        src={assetUrl(displayed.avatar_hash, 'thumbnail_128', displayed)}
        alt=""
      />
    {:else}{displayed?.username.slice(0, 1).toUpperCase() ?? 'K'}{/if}
  </span>
  <span class="identity"
    ><strong>{displayed?.display_name ?? displayed?.username ?? 'Your account'}</strong><small
      >{displayed?.custom_status || displayed?.handle || 'Loading…'}</small
    ></span
  >
</button>

<div
  bind:this={panel}
  {id}
  popover="auto"
  ontoggle={opened}
  class="account-panel"
  style:left={`${left}px`}
  style:bottom={`${bottom}px`}
  style:max-height={`calc(100dvh - ${bottom + 8}px)`}
  role="dialog"
  aria-label="Your profile and account"
>
  <div class="banner">
    {#if displayed?.banner_hash}<img
        src={assetUrl(displayed.banner_hash, 'original', displayed)}
        alt=""
      />{/if}
    <button
      class="close"
      aria-label="Close account menu"
      popovertarget={id}
      popovertargetaction="hide">×</button
    >
  </div>
  <div class="body">
    <a
      class="avatar avatar-large profile-avatar"
      href={resolve('settings#profile')}
      aria-label="Change your profile picture"
    >
      {#if displayed?.avatar_hash}<img
          src={assetUrl(displayed.avatar_hash, 'thumbnail_128', displayed)}
          alt=""
        />
      {:else}{displayed?.username.slice(0, 1).toUpperCase() ?? 'K'}{/if}
    </a>
    <h2>{displayed?.display_name ?? displayed?.username}</h2>
    <p class="handle">{displayed?.handle}</p>
    {#if displayed?.bio}<p class="bio">{displayed.bio}</p>{/if}
    {#if editing}
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void saveStatus();
        }}
      >
        <label for={`${id}-status`}>Custom status</label>
        <input
          id={`${id}-status`}
          bind:value={status}
          maxlength="128"
          placeholder="What's on your mind?"
          disabled={busy}
        />
        <div class="status-actions">
          <button
            class="primary-button"
            disabled={busy || loading || status.trim() === (displayed?.custom_status ?? '')}
            >Save</button
          >
          <button
            type="button"
            class="secondary-button"
            disabled={busy}
            onclick={() => {
              status = '';
              void saveStatus();
            }}>Clear status</button
          >
          <button
            type="button"
            class="secondary-button"
            disabled={busy}
            onclick={() => {
              editing = false;
              status = displayed?.custom_status ?? '';
            }}>Cancel</button
          >
        </div>
      </form>
    {:else}
      <button class="status-bubble" disabled={loading || busy} onclick={editStatus}
        >{displayed?.custom_status || 'Set a custom status'}</button
      >
    {/if}
    <div class="actions">
      <a href={resolve('settings#profile')}
        ><Icon name="edit" size={18} />Edit profile / change avatar</a
      >
      <div class="presence">
        <PresencePicker
          value={effectivePreference}
          presenceState={presenceControl.state}
          disabled={busy || loading}
          onChange={changePresence}
          onResetOverride={resetPresenceOverride}
          onOpenSchedules={openSchedulesModal}
        />
      </div>
    </div>
    <div class="actions">
      {#if isNativeDesktop()}<a href={resolve('accounts')}
          ><Icon name="users" size={18} />Switch accounts <span class="chevron">›</span></a
        >{/if}
      <button onclick={copyId}><span class="id-icon">ID</span>Copy user ID</button>
      <a href={resolve('settings')}><Icon name="settings" size={18} />Account settings</a>
    </div>
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    {#if notice}<p class="feedback" role="status">{notice}</p>{/if}
  </div>
</div>

<VoiceQuickControls />

{#if schedulesOpen}
  <dialog
    bind:this={schedulesDialog}
    class="schedules-modal"
    closedby="any"
    aria-label="Presence schedules"
    onkeydown={(event) => {
      if (event.key === 'Escape' || event.key === 'Tab') event.stopPropagation();
    }}
    onclose={() => {
      schedulesOpen = false;
      trigger?.focus();
    }}
  >
    <div class="schedules-modal-header">
      <div>
        <h2>Presence schedules</h2>
        <p>Automatically change your status during sleep, work, or focus hours.</p>
      </div>
      <button
        type="button"
        class="modal-close"
        aria-label="Close presence schedules"
        onclick={() => schedulesDialog?.close()}
      >
        ×
      </button>
    </div>
    <PresenceSchedulesPanel
      compact
      onSaved={(nextState) => {
        preference = nextState.effective_status;
        onPresenceChange?.(nextState.effective_status);
      }}
    />
  </dialog>
{/if}

<style>
  .account-trigger {
    grid-column: 1;
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    padding: 4px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--text);
    text-align: left;
    cursor: pointer;
  }
  .account-trigger:hover {
    background: var(--surface-hover);
  }
  .identity {
    display: grid;
    min-width: 0;
    gap: 2px;
  }
  .identity strong,
  .identity small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .identity small {
    color: var(--text-muted);
    font-size: 0.68rem;
  }
  .account-panel {
    position: fixed;
    top: auto;
    right: auto;
    margin: 0;
    padding: 0;
    width: min(360px, calc(100vw - 16px));
    max-height: calc(100dvh - 100px);
    overflow-y: auto;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--surface-raised);
    color: var(--text);
    box-shadow: 0 20px 64px #0009;
  }
  .banner {
    height: 110px;
    position: relative;
    background: linear-gradient(125deg, var(--accent), var(--surface-hover));
  }
  .banner img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .close {
    position: absolute;
    right: 8px;
    top: 8px;
    width: 28px;
    height: 28px;
    border: 0;
    border-radius: 50%;
    background: #0009;
    color: white;
    cursor: pointer;
    font-size: 22px;
  }
  .body {
    padding: 0 16px 16px;
  }
  .profile-avatar {
    position: relative;
    margin-top: -32px;
    width: 72px;
    height: 72px;
    border: 5px solid var(--surface-raised);
  }
  h2 {
    margin: 10px 0 2px;
    font-size: 1.3rem;
    overflow-wrap: anywhere;
  }
  .handle {
    margin: 0 0 12px;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  .bio {
    font-size: 0.8rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .status-bubble {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px;
    background: var(--surface-subtle);
    color: var(--text-soft);
    text-align: left;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .actions {
    display: grid;
    margin-top: 12px;
    padding: 5px;
    border-radius: 12px;
    background: var(--surface-subtle);
  }
  .actions > a,
  .actions > button {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--text-soft);
    font-size: 0.85rem;
    text-decoration: none;
    text-align: left;
    cursor: pointer;
  }
  .actions > a:hover,
  .actions > button:hover {
    background: var(--surface-hover);
    color: var(--text);
  }
  .presence {
    margin-top: 4px;
    padding: 4px 0 0;
    border-top: 1px solid var(--line-soft);
  }
  .presence :global(.presence-picker-trigger) {
    width: 100%;
    min-height: 42px;
    padding: 10px 12px;
    font-size: 0.85rem;
  }
  .schedules-modal::backdrop {
    background: rgb(0 0 0 / 58%);
    backdrop-filter: blur(2px);
  }
  .schedules-modal {
    margin: auto;
    gap: 16px;
    width: min(580px, calc(100vw - 32px));
    max-height: min(88dvh, 780px);
    overflow-y: auto;
    padding: 20px;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: var(--surface-raised);
    color: var(--text);
    box-shadow: var(--shadow-lg, 0 24px 64px rgb(0 0 0 / 52%));
  }
  .schedules-modal[open] {
    display: grid;
  }
  .schedules-modal-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }
  .schedules-modal-header h2 {
    margin: 0 0 4px;
    font-size: 1.15rem;
    line-height: 1.2;
  }
  .schedules-modal-header p {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.82rem;
    line-height: 1.4;
  }
  .modal-close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    flex: 0 0 auto;
    border: 1px solid var(--line);
    border-radius: 50%;
    background: var(--surface-subtle);
    color: var(--text-soft);
    font-size: 20px;
    line-height: 1;
    cursor: pointer;
  }
  .modal-close:hover {
    background: var(--surface-hover);
    color: var(--text);
  }
  @media (max-width: 560px) {
    .schedules-modal {
      width: min(580px, calc(100vw - 20px));
      padding: 16px;
      border-radius: 14px;
    }
  }
  .chevron {
    margin-left: auto;
  }
  .id-icon {
    font-size: 0.65rem;
    font-weight: 800;
  }
  .status-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  .feedback {
    color: var(--text-muted);
    font-size: 0.8rem;
    margin-bottom: 0;
  }
</style>
