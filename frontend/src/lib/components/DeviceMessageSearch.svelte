<script lang="ts">
  import { t } from '$lib/ui/locale';
  import type { Message } from '$lib/chat/types';
  import { entityRef } from '$lib/chat/refs';
  import { searchLoadedMessages } from '$lib/chat/search-loaded-messages';
  import { userDisplayName } from '$lib/chat/users';
  import Icon from './Icon.svelte';

  let { messages, onJump }: { messages: Message[]; onJump: (message: Message) => void } = $props();
  let dialog: HTMLDialogElement;
  let input: HTMLInputElement;
  let query = $state('');
  const needle = $derived(query.trim().toLocaleLowerCase());
  const results = $derived(searchLoadedMessages(messages, needle));
</script>

<button
  type="button"
  class="icon-button"
  title={$t('device_search')}
  aria-label={$t('device_search')}
  onclick={() => {
    dialog.showModal();
    input.focus();
  }}
>
  <Icon name="search" size={18} />
</button>
<dialog bind:this={dialog} onclose={() => (query = '')} aria-label={$t('device_search')}>
  <header>
    <strong>{$t('device_search')}</strong>
    <form method="dialog"><button type="submit">{$t('ui_close_7d9eb7ac')}</button></form>
  </header>
  <p>{$t('device_search_hint')}</p>
  <input
    bind:this={input}
    type="search"
    aria-label={$t('device_search_label')}
    placeholder={$t('ui_search_messages_ddf0602b')}
    autocomplete="off"
    spellcheck="false"
    bind:value={query}
  />
  {#if needle}
    <p role="status">
      {results.length
        ? $t('device_search_count', { count: String(results.length) })
        : $t('device_search_none')}
    </p>
    {#each results as { message, snippet } (entityRef(message))}
      <button
        class="result"
        type="button"
        onclick={() => {
          dialog.close();
          onJump(message);
        }}
      >
        <small
          >{message.author ? userDisplayName(message.author) : message.author_id} · {new Date(
            message.created_at
          ).toLocaleString()}</small
        >
        <span>{snippet}</span>
      </button>
    {/each}
  {/if}
</dialog>

<style>
  dialog {
    width: min(30rem, calc(100% - 2rem));
    max-height: 75dvh;
    box-sizing: border-box;
    overflow: auto;
    padding: 1rem;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    color: var(--text);
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 55%);
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
  }
  p,
  small {
    color: var(--muted);
  }
  input,
  dialog button {
    padding: 0.6rem;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: inherit;
    background: var(--surface-raised);
  }
  input,
  .result {
    width: 100%;
    box-sizing: border-box;
  }
  .result {
    display: grid;
    text-align: start;
    margin-top: 0.4rem;
  }
  .result span {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
</style>
