<script lang="ts">
  import {
    emojiCategories,
    loadUnicodeEmojis,
    type CustomEmojiOption,
    type EmojiOption
  } from '$lib/chat/emojis';
  import { onMount } from 'svelte';

  let {
    customEmojis = [],
    onSelect,
    onClose
  }: {
    customEmojis?: CustomEmojiOption[];
    onSelect: (value: string) => void;
    onClose: () => void;
  } = $props();
  let query = $state('');
  let category = $state<'custom' | EmojiOption['category']>('people');
  let unicodeEmojis = $state<EmojiOption[]>([]);
  let loading = $state(true);
  const needle = $derived(query.trim().toLowerCase());
  const unicode = $derived(
    unicodeEmojis
      .filter(
        (emoji) =>
          (!needle && category !== 'custom' && emoji.category === category) ||
          (needle &&
            (emoji.name.includes(needle) ||
              emoji.keywords.some((keyword) => keyword.includes(needle))))
      )
      .slice(0, 160)
  );
  const custom = $derived(
    customEmojis.filter(
      (emoji) =>
        (category === 'custom' || Boolean(needle)) &&
        (!needle ||
          emoji.name.toLowerCase().includes(needle) ||
          emoji.guild_name?.toLowerCase().includes(needle))
    )
  );

  onMount(async () => {
    try {
      unicodeEmojis = await loadUnicodeEmojis();
    } finally {
      loading = false;
    }
  });
</script>

<section class="reaction-picker" aria-label="Choose a reaction">
  <header>
    <strong>Add reaction</strong>
    <button type="button" onclick={onClose} aria-label="Close reaction picker">×</button>
  </header>
  <label>
    <span class="visually-hidden">Search reactions</span>
    <input bind:value={query} placeholder="Search emoji" />
  </label>
  <nav aria-label="Reaction categories">
    {#if customEmojis.length}
      <button
        class:active={!needle && category === 'custom'}
        type="button"
        title="Custom emoji"
        onclick={() => {
          category = 'custom';
          query = '';
        }}>✦</button
      >
    {/if}
    {#each emojiCategories as item (item.id)}
      <button
        class:active={!needle && category === item.id}
        type="button"
        title={item.label}
        onclick={() => {
          category = item.id;
          query = '';
        }}>{item.icon}</button
      >
    {/each}
  </nav>
  <div class="results">
    {#if custom.length}
      <div class="grid">
        {#each custom as emoji (`${emoji.id}@${emoji.origin_domain}`)}
          <button type="button" title={`:${emoji.name}:`} onclick={() => onSelect(emoji.value)}>
            <img src={emoji.url} alt={`:${emoji.name}:`} loading="lazy" />
          </button>
        {/each}
      </div>
    {/if}
    {#if loading}
      <p>Loading emoji…</p>
    {:else if category !== 'custom' || needle}
      <div class="grid">
        {#each unicode as emoji (emoji.value)}
          <button type="button" title={emoji.name} onclick={() => onSelect(emoji.value)}>
            {emoji.value}
          </button>
        {/each}
      </div>
    {/if}
    {#if !loading && !custom.length && !unicode.length}<p>No emoji found.</p>{/if}
  </div>
</section>

<style>
  .reaction-picker {
    display: grid;
    grid-template-rows: auto auto auto minmax(0, 1fr);
    width: min(340px, calc(100vw - 24px));
    height: min(390px, calc(100dvh - 24px));
    overflow: hidden;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.35rem 0.45rem 0.55rem;
  }
  header button {
    width: 30px !important;
    min-height: 30px !important;
    justify-content: center !important;
    padding: 0 !important;
    font-size: 1.15rem !important;
  }
  label {
    padding: 0 0.4rem 0.45rem;
  }
  input {
    width: 100%;
    min-height: 36px;
  }
  nav {
    display: flex;
    gap: 2px;
    overflow-x: auto;
    padding: 0 0.35rem 0.4rem;
    border-bottom: 1px solid var(--line-soft);
  }
  nav button {
    width: 34px !important;
    min-width: 34px;
    min-height: 32px !important;
    justify-content: center !important;
    padding: 0 !important;
  }
  nav button.active {
    background: var(--surface-hover);
  }
  .results {
    min-height: 0;
    overflow-y: auto;
    padding: 0.45rem;
  }
  .results p {
    color: var(--text-muted);
    text-align: center;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
  }
  .grid button {
    aspect-ratio: 1;
    min-height: 0 !important;
    justify-content: center !important;
    padding: 0 !important;
    font-size: 1.4rem !important;
  }
  .grid img {
    width: 28px;
    height: 28px;
    object-fit: contain;
  }
</style>
