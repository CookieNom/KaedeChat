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
  import Icon from './Icon.svelte';
  import InboxMessagePreview from './InboxMessagePreview.svelte';
  import { assetUrl } from '$lib/media/assets';
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
  const tabs = ['unreads', 'mentions', 'bookmarks'];
  let tab = $state('unreads');
  let guild = $state('');
  let everyone = $state(true);
  let roles = $state(true);
  let showMuted = $state(false);
  let entries = $state<Entry[]>([]);
  let busy = $state(false);
  let error = $state('');
  let more = $state(false);
  let collapsed = $state<Record<string, boolean>>({});
  let filtersOpen = $state(false);
  const request = new AbortController();
  function entryKey(entry: Entry) {
    return `${entry.channel_id}@${entry.channel_domain}:${entry.message_id ?? ''}@${entry.message_domain ?? ''}`;
  }
  function target(entry: Entry) {
    const id = entry.message_id ?? entry.first_unread_message_id;
    const domain = entry.message_domain ?? entry.first_unread_message_domain;
    return id && domain ? `${id}@${domain}` : null;
  }
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
        tab !== 'unreads' ? `/users/@me/inbox/${tab}?${query}` : '/users/@me/read-states',
        { signal: request.signal }
      );
      if (request.signal.aborted) return;
      entries = append ? [...entries, ...values] : values;
      more = tab !== 'unreads' && values.length === 50;
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
    if (busy) return;
    busy = true;
    error = '';
    try {
      if (tab === 'bookmarks') {
        await api(`/users/@me/inbox/bookmarks/${encodeURIComponent(target(entry)!)}`, {
          method: 'DELETE'
        });
      } else
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
    const reference = target(entry);
    const around = reference ? `?around=${encodeURIComponent(reference)}` : '';
    onClose();
    await goto(resolve((path + around) as '/home'));
  }
  onMount(() => {
    dialog.showPopover();
    dialog.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')?.focus();
    void refresh();
    const timer = setInterval(() => {
      if (!busy && document.visibilityState === 'visible') void refresh();
    }, 30000);
    return () => {
      clearInterval(timer);
      request.abort();
    };
  });
  const visible = $derived(
    entries.filter(
      (entry) =>
        (tab !== 'unreads' || (entry.unread && (showMuted || !entry.muted))) &&
        (tab === 'bookmarks' || !guild || `${entry.guild_id}@${entry.guild_domain}` === guild)
    )
  );
</script>

<dialog
  bind:this={dialog}
  open
  popover="auto"
  ontoggle={(event) => {
    if (event.newState === 'closed') onClose();
  }}
  aria-label={$t('chat_inbox')}
  class="read-inbox"
  class:bookmark-inbox={tab === 'bookmarks'}
>
  <header class="inbox-header">
    <h2><Icon name="inbox" size={24} />{$t('chat_inbox')}</h2>
    <div class="inbox-actions">
      {#if tab !== 'bookmarks'}
        <button
          class="icon-action"
          disabled={busy}
          onclick={() => void mark()}
          title={$t('chat_mark_all_read')}
          aria-label={$t('chat_mark_all_read')}><Icon name="check" /></button
        >
        <button
          class="icon-action"
          class:active={filtersOpen}
          aria-expanded={filtersOpen}
          title={$t('chat_inbox_filter')}
          aria-label={$t('chat_inbox_filter')}
          onclick={() => (filtersOpen = !filtersOpen)}><Icon name="settings" /></button
        >
      {/if}
      <button
        class="icon-action"
        onclick={onClose}
        aria-label={$t('chat_close')}
        title={$t('chat_close')}><Icon name="x" /></button
      >
    </div>
  </header>
  <div class="inbox-tabs" role="tablist" aria-label={$t('chat_inbox')}>
    {#each tabs as name (name)}
      <button
        role="tab"
        id={`inbox-tab-${name}`}
        aria-controls="inbox-results"
        aria-selected={tab === name}
        disabled={busy}
        onclick={() => {
          tab = name;
          entries = [];
          more = false;
          void refresh();
        }}
        onkeydown={(event) => {
          if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            event.preventDefault();
            if (busy) return;
            tab =
              event.key === 'Home'
                ? tabs[0]
                : event.key === 'End'
                  ? tabs.at(-1)!
                  : tabs[
                      (tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) %
                        tabs.length
                    ];
            entries = [];
            more = false;
            dialog.querySelector<HTMLButtonElement>(`#inbox-tab-${tab}`)?.focus();
            void refresh();
          }
        }}
        >{name === 'bookmarks'
          ? 'Bookmarks'
          : name === 'unreads'
            ? $t('chat_unreads')
            : $t('chat_mentions')}</button
      >
    {/each}
  </div>
  {#if filtersOpen && tab !== 'bookmarks'}
    <div class="inbox-filters">
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
      {:else}<label><input type="checkbox" bind:checked={showMuted} />{$t('chat_show_muted')}</label
        >{/if}
      <button disabled={busy} onclick={() => void refresh()}>{$t('chat_refresh')}</button>
    </div>
  {/if}
  {#if error}<div class="inbox-error" role="alert">
      {error}<button disabled={busy} onclick={() => void refresh()}>{$t('chat_refresh')}</button>
    </div>{/if}
  <div
    class="inbox-entries"
    id="inbox-results"
    role="tabpanel"
    aria-labelledby={`inbox-tab-${tab}`}
    aria-busy={busy}
    tabindex="0"
  >
    {#each visible as entry (entryKey(entry))}
      {@const key = entryKey(entry)}
      {@const channel = chatEntities.channels.get(`${entry.channel_id}@${entry.channel_domain}`)}
      {@const server = entry.guild_id
        ? chatEntities.guilds.get(`${entry.guild_id}@${entry.guild_domain}`)
        : null}
      {@const category = channel?.parent_id
        ? chatEntities.channels.get(`${channel.parent_id}@${channel.parent_domain}`)
        : null}
      <article class="inbox-card">
        <div class="inbox-card-heading">
          <button class="inbox-conversation" onclick={() => void jump(entry)}>
            <span class="inbox-avatar"
              >{#if server?.icon_hash}<img
                  src={assetUrl(server.icon_hash, 'thumbnail_128', server)}
                  alt=""
                />{:else if server}{server.name.slice(0, 1)}{:else}<Icon
                  name="message"
                />{/if}</span
            >
            <span class="inbox-conversation-text"
              ><strong
                >{#if entry.guild_id}<span class="channel-hash">#</span>
                {/if}{entry.channel_name ||
                  channel?.recipients?.map(userDisplayName).join(', ') ||
                  $t('chat_direct_message')}</strong
              >
              <small
                >{server?.name ?? $t('chat_direct_message')}{#if category}
                  · {category.name}{/if}{#if tab === 'unreads' && entry.unread_count}
                  · {$t('chat_unread_count', { count: entry.unread_count })}{/if}</small
              >
            </span>
          </button>
          <button
            class="icon-action"
            disabled={busy}
            onclick={() => (tab !== 'unreads' ? void dismiss(entry) : void mark(entry))}
            title={tab === 'bookmarks'
              ? 'Remove Bookmark'
              : tab === 'mentions'
                ? $t('chat_dismiss')
                : $t('chat_mark_read')}
            aria-label={tab === 'bookmarks'
              ? 'Remove Bookmark'
              : tab === 'mentions'
                ? $t('chat_dismiss')
                : $t('chat_mark_read')}><Icon name={tab !== 'unreads' ? 'x' : 'check'} /></button
          >
          <button
            class="collapse-action"
            aria-expanded={!collapsed[key]}
            aria-label={$t(collapsed[key] ? 'chat_inbox_expand' : 'chat_inbox_collapse')}
            onclick={() => (collapsed[key] = !collapsed[key])}
            ><Icon name={collapsed[key] ? 'chevron-right' : 'chevron-down'} /></button
          >
        </div>
        {#if !collapsed[key]}
          {#if entry.can_read_history !== false}
            {#key `${tab}:${target(entry) ?? ''}`}
              <InboxMessagePreview
                channel={`${entry.channel_id}@${entry.channel_domain}`}
                target={target(entry)}
                mention={tab !== 'unreads'}
              />
            {/key}
          {/if}
          <button class="inbox-open" onclick={() => void jump(entry)}
            >{tab === 'bookmarks'
              ? 'Jump to Message'
              : $t(tab === 'mentions' ? 'chat_jump_to_mention' : 'chat_jump_to_read')}<Icon
              name="chevron-right"
              size={16}
            /></button
          >
        {/if}
      </article>
    {:else}
      {#if tab === 'bookmarks' && !busy && !error}
        <div class="bookmark-empty">
          <svg class="bookmark-book" viewBox="0 0 160 110" aria-hidden="true">
            <path fill="#7256ce" d="m9 67 60 29 78-26-18-39-60 19L26 29Z" />
            <path
              fill="#b4adff"
              stroke="#7b69dd"
              stroke-width="3"
              d="M20 25q30-6 53 18 25-27 54-22l19 47q-41 2-67 20Q52 69 12 67Z"
            />
            <path
              fill="#ddd9ff"
              d="M28 22q26-2 45 21l6 38Q57 61 23 59Zm49 21q20-22 48-22l12 36q-34 4-54 23Z"
            />
            <path
              fill="none"
              stroke="#9e90f5"
              stroke-width="3"
              d="m32 33 19 7m-15 3 22 9m-19 2 24 9m29-17 28-12m-25 21 29-12m-26 21 30-12"
            />
            <path fill="#d793ff" d="M88 27q8-13 16-8l11 24-12-3-5 11Z" />
          </svg>
          <h3>Collect your favorite memories</h3>
          <p>
            Right-click any message and pick <b>Bookmark Message</b>. Find it here, for your eyes
            only.
          </p>
          <div class="bookmark-example" aria-hidden="true">
            <span class="example-avatar">✦</span>
            <div>
              <b>Kaede</b><small>7:41 PM</small>
              <p>Concert tickets go on sale Friday at 10am!</p>
              <p>wait, did you see this</p>
              <p>the setlist for tonight looks unreal</p>
            </div>
            <span class="example-menu"
              ><svg width="14" height="16" viewBox="0 0 24 24"
                ><path fill="currentColor" d="M6 3h12v18l-6-4-6 4V3Z" /></svg
              >Bookmark Message</span
            >
          </div>
        </div>
      {:else}<div class="inbox-empty">
          <Icon name="inbox" size={40} /><strong
            >{busy ? $t('chat_loading') : $t('chat_caught_up')}</strong
          >
        </div>{/if}
    {/each}
    {#if more}<button class="load-more" disabled={busy} onclick={() => void refresh(true)}
        >{$t('chat_load_more')}</button
      >{/if}
  </div>
</dialog>

<style>
  .bookmark-empty {
    padding: 44px 0 40px;
    text-align: center;
  }
  .bookmark-book {
    width: 150px;
    height: 110px;
    margin: 0 auto 20px;
  }
  .bookmark-inbox .inbox-header {
    padding-block: 10px;
  }
  .bookmark-inbox {
    --surface: light-dark(#fffdf9, #0b0b0d);
    --surface-subtle: light-dark(#f8f4ee, #080809);
    --surface-hover: light-dark(#eee8df, #252529);
    --line: light-dark(#d2c9bd, #27272b);
    --text: light-dark(#28231f, #d6d6da);
    --text-soft: light-dark(#5f574f, #c8c8cc);
    --text-muted: light-dark(#756c63, #94949c);
    --purple: light-dark(#6b52a3, #8070ff);
  }

  .bookmark-empty h3 {
    font-family: inherit;
    font-size: 23px;
    margin: 0 0 10px;
  }
  .bookmark-empty > p {
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.6;
    margin: 0 12px 30px;
  }
  .bookmark-example {
    position: relative;
    display: flex;
    gap: 14px;
    padding: 16px;
    border: 1px solid var(--line);
    border-radius: 12px;
    text-align: left;
    background: var(--surface-subtle);
    font-size: 14px;
  }
  .example-avatar {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: #7653c5;
    color: #d7ccff;
    flex-shrink: 0;
    font-size: 26px;
  }
  .bookmark-example > div {
    min-width: 0;
  }
  .bookmark-example small {
    display: inline;
    margin-left: 8px;
  }
  .bookmark-example p {
    margin: 6px 0;
    color: var(--text-soft);
  }
  .example-menu {
    display: flex;
    align-items: center;
    gap: 8px;
    position: absolute;
    bottom: -8px;
    right: 16px;
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface-hover);
    font-size: 12px;
    font-weight: 600;
    box-shadow: 0 4px 12px #0004;
  }
  @media (max-width: 420px) {
    .bookmark-empty {
      padding: 28px 0 44px;
    }
    .bookmark-inbox .inbox-header {
      padding-block: 10px;
    }
    :global([data-theme='dark']) .bookmark-inbox {
      --surface: #0b0b0d;
      --surface-subtle: #080809;
      --surface-hover: #252529;
      --line: #27272b;
      --text: #d6d6da;
      --text-soft: #c8c8cc;
      --text-muted: #94949c;
      --purple: #8070ff;
    }
    .bookmark-empty h3 {
      font-family: inherit;
      font-size: 20px;
    }
    .bookmark-example {
      padding: 12px;
      gap: 8px;
      font-size: 12px;
    }
    .example-avatar {
      width: 30px;
      height: 30px;
    }
  }

  .read-inbox {
    position: fixed;
    inset: calc(var(--app-utility-height, 36px) + 6px) 12px auto auto;
    margin: 0;
    z-index: 1100;
    width: min(600px, calc(100vw - 24px));
    max-height: calc(100dvh - var(--app-utility-height, 36px) - 18px);
    padding: 0;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 12px;
    box-shadow: 0 16px 48px #0006;
    overflow: hidden;
  }
  .read-inbox:popover-open {
    display: flex;
    flex-direction: column;
  }
  .inbox-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 18px 20px;
    flex-shrink: 0;
  }
  h2 {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    font-size: 21px;
  }
  .inbox-actions {
    display: flex;
    gap: 6px;
  }
  button,
  select {
    font: inherit;
    color: var(--text-soft);
    border: 1px solid var(--line);
    background: var(--surface-subtle);
    border-radius: 7px;
    cursor: pointer;
  }
  button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
  button:focus-visible,
  select:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  button:disabled {
    opacity: 0.5;
    cursor: wait;
  }
  .icon-action {
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    flex-shrink: 0;
    padding: 0;
  }
  .inbox-tabs {
    display: flex;
    border-bottom: 1px solid var(--line);
    flex-shrink: 0;
  }
  .inbox-tabs button {
    flex: 1;
    min-width: 0;
    font-size: 13px;
    border: 0;
    border-bottom: 3px solid transparent;
    border-radius: 0;
    padding: 14px;
    background: transparent;
    font-weight: 700;
  }
  .inbox-tabs button[aria-selected='true'] {
    border-bottom-color: var(--accent);
    color: var(--accent-text);
  }
  #inbox-tab-bookmarks[aria-selected='true'] {
    color: var(--purple);
    border-bottom-color: var(--purple);
  }
  .inbox-filters {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 12px 20px;
    border-bottom: 1px solid var(--line);
    font-size: 12px;
  }
  .inbox-filters select {
    width: 100%;
    padding: 8px;
  }
  .inbox-filters label {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .inbox-filters button,
  .inbox-error button,
  .load-more {
    padding: 8px 12px;
  }
  .inbox-error {
    padding: 12px 20px;
    color: var(--danger);
    font-size: 13px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .inbox-entries {
    overflow-y: auto;
    overscroll-behavior: contain;
    min-height: 0;
    padding: 16px;
    scrollbar-width: thin;
  }
  .inbox-card {
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    background: var(--surface-subtle);
    margin-bottom: 12px;
  }
  .inbox-card:last-child {
    margin-bottom: 0;
  }
  .inbox-card-heading {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px;
    border-bottom: 1px solid var(--line-soft);
  }
  .inbox-conversation {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
    flex: 1;
    text-align: left;
    padding: 0;
    border: 0;
    background: transparent;
  }
  .inbox-avatar {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    flex-shrink: 0;
    border-radius: 10px;
    background: var(--surface);
    overflow: hidden;
    font-weight: 700;
  }
  .inbox-avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .inbox-conversation-text {
    min-width: 0;
  }
  strong,
  small {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  strong {
    font-size: 14px;
    color: var(--text);
  }
  small {
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 4px;
  }
  .channel-hash {
    color: var(--text-muted);
  }
  .collapse-action {
    display: grid;
    place-items: center;
    width: 28px;
    height: 34px;
    padding: 0;
    border: 0;
    background: transparent;
    flex-shrink: 0;
  }
  .inbox-open {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
    width: 100%;
    padding: 10px 14px;
    border: 0;
    border-top: 1px solid var(--line-soft);
    border-radius: 0;
    background: transparent;
    color: var(--accent-text);
    font-size: 12px;
    font-weight: 600;
  }
  .inbox-empty {
    display: flex;
    align-items: center;
    flex-direction: column;
    justify-content: center;
    gap: 16px;
    min-height: 220px;
    color: var(--text-muted);
  }
  @media (max-width: 600px) {
    .read-inbox {
      inset-inline: 8px;
      width: calc(100vw - 16px);
    }
    .inbox-header {
      padding: 12px;
    }
    .inbox-entries {
      padding: 10px;
    }
    .inbox-card-heading {
      padding: 10px;
      gap: 4px;
    }
    .inbox-conversation {
      gap: 8px;
    }
    .icon-action,
    .collapse-action {
      min-width: 40px;
      height: 40px;
    }
    .inbox-avatar {
      display: none;
    }
  }
</style>
