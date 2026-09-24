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
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import PresencePicker, { type PresencePreference } from './PresencePicker.svelte';
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
        api<{ presence_preference: PresencePreference }>('/users/@me/settings')
      ]);
      if (current !== generation) return;
      profile = loaded;
      status = loaded.custom_status ?? '';
      preference = settings.presence_preference;
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

  async function changePresence(value: PresencePreference) {
    if (busy || loading) return;
    busy = true;
    error = '';
    try {
      await api('/users/@me/settings', {
        method: 'PATCH',
        body: JSON.stringify({ presence_preference: value })
      });
      preference = value;
      authenticatedGateway.client.setPresence(value);
      if (user) chatEntities.setPresence(user, value === 'invisible' ? 'offline' : value);
      try {
        localStorage.setItem('kaede.presence', value);
      } catch {
        /* Live preference still applies. */
      }
      onPresenceChange?.(value);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not change your presence.');
    } finally {
      busy = false;
    }
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
      href={resolve('/settings#profile')}
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
      <a href={resolve('/settings#profile')}
        ><Icon name="edit" size={18} />Edit profile / change avatar</a
      >
      <div class="presence"><PresencePicker value={preference} onChange={changePresence} /></div>
    </div>
    <div class="actions">
      {#if isNativeDesktop()}<a href={resolve('/accounts')}
          ><Icon name="users" size={18} />Switch accounts <span class="chevron">›</span></a
        >{/if}
      <button onclick={copyId}><span class="id-icon">ID</span>Copy user ID</button>
      <a href={resolve('/settings')}><Icon name="settings" size={18} />Account settings</a>
    </div>
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    {#if notice}<p class="feedback" role="status">{notice}</p>{/if}
  </div>
</div>

<VoiceQuickControls />

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
    padding: 10px;
    border-top: 1px solid var(--line-soft);
  }
  .presence :global(.presence-picker-trigger) {
    width: 100%;
    font-size: 0.85rem;
  }
  .presence :global(.presence-picker-menu) {
    bottom: calc(100% + 8px);
    left: 0;
    width: min(280px, calc(100vw - 70px));
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
