<script lang="ts">
  import { t } from '$lib/ui/locale';
  import { onMount, tick } from 'svelte';
  import { api, userErrorMessage } from '$lib/api/client';
  import type { Channel, Message, UserSummary } from '$lib/chat/types';
  import { entityRef } from '$lib/chat/refs';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { decryptConversationMessages, initializeE2EE } from '$lib/e2ee/client';
  import { buildTimeline } from '$lib/chat/timeline';
  import MessageRow from './MessageRow.svelte';

  let {
    channel,
    user,
    refs = $bindable([])
  }: { channel: Channel; user: UserSummary; refs?: string[] } = $props();
  let segment = $state<Channel | null>(null);
  let parent = $state<Channel | null>(null);
  let before = $state('');

  let more = $state(true);
  let busy = $state(false);
  let error = $state('');
  let disposed = false;
  let marker: HTMLDivElement;
  let viewport: HTMLElement | null = null;
  function nearTop() {
    if (viewport && viewport.scrollTop <= 24 && more && !error) void load();
  }
  async function load() {
    if (busy || !more || disposed) return;
    busy = true;
    error = '';
    try {
      let source = segment;
      if (!source) {
        source = (
          await api<{ channel: Channel | null }>(
            '/e2ee/channels/' +
              encodeURIComponent(entityRef(parent ?? channel)) +
              '/previous-segment'
          )
        ).channel;
        if (disposed) return;
        if (!source) {
          more = false;
          return;
        }
      }
      const initial = new Map(chatEntities.messages.values.map((m) => [entityRef(m), m]));
      const page = await api<Message[]>(
        '/channels/' +
          encodeURIComponent(entityRef(source)) +
          '/messages' +
          (before ? '?before=' + encodeURIComponent(before) : '')
      );
      if (disposed) return;
      const rows = await decryptConversationMessages(
        await initializeE2EE(user),
        source,
        page.slice().reverse()
      );
      if (disposed) return;
      chatEntities.messages.upsertMany(
        rows.filter(
          (m) =>
            !initial.get(entityRef(m))?.deleted_at &&
            initial.get(entityRef(m)) === chatEntities.messages.get(entityRef(m))
        )
      );
      const height = viewport?.scrollHeight ?? 0;
      const top = viewport?.scrollTop ?? 0;
      refs = [...new Set([...rows.map(entityRef), ...refs])];
      if (page.length === 50) {
        segment = source;
        before = entityRef(page[page.length - 1]);
      } else {
        parent = source;
        segment = null;
        before = '';
        more = Boolean(source.dm_previous_id);
      }
      await tick();
      if (viewport && !disposed) viewport.scrollTop = top + viewport.scrollHeight - height;
    } catch (e) {
      if (!disposed)
        error = userErrorMessage(e, $t('ui_could_not_load_earlier_messages_try_again_7d0ea620'));
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    viewport = marker.closest('.virtual-message-viewport');
    viewport?.addEventListener('scroll', nearTop);
    void load();
    return () => {
      viewport?.removeEventListener('scroll', nearTop);
      disposed = true;
      refs = [];
    };
  });
</script>

<div bind:this={marker}></div>
{#if more}
  <button class="history-button" type="button" disabled={busy} onclick={load}
    >{busy ? $t('ui_loading_ba3bbbe1') : $t('ui_load_earlier_messages_33fd46b9')}</button
  >
{/if}
{#if error}<p role="alert">{error}</p>{/if}
{#each buildTimeline(refs.flatMap((ref) => {
    const m = chatEntities.messages.get(ref);
    return m && !chatEntities.isMessageBlocked(m) ? [m] : [];
  })) as item (item.key)}
  {#if item.kind === 'day'}<div class="timeline-divider" role="separator">
      <span>{item.label}</span>
    </div>{:else if item.kind === 'message'}<MessageRow
      message={item.message}
      compact={item.compact}
      actionsEnabled={false}
    />{/if}
{/each}
<div class="timeline-divider" role="separator">
  <span
    >{channel.created_at ? new Date(channel.created_at).toLocaleDateString() + ' · ' : ''}{$t(
      'dm_privacy_divider'
    )}</span
  >
</div>
