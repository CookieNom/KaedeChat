<script lang="ts">
  import { onMount } from 'svelte';
  import { SvelteURLSearchParams } from 'svelte/reactivity';
  import { goto } from '$app/navigation';
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import type { ReadStateStatus } from '$lib/chat/types';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { markConversationsRead } from '$lib/notifications/read-actions';
  import { t } from '$lib/ui/locale';
  import { userDisplayName } from '$lib/chat/users';
  let { onClose }: { onClose: () => void } = $props();
  type Entry = ReadStateStatus & {
    channel_name?: string;
    created_at?: string;
    unread_count?: number;
    message_id?: string;
    message_domain?: string;
  };
  let dialog: HTMLDialogElement;
  let tab = $state('unreads');
  let guild = $state('');
  let everyone = $state(true);
  let roles = $state(true);
  let showMuted = $state(false);
  let entries = $state<Entry[]>([]);
  let busy = $state(false);
  let error = $state('');
  let more = $state(false);
  async function refresh(append = false) {
    busy = true;
    error = '';
    try {
      const query = new SvelteURLSearchParams({
        include_everyone: String(everyone),
        include_roles: String(roles)
      });
      if (guild) query.set('guild', guild);
      const last = entries.at(-1);
      if (append && last?.message_id)
        query.set('before', `${last.message_id}@${last.message_domain}`);
      const values = await api<Entry[]>(
        tab === 'mentions' ? `/users/@me/inbox/mentions?${query}` : '/users/@me/read-states'
      );
      entries = append ? [...entries, ...values] : values;
      more = tab === 'mentions' && values.length === 50;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load inbox. Try again.');
    } finally {
      busy = false;
    }
  }
  async function mark(entry?: Entry) {
    busy = true;
    try {
      await markConversationsRead(
        entry ? { channel: `${entry.channel_id}@${entry.channel_domain}` } : guild ? { guild } : {}
      );
      await refresh();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not mark conversations read. Try again.');
    } finally {
      busy = false;
    }
  }
  async function dismiss(entry: Entry) {
    busy = true;
    try {
      await api('/users/@me/inbox/dismiss', {
        method: 'POST',
        body: JSON.stringify({ message_id: `${entry.message_id}@${entry.message_domain}` })
      });
      entries = entries.filter((item) => item !== entry);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not dismiss mention. Try again.');
    } finally {
      busy = false;
    }
  }
  async function jump(entry: Entry) {
    const channel = encodeURIComponent(`${entry.channel_id}@${entry.channel_domain}`);
    const path = entry.guild_id
      ? `/g/${encodeURIComponent(`${entry.guild_id}@${entry.guild_domain}`)}/${channel}`
      : `/home/${channel}`;
    const around = entry.message_id
      ? `?around=${encodeURIComponent(`${entry.message_id}@${entry.message_domain}`)}`
      : '';
    onClose();
    await goto(resolve((path + around) as '/home'));
  }
  onMount(() => {
    dialog.showModal();
    void refresh();
    const timer = setInterval(() => {
      if (!busy && document.visibilityState === 'visible') void refresh();
    }, 30000);
    return () => clearInterval(timer);
  });
  const visible = $derived(
    entries.filter(
      (entry) =>
        (tab === 'mentions' || (entry.unread && (showMuted || !entry.muted))) &&
        (!guild || `${entry.guild_id}@${entry.guild_domain}` === guild)
    )
  );
</script>

<dialog bind:this={dialog} onclose={onClose} aria-label={$t('chat_inbox')} class="read-inbox">
  <header>
    <h2>{$t('chat_inbox')}</h2>
    <button onclick={onClose} aria-label={$t('chat_close')}>×</button>
  </header>
  <div class="inbox-controls">
    <button
      disabled={busy}
      aria-pressed={tab === 'unreads'}
      onclick={() => {
        tab = 'unreads';
        void refresh();
      }}>{$t('chat_unreads')}</button
    >
    <button
      disabled={busy}
      aria-pressed={tab === 'mentions'}
      onclick={() => {
        tab = 'mentions';
        void refresh();
      }}>{$t('chat_mentions')}</button
    >
    <select
      aria-label={$t('chat_inbox_filter')}
      bind:value={guild}
      disabled={busy}
      onchange={() => void refresh()}
    >
      <option value="">{$t('chat_all_conversations')}</option>
      {#each chatEntities.guilds.values as server (`${server.id}@${server.origin_domain}`)}<option
          value={`${server.id}@${server.origin_domain}`}>{server.name}</option
        >{/each}
    </select>
    {#if tab === 'mentions'}
      <label
        ><input
          type="checkbox"
          bind:checked={everyone}
          disabled={busy}
          onchange={() => void refresh()}
        />@everyone</label
      >
      <label
        ><input
          type="checkbox"
          bind:checked={roles}
          disabled={busy}
          onchange={() => void refresh()}
        />@roles</label
      >
    {/if}
    {#if tab === 'unreads'}<label
        ><input type="checkbox" bind:checked={showMuted} />{$t('chat_show_muted')}</label
      >{/if}
    <button disabled={busy} onclick={() => void mark()}>{$t('chat_mark_all_read')}</button>
    <button disabled={busy} onclick={() => void refresh()}>{$t('chat_refresh')}</button>
  </div>
  {#if error}<p role="alert">{error}</p>{/if}
  <div class="inbox-entries" aria-busy={busy}>
    {#each visible as entry (`${entry.channel_id}@${entry.channel_domain}:${entry.message_id ?? ''}@${entry.message_domain ?? ''}`)}
      <article>
        <button class="inbox-jump" onclick={() => void jump(entry)}
          >{entry.channel_name ||
            chatEntities.channels
              .get(`${entry.channel_id}@${entry.channel_domain}`)
              ?.recipients?.map(userDisplayName)
              .join(', ') ||
            $t('chat_direct_message')}<small
            >{tab === 'mentions'
              ? `${$t('chat_jump_to_mention')}${entry.created_at ? ` · ${new Date(entry.created_at).toLocaleString()}` : ''}`
              : `${entry.unread_count ?? ''} ${$t('chat_unreads')}`}</small
          ></button
        >
        <button
          disabled={busy}
          onclick={() => (tab === 'mentions' ? void dismiss(entry) : void mark(entry))}
          >{tab === 'mentions' ? $t('chat_dismiss') : $t('chat_mark_read')}</button
        >
      </article>
    {:else}<p>{busy ? $t('chat_loading') : $t('chat_caught_up')}</p>{/each}
    {#if more}<button disabled={busy} onclick={() => void refresh(true)}
        >{$t('chat_load_more')}</button
      >{/if}
  </div>
</dialog>

<style>
  .read-inbox {
    position: fixed;
    inset: 5vh 0 auto;
    z-index: 1100;
    width: min(680px, 94vw);
    max-height: 90vh;
    padding: 20px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 14px;
    box-shadow: 0 16px 80px #0009;
  }
  header,
  .inbox-controls,
  article {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  header {
    justify-content: space-between;
  }
  .inbox-controls {
    flex-wrap: wrap;
    margin-block: 16px;
  }
  .inbox-entries {
    overflow-y: auto;
    max-height: 60vh;
  }
  article {
    justify-content: space-between;
    padding-block: 12px;
    border-bottom: 1px solid #8884;
  }
  .inbox-jump {
    text-align: left;
    flex: 1;
  }
  small {
    display: block;
    opacity: 0.7;
  }
  button,
  select {
    min-height: 36px;
    padding: 6px 10px;
    color: var(--text);
    background: var(--surface-subtle);
    border: 1px solid var(--line);
    border-radius: 6px;
  }
  button[aria-pressed='true'] {
    background: var(--accent-soft);
    color: var(--accent-text);
  }
  button:disabled {
    opacity: 0.5;
  }
  label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  dialog::backdrop {
    background: #0006;
  }
</style>
