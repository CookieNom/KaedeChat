<script lang="ts">
  import { t } from '$lib/ui/locale';

  import type { Message } from '$lib/chat/types';
  import { entityRef } from '$lib/chat/refs';
  import { preferredLocale } from '$lib/ui/locale';
  import { userDisplayName } from '$lib/chat/users';

  let {
    messages,
    loading = false,
    error = '',
    onClose,
    onJump,
    onUnpin,
    onRetry
  }: {
    messages: Message[];
    loading?: boolean;
    error?: string;
    onClose: () => void;
    onJump: (message: Message) => void;
    onUnpin?: (message: Message) => void;
    onRetry?: () => void;
  } = $props();
</script>

<aside class="pinned-messages-panel" aria-label={$t('ui_pinned_messages_4c0dbc8c')}>
  <header>
    <span aria-hidden="true">📌</span>
    <div>
      <strong>{$t('ui_pinned_messages_4c0dbc8c')}</strong>
      <small
        >{$t('ui_value0_saved_in_this_conversation_7f904862', {
          value0: String(messages.length)
        })}</small
      >
    </div>
    <button type="button" aria-label={$t('ui_close_pinned_messages_633d86f4')} onclick={onClose}
      >×</button
    >
  </header>
  <div class="pinned-message-list">
    {#if loading}
      <p>{$t('ui_loading_pinned_messages_f839bbf2')}</p>
    {:else if error}
      <div role="alert">
        <p class="form-error">{error}</p>
        {#if onRetry}<button type="button" onclick={onRetry}>{$t('ui_try_again_d8b8392e')}</button
          >{/if}
      </div>
    {:else if !messages.length}
      <div class="pinned-empty">
        <span aria-hidden="true">📌</span>
        <strong>{$t('ui_no_pinned_messages_yet_2ccc2323')}</strong>
        <p>{$t('ui_pinned_messages_stay_easy_to_find_here_a8a94032')}</p>
      </div>
    {:else}
      {#each messages as message (entityRef(message))}
        <div class="pinned-message-card">
          <button class="pinned-message-jump" type="button" onclick={() => onJump(message)}>
            <span class="pinned-message-avatar" aria-hidden="true"
              >{message.author?.profile_resolved === false
                ? '•'
                : (message.author?.username.slice(0, 1).toUpperCase() ?? '•')}</span
            >
            <span>
              <strong
                >{message.author
                  ? userDisplayName(message.author)
                  : $t('ui_unknown_author_d5edd2d1')}</strong
              >
              <time datetime={message.created_at}
                >{new Date(message.created_at).toLocaleString(preferredLocale(), {
                  dateStyle: 'medium',
                  timeStyle: 'short'
                })}</time
              >
              <span class="pinned-message-content"
                >{message.deleted_at
                  ? $t('ui_message_removed_e82a13c0')
                  : message.content || $t('ui_attachment_040d2b36')}</span
              >
            </span>
          </button>
          {#if onUnpin}
            <button
              class="pinned-message-unpin"
              type="button"
              aria-label={$t('ui_unpin_message_be8d20ff')}
              title={$t('ui_unpin_message_be8d20ff')}
              onclick={() => onUnpin?.(message)}>×</button
            >
          {/if}
        </div>
      {/each}
    {/if}
  </div>
</aside>
