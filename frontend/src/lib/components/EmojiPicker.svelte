<script lang="ts">
  import {
    emojiCategories,
    loadUnicodeEmojis,
    type CustomEmojiOption,
    type EmojiOption
  } from '$lib/chat/emojis';
  import type { StickerOption } from '$lib/chat/stickers';
  import { onMount } from 'svelte';

  let {
    customEmojis = [],
    stickers = [],
    onSelect,
    onStickerSelect,
    onClose
  }: {
    customEmojis?: CustomEmojiOption[];
    stickers?: StickerOption[];
    onSelect: (value: string) => void;
    onStickerSelect?: (value: string) => void;
    onClose: () => void;
  } = $props();
  let mode = $state<'emoji' | 'sticker'>('emoji');
  let query = $state('');
  let stickerQuery = $state('');
  let category = $state<'custom' | EmojiOption['category']>('people');
  let searchInput = $state<HTMLInputElement | null>(null);
  let stickerSearchInput = $state<HTMLInputElement | null>(null);
  let unicodeEmojis = $state<EmojiOption[]>([]);
  let loading = $state(true);
  let loadFailed = $state(false);
  let visibleLimit = $state(240);
  const normalizedQuery = $derived(query.trim().toLowerCase());
  const matchingUnicode = $derived(
    unicodeEmojis.filter(
      (emoji) =>
        (!normalizedQuery && category !== 'custom' && emoji.category === category) ||
        (normalizedQuery &&
          (emoji.name.includes(normalizedQuery) ||
            emoji.keywords.some((keyword) => keyword.includes(normalizedQuery))))
    )
  );
  const visibleUnicode = $derived(matchingUnicode.slice(0, visibleLimit));
  const matchingCustom = $derived(
    customEmojis.filter(
      (emoji) =>
        (category === 'custom' || Boolean(normalizedQuery)) &&
        (!normalizedQuery ||
          emoji.name.toLowerCase().includes(normalizedQuery) ||
          emoji.guild_name?.toLowerCase().includes(normalizedQuery))
    )
  );
  const matchingStickers = $derived(
    stickers.filter((sticker) => {
      const needle = stickerQuery.trim().toLowerCase();
      return (
        !needle ||
        sticker.name.toLowerCase().includes(needle) ||
        sticker.guild_name?.toLowerCase().includes(needle) ||
        sticker.description?.toLowerCase().includes(needle)
      );
    })
  );
  const stickerGroups = $derived.by(() => {
    const result: Array<{ key: string; name: string; stickers: StickerOption[] }> = [];
    for (const sticker of matchingStickers) {
      const key = `${sticker.guild_id}@${sticker.guild_domain}`;
      let group = result.find((item) => item.key === key);
      if (!group) {
        group = { key, name: sticker.guild_name ?? 'Guild stickers', stickers: [] };
        result.push(group);
      }
      group.stickers.push(sticker);
    }
    return result;
  });

  function selectMode(next: 'emoji' | 'sticker') {
    mode = next;
    void Promise.resolve().then(() => {
      if (next === 'emoji') searchInput?.focus();
      else stickerSearchInput?.focus();
    });
  }

  async function loadEmoji() {
    loading = true;
    loadFailed = false;
    try {
      unicodeEmojis = await loadUnicodeEmojis();
    } catch {
      unicodeEmojis = [];
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    searchInput?.focus();
    void loadEmoji();
  });
</script>

<div
  class="emoji-picker"
  role="dialog"
  aria-modal="false"
  aria-label={onStickerSelect ? 'Choose an emoji or sticker' : 'Choose an emoji'}
>
  <header>
    <div class="expression-tabs" role="tablist" aria-label="Expression type">
      <button
        class:active={mode === 'emoji'}
        type="button"
        role="tab"
        aria-selected={mode === 'emoji'}
        onclick={() => selectMode('emoji')}>Emoji</button
      >
      {#if onStickerSelect}
        <button
          class:active={mode === 'sticker'}
          type="button"
          role="tab"
          aria-selected={mode === 'sticker'}
          onclick={() => selectMode('sticker')}>Stickers</button
        >
      {/if}
    </div>
    <button class="icon-button" type="button" onclick={onClose} aria-label="Close expression picker"
      >×</button
    >
  </header>
  {#if mode === 'emoji'}
    <label class="emoji-search">
      <span class="visually-hidden">Search emoji</span>
      <input
        bind:this={searchInput}
        bind:value={query}
        oninput={() => (visibleLimit = 240)}
        placeholder="Search emoji"
      />
    </label>
    <nav aria-label="Emoji categories">
      {#if customEmojis.length}
        <button
          class:active={!normalizedQuery && category === 'custom'}
          class="emoji-custom-tab"
          type="button"
          title="Custom emoji"
          aria-label="Custom emoji"
          onclick={() => {
            category = 'custom';
            query = '';
          }}>✦</button
        >
      {/if}
      {#each emojiCategories as item (item.id)}
        <button
          class:active={!normalizedQuery && category === item.id}
          type="button"
          title={item.label}
          aria-label={item.label}
          onclick={() => {
            category = item.id;
            query = '';
            visibleLimit = 240;
          }}>{item.icon}</button
        >
      {/each}
    </nav>
    <div class="emoji-results" role="region" aria-label="Emoji results" aria-live="polite">
      {#if matchingCustom.length}
        <h3>Custom emoji from your guilds</h3>
        <div class="emoji-grid custom-emojis">
          {#each matchingCustom as emoji (`${emoji.id}@${emoji.origin_domain}`)}
            <button
              type="button"
              title={`:${emoji.name}:${emoji.guild_name ? ` — ${emoji.guild_name}` : ''}`}
              onclick={() => onSelect(emoji.value)}
            >
              <img src={emoji.url} alt={`:${emoji.name}:`} loading="lazy" />
            </button>
          {/each}
        </div>
      {/if}
      {#if category !== 'custom' || normalizedQuery}
        <h3>
          {normalizedQuery
            ? 'Unicode results'
            : emojiCategories.find((item) => item.id === category)?.label}
        </h3>
      {/if}
      {#if loading && category !== 'custom'}
        <p>Loading emoji…</p>
      {:else if loadFailed && category !== 'custom'}
        <div role="alert">
          <p class="form-error">Could not load emoji data. Check your connection and try again.</p>
          <button class="show-more" type="button" onclick={() => void loadEmoji()}>Try again</button
          >
        </div>
      {:else if category !== 'custom' || normalizedQuery}
        <div class="emoji-grid">
          {#each visibleUnicode as emoji (emoji.value)}
            <button type="button" title={emoji.name} onclick={() => onSelect(emoji.value)}
              >{emoji.value}</button
            >
          {/each}
        </div>
        {#if matchingUnicode.length > visibleUnicode.length}
          <button class="show-more" type="button" onclick={() => (visibleLimit += 240)}>
            Show more emoji
          </button>
        {/if}
      {/if}
      {#if !loading && !loadFailed && !matchingCustom.length && !matchingUnicode.length}
        <p>No emoji found.</p>
      {/if}
    </div>
    <footer>
      <span aria-hidden="true">{matchingUnicode[0]?.value ?? '😀'}</span>
      <small>{matchingUnicode[0]?.name ?? 'Choose an emoji'}</small>
    </footer>
  {:else}
    <label class="sticker-search">
      <span class="visually-hidden">Search stickers</span>
      <input
        bind:this={stickerSearchInput}
        bind:value={stickerQuery}
        placeholder="Search stickers"
      />
    </label>
    <div class="sticker-results" role="region" aria-label="Sticker results" aria-live="polite">
      {#each stickerGroups as group (group.key)}
        <section>
          <h3>{group.name}</h3>
          <div class="sticker-grid">
            {#each group.stickers as sticker (`${sticker.id}@${sticker.origin_domain}`)}
              <button
                type="button"
                title={sticker.description ?? sticker.name}
                onclick={() => onStickerSelect?.(sticker.value)}
              >
                <img src={sticker.url} alt={sticker.name} loading="lazy" />
                <span>{sticker.name}</span>
              </button>
            {/each}
          </div>
        </section>
      {:else}
        <p>{stickers.length ? 'No stickers found.' : 'No stickers are available yet.'}</p>
      {/each}
    </div>
  {/if}
</div>

<style>
  .emoji-picker {
    position: absolute;
    right: 0;
    bottom: calc(100% + 10px);
    z-index: 25;
    display: flex;
    flex-direction: column;
    width: min(390px, calc(100vw - 28px));
    height: min(520px, 65dvh);
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: var(--surface-raised);
    box-shadow: 0 22px 55px rgb(0 0 0 / 38%);
  }
  header,
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
  }
  header {
    border-bottom: 1px solid var(--line-soft);
  }
  .expression-tabs {
    display: flex;
    gap: 4px;
  }
  .expression-tabs button {
    padding: 8px 13px;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: var(--text-muted);
    font-weight: 750;
  }
  .expression-tabs button:hover,
  .expression-tabs button.active {
    background: var(--surface-hover);
    color: var(--text);
  }
  .emoji-search {
    padding: 12px 14px 8px;
  }
  .emoji-search input {
    width: 100%;
  }
  .sticker-search {
    padding: 12px 14px 8px;
  }
  .sticker-search input {
    width: 100%;
  }
  nav {
    display: flex;
    min-width: 0;
    gap: 3px;
    overflow-x: auto;
    padding: 0 12px 9px;
    border-bottom: 1px solid var(--line-soft);
    scrollbar-width: none;
  }
  nav::-webkit-scrollbar {
    display: none;
  }
  nav button,
  .emoji-custom-tab {
    display: grid;
    width: 38px;
    height: 36px;
    place-items: center;
    border: 0;
    border-radius: 10px;
    background: transparent;
    filter: grayscale(0.5);
  }
  nav button:hover,
  nav button.active {
    background: var(--surface-hover);
    filter: none;
  }
  .emoji-custom-tab {
    color: var(--accent);
    font-weight: 800;
  }
  .emoji-results {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 12px 14px;
    scrollbar-gutter: stable;
    touch-action: pan-y;
    -webkit-overflow-scrolling: touch;
  }
  .emoji-results h3 {
    margin: 3px 0 8px;
    color: var(--text-muted);
    font-size: 0.72rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }
  .emoji-grid {
    display: grid;
    grid-template-columns: repeat(8, minmax(0, 1fr));
    gap: 3px;
  }
  .emoji-grid button {
    display: grid;
    aspect-ratio: 1;
    min-width: 0;
    place-items: center;
    border: 0;
    border-radius: 9px;
    background: transparent;
    font-size: 1.55rem;
  }
  .emoji-grid button:hover {
    background: var(--surface-hover);
    transform: scale(1.08);
  }
  .custom-emojis {
    margin-bottom: 14px;
  }
  .custom-emojis img {
    width: 30px;
    height: 30px;
    object-fit: contain;
  }
  .emoji-results p {
    color: var(--text-muted);
    text-align: center;
  }
  .show-more {
    display: block;
    margin: 10px auto 2px;
    border: 0;
    background: transparent;
    color: var(--accent);
    font-weight: 750;
  }
  footer {
    justify-content: flex-start;
    gap: 10px;
    border-top: 1px solid var(--line-soft);
  }
  footer > span {
    font-size: 1.6rem;
  }
  footer small {
    color: var(--text-muted);
    text-transform: capitalize;
  }
  .sticker-results {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 8px 14px 14px;
    overscroll-behavior: contain;
  }
  .sticker-results h3 {
    margin: 10px 0 8px;
    color: var(--text-muted);
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .sticker-results > p {
    color: var(--text-muted);
    text-align: center;
  }
  .sticker-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }
  .sticker-grid button {
    display: grid;
    gap: 3px;
    min-width: 0;
    padding: 7px;
    border: 0;
    border-radius: 12px;
    background: transparent;
    color: var(--text-muted);
    font-size: 0.7rem;
  }
  .sticker-grid button:hover {
    background: var(--surface-hover);
    color: var(--text);
  }
  .sticker-grid img {
    width: 100%;
    aspect-ratio: 1;
    object-fit: contain;
  }
  .sticker-grid span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  @media (max-width: 620px) {
    .emoji-picker {
      position: fixed;
      right: 8px;
      bottom: 82px;
      left: 8px;
      width: auto;
      height: min(520px, calc(100dvh - 98px));
    }
  }
</style>
