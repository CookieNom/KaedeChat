<script lang="ts">
  import { t } from '$lib/ui/locale';

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
  let panel: HTMLDetailsElement;
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

  function dismissOnOutsidePointer(event: PointerEvent) {
    const target = event.target;
    if (open && target instanceof Node && !panel.contains(target)) open = false;
  }

  function dismissOnEscape(event: KeyboardEvent) {
    if (!open || event.key !== 'Escape') return;
    event.preventDefault();
    open = false;
    panel.querySelector<HTMLElement>('summary')?.focus();
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- guildChannelPath resolves the typed route -->
<svelte:window onpointerdown={dismissOnOutsidePointer} onkeydown={dismissOnEscape} />

<details bind:this={panel} class="threads-panel" bind:open ontoggle={opened}>
  <summary
    class="icon-button"
    aria-label={$t('ui_threads_3e42e385')}
    title={$t('ui_threads_3e42e385')}
  >
    <Icon name="threads" size={19} />
  </summary>
  <div class="threads-popover">
    <header>
      <strong>{$t('ui_threads_in_value0_e6d5adc3', { value0: String(parent.name) })}</strong>
      {#if canCreatePublic || canCreatePrivate}
        <button type="button" disabled={busy} onclick={startCreating}
          >{$t('ui_create_thread_4ce9bbfd')}</button
        >
      {/if}
    </header>
    <div class="thread-tabs" role="tablist" aria-label={$t('ui_thread_status_b5c2efab')}>
      <button
        class:active={view === 'active'}
        type="button"
        role="tab"
        aria-selected={view === 'active'}
        onclick={() => (view = 'active')}>{$t('ui_active_92340695')}</button
      >
      <button
        class:active={view === 'archived'}
        type="button"
        role="tab"
        aria-selected={view === 'archived'}
        onclick={() => (view = 'archived')}>{$t('ui_archived_bdb86505')}</button
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
          {$t('ui_thread_name_abe55ef5')}
          <input bind:value={name} maxlength="100" required disabled={busy} />
        </label>
        <label>
          {$t('ui_message_2f77668a')} <small>{$t('ui_optional_59be7133')}</small>
          <textarea
            bind:value={message}
            rows="2"
            maxlength="4000"
            disabled={busy || !canSendStarter}
            placeholder={canSendStarter
              ? $t('ui_type_the_first_message_in_your_thread_dc3d4661')
              : $t('ui_you_can_create_the_thread_but_cannot_send_its_5173d0fb')}
          ></textarea>
        </label>
        {#if canCreatePrivate}
          <label class="private-thread-toggle">
            <input
              type="checkbox"
              bind:checked={privateThread}
              disabled={busy || !canCreatePublic}
            />
            <span
              ><strong>{$t('ui_private_thread_e1fd6c53')}</strong><small
                >{$t('ui_only_invited_people_can_join_9f1af1ab')}</small
              ></span
            >
          </label>
        {/if}
        <footer>
          <button type="button" disabled={busy} onclick={() => (creating = false)}
            >{$t('ui_cancel_19766ed6')}</button
          >
          <button class="primary" disabled={busy || !name.trim()}>
            {busy ? $t('ui_creating_c79ed949') : $t('ui_create_thread_4ce9bbfd')}
          </button>
        </footer>
      </form>
    {/if}
    {#if loading}
      <p role="status">{$t('ui_loading_threads_b732ef3e')}</p>
    {:else if !visibleThreads.length}
      <p>{$t('ui_no_value0_threads_c9814454', { value0: String(view) })}</p>
    {:else}
      <nav aria-label={`${view} threads`} onscroll={directoryScrolled}>
        {#each visibleThreads as thread (entityKey(thread))}
          <a href={guildChannelPath(guild, thread)}>
            <Icon name="message" size={16} />
            <span
              ><strong>{thread.name}</strong><small
                >{$t('ui_value0_messages_2f6a6143', {
                  value0: String(thread.message_count ?? 0)
                })}</small
              ></span
            >
            {#if thread.type === 12}<Icon name="lock" size={14} />{/if}
          </a>
        {/each}
        {#if loadingMore}<p role="status">{$t('ui_loading_threads_b732ef3e')}</p>{/if}
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
    padding: 0.4rem 0.65rem;
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
