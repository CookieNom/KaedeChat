<script lang="ts">
  import type { Message } from '$lib/chat/types';
  import { entityRef } from '$lib/chat/refs';
  import { preferredLocale } from '$lib/ui/locale';

  let {
    messages,
    loading = false,
    error = '',
    onClose,
    onJump
  }: {
    messages: Message[];
    loading?: boolean;
    error?: string;
    onClose: () => void;
    onJump: (message: Message) => void;
  } = $props();
</script>

<aside class="pinned-messages-panel" aria-label="Pinned messages">
  <header>
    <span aria-hidden="true">📌</span>
    <div>
      <strong>Pinned messages</strong>
      <small>{messages.length} saved in this conversation</small>
    </div>
    <button type="button" aria-label="Close pinned messages" onclick={onClose}>×</button>
  </header>
  <div class="pinned-message-list">
    {#if loading}
      <p>Loading pinned messages…</p>
    {:else if error}
      <p class="form-error">{error}</p>
    {:else if !messages.length}
      <div class="pinned-empty">
        <span aria-hidden="true">📌</span>
        <strong>No pinned messages yet</strong>
        <p>Pinned messages stay easy to find here.</p>
      </div>
    {:else}
      {#each messages as message (entityRef(message))}
        <button class="pinned-message-card" type="button" onclick={() => onJump(message)}>
          <span class="pinned-message-avatar" aria-hidden="true"
            >{message.author?.username.slice(0, 1).toUpperCase() ?? '•'}</span
          >
          <span>
            <strong>{message.author?.display_name ?? message.author?.username ?? 'Unknown author'}</strong>
            <time datetime={message.created_at}
              >{new Date(message.created_at).toLocaleString(preferredLocale(), {
                dateStyle: 'medium',
                timeStyle: 'short'
              })}</time
            >
            <span class="pinned-message-content"
              >{message.deleted_at ? 'Message removed' : message.content || 'Attachment'}</span
            >
          </span>
        </button>
      {/each}
    {/if}
  </div>
</aside>
