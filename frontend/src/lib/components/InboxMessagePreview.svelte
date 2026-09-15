<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { compareEntityRefs, parseCanonicalEntityRef } from '$lib/chat/refs';
  import type { Message } from '$lib/chat/types';
  import { t } from '$lib/ui/locale';
  import MessageRow from './MessageRow.svelte';
  let {
    channel,
    target,
    mention = false
  }: { channel: string; target: string | null; mention?: boolean } = $props();
  let root: HTMLDivElement;
  let messages = $state<Message[]>([]);
  let loading = $state(true);
  let failed = $state(false);
  onMount(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const query = target ? `around=${encodeURIComponent(target)}&limit=5` : 'limit=3';
        const values = await api<Message[]>(
          `/channels/${encodeURIComponent(channel)}/messages?${query}`,
          { signal: controller.signal }
        );
        if (controller.signal.aborted) return;
        const ordered = values.sort(compareEntityRefs);
        const reference = target ? parseCanonicalEntityRef(target) : null;
        const candidates = reference
          ? ordered.filter((message) =>
              mention
                ? `${message.id}@${message.origin_domain}` === target
                : compareEntityRefs(message, reference) >= 0
            )
          : ordered;
        messages = candidates.slice(0, mention ? 1 : 3).map((message) => {
          // Already decrypted messages can be reused without joining an encrypted room from Inbox.
          const cached = chatEntities.messages.get(`${message.id}@${message.origin_domain}`);
          return message.e2ee &&
            cached?.e2ee_verified &&
            cached.edited_at === message.edited_at &&
            cached.channel_id === message.channel_id &&
            cached.channel_domain === message.channel_domain
            ? cached
            : message;
        });
      } catch {
        if (!controller.signal.aborted) failed = true;
      } finally {
        if (!controller.signal.aborted) loading = false;
      }
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer.disconnect();
          void load();
        }
      },
      { rootMargin: '100px' }
    );
    observer.observe(root);
    return () => {
      observer.disconnect();
      controller.abort();
    };
  });
</script>

<div bind:this={root} class="inbox-preview" aria-busy={loading}>
  {#each messages as message (`${message.id}@${message.origin_domain}`)}
    <MessageRow
      {message}
      actionsEnabled={false}
      domIdPrefix="inbox-message"
      timestampFormat="date-time"
    />
  {:else}<p>
      {loading
        ? $t('chat_loading')
        : failed
          ? $t('chat_inbox_preview_failed')
          : $t('chat_inbox_preview_empty')}
    </p>{/each}
</div>

<style>
  .inbox-preview {
    min-height: 76px;
    padding: 12px 4px;
    overflow: hidden;
  }
  p {
    margin: 12px 16px;
    color: var(--text-muted);
    font-size: 13px;
  }
  .inbox-preview :global(.message-row) {
    min-width: 0;
  }
</style>
