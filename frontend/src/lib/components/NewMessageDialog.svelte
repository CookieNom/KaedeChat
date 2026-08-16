<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { filterDmFriends } from '$lib/chat/dm-picker';
  import { entityKey } from '$lib/chat/refs';
  import type { Channel, Relationship, UserSummary } from '$lib/chat/types';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import { assetUrl } from '$lib/media/assets';
  import { directMessagePath } from '$lib/navigation/routes';
  import Icon from './Icon.svelte';
  import { tick } from 'svelte';

  let { open = $bindable(false) }: { open?: boolean } = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let searchInput = $state<HTMLInputElement | null>(null);
  let friends = $state<UserSummary[]>([]);
  let selectedKeys = $state<string[]>([]);
  let query = $state('');
  let loading = $state(false);
  let submitting = $state(false);
  let error = $state('');
  let loadGeneration = 0;

  const filteredFriends = $derived(filterDmFriends(friends, query));

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      void loadFriends();
      void tick().then(() => searchInput?.focus());
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  async function loadFriends() {
    const generation = ++loadGeneration;
    loading = true;
    error = '';
    try {
      const relationships = await api<Relationship[]>('/users/@me/relationships');
      if (generation !== loadGeneration || !open) return;
      friends = relationships
        .filter((relationship) => relationship.type === 'friend')
        .map((relationship) => relationship.user)
        .filter((user) => Boolean(userPublicHandle(user)))
        .sort((left, right) =>
          userDisplayName(left).localeCompare(userDisplayName(right), undefined, {
            sensitivity: 'base'
          })
        );
    } catch (caught) {
      if (generation !== loadGeneration || !open) return;
      error = userErrorMessage(caught, 'Could not load your friends. Try again.');
    } finally {
      if (generation === loadGeneration) loading = false;
    }
  }

  function close() {
    open = false;
  }

  function reset() {
    loadGeneration += 1;
    open = false;
    query = '';
    friends = [];
    selectedKeys = [];
    loading = false;
    submitting = false;
    error = '';
  }

  function toggleFriend(friend: UserSummary) {
    const key = entityKey(friend);
    if (selectedKeys.includes(key)) {
      selectedKeys = selectedKeys.filter((item) => item !== key);
    } else if (selectedKeys.length < 9) {
      selectedKeys = [...selectedKeys, key];
    }
    error = '';
  }

  async function createMessage() {
    if (submitting || selectedKeys.length === 0) return;
    const selectedFriends = friends.filter((friend) => selectedKeys.includes(entityKey(friend)));
    const handles = selectedFriends
      .map(userPublicHandle)
      .filter((value): value is string => Boolean(value));
    if (handles.length !== selectedKeys.length) {
      error = 'One of the selected profiles is unavailable. Refresh the list and try again.';
      return;
    }

    submitting = true;
    error = '';
    try {
      if (handles.length === 1) {
        const result = await api<
          Channel | { status: 'queued'; operation_id: string; pair_key: string }
        >('/users/@me/channels', {
          method: 'POST',
          body: JSON.stringify({ handle: handles[0] })
        });
        if ('status' in result) {
          error = 'That person’s instance is temporarily unavailable. Try again shortly.';
          return;
        }
        window.location.assign(directMessagePath(result));
        return;
      }

      const result = await api<Channel>('/users/@me/channels/group', {
        method: 'POST',
        body: JSON.stringify({ handles, name: null })
      });
      window.location.assign(directMessagePath(result));
    } catch (caught) {
      error = userErrorMessage(
        caught,
        selectedKeys.length === 1
          ? 'Could not open that conversation. Try again.'
          : 'Could not create the group conversation. Try again.'
      );
    } finally {
      submitting = false;
    }
  }
</script>

<dialog
  bind:this={dialog}
  class="new-message-dialog"
  aria-labelledby="new-message-title"
  onclose={reset}
  oncancel={(event) => {
    event.preventDefault();
    close();
  }}
  onclick={(event) => {
    if (event.target === dialog) close();
  }}
>
  <form
    onsubmit={(event) => {
      event.preventDefault();
      void createMessage();
    }}
  >
    <header>
      <div>
        <h2 id="new-message-title">New message</h2>
        <p>Group DMs can have up to 10 members.</p>
      </div>
      <button class="close-button" type="button" aria-label="Close" onclick={close}>×</button>
    </header>

    <label class="search-field">
      <Icon name="search" size={18} />
      <input
        bind:this={searchInput}
        bind:value={query}
        type="search"
        placeholder="Search friends"
        autocomplete="off"
      />
    </label>
    <p class="picker-help">
      Select one friend for a direct message, or two or more friends for a group DM.
    </p>

    <div class="selection-summary" aria-live="polite">
      <strong>Friends</strong>
      <span>{selectedKeys.length} of 9 selected</span>
    </div>

    <section class="friend-list" aria-label="Friends">
      {#if loading}
        <div class="picker-state"><span class="spinner"></span>Loading friends…</div>
      {:else if friends.length === 0 && !error}
        <div class="picker-state">
          <strong>No friends available</strong>
          <span>Add someone as a friend before starting a group DM.</span>
        </div>
      {:else}
        {#each filteredFriends as friend (entityKey(friend))}
          {@const selected = selectedKeys.includes(entityKey(friend))}
          <button
            class:selected
            type="button"
            aria-pressed={selected}
            disabled={!selected && selectedKeys.length >= 9}
            onclick={() => toggleFriend(friend)}
          >
            <span class="avatar">
              {#if friend.avatar_hash}
                <img
                  src={assetUrl(friend.avatar_hash, 'thumbnail_128', friend)}
                  alt=""
                  referrerpolicy="no-referrer"
                />
              {:else}
                {friend.profile_resolved === false
                  ? '•'
                  : friend.username.slice(0, 1).toUpperCase()}
              {/if}
            </span>
            <span class="friend-copy">
              <strong>{userDisplayName(friend)}</strong>
              <small>@{userPublicHandle(friend)?.replace(/^@/, '')}</small>
            </span>
            <span class="checkbox" aria-hidden="true">
              {#if selected}<Icon name="check" size={16} />{/if}
            </span>
          </button>
        {:else}
          <div class="picker-state">
            <strong>No matching friends</strong>
            <span>Try another name or federated username.</span>
          </div>
        {/each}
      {/if}
    </section>

    {#if error}<p class="form-error" role="alert">{error}</p>{/if}

    <footer>
      <button class="secondary-button" type="button" onclick={close}>Cancel</button>
      <button class="primary-button" disabled={submitting || selectedKeys.length === 0}>
        {submitting ? 'Creating…' : 'Create message'}
      </button>
    </footer>
  </form>
</dialog>

<style>
  .new-message-dialog {
    width: min(560px, calc(100vw - 32px));
    max-height: min(720px, calc(100dvh - 32px));
    padding: 0;
    overflow: hidden;
    color: var(--text);
    background: var(--surface-raised);
    border: 1px solid var(--line-strong);
    border-radius: 22px;
    box-shadow: 0 24px 80px rgb(0 0 0 / 0.55);
  }

  .new-message-dialog::backdrop {
    background: rgb(0 0 0 / 0.68);
    backdrop-filter: blur(2px);
  }

  form {
    display: flex;
    max-height: min(720px, calc(100dvh - 32px));
    flex-direction: column;
  }

  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 24px;
    padding: 26px 28px 18px;
  }

  header h2,
  header p,
  .picker-help,
  .form-error {
    margin: 0;
  }

  header h2 {
    font-size: 1.55rem;
    text-transform: capitalize;
  }

  header p,
  .picker-help,
  .friend-copy small,
  .selection-summary span,
  .picker-state span {
    color: var(--text-muted);
  }

  header p {
    margin-top: 7px;
  }

  .close-button {
    padding: 0;
    color: var(--text-muted);
    background: transparent;
    border: 0;
    font-size: 2rem;
    line-height: 1;
    cursor: pointer;
  }

  .search-field {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0 28px;
    padding: 0 14px;
    color: var(--text-muted);
    background: var(--surface-subtle);
    border: 1px solid var(--line-strong);
    border-radius: 12px;
  }

  .search-field:focus-within {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 30%, transparent);
  }

  .search-field input {
    width: 100%;
    min-height: 48px;
    padding: 0;
    color: inherit;
    background: transparent;
    border: 0;
    outline: 0;
    font: inherit;
  }

  .picker-help {
    padding: 9px 28px 16px;
    font-size: 0.88rem;
  }

  .selection-summary {
    display: flex;
    justify-content: space-between;
    padding: 0 28px 9px;
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }

  .friend-list {
    min-height: 220px;
    max-height: min(410px, calc(100dvh - 340px));
    margin: 0 12px;
    padding: 0 8px 8px;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .friend-list > button {
    display: grid;
    width: 100%;
    grid-template-columns: 42px minmax(0, 1fr) 24px;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
    color: inherit;
    text-align: left;
    background: transparent;
    border: 0;
    border-radius: 12px;
    cursor: pointer;
  }

  .friend-list > button:hover,
  .friend-list > button.selected {
    background: var(--surface-hover);
  }

  .friend-list > button:disabled {
    opacity: 0.48;
    cursor: not-allowed;
  }

  .avatar {
    display: grid;
    width: 42px;
    height: 42px;
    place-items: center;
    overflow: hidden;
    background: var(--surface-subtle);
    border-radius: 50%;
    font-weight: 800;
  }

  .avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .friend-copy {
    display: grid;
    min-width: 0;
    gap: 2px;
  }

  .friend-copy strong,
  .friend-copy small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .checkbox {
    display: grid;
    width: 22px;
    height: 22px;
    place-items: center;
    color: var(--on-accent);
    border: 2px solid var(--line-strong);
    border-radius: 6px;
  }

  button.selected .checkbox {
    background: var(--accent);
    border-color: var(--accent);
  }

  .picker-state {
    display: grid;
    min-height: 180px;
    place-content: center;
    gap: 6px;
    padding: 24px;
    text-align: center;
  }

  .spinner {
    width: 22px;
    height: 22px;
    margin: 0 auto 4px;
    border: 3px solid var(--line-strong);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  .form-error {
    padding: 10px 28px 0;
    color: var(--danger);
  }

  footer {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 16px;
    padding: 18px 28px;
    background: var(--surface-subtle);
    border-top: 1px solid var(--line-soft);
  }

  footer button {
    min-width: 132px;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 600px) {
    .new-message-dialog {
      width: calc(100vw - 20px);
      max-height: calc(100dvh - 20px);
      border-radius: 18px;
    }

    form {
      max-height: calc(100dvh - 20px);
    }

    header {
      padding: 20px 20px 16px;
    }

    .search-field {
      margin: 0 20px;
    }

    .picker-help,
    .selection-summary {
      padding-right: 20px;
      padding-left: 20px;
    }

    footer {
      padding: 16px 20px;
    }

    footer button {
      min-width: 0;
      flex: 1;
    }
  }
</style>
