<script lang="ts">
  /* eslint-disable svelte/no-navigation-without-resolve -- App Directory helpers validate internal path prefixes and resolve the configured base path. */
  import { replaceState } from '$app/navigation';
  import { resolve } from '$app/paths';
  import { page } from '$app/state';
  import { api, userErrorMessage } from '$lib/api/client';
  import { safeReturnPath } from '$lib/auth/return-path';
  import {
    canonicalDirectoryFilters,
    directoryDetailPath,
    directoryFiltersFromSearchParams,
    directoryPagePath,
    directoryQuery,
    directoryRestoredPageCount,
    type DirectoryApplicationSummary,
    type DirectoryCategory,
    type DirectoryCollection,
    type DirectoryCollectionSlug,
    type DirectoryFilters,
    type DirectoryPage
  } from '$lib/chat/application-directory';
  import { assetUrl } from '$lib/media/assets';
  import { resolveApplicationDirectoryPath } from '$lib/navigation/routes';
  import { onDestroy, onMount } from 'svelte';

  const categories = [
    ['', 'All apps'],
    ['entertainment', 'Entertainment'],
    ['games', 'Games'],
    ['moderation', 'Moderation'],
    ['productivity', 'Productivity'],
    ['social', 'Social'],
    ['utilities', 'Utilities']
  ] as const;
  const initialFilters = directoryFiltersFromSearchParams(page.url.searchParams);
  const sourcePath = safeReturnPath(page.url.searchParams.get('from'), page.url.origin);
  const initialPages = directoryRestoredPageCount(page.url.searchParams);
  let query = $state(initialFilters.query);
  let category = $state<DirectoryCategory | ''>(initialFilters.category);
  let domain = $state(initialFilters.domain);
  let collection = $state<DirectoryCollectionSlug | ''>(initialFilters.collection);
  let collections = $state<DirectoryCollection[]>([]);
  let applications = $state<DirectoryApplicationSummary[]>([]);
  let nextCursor = $state<string | null>(null);
  let appliedFilters = $state<DirectoryFilters>(initialFilters);
  let loadedPages = $state(initialPages);
  let loading = $state(false);
  let loadingMore = $state(false);
  let error = $state('');
  let requestController: AbortController | null = null;
  let requestGeneration = 0;
  const backPath = sourcePath ?? '/home';
  const currentDirectoryPath = $derived(directoryPagePath(appliedFilters, sourcePath, loadedPages));
  const resultStatus = $derived(
    loading
      ? 'Loading applications.'
      : loadingMore
        ? 'Loading more applications.'
        : error
          ? ''
          : applications.length
            ? `${applications.length} application${applications.length === 1 ? '' : 's'} shown.`
            : 'No applications match this search.'
  );

  function draftFilters(): DirectoryFilters {
    return canonicalDirectoryFilters({ query, category, domain, collection });
  }

  function applyDraft(filters: DirectoryFilters): void {
    query = filters.query;
    category = filters.category;
    domain = filters.domain;
    collection = filters.collection;
  }

  function updateLocation(filters: DirectoryFilters, pages: number): void {
    const path = directoryPagePath(filters, sourcePath, pages);
    replaceState(resolveApplicationDirectoryPath(path), page.state);
  }

  function beginRequest(kind: 'replace' | 'append'): {
    controller: AbortController;
    generation: number;
  } {
    requestController?.abort();
    const controller = new AbortController();
    requestController = controller;
    const generation = ++requestGeneration;
    loading = kind === 'replace';
    loadingMore = kind === 'append';
    error = '';
    return { controller, generation };
  }

  function requestIsCurrent(controller: AbortController, generation: number): boolean {
    return !controller.signal.aborted && requestGeneration === generation;
  }

  function finishRequest(controller: AbortController, generation: number): void {
    if (!requestIsCurrent(controller, generation)) return;
    loading = false;
    loadingMore = false;
    requestController = null;
  }

  async function search(filters = draftFilters(), pages = 1) {
    const canonical = canonicalDirectoryFilters(filters);
    const { controller, generation } = beginRequest('replace');
    applyDraft(canonical);
    appliedFilters = canonical;
    applications = [];
    nextCursor = null;
    loadedPages = 1;
    updateLocation(canonical, pages);
    try {
      const items: DirectoryApplicationSummary[] = [];
      let cursor: string | undefined;
      let result: DirectoryPage | null = null;
      let completedPages = 0;
      for (let pageNumber = 0; pageNumber < pages; pageNumber += 1) {
        result = await api<DirectoryPage>(directoryQuery(canonical, cursor), {
          signal: controller.signal
        });
        if (!requestIsCurrent(controller, generation)) return;
        items.push(...result.items);
        completedPages += 1;
        cursor = result.next_cursor ?? undefined;
        if (!cursor) break;
      }
      if (!requestIsCurrent(controller, generation) || !result) return;
      applications = items;
      nextCursor = result.next_cursor;
      collections = result.collections;
      appliedFilters = {
        ...canonical,
        collection: result.selected_collection ?? canonical.collection
      };
      collection = appliedFilters.collection;
      loadedPages = Math.max(1, completedPages);
      updateLocation(appliedFilters, loadedPages);
    } catch (caught) {
      if (requestIsCurrent(controller, generation)) {
        error = userErrorMessage(caught, 'Could not load the App Directory.');
      }
    } finally {
      finishRequest(controller, generation);
    }
  }

  async function loadMore() {
    if (!nextCursor || loading || loadingMore) return;
    const cursor = nextCursor;
    const filters = { ...appliedFilters };
    const { controller, generation } = beginRequest('append');
    try {
      const result = await api<DirectoryPage>(directoryQuery(filters, cursor), {
        signal: controller.signal
      });
      if (!requestIsCurrent(controller, generation)) return;
      const known = new Set(applications.map((application) => application.ref));
      applications = [
        ...applications,
        ...result.items.filter((application) => !known.has(application.ref))
      ];
      nextCursor = result.next_cursor;
      collections = result.collections;
      loadedPages += 1;
      updateLocation(filters, loadedPages);
    } catch (caught) {
      if (requestIsCurrent(controller, generation)) {
        error = userErrorMessage(caught, 'Could not load more applications.');
      }
    } finally {
      finishRequest(controller, generation);
    }
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    void search();
  }

  onMount(() => void search(initialFilters, initialPages));
  onDestroy(() => {
    requestGeneration += 1;
    requestController?.abort();
  });
</script>

<svelte:head><title>App Directory · Kaede Chat</title></svelte:head>

<main aria-busy={loading || loadingMore}>
  <nav><a href={resolve(backPath as '/home')}>← Back to Kaede</a></nav>
  <header class="hero">
    <span>APP DIRECTORY</span>
    <h1>Find apps for your community</h1>
    <p>
      Discover reviewed apps, then inspect their access before adding them to a server or account.
    </p>
    <form onsubmit={submit}>
      <input
        bind:value={query}
        aria-label="Search apps"
        maxlength="100"
        placeholder="Search apps"
      />
      <select bind:value={category} aria-label="Category">
        {#each categories as option (option[0])}<option value={option[0]}>{option[1]}</option
          >{/each}
      </select>
      <input
        bind:value={domain}
        aria-label="Instance"
        maxlength="253"
        placeholder="Instance (optional)"
      />
      <button>Search</button>
    </form>
  </header>
  {#if collections.length}
    <nav class="collections" aria-label="Curated app collections">
      <button
        class:active={!collection}
        aria-pressed={!collection}
        onclick={() => {
          collection = '';
          void search();
        }}>All apps</button
      >
      {#each collections as item (item.slug)}
        <button
          class:active={collection === item.slug}
          aria-pressed={collection === item.slug}
          title={item.description}
          onclick={() => {
            collection = item.slug;
            void search();
          }}>{item.name}</button
        >
      {/each}
    </nav>
  {/if}
  <p class="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
    {resultStatus}
  </p>
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
  {#if loading}
    <p class="state" role="status">Loading apps…</p>
  {:else if !error && applications.length === 0}
    <p class="state">No apps match this search.</p>
  {:else}
    <section class="grid" aria-label="Applications">
      {#each applications as application (application.ref)}
        {@const bannerUrl = application.banner_hash
          ? assetUrl(application.banner_hash, 'thumbnail_512', application.origin_domain)
          : ''}
        {@const iconUrl = application.icon_hash
          ? assetUrl(application.icon_hash, 'thumbnail_128', application.origin_domain)
          : ''}
        <a
          class="card"
          href={resolveApplicationDirectoryPath(
            directoryDetailPath(application.ref, currentDirectoryPath)
          )}
        >
          <div class="banner">
            {#if bannerUrl}<img src={bannerUrl} alt="" loading="lazy" />{/if}
          </div>
          <div class="body">
            <span class="icon">
              {#if iconUrl}<img src={iconUrl} alt="" loading="lazy" />{:else}{application.name
                  .slice(0, 1)
                  .toUpperCase()}{/if}
            </span>
            <div>
              <h2>
                {application.name}{#if application.verified}<small aria-label="Reviewed application"
                    ><span aria-hidden="true">✓</span></small
                  >{/if}
              </h2>
              <p>{application.summary}</p>
            </div>
          </div>
          <footer>
            {#if application.category}<span>{application.category}</span
              >{/if}{#each application.tags.slice(0, 2) as tag (tag)}<span>{tag}</span>{/each}
          </footer>
        </a>
      {/each}
    </section>
    {#if nextCursor}<button class="more" disabled={loadingMore} onclick={() => void loadMore()}
        >{loadingMore ? 'Loading…' : 'Load more'}</button
      >{/if}
  {/if}
</main>

<style>
  :global(body) {
    overflow: auto;
    background: var(--app-bg);
  }
  main {
    min-height: 100dvh;
    padding: 26px clamp(20px, 5vw, 72px) 72px;
    color: var(--text);
  }
  nav {
    max-width: 1180px;
    margin: 0 auto 22px;
  }
  nav a {
    color: var(--text-muted);
    text-decoration: none;
  }
  .hero {
    max-width: 1180px;
    margin: auto;
    padding: 44px;
    border-radius: 20px;
    background:
      radial-gradient(
        circle at 80% 0,
        color-mix(in srgb, var(--accent) 72%, transparent) 0,
        transparent 45%
      ),
      var(--surface);
  }
  .hero > span {
    color: var(--text-muted);
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.12em;
  }
  h1 {
    margin: 8px 0;
    font-size: clamp(30px, 5vw, 52px);
  }
  .hero p {
    color: var(--text-soft);
  }
  form {
    display: grid;
    grid-template-columns: minmax(180px, 1fr) repeat(2, minmax(150px, 210px)) auto;
    gap: 10px;
    margin-top: 26px;
  }
  input,
  select,
  button {
    box-sizing: border-box;
    border: 0;
    border-radius: 8px;
    padding: 12px 14px;
    font: inherit;
  }
  input,
  select {
    color: var(--text);
    background: var(--surface-raised);
  }
  button {
    cursor: pointer;
    color: var(--text-inverse);
    background: var(--accent);
    font-weight: 700;
  }
  .notice,
  .state {
    max-width: 1180px;
    margin: 24px auto;
    padding: 18px;
    border-radius: 8px;
    background: var(--surface);
  }
  .collections {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    max-width: 1180px;
    margin: 20px auto 0;
  }
  .collections button {
    flex: 0 0 auto;
    color: var(--text-soft);
    background: var(--surface);
  }
  .collections button.active {
    color: var(--text-inverse);
    background: var(--accent);
  }
  .notice {
    color: var(--danger);
  }
  .grid {
    max-width: 1180px;
    margin: 28px auto;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
    gap: 18px;
  }
  .card {
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 12px;
    color: inherit;
    background: var(--surface);
    text-decoration: none;
    transition:
      transform 0.15s,
      border-color 0.15s;
  }
  .card:hover {
    transform: translateY(-2px);
    border-color: var(--accent);
  }
  .banner {
    height: 92px;
    background: linear-gradient(135deg, var(--surface-hover), var(--surface-subtle));
  }
  .banner img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .body {
    display: grid;
    grid-template-columns: 52px 1fr;
    gap: 13px;
    padding: 16px;
  }
  .icon {
    display: grid;
    place-items: center;
    width: 52px;
    height: 52px;
    overflow: hidden;
    border-radius: 14px;
    background: var(--accent);
    font-size: 24px;
    font-weight: 800;
  }
  .icon img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  h2 {
    margin: 2px 0 6px;
    font-size: 18px;
  }
  h2 small {
    margin-left: 6px;
    color: var(--pine);
  }
  .body p {
    margin: 0;
    color: var(--text-muted);
    line-height: 1.4;
    display: -webkit-box;
    overflow: hidden;
    line-clamp: 2;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  footer {
    display: flex;
    gap: 6px;
    padding: 0 16px 16px;
  }
  footer span {
    padding: 4px 8px;
    border-radius: 999px;
    color: var(--text-muted);
    background: var(--surface-subtle);
    font-size: 11px;
    text-transform: capitalize;
  }
  .more {
    display: block;
    margin: 0 auto;
  }
  @media (max-width: 650px) {
    .hero {
      padding: 26px;
    }
    form {
      grid-template-columns: 1fr;
    }
  }
</style>
