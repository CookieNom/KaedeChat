<script lang="ts">
  import { api, ApiError } from '$lib/api/client';
  import type { GifPage, GifResult } from '$lib/chat/gifs';
  import { onDestroy, onMount } from 'svelte';

  let { onSelect, onClose }: { onSelect: (gif: GifResult) => void; onClose: () => void } = $props();
  let query = $state('');
  let items = $state<GifResult[]>([]);
  let nextPage = $state<number | null>(null);
  let loading = $state(false);
  let error = $state('');
  let request: AbortController | null = null;
  let debounce: ReturnType<typeof setTimeout> | null = null;
  let searchInput = $state<HTMLInputElement | null>(null);

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
      error = caught instanceof ApiError ? caught.message : 'Could not load GIFs.';
      if (!append) items = [];
    } finally {
      if (request === controller) loading = false;
    }
  }

  function search() {
    if (debounce) clearTimeout(debounce);
    debounce = setTimeout(() => void load(), 300);
  }

  function windowKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onClose();
  }

  onMount(() => {
    window.addEventListener('keydown', windowKeydown);
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
    <strong>GIFs</strong>
    <button type="button" class="icon-button" onclick={onClose} aria-label="Close GIF picker"
      >×</button
    >
  </header>
  <label class="gif-search">
    <span class="visually-hidden">Search KLIPY</span>
    <input bind:this={searchInput} bind:value={query} oninput={search} placeholder="Search KLIPY" />
  </label>
  <div class="gif-results" aria-live="polite">
    {#each items as gif (`${gif.id}:${gif.preview_url}`)}
      <button type="button" class="gif-result" onclick={() => onSelect(gif)} aria-label={gif.title}>
        <img
          src={gif.preview_url}
          alt={gif.title}
          loading="lazy"
          width={gif.width ?? 240}
          height={gif.height ?? 160}
        />
        <span>{gif.title}</span>
      </button>
    {/each}
    {#if loading && !items.length}<p class="gif-state">Loading GIFs…</p>{/if}
    {#if error}<p class="gif-state form-error">{error}</p>{/if}
    {#if !loading && !error && !items.length}<p class="gif-state">No GIFs found.</p>{/if}
  </div>
  <footer>
    <span>Powered by <strong>KLIPY</strong></span>
    {#if nextPage}
      <button type="button" disabled={loading} onclick={() => void load(nextPage ?? 1, true)}>
        {loading ? 'Loading…' : 'Load more'}
      </button>
    {/if}
  </footer>
</div>
