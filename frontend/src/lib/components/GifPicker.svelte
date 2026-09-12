<script lang="ts">
  import { t } from '$lib/ui/locale';

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
  let results: HTMLDivElement;
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
    if (append && (loading || debounce || view !== 'browse')) return;
    request?.abort();
    const controller = new AbortController();
    request = controller;
    loading = true;
    if (!append) nextPage = null;
    error = '';
    const parameter = query.trim() ? `&query=${encodeURIComponent(query.trim())}` : '';
    try {
      const result = await api<GifPage>(`/gifs?page=${page}&limit=24${parameter}`, {
        signal: controller.signal
      });
      if (controller.signal.aborted) return;
      items = append ? [...items, ...result.items] : result.items;
      nextPage = result.next_page;
    } catch (caught) {
      if (controller.signal.aborted) return;
      error = userErrorMessage(caught, $t('ui_could_not_load_gifs_try_again_48c68a6d'));
      if (!append) items = [];
    } finally {
      if (request === controller) loading = false;
    }
  }

  function search() {
    if (debounce) clearTimeout(debounce);
    request?.abort();
    nextPage = null;
    if (view === 'browse')
      debounce = setTimeout(() => {
        debounce = null;
        void load();
      }, 300);
  }

  function show(next: 'browse' | 'favorites') {
    if (debounce) clearTimeout(debounce);
    debounce = null;
    view = next;
    query = '';
    if (next === 'browse') void load();
    void Promise.resolve().then(() => searchInput?.focus());
  }

  function observeEnd(node: HTMLElement) {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && nextPage && !error) void load(nextPage, true);
      },
      { root: results, rootMargin: '0px 0px 200px 0px' }
    );
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
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

<div
  class="gif-picker"
  role="dialog"
  aria-modal="false"
  aria-label={$t('ui_choose_a_gif_261f5249')}
>
  <header>
    <div class="gif-tabs" role="tablist" aria-label={$t('ui_gif_picker_sections_eea2a71e')}>
      <button
        type="button"
        role="tab"
        aria-selected={view === 'browse'}
        class:active={view === 'browse'}
        onclick={() => show('browse')}>{$t('ui_gifs_c275c265')}</button
      >
      <button
        type="button"
        role="tab"
        aria-selected={view === 'favorites'}
        class:active={view === 'favorites'}
        onclick={() => show('favorites')}>{$t('ui_favorites_7a1f2a83')}</button
      >
    </div>
    <button
      type="button"
      class="icon-button"
      onclick={onClose}
      aria-label={$t('ui_close_gif_picker_4883e82e')}>×</button
    >
  </header>
  <label class="gif-search">
    <span class="visually-hidden">{$t('ui_search_klipy_90f8c85f')}</span>
    <input
      bind:this={searchInput}
      bind:value={query}
      oninput={search}
      placeholder={$t('ui_search_klipy_90f8c85f')}
    />
  </label>
  <div class="gif-results" bind:this={results} aria-live="polite">
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
    {#if view === 'browse' && loading}<p class="gif-state">{$t('ui_loading_gifs_81089b1e')}</p>{/if}
    {#if view === 'browse' && error}
      <div class="gif-state" role="alert">
        <p class="form-error">{error}</p>
        <button
          type="button"
          disabled={loading}
          onclick={() => void load(nextPage ?? 1, nextPage !== null)}
          >{$t('ui_try_again_d8b8392e')}</button
        >
      </div>
    {/if}
    {#if view === 'browse' && !loading && !error && !items.length}<p class="gif-state">
        {$t('ui_no_gifs_found_0f57a370')}
      </p>{/if}
    {#if view === 'favorites' && !displayedItems.length}
      <p class="gif-state">
        {favorites.length
          ? $t('ui_no_favorites_match_that_search_7272a490')
          : $t('ui_favorite_gifs_with_the_star_to_find_them_here_fc06b654')}
      </p>
    {/if}
    {#if view === 'browse' && nextPage && !loading && !error}
      <div class="gif-state" style="height: 1px; width: 100%" use:observeEnd></div>
    {/if}
  </div>
  <footer>
    <span>{$t('ui_powered_by_fdc5d2e9')} <strong>{$t('ui_klipy_487eac26')}</strong></span>
  </footer>
</div>
