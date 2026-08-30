<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { mergeReactionUsers } from '$lib/chat/reaction-viewer';
  import { entityRef } from '$lib/chat/refs';
  import type { Message, ReactionUsersResponse, UserSummary } from '$lib/chat/types';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import { assetUrl } from '$lib/media/assets';
  import { portal } from '$lib/ui/portal';
  import { onMount, tick } from 'svelte';
  import ReactionEmoji from './ReactionEmoji.svelte';

  let {
    message,
    initialEmoji,
    canManage = false,
    onClearReactions,
    onClose
  }: {
    message: Message;
    initialEmoji?: string;
    canManage?: boolean;
    onClearReactions?: (message: Message, emoji?: string) => Promise<void> | void;
    onClose: () => void;
  } = $props();

  const reactions = $derived(
    Object.entries(message.reaction_counts ?? {}).filter(([, count]) => count > 0)
  );
  let selectedEmoji = $state('');
  let users = $state<UserSummary[]>([]);
  let total = $state(0);
  let nextAfter = $state<string | null>(null);
  let loading = $state(false);
  let error = $state('');
  let managementError = $state('');
  let clearConfirmation = $state<'emoji' | 'all' | null>(null);
  let clearing = $state(false);
  let dialog = $state<HTMLElement | null>(null);
  let requestGeneration = 0;

  function reactionCount(emoji: string): number {
    return message.reaction_counts?.[emoji] ?? 0;
  }

  async function loadUsers(append = false) {
    if (!selectedEmoji || loading) return;
    const generation = ++requestGeneration;
    loading = true;
    error = '';
    const cursor = append ? nextAfter : null;
    try {
      const channel = entityRef({
        id: message.channel_id,
        origin_domain: message.channel_domain
      });
      const query = cursor ? `limit=50&after=${encodeURIComponent(cursor)}` : 'limit=50';
      const response = await api<ReactionUsersResponse>(
        `/channels/${channel}/messages/${entityRef(message)}/reactions/${encodeURIComponent(selectedEmoji)}?${query}`
      );
      if (generation !== requestGeneration) return;
      users = append ? mergeReactionUsers(users, response.items) : response.items;
      total = response.total;
      nextAfter = response.next_after;
    } catch (caught) {
      if (generation !== requestGeneration) return;
      error = userErrorMessage(caught, 'Could not load the people who reacted. Try again.');
    } finally {
      if (generation === requestGeneration) loading = false;
    }
  }

  function selectReaction(emoji: string) {
    if (emoji === selectedEmoji) return;
    selectedEmoji = emoji;
    users = [];
    total = reactionCount(emoji);
    nextAfter = null;
    requestGeneration += 1;
    loading = false;
    void loadUsers();
  }

  function requestClear(kind: 'emoji' | 'all') {
    managementError = '';
    clearConfirmation = kind;
  }

  async function confirmClear() {
    const kind = clearConfirmation;
    if (!kind || !onClearReactions || clearing) return;
    const emoji = kind === 'emoji' ? selectedEmoji : undefined;
    if (kind === 'emoji' && !emoji) return;
    clearing = true;
    managementError = '';
    try {
      await onClearReactions(message, emoji);
      clearConfirmation = null;
      if (kind === 'all') {
        onClose();
        return;
      }
      users = [];
      nextAfter = null;
      total = 0;
      selectedEmoji = '';
      await tick();
      const nextEmoji = reactions[0]?.[0];
      if (!nextEmoji) {
        onClose();
        return;
      }
      selectReaction(nextEmoji);
    } catch (caught) {
      managementError = userErrorMessage(
        caught,
        emoji
          ? `Could not clear the ${emoji} reactions. Check your permissions and try again.`
          : 'Could not clear reactions from this message. Check your permissions and try again.'
      );
    } finally {
      clearing = false;
    }
  }

  function backdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) onClose();
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== 'Tab' || !dialog) return;
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLButtonElement>('button:not([disabled])')
    );
    if (!focusable.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const current = focusable.indexOf(document.activeElement as HTMLButtonElement);
    if (event.shiftKey && current <= 0) {
      event.preventDefault();
      focusable.at(-1)?.focus();
    } else if (!event.shiftKey && current === focusable.length - 1) {
      event.preventDefault();
      focusable[0]?.focus();
    }
  }

  onMount(() => {
    selectedEmoji =
      (initialEmoji && reactions.some(([emoji]) => emoji === initialEmoji)
        ? initialEmoji
        : reactions[0]?.[0]) ?? '';
    total = reactionCount(selectedEmoji);
    void loadUsers();
    void tick().then(() => dialog?.focus());
  });
</script>

<div
  use:portal
  class="reaction-viewer-backdrop"
  role="presentation"
  onclick={backdropClick}
  onkeydown={keydown}
>
  <div
    bind:this={dialog}
    class="reaction-viewer"
    role="dialog"
    aria-modal="true"
    aria-labelledby="reaction-viewer-title"
    tabindex="-1"
  >
    <header>
      <h2 id="reaction-viewer-title">Reactions</h2>
      <div class="reaction-header-actions">
        {#if canManage && onClearReactions && reactions.length}
          <button
            class="clear-all-trigger"
            type="button"
            disabled={clearing}
            onclick={() => requestClear('all')}>Clear all</button
          >
        {/if}
        <button class="close-trigger" type="button" aria-label="Close reactions" onclick={onClose}
          >×</button
        >
      </div>
    </header>
    <div class="reaction-viewer-body">
      <nav aria-label="Reaction types">
        {#each reactions as [emoji, count] (emoji)}
          <button
            type="button"
            disabled={clearing}
            class:active={emoji === selectedEmoji}
            aria-current={emoji === selectedEmoji ? 'true' : undefined}
            onclick={() => selectReaction(emoji)}
          >
            <ReactionEmoji value={emoji} />
            <span>{count}</span>
          </button>
        {/each}
      </nav>
      <div class="reaction-user-panel">
        <p class="visually-hidden" aria-live="polite">
          {loading ? 'Loading reactions' : `${total} people reacted`}
        </p>
        {#if error}
          <div class="reaction-viewer-state" role="alert">
            <p>{error}</p>
            <button type="button" onclick={() => void loadUsers(users.length > 0)}>Try again</button
            >
          </div>
        {:else if !users.length && loading}
          <div class="reaction-viewer-state"><p>Loading…</p></div>
        {:else if !users.length}
          <div class="reaction-viewer-state"><p>No reactions to show.</p></div>
        {:else}
          <ul>
            {#each users as user (entityRef(user))}
              <li>
                <span class="reaction-user-avatar" aria-hidden="true">
                  {#if user.avatar_hash}
                    <img src={assetUrl(user.avatar_hash, 'thumbnail_128', user)} alt="" />
                  {:else}
                    {user.profile_resolved === false
                      ? '•'
                      : user.username.slice(0, 1).toUpperCase()}
                  {/if}
                </span>
                <span class="reaction-user-name">
                  <strong>{userDisplayName(user)}</strong>
                  {#if userPublicHandle(user)}<small>@{userPublicHandle(user)}</small>{/if}
                </span>
              </li>
            {/each}
          </ul>
          {#if nextAfter}
            <button
              class="reaction-load-more"
              type="button"
              disabled={loading}
              onclick={() => void loadUsers(true)}
            >
              {loading ? 'Loading…' : 'Load more'}
            </button>
          {/if}
        {/if}
        {#if canManage && onClearReactions && selectedEmoji}
          <div class="reaction-management">
            <button type="button" disabled={clearing} onclick={() => requestClear('emoji')}>
              Clear <ReactionEmoji value={selectedEmoji} /> reactions
            </button>
          </div>
        {/if}
        {#if managementError}<p class="reaction-management-error" role="alert">
            {managementError}
          </p>{/if}
      </div>
    </div>
    {#if clearConfirmation}
      <section class="reaction-confirmation" role="alert" aria-live="assertive">
        <div>
          <strong>
            {clearConfirmation === 'all'
              ? 'Clear every reaction?'
              : `Clear all ${selectedEmoji} reactions?`}
          </strong>
          <p>
            {clearConfirmation === 'all'
              ? 'Every reaction will be removed from this message. This cannot be undone.'
              : `${reactionCount(selectedEmoji)} reaction${reactionCount(selectedEmoji) === 1 ? '' : 's'} will be removed from this message. This cannot be undone.`}
          </p>
        </div>
        <div class="reaction-confirmation-actions">
          <button type="button" disabled={clearing} onclick={() => (clearConfirmation = null)}
            >Cancel</button
          >
          <button
            class="danger"
            type="button"
            disabled={clearing}
            onclick={() => void confirmClear()}
          >
            {clearing
              ? 'Clearing…'
              : clearConfirmation === 'all'
                ? 'Clear all reactions'
                : `Clear ${selectedEmoji}`}
          </button>
        </div>
      </section>
    {/if}
  </div>
</div>

<style>
  .reaction-viewer-backdrop {
    position: fixed;
    z-index: 240;
    display: grid;
    inset: 0;
    place-items: center;
    padding: 1rem;
    background: rgb(0 0 0 / 58%);
  }

  .reaction-viewer {
    display: grid;
    width: min(580px, calc(100vw - 2rem));
    height: min(430px, calc(100dvh - 2rem));
    grid-template-rows: auto minmax(0, 1fr) auto;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    color: var(--text);
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg);
  }

  header {
    display: flex;
    min-height: 68px;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--line);
    padding: 0 1.25rem;
  }

  h2 {
    margin: 0;
    font-size: 1.25rem;
  }

  .reaction-header-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  header button {
    border: 0;
    padding: 0.25rem;
    color: var(--text-muted);
    background: transparent;
    cursor: pointer;
  }

  header .close-trigger {
    font-size: 2rem;
    line-height: 1;
  }

  header .clear-all-trigger {
    border: 1px solid color-mix(in srgb, var(--danger) 55%, var(--line));
    border-radius: 8px;
    padding: 0.45rem 0.65rem;
    color: var(--danger);
    font-size: 0.78rem;
    font-weight: 800;
  }

  header button:hover,
  header button:focus-visible {
    color: var(--text);
  }

  .reaction-viewer-body {
    display: grid;
    min-height: 0;
    grid-template-columns: 145px minmax(0, 1fr);
  }

  nav {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.3rem;
    overflow-y: auto;
    border-right: 1px solid var(--line);
    padding: 0.8rem;
  }

  nav button {
    display: flex;
    min-height: 44px;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    border-radius: 9px;
    padding: 0.45rem 0.65rem;
    color: var(--text-soft);
    background: transparent;
    font-size: 0.9rem;
    font-weight: 750;
    cursor: pointer;
  }

  nav button:hover,
  nav button.active {
    color: var(--text);
    background: var(--surface-hover);
  }

  nav :global(.reaction-emoji-image) {
    width: 26px;
    height: 26px;
  }

  .reaction-user-panel {
    display: flex;
    min-width: 0;
    flex-direction: column;
    overflow-y: auto;
    padding: 0.8rem 1rem;
  }

  ul {
    display: grid;
    gap: 0.25rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  li {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.75rem;
    border-radius: 9px;
    padding: 0.5rem;
  }

  li:hover {
    background: var(--surface-hover);
  }

  .reaction-user-avatar {
    display: grid;
    width: 38px;
    height: 38px;
    flex: 0 0 auto;
    place-items: center;
    overflow: hidden;
    border-radius: 50%;
    background: var(--surface-subtle);
    font-weight: 800;
  }

  .reaction-user-avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .reaction-user-name {
    display: grid;
    min-width: 0;
    gap: 0.1rem;
  }

  .reaction-user-name strong,
  .reaction-user-name small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .reaction-user-name strong {
    font-size: 0.9rem;
  }

  .reaction-user-name small {
    color: var(--text-muted);
    font-size: 0.72rem;
  }

  .reaction-viewer-state {
    display: grid;
    min-height: 100%;
    place-content: center;
    justify-items: center;
    color: var(--text-muted);
    text-align: center;
  }

  .reaction-management {
    margin-top: auto;
    border-top: 1px solid var(--line);
    padding-top: 0.75rem;
  }

  .reaction-management button {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid color-mix(in srgb, var(--danger) 55%, var(--line));
    border-radius: 8px;
    padding: 0.5rem 0.7rem;
    color: var(--danger);
    background: transparent;
    font-weight: 800;
    cursor: pointer;
  }

  .reaction-management :global(.reaction-emoji-image) {
    width: 20px;
    height: 20px;
  }

  .reaction-management-error {
    margin: 0.7rem 0 0;
    color: var(--danger);
    font-size: 0.82rem;
  }

  .reaction-confirmation {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-top: 1px solid var(--line);
    padding: 0.8rem 1rem;
    background: var(--surface-subtle);
  }

  .reaction-confirmation p {
    margin: 0.2rem 0 0;
    color: var(--text-muted);
    font-size: 0.78rem;
  }

  .reaction-confirmation-actions {
    display: flex;
    flex: 0 0 auto;
    gap: 0.45rem;
  }

  .reaction-confirmation-actions button {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.5rem 0.7rem;
    color: var(--text);
    background: var(--surface-raised);
    font-weight: 800;
    cursor: pointer;
  }

  .reaction-confirmation-actions .danger {
    border-color: var(--danger);
    color: white;
    background: var(--danger);
  }

  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .reaction-viewer-state button,
  .reaction-load-more {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.5rem 0.75rem;
    color: var(--text);
    background: var(--surface-subtle);
    font-weight: 700;
    cursor: pointer;
  }

  .reaction-load-more {
    width: 100%;
    margin-top: 0.6rem;
  }

  @media (max-width: 560px) {
    .reaction-viewer {
      height: min(480px, calc(100dvh - 1rem));
    }

    .reaction-viewer-body {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: auto minmax(0, 1fr);
    }

    nav {
      flex-direction: row;
      overflow-x: auto;
      overflow-y: hidden;
      border-right: 0;
      border-bottom: 1px solid var(--line);
      padding: 0.55rem;
    }

    nav button {
      flex: 0 0 auto;
    }

    .reaction-confirmation {
      align-items: stretch;
      flex-direction: column;
    }

    .reaction-confirmation-actions {
      justify-content: flex-end;
    }
  }
</style>
