<script lang="ts">
  import {
    emojiCategories,
    groupCustomEmojis,
    loadUnicodeEmojis,
    type CustomEmojiOption,
    type EmojiOption
  } from '$lib/chat/emojis';
  import { canonicalReactionEmoji } from '$lib/chat/reactions';
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
  const customGroups = $derived(groupCustomEmojis(custom));

  function selectReaction(value: string) {
    const canonical = canonicalReactionEmoji(value);
    if (canonical) onSelect(canonical);
  }

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
    {#each customGroups as group (group.key)}
      <section class="custom-group" aria-labelledby={`reaction-guild-${group.key}`}>
        <h3 id={`reaction-guild-${group.key}`}>{group.name}</h3>
        <div class="grid">
          {#each group.emojis as emoji (`${emoji.id}@${emoji.origin_domain}`)}
            <button
              type="button"
              title={`:${emoji.name}: — ${group.name}`}
              onclick={() => selectReaction(emoji.value)}
            >
              <img src={emoji.url} alt={`:${emoji.name}:`} loading="lazy" />
            </button>
          {/each}
        </div>
      </section>
    {/each}
    {#if loading}
      <p>Loading emoji…</p>
    {:else if category !== 'custom' || needle}
      <div class="grid">
        {#each unicode as emoji (emoji.value)}
          <button type="button" title={emoji.name} onclick={() => selectReaction(emoji.value)}>
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
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(30px, 1fr));
    gap: 2px;
    min-width: 0;
    overflow-x: hidden;
    padding: 0 0.35rem 0.4rem;
    border-bottom: 1px solid var(--line-soft);
  }
  nav button {
    width: 100% !important;
    min-width: 0;
    min-height: 32px !important;
    justify-content: center !important;
    padding: 0 !important;
  }
  nav button.active {
    background: var(--surface-hover);
  }
  .results {
    min-height: 0;
    min-width: 0;
    overflow-x: hidden;
    overflow-y: auto;
    padding: 0.45rem;
  }
  .results p {
    color: var(--text-muted);
    text-align: center;
  }
  .custom-group + .custom-group {
    margin-top: 0.65rem;
  }
  .custom-group h3 {
    margin: 0 0 0.35rem;
    color: var(--text-muted);
    font-size: 0.72rem;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }
  .grid {
    display: grid;
    width: 100%;
    min-width: 0;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    gap: 2px;
  }
  .grid button {
    width: 100% !important;
    min-width: 0;
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
