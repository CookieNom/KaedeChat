<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { loadGifFavorites, saveGifFavorites, type GifPage, type GifResult } from '$lib/chat/gifs';
  import { onDestroy, onMount } from 'svelte';

  let { onSelect, onClose }: { onSelect: (gif: GifResult) => void; onClose: () => void } = $props();
  let query = $state('');
  let items = $state<GifResult[]>([]);
  let favorites = $state<GifResult[]>([]);
  let view = $state<'browse' | 'favorites'>('browse');
  let nextPage = $state<number | null>(null);
  let loading = $state(false);
  let error = $state('');
  let request: AbortController | null = null;
  let debounce: ReturnType<typeof setTimeout> | null = null;
  let searchInput = $state<HTMLInputElement | null>(null);
  const displayedItems = $derived(
    view === 'browse'
      ? items
      : favorites.filter((gif) => gif.title.toLowerCase().includes(query.trim().toLowerCase()))
  );

  function saveFavorites() {
    saveGifFavorites(favorites);
  }

  function isFavorite(gif: GifResult) {
    return favorites.some((favorite) => favorite.id === gif.id);
  }

  function toggleFavorite(gif: GifResult) {
    favorites = isFavorite(gif)
      ? favorites.filter((favorite) => favorite.id !== gif.id)
      : [gif, ...favorites].slice(0, 100);
    saveFavorites();
  }

  async function load(page = 1, append = false) {
    request?.abort();
    const controller = new AbortController();
    request = controller;
    loading = true;
    error = '';
    const parameter = query.trim() ? `&query=${encodeURIComponent(query.trim())}` : '';
    try {
      const result = await api<GifPage>(`/gifs?page=${page}&limit=24${parameter}`, {
        signal: controller.signal
      });
      items = append ? [...items, ...result.items] : result.items;
      nextPage = result.next_page;
    } catch (caught) {
      if (controller.signal.aborted) return;
      error = userErrorMessage(caught, 'Could not load GIFs. Try again.');
      if (!append) items = [];
    } finally {
      if (request === controller) loading = false;
    }
  }

  function search() {
    if (debounce) clearTimeout(debounce);
    if (view === 'browse') debounce = setTimeout(() => void load(), 300);
  }

  function show(next: 'browse' | 'favorites') {
    if (debounce) clearTimeout(debounce);
    debounce = null;
    view = next;
    query = '';
    if (next === 'browse' && !items.length) void load();
    void Promise.resolve().then(() => searchInput?.focus());
  }

  function windowKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onClose();
  }

  onMount(() => {
    window.addEventListener('keydown', windowKeydown);
    favorites = loadGifFavorites();
    void load();
    searchInput?.focus();
  });

  onDestroy(() => {
    window.removeEventListener('keydown', windowKeydown);
    request?.abort();
    if (debounce) clearTimeout(debounce);
  });
</script>

<div class="gif-picker" role="dialog" aria-modal="false" aria-label="Choose a GIF">
  <header>
    <div class="gif-tabs" role="tablist" aria-label="GIF picker sections">
      <button
        type="button"
        role="tab"
        aria-selected={view === 'browse'}
        class:active={view === 'browse'}
        onclick={() => show('browse')}>GIFs</button
      >
      <button
        type="button"
        role="tab"
        aria-selected={view === 'favorites'}
        class:active={view === 'favorites'}
        onclick={() => show('favorites')}>Favorites</button
      >
    </div>
    <button type="button" class="icon-button" onclick={onClose} aria-label="Close GIF picker"
      >×</button
    >
  </header>
  <label class="gif-search">
    <span class="visually-hidden">Search KLIPY</span>
    <input bind:this={searchInput} bind:value={query} oninput={search} placeholder="Search KLIPY" />
  </label>
  <div class="gif-results" aria-live="polite">
    {#each displayedItems as gif (`${gif.id}:${gif.preview_url}`)}
      <div class="gif-result">
        <button
          type="button"
          class="gif-select"
          onclick={() => onSelect(gif)}
          aria-label={gif.title}
        >
          <img
            src={gif.preview_url}
            alt={gif.title}
            loading="lazy"
            width={gif.width ?? 240}
            height={gif.height ?? 160}
          />
          <span>{gif.title}</span>
        </button>
        <button
          type="button"
          class="gif-favorite"
          class:active={isFavorite(gif)}
          aria-label={isFavorite(gif)
            ? `Remove ${gif.title} from favorites`
            : `Favorite ${gif.title}`}
          aria-pressed={isFavorite(gif)}
          onclick={() => toggleFavorite(gif)}>★</button
        >
      </div>
    {/each}
    {#if view === 'browse' && loading && !items.length}<p class="gif-state">Loading GIFs…</p>{/if}
    {#if view === 'browse' && error}
      <div class="gif-state" role="alert">
        <p class="form-error">{error}</p>
        <button type="button" disabled={loading} onclick={() => void load()}>Try again</button>
      </div>
    {/if}
    {#if view === 'browse' && !loading && !error && !items.length}<p class="gif-state">
        No GIFs found.
      </p>{/if}
    {#if view === 'favorites' && !displayedItems.length}
      <p class="gif-state">
        {favorites.length
          ? 'No favorites match that search.'
          : 'Favorite GIFs with the star to find them here.'}
      </p>
    {/if}
  </div>
  <footer>
    <span>Powered by <strong>KLIPY</strong></span>
    {#if view === 'browse' && nextPage}
      <button type="button" disabled={loading} onclick={() => void load(nextPage ?? 1, true)}>
        {loading ? 'Loading…' : 'Load more'}
      </button>
    {/if}
  </footer>
</div>
