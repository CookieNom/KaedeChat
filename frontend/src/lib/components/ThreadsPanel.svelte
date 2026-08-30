<script lang="ts">
  import { entityKey } from '$lib/chat/refs';
  import type { Channel, Guild } from '$lib/chat/types';
  import { guildChannelPath } from '$lib/navigation/routes';
  import Icon from './Icon.svelte';

  let {
    open = $bindable(false),
    guild,
    parent,
    activeThreads,
    archivedThreads,
    loading = false,
    loadingMore = false,
    activeHasMore = false,
    archivedHasMore = false,
    busy = false,
    canCreatePublic = false,
    canCreatePrivate = false,
    canSendStarter = false,
    onOpen,
    onLoadMore,
    onCreate
  }: {
    open?: boolean;
    guild: Guild;
    parent: Channel;
    activeThreads: Channel[];
    archivedThreads: Channel[];
    loading?: boolean;
    loadingMore?: boolean;
    activeHasMore?: boolean;
    archivedHasMore?: boolean;
    busy?: boolean;
    canCreatePublic?: boolean;
    canCreatePrivate?: boolean;
    canSendStarter?: boolean;
    onOpen: () => Promise<void> | void;
    onLoadMore?: (archived: boolean) => Promise<void> | void;
    onCreate: (draft: { name: string; message: string; private: boolean }) => Promise<void> | void;
  } = $props();

  let view = $state<'active' | 'archived'>('active');
  let creating = $state(false);
  let name = $state('');
  let message = $state('');
  let privateThread = $state(false);
  const visibleThreads = $derived(view === 'active' ? activeThreads : archivedThreads);
  const hasMore = $derived(view === 'active' ? activeHasMore : archivedHasMore);

  function opened(event: Event) {
    if ((event.currentTarget as HTMLDetailsElement).open) void onOpen();
  }

  function startCreating() {
    creating = true;
    privateThread = canCreatePrivate && !canCreatePublic;
  }

  async function submit() {
    if (!name.trim() || busy || (!canCreatePublic && !canCreatePrivate)) return;
    await onCreate({
      name: name.trim(),
      message: canSendStarter ? message.trim() : '',
      private: canCreatePrivate && privateThread
    });
    name = '';
    message = '';
    creating = false;
  }

  function directoryScrolled(event: Event) {
    if (!hasMore || loading || loadingMore || !onLoadMore) return;
    const target = event.currentTarget as HTMLElement;
    if (target.scrollHeight - target.scrollTop - target.clientHeight < 120) {
      void onLoadMore(view === 'archived');
    }
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- guildChannelPath resolves the typed route -->
<details class="threads-panel" bind:open ontoggle={opened}>
  <summary class="icon-button" aria-label="Threads" title="Threads">
    <Icon name="threads" size={19} />
  </summary>
  <div class="threads-popover">
    <header>
      <strong>Threads in {parent.name}</strong>
      {#if canCreatePublic || canCreatePrivate}
        <button type="button" disabled={busy} onclick={startCreating}>Create Thread</button>
      {/if}
    </header>
    <div class="thread-tabs" role="tablist" aria-label="Thread status">
      <button
        class:active={view === 'active'}
        type="button"
        role="tab"
        aria-selected={view === 'active'}
        onclick={() => (view = 'active')}>Active</button
      >
      <button
        class:active={view === 'archived'}
        type="button"
        role="tab"
        aria-selected={view === 'archived'}
        onclick={() => (view = 'archived')}>Archived</button
      >
    </div>
    {#if creating}
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label>
          Thread Name
          <input bind:value={name} maxlength="100" required disabled={busy} />
        </label>
        <label>
          Message <small>Optional</small>
          <textarea
            bind:value={message}
            rows="2"
            maxlength="4000"
            disabled={busy || !canSendStarter}
            placeholder={canSendStarter
              ? 'Type the first message in your thread'
              : 'You can create the thread, but cannot send its first message'}
          ></textarea>
        </label>
        {#if canCreatePrivate}
          <label class="private-thread-toggle">
            <input
              type="checkbox"
              bind:checked={privateThread}
              disabled={busy || !canCreatePublic}
            />
            <span><strong>Private Thread</strong><small>Only invited people can join.</small></span>
          </label>
        {/if}
        <footer>
          <button type="button" disabled={busy} onclick={() => (creating = false)}>Cancel</button>
          <button class="primary" disabled={busy || !name.trim()}>
            {busy ? 'Creating…' : 'Create Thread'}
          </button>
        </footer>
      </form>
    {/if}
    {#if loading}
      <p role="status">Loading threads…</p>
    {:else if !visibleThreads.length}
      <p>No {view} threads.</p>
    {:else}
      <nav aria-label={`${view} threads`} onscroll={directoryScrolled}>
        {#each visibleThreads as thread (entityKey(thread))}
          <a href={guildChannelPath(guild, thread)}>
            <Icon name="message" size={16} />
            <span
              ><strong>{thread.name}</strong><small>{thread.message_count ?? 0} messages</small
              ></span
            >
            {#if thread.type === 12}<Icon name="lock" size={14} />{/if}
          </a>
        {/each}
        {#if loadingMore}<p role="status">Loading threads…</p>{/if}
      </nav>
    {/if}
  </div>
</details>

<style>
  .threads-panel {
    position: relative;
  }

  summary {
    display: grid;
    width: 38px;
    min-width: 38px;
    height: 38px;
    place-items: center;
    align-items: center;
    padding: 0;
    list-style: none;
  }

  summary::-webkit-details-marker {
    display: none;
  }

  .threads-popover {
    position: absolute;
    z-index: 30;
    top: calc(100% + 0.45rem);
    right: 0;
    display: grid;
    width: min(420px, calc(100vw - 2rem));
    gap: 0.65rem;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 0.8rem;
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg);
  }

  header,
  footer,
  .thread-tabs,
  .private-thread-toggle {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  header {
    justify-content: space-between;
  }

  header button,
  footer button,
  .thread-tabs button {
    min-height: 34px;
    border: 0;
    border-radius: 7px;
    padding: 0 0.65rem;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font-weight: 750;
  }

  .thread-tabs {
    border-bottom: 1px solid var(--line);
  }

  .thread-tabs button {
    border-bottom: 2px solid transparent;
    border-radius: 0;
    background: transparent;
  }

  .thread-tabs button.active {
    border-bottom-color: var(--accent);
    color: var(--text);
  }

  form,
  form > label,
  nav,
  nav a > span {
    display: grid;
    gap: 0.35rem;
  }

  form {
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.65rem;
  }

  form > label {
    color: var(--text-soft);
    font-size: 0.7rem;
    font-weight: 750;
  }

  input,
  textarea {
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0.55rem;
    color: var(--text);
    background: var(--surface-subtle);
  }

  .private-thread-toggle {
    display: flex;
  }

  .private-thread-toggle > span {
    display: grid;
  }

  small,
  .threads-popover > p {
    color: var(--text-muted);
    font-size: 0.65rem;
  }

  footer {
    justify-content: flex-end;
  }

  footer button.primary {
    color: var(--on-accent);
    background: var(--accent);
  }

  nav a {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.55rem;
    border-radius: 8px;
    padding: 0.55rem;
    color: var(--text-soft);
    text-decoration: none;
  }

  nav {
    max-height: min(52vh, 420px);
    overflow: auto;
  }

  nav a:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  nav a strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
