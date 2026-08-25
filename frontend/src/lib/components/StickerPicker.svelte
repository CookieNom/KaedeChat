<script lang="ts">
  import type { StickerOption } from '$lib/chat/stickers';
  import { onMount } from 'svelte';

  let {
    stickers = [],
    onSelect,
    onClose
  }: {
    stickers?: StickerOption[];
    onSelect: (value: string) => void;
    onClose: () => void;
  } = $props();
  let query = $state('');
  let searchInput = $state<HTMLInputElement | null>(null);
  const matching = $derived(
    stickers.filter((sticker) => {
      const needle = query.trim().toLowerCase();
      return (
        !needle ||
        sticker.name.toLowerCase().includes(needle) ||
        sticker.guild_name?.toLowerCase().includes(needle) ||
        sticker.description?.toLowerCase().includes(needle)
      );
    })
  );
  const groups = $derived.by(() => {
    const result: Array<{ key: string; name: string; stickers: StickerOption[] }> = [];
    for (const sticker of matching) {
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
  onMount(() => searchInput?.focus());
</script>

<div class="sticker-picker" role="dialog" aria-modal="false" aria-label="Choose a sticker">
  <header>
    <strong>Stickers</strong><button
      type="button"
      onclick={onClose}
      aria-label="Close sticker picker">×</button
    >
  </header>
  <label
    ><span class="visually-hidden">Search stickers</span><input
      bind:this={searchInput}
      bind:value={query}
      placeholder="Search stickers"
    /></label
  >
  <div class="sticker-results">
    {#each groups as group (group.key)}
      <section>
        <h3>{group.name}</h3>
        <div class="sticker-grid">
          {#each group.stickers as sticker (`${sticker.id}@${sticker.origin_domain}`)}
            <button
              type="button"
              title={sticker.description ?? sticker.name}
              onclick={() => onSelect(sticker.value)}
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
</div>

<style>
  .sticker-picker {
    position: absolute;
    right: 0;
    bottom: calc(100% + 10px);
    z-index: 26;
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr);
    width: min(390px, calc(100vw - 28px));
    height: min(520px, 65dvh);
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: var(--surface-raised);
    box-shadow: 0 22px 55px rgb(0 0 0 / 38%);
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    border-bottom: 1px solid var(--line-soft);
  }
  header button {
    border: 0;
    background: transparent;
    color: var(--text-muted);
    font-size: 1.35rem;
  }
  label {
    padding: 12px 14px 8px;
  }
  input {
    width: 100%;
  }
  .sticker-results {
    min-height: 0;
    overflow-y: auto;
    padding: 8px 14px 14px;
    overscroll-behavior: contain;
  }
  h3 {
    margin: 10px 0 8px;
    color: var(--text-muted);
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
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
  img {
    width: 100%;
    aspect-ratio: 1;
    object-fit: contain;
  }
  span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
