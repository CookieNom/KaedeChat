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
  let category = $state<EmojiOption['category']>('people');
  let searchInput = $state<HTMLInputElement | null>(null);
  let unicodeEmojis = $state<EmojiOption[]>([]);
  let loading = $state(true);
  let loadFailed = $state(false);
  let visibleLimit = $state(240);
  const normalizedQuery = $derived(query.trim().toLowerCase());
  const matchingUnicode = $derived(
    unicodeEmojis.filter(
      (emoji) =>
        (!normalizedQuery && emoji.category === category) ||
        (normalizedQuery &&
          (emoji.name.includes(normalizedQuery) ||
            emoji.keywords.some((keyword) => keyword.includes(normalizedQuery))))
    )
  );
  const visibleUnicode = $derived(matchingUnicode.slice(0, visibleLimit));
  const matchingCustom = $derived(
    customEmojis.filter(
      (emoji) => !normalizedQuery || emoji.name.toLowerCase().includes(normalizedQuery)
    )
  );

  onMount(() => {
    searchInput?.focus();
    void loadUnicodeEmojis()
      .then((emojis) => {
        unicodeEmojis = emojis;
      })
      .catch(() => {
        loadFailed = true;
      })
      .finally(() => {
        loading = false;
      });
  });
</script>

<div class="emoji-picker" role="dialog" aria-modal="false" aria-label="Choose an emoji">
  <header>
    <strong>Emoji</strong>
    <button class="icon-button" type="button" onclick={onClose} aria-label="Close emoji picker"
      >×</button
    >
  </header>
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
      <span class="emoji-custom-tab" title="Guild emoji" aria-label="Guild emoji">K</span>
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
  <div class="emoji-results" aria-live="polite">
    {#if matchingCustom.length}
      <h3>Guild emoji</h3>
      <div class="emoji-grid custom-emojis">
        {#each matchingCustom as emoji (`${emoji.id}@${emoji.origin_domain}`)}
          <button type="button" title={`:${emoji.name}:`} onclick={() => onSelect(emoji.value)}>
            <img src={emoji.url} alt={`:${emoji.name}:`} loading="lazy" />
          </button>
        {/each}
      </div>
    {/if}
    <h3>
      {normalizedQuery
        ? 'Search results'
        : emojiCategories.find((item) => item.id === category)?.label}
    </h3>
    {#if loading}
      <p>Loading emoji…</p>
    {:else if loadFailed}
      <p>Could not load emoji.</p>
    {:else}
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
      {#if !matchingCustom.length && !matchingUnicode.length}<p>No emoji found.</p>{/if}
    {/if}
  </div>
  <footer>
    <span aria-hidden="true">{matchingUnicode[0]?.value ?? '😀'}</span>
    <small>{matchingUnicode[0]?.name ?? 'Choose an emoji'}</small>
  </footer>
</div>

<style>
  .emoji-picker {
    position: absolute;
    right: 0;
    bottom: calc(100% + 10px);
    z-index: 25;
    display: grid;
    width: min(390px, calc(100vw - 28px));
    max-height: min(520px, 65vh);
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
  .emoji-search {
    padding: 12px 14px 8px;
  }
  .emoji-search input {
    width: 100%;
  }
  nav {
    display: flex;
    gap: 3px;
    padding: 0 12px 9px;
    border-bottom: 1px solid var(--line-soft);
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
    min-height: 230px;
    overflow-y: auto;
    padding: 12px 14px;
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
  @media (max-width: 620px) {
    .emoji-picker {
      position: fixed;
      right: 8px;
      bottom: 82px;
      left: 8px;
      width: auto;
    }
  }
</style>
