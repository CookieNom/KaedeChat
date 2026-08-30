<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import {
    beginMessageSearchOperator,
    messageSearchOperator,
    moveSearchSuggestion,
    replaceMessageSearchOperator,
    type MessageSearchAuthorType,
    type MessageSearchOperator
  } from '$lib/chat/message-search';
  import { entityRef } from '$lib/chat/refs';
  import type {
    Channel,
    MessageSearchResponse,
    MessageSearchResult,
    UserSummary
  } from '$lib/chat/types';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import { assetUrl } from '$lib/media/assets';
  import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
  import Icon from './Icon.svelte';
  import MessageRow from './MessageRow.svelte';

  let {
    open = $bindable(false),
    scope,
    scopeRef,
    accountRef,
    channel,
    users = [],
    onJump,
    placement = 'dialog'
  }: {
    open: boolean;
    scope: 'channel' | 'guild' | 'dms';
    scopeRef: string | null;
    accountRef: string | null;
    channel?: Channel | null;
    users?: UserSummary[];
    onJump?: (result: MessageSearchResult) => void | Promise<void>;
    placement?: 'dialog' | 'header';
  } = $props();

  let query = $state('');
  let authorRef = $state('');
  let mentionRef = $state('');
  let has = $state<string[]>([]);
  let pinned = $state<'any' | 'yes' | 'no'>('any');
  let authorType = $state<'any' | MessageSearchAuthorType>('any');
  let sort = $state<'relevance' | 'newest' | 'oldest'>('relevance');
  let before = $state('');
  let after = $state('');
  let loading = $state(false);
  let error = $state('');
  let response = $state<MessageSearchResponse | null>(null);
  let cursor = $state<string | null>(null);
  let history = $state<string[]>([]);
  let loadedStorageKey = $state<string | null>(null);
  let configuredAccountRef = $state<string | null>(null);
  let featureEnabled = $state<boolean | null>(null);
  let advancedOpen = $state(false);
  let suggestionsOpen = $state(false);
  let highlightedSuggestion = $state(0);
  let searchInput: HTMLInputElement | null = $state(null);
  let searchRoot: HTMLDivElement | null = $state(null);
  const storageKey = $derived(accountRef ? `kaede.message-search.history.${accountRef}` : null);

  type SearchOperator = MessageSearchOperator;
  type Suggestion =
    | { kind: 'operator'; operator: SearchOperator; label: string; hint: string }
    | { kind: 'user'; operator: 'from' | 'mentions'; user: UserSummary }
    | { kind: 'content'; value: string }
    | { kind: 'advanced' };

  const contentKinds = ['image', 'video', 'audio', 'file', 'link', 'embed'] as const;
  const operatorMatch = $derived(messageSearchOperator(query));
  const activeOperator = $derived(operatorMatch?.operator ?? null);
  const operatorNeedle = $derived(operatorMatch?.needle ?? '');

  const encrypted = $derived(
    scope === 'channel' &&
      (channel?.encryption_mode === 'e2ee' || channel?.search_available === false)
  );
  const disabledByInstance = $derived(featureEnabled === false);
  const hasCriteria = $derived(
    Boolean(
      query.trim() ||
      authorRef ||
      mentionRef ||
      has.length ||
      pinned !== 'any' ||
      authorType !== 'any' ||
      before ||
      after
    )
  );
  const activeFilterCount = $derived(
    Number(Boolean(authorRef)) +
      Number(Boolean(mentionRef)) +
      has.length +
      Number(pinned !== 'any') +
      Number(authorType !== 'any') +
      Number(Boolean(before)) +
      Number(Boolean(after))
  );
  const uniqueUsers = $derived(
    [...new Map(users.map((user) => [entityRef(user), user])).values()].sort((a, b) =>
      userDisplayName(a).localeCompare(userDisplayName(b))
    )
  );
  const authorUser = $derived(authorRef ? userForRef(authorRef) : null);
  const mentionedUser = $derived(mentionRef ? userForRef(mentionRef) : null);
  const suggestions = $derived.by((): Suggestion[] => {
    if (activeOperator === 'from' || activeOperator === 'mentions') {
      return uniqueUsers
        .filter((user) => {
          const searchable = `${userDisplayName(user)} ${userPublicHandle(user)}`.toLowerCase();
          return !operatorNeedle || searchable.includes(operatorNeedle);
        })
        .slice(0, 8)
        .map((user) => ({ kind: 'user', operator: activeOperator, user }));
    }
    if (activeOperator === 'has') {
      return contentKinds
        .filter((value) => !operatorNeedle || value.includes(operatorNeedle))
        .map((value) => ({ kind: 'content', value }));
    }
    return [
      {
        kind: 'operator',
        operator: 'from',
        label: 'From a specific user',
        hint: 'from: user'
      },
      {
        kind: 'operator',
        operator: 'has',
        label: 'Includes a specific type of data',
        hint: 'has: link, embed or file'
      },
      {
        kind: 'operator',
        operator: 'mentions',
        label: 'Mentions a specific user',
        hint: 'mentions: user'
      },
      { kind: 'advanced' }
    ];
  });

  $effect(() => {
    if (
      !open ||
      !storageKey ||
      loadedStorageKey === storageKey ||
      typeof sessionStorage === 'undefined'
    )
      return;
    loadedStorageKey = storageKey;
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey) ?? '[]');
      history = Array.isArray(stored)
        ? stored.filter((item): item is string => typeof item === 'string').slice(0, 8)
        : [];
    } catch {
      sessionStorage.removeItem(storageKey);
      history = [];
    }
  });

  $effect(() => {
    if (!open || configuredAccountRef === accountRef) return;
    configuredAccountRef = accountRef;
    featureEnabled = null;
    const expected = accountRef;
    void loadAuthConfiguration()
      .then((configuration) => {
        if (configuredAccountRef === expected) {
          featureEnabled = configuration.message_search_enabled;
        }
      })
      .catch(() => {
        // The search request itself retains the structured retry/error path.
      });
  });

  function toggleHas(value: string) {
    has = has.includes(value) ? has.filter((item) => item !== value) : [...has, value];
  }

  function clearFilters() {
    authorRef = '';
    mentionRef = '';
    has = [];
    pinned = 'any';
    authorType = 'any';
    before = '';
    after = '';
  }

  function closeSearch() {
    open = false;
    advancedOpen = false;
    suggestionsOpen = false;
    response = null;
    error = '';
  }

  function resetSearch() {
    query = '';
    clearFilters();
    sort = 'relevance';
    cursor = null;
    closeSearch();
  }

  function dismissSuggestions() {
    suggestionsOpen = false;
    if (!response) open = false;
  }

  function dismissOnOutsidePointer(event: PointerEvent) {
    if (!open || placement !== 'header' || advancedOpen) return;
    const target = event.target;
    if (target instanceof Node && !searchRoot?.contains(target)) closeSearch();
  }

  function closeAdvanced() {
    advancedOpen = false;
    if (!response) {
      suggestionsOpen = true;
      queueMicrotask(() => searchInput?.focus());
    }
  }

  function focusSearch() {
    open = true;
    suggestionsOpen = true;
    highlightedSuggestion = 0;
  }

  function replaceOperator(value = '') {
    query = replaceMessageSearchOperator(query, value);
  }

  function beginOperator(operator: SearchOperator) {
    query = beginMessageSearchOperator(query, operator);
    highlightedSuggestion = 0;
    queueMicrotask(() => searchInput?.focus());
  }

  function selectSuggestion(suggestion: Suggestion) {
    if (suggestion.kind === 'operator') {
      beginOperator(suggestion.operator);
      return;
    }
    if (suggestion.kind === 'advanced') {
      suggestionsOpen = false;
      advancedOpen = true;
      return;
    }
    if (suggestion.kind === 'user') {
      replaceOperator();
      if (suggestion.operator === 'from') authorRef = entityRef(suggestion.user);
      else mentionRef = entityRef(suggestion.user);
    } else {
      replaceOperator();
      if (!has.includes(suggestion.value)) has = [...has, suggestion.value];
    }
    void runSearch();
  }

  function removeFilter(kind: 'author' | 'mention' | 'has', value = '') {
    if (kind === 'author') authorRef = '';
    else if (kind === 'mention') mentionRef = '';
    else has = has.filter((item) => item !== value);
    response = null;
    suggestionsOpen = true;
    queueMicrotask(() => searchInput?.focus());
  }

  function userForRef(reference: string) {
    return uniqueUsers.find((user) => entityRef(user) === reference) ?? null;
  }

  function handleSearchKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      if (suggestionsOpen) dismissSuggestions();
      else closeSearch();
      return;
    }
    if (!suggestionsOpen && event.key === 'ArrowDown') {
      suggestionsOpen = true;
      highlightedSuggestion = 0;
      event.preventDefault();
      return;
    }
    if (!suggestionsOpen || suggestions.length === 0) return;
    if (event.key === 'ArrowDown') {
      highlightedSuggestion = moveSearchSuggestion(highlightedSuggestion, 1, suggestions.length);
      event.preventDefault();
    } else if (event.key === 'ArrowUp') {
      highlightedSuggestion = moveSearchSuggestion(highlightedSuggestion, -1, suggestions.length);
      event.preventDefault();
    } else if (event.key === 'Enter' && (activeOperator || !query.trim())) {
      selectSuggestion(suggestions[highlightedSuggestion]);
      event.preventDefault();
    }
  }

  function rememberSearch() {
    if (typeof sessionStorage === 'undefined' || !storageKey || !query) return;
    const next = [query, ...history.filter((item) => item !== query)].slice(0, 8);
    history = next;
    sessionStorage.setItem(storageKey, JSON.stringify(next));
  }

  async function runSearch(next = false) {
    if (encrypted || disabledByInstance || loading || (!next && !hasCriteria)) return;
    loading = true;
    error = '';
    if (!next) {
      suggestionsOpen = false;
      advancedOpen = false;
    }
    try {
      const result = await api<MessageSearchResponse>('/search/messages', {
        method: 'POST',
        body: JSON.stringify({
          query,
          scope,
          scope_ref: scopeRef,
          sort,
          cursor: next ? cursor : null,
          limit: 25,
          filters: {
            authors: authorRef ? [authorRef] : [],
            mentions: mentionRef ? [mentionRef] : [],
            has,
            pinned: pinned === 'any' ? null : pinned === 'yes',
            author_type: authorType === 'any' ? null : authorType,
            before: before ? new Date(`${before}T23:59:59`).toISOString() : null,
            after: after ? new Date(`${after}T00:00:00`).toISOString() : null
          }
        })
      });
      response =
        next && response
          ? { ...result, results: [...response.results, ...result.results] }
          : result;
      cursor = result.next_cursor;
      rememberSearch();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not search messages. Try again.');
    } finally {
      loading = false;
    }
  }

  function jump(result: MessageSearchResult) {
    if (onJump) {
      void onJump(result);
      closeSearch();
      return;
    }
    const messageRef = entityRef(result.message);
    const base = result.guild
      ? guildChannelPath(result.guild, result.channel)
      : directMessagePath(result.channel);
    window.location.assign(`${base}?${new URLSearchParams({ around: messageRef })}`);
  }
</script>

<svelte:window onpointerdown={dismissOnOutsidePointer} />

<div bind:this={searchRoot} class:header-placement={placement === 'header'} class="message-search">
  {#if placement === 'header'}
    <form
      class:open
      class="header-search-box"
      role="search"
      onsubmit={(event) => {
        event.preventDefault();
        open = true;
        if (activeOperator && suggestions.length)
          selectSuggestion(suggestions[highlightedSuggestion]);
        else void runSearch();
      }}
    >
      <div class="search-composer">
        {#if authorUser}
          <button
            class="search-token"
            type="button"
            aria-label="Remove sender filter"
            onclick={() => removeFilter('author')}
          >
            <span>from:</span>{userDisplayName(authorUser)} <b>×</b>
          </button>
        {/if}
        {#if mentionedUser}
          <button
            class="search-token"
            type="button"
            aria-label="Remove mention filter"
            onclick={() => removeFilter('mention')}
          >
            <span>mentions:</span>{userDisplayName(mentionedUser)} <b>×</b>
          </button>
        {/if}
        {#each has as kind (kind)}
          <button
            class="search-token"
            type="button"
            aria-label={`Remove ${kind} content filter`}
            onclick={() => removeFilter('has', kind)}
          >
            <span>has:</span>{kind} <b>×</b>
          </button>
        {/each}
        <input
          bind:this={searchInput}
          bind:value={query}
          maxlength="512"
          aria-label="Search messages"
          placeholder={channel?.name ? `Search ${channel.name}` : 'Search'}
          onfocus={focusSearch}
          oninput={() => {
            open = true;
            suggestionsOpen = true;
            highlightedSuggestion = 0;
          }}
          onkeydown={handleSearchKeydown}
        />
      </div>
      {#if hasCriteria || response}
        <button class="search-clear" type="button" aria-label="Clear search" onclick={resetSearch}
          >×</button
        >
      {:else}
        <button type="submit" aria-label="Run message search" disabled={loading || !hasCriteria}>
          <Icon name="search" size={17} strokeWidth={2.2} />
        </button>
      {/if}
    </form>
  {/if}

  {#if open}
    {#if suggestionsOpen || advancedOpen || response}
      <div
        class:advanced-layer={advancedOpen && placement === 'header'}
        class:dialog-backdrop={placement === 'dialog'}
        class:header-layer={placement === 'header'}
        class:results-layer={Boolean(response) && !suggestionsOpen && !advancedOpen}
        class="search-layer"
        role="presentation"
      >
        {#if placement === 'dialog' || advancedOpen}
          <button
            class="backdrop-close"
            type="button"
            aria-label={advancedOpen ? 'Close advanced filters' : 'Close message search'}
            onclick={() => {
              if (advancedOpen) closeAdvanced();
              else closeSearch();
            }}
          ></button>
        {/if}
        <div
          class:advanced={advancedOpen}
          class:header-popover={placement === 'header'}
          class="search-panel"
          role="dialog"
          tabindex="-1"
          aria-modal={placement === 'dialog' ? 'true' : undefined}
          aria-label="Search messages"
          onkeydown={(event) => {
            if (event.key === 'Escape') {
              if (advancedOpen) closeAdvanced();
              else closeSearch();
            }
          }}
        >
          {#if placement === 'dialog' || advancedOpen || (response && !suggestionsOpen) || error || encrypted || disabledByInstance}
            <header>
              <div>
                <h2>
                  {advancedOpen
                    ? 'Filters'
                    : response
                      ? `${response.results.length}${cursor ? '+' : ''} results`
                      : 'Search messages'}
                </h2>
                {#if placement === 'dialog'}
                  <p>
                    Typo-tolerant search across {scope === 'guild'
                      ? 'this guild'
                      : scope === 'dms'
                        ? 'your direct messages'
                        : 'this conversation'}.
                  </p>
                {/if}
              </div>
              <div class="panel-tools">
                {#if (placement === 'dialog' || response) && !advancedOpen}
                  <button
                    class:active={advancedOpen || activeFilterCount > 0}
                    class="advanced-toggle"
                    type="button"
                    aria-expanded={advancedOpen}
                    onclick={() => {
                      advancedOpen = !advancedOpen;
                      suggestionsOpen = false;
                    }}
                  >
                    Filters{activeFilterCount ? ` · ${activeFilterCount}` : ''}
                  </button>
                {/if}
                <button
                  class="close"
                  type="button"
                  aria-label={advancedOpen ? 'Close advanced filters' : 'Close search'}
                  onclick={advancedOpen ? closeAdvanced : closeSearch}>×</button
                >
              </div>
            </header>
          {/if}

          {#if placement === 'dialog' && !advancedOpen}
            <form
              class="dialog-query"
              role="search"
              onsubmit={(event) => {
                event.preventDefault();
                void runSearch();
              }}
            >
              <label class="query"
                ><span class="visually-hidden">Search text</span><input
                  bind:value={query}
                  maxlength="512"
                  placeholder="Search messages"
                /></label
              >
              <button
                type="submit"
                aria-label="Run message search"
                disabled={loading || !hasCriteria}><Icon name="search" size={18} /></button
              >
            </form>
          {/if}

          {#if encrypted}
            <div class="encrypted-notice" role="status">
              <strong>Search is unavailable for this encrypted conversation.</strong>
              <span
                >End-to-end encrypted message bodies never leave your devices and are never indexed
                by Kaede.</span
              >
            </div>
          {:else if disabledByInstance}
            <div class="encrypted-notice" role="status">
              <strong>Message search is disabled on this instance.</strong>
              <span
                >Your instance administrator can enable the private search service during setup.</span
              >
            </div>
          {:else}
            {#if advancedOpen}
              <form
                class="advanced-filters"
                onsubmit={(event) => {
                  event.preventDefault();
                  void runSearch();
                }}
              >
                <div class="filters">
                  <label
                    >From
                    <select bind:value={authorRef}
                      ><option value="">Anyone</option
                      >{#each uniqueUsers as user (entityRef(user))}<option value={entityRef(user)}
                          >{userDisplayName(user)} · @{userPublicHandle(user)}</option
                        >{/each}</select
                    >
                  </label>
                  <label
                    >Mentions
                    <select bind:value={mentionRef}
                      ><option value="">Anyone</option
                      >{#each uniqueUsers as user (entityRef(user))}<option value={entityRef(user)}
                          >{userDisplayName(user)} · @{userPublicHandle(user)}</option
                        >{/each}</select
                    >
                  </label>
                  <label
                    >Sort<select bind:value={sort}
                      ><option value="relevance">Most relevant</option><option value="newest"
                        >Newest</option
                      ><option value="oldest">Oldest</option></select
                    ></label
                  >
                  <label
                    >Pinned<select bind:value={pinned}
                      ><option value="any">Either</option><option value="yes">Pinned</option><option
                        value="no">Not pinned</option
                      ></select
                    ></label
                  >
                  <label
                    >Author type<select bind:value={authorType}
                      ><option value="any">Anyone</option><option value="user">People</option
                      ><option value="bot">Bots</option><option value="webhook">Webhooks</option
                      ></select
                    ></label
                  >
                  <label>After<input type="date" bind:value={after} /></label>
                  <label>Before<input type="date" bind:value={before} /></label>
                </div>
                <fieldset>
                  <legend>Contains</legend>
                  <div class="chips">
                    {#each ['image', 'video', 'audio', 'file', 'link', 'embed'] as kind (kind)}<button
                        type="button"
                        class:active={has.includes(kind)}
                        aria-pressed={has.includes(kind)}
                        onclick={() => toggleHas(kind)}>{kind}</button
                      >{/each}
                  </div>
                </fieldset>
                <div class="form-actions">
                  <button class="clear-filters" type="button" onclick={clearFilters}
                    >Clear filters</button
                  >
                  <button class="submit" type="submit" disabled={loading || !hasCriteria}
                    >{loading ? 'Searching…' : 'Search'}</button
                  >
                </div>
              </form>
            {:else if suggestionsOpen}
              <section class="search-start" aria-label="Search options">
                <h3>
                  {activeOperator === 'from'
                    ? 'From user'
                    : activeOperator === 'mentions'
                      ? 'Mentions user'
                      : activeOperator === 'has'
                        ? 'Message contains'
                        : 'Filters'}
                </h3>
                {#if suggestions.length === 0}
                  <p class="suggestion-empty">No matching options.</p>
                {/if}
                {#each suggestions as suggestion, index (`${suggestion.kind}-${index}`)}
                  <button
                    class:highlighted={highlightedSuggestion === index}
                    type="button"
                    onpointerenter={() => (highlightedSuggestion = index)}
                    onclick={() => selectSuggestion(suggestion)}
                  >
                    {#if suggestion.kind === 'operator'}
                      <span class="quick-icon">
                        {#if suggestion.operator === 'from'}
                          <Icon name="user" size={19} />
                        {:else if suggestion.operator === 'has'}
                          <Icon name="image" size={19} />
                        {:else}
                          @
                        {/if}
                      </span>
                      <span
                        ><strong>{suggestion.label}</strong><small>{suggestion.hint}</small></span
                      >
                    {:else if suggestion.kind === 'user'}
                      <span class="quick-avatar" aria-hidden="true">
                        {#if suggestion.user.avatar_hash}
                          <img
                            src={assetUrl(
                              suggestion.user.avatar_hash,
                              'thumbnail_128',
                              suggestion.user
                            )}
                            alt=""
                          />
                        {:else}
                          {userDisplayName(suggestion.user).slice(0, 1).toUpperCase()}
                        {/if}
                      </span>
                      <span
                        ><strong>{userDisplayName(suggestion.user)}</strong><small
                          >@{userPublicHandle(suggestion.user)}</small
                        ></span
                      >
                    {:else if suggestion.kind === 'content'}
                      <span class="quick-icon"><Icon name="image" size={19} /></span>
                      <span><strong>{suggestion.value}</strong></span>
                    {:else}
                      <span class="quick-icon"><Icon name="settings" size={19} /></span>
                      <span
                        ><strong>More filters</strong><small
                          >dates, author type, pins and more</small
                        ></span
                      >
                    {/if}
                  </button>
                {/each}
              </section>
            {/if}

            {#if history.length && suggestionsOpen && !activeOperator}
              <section class="history" aria-label="Recent searches">
                <div>
                  <strong>Recent searches</strong><button
                    type="button"
                    onclick={() => {
                      history = [];
                      if (storageKey) sessionStorage.removeItem(storageKey);
                    }}>Clear</button
                  >
                </div>
                <div class="history-list">
                  {#each history as item (item)}<button
                      type="button"
                      onclick={() => {
                        query = item;
                        void runSearch();
                      }}><Icon name="search" size={17} />{item}</button
                    >{/each}
                </div>
              </section>
            {/if}
          {/if}

          {#if error}<p class="error" role="alert">{error}</p>{/if}
          {#if response && !suggestionsOpen && !advancedOpen}
            {#if response.indexing}<p class="partial" role="status">
                Search is catching up with recent messages. Results may be incomplete for a moment.
              </p>{/if}
            {#if response.coverage.authority === 'unavailable' || response.coverage.authority === 'unsupported'}<p
                class="partial"
                role="status"
              >
                Showing locally cached matches. The conversation’s home instance could not provide
                complete results.
              </p>{:else if response.coverage.local === 'cached' && response.coverage.authority === 'not_queried'}<p
                class="partial"
                role="status"
              >
                Account-wide direct-message search uses this home’s recent federated cache. Search
                inside a conversation to ask its authority for complete results.
              </p>{/if}
            {#if response.encrypted_channel_refs.length}<p class="partial">
                Encrypted channels were excluded from these results.
              </p>{/if}
            <div class="results" aria-live="polite">
              {#if response.results.length === 0}<p class="empty">
                  No messages matched those filters.
                </p>{/if}
              {#each response.results as result (entityRef(result.message))}
                <article class="result">
                  <button class="result-context" type="button" onclick={() => jump(result)}>
                    <span
                      >{result.guild?.name ??
                        result.channel.recipients?.map(userDisplayName).join(', ') ??
                        'Direct message'} · {result.channel.name ?? 'conversation'}</span
                    >
                    <span>Jump</span>
                  </button>
                  <MessageRow
                    message={result.message}
                    mentionUsers={uniqueUsers}
                    domIdPrefix="search-result"
                    actionsEnabled={false}
                    timestampFormat="date-time"
                  />
                  {#if !result.message.content && result.snippet}
                    <p class="remote-snippet">{result.snippet}</p>
                  {/if}
                </article>
              {/each}
            </div>
            {#if cursor}<button
                class="more"
                type="button"
                disabled={loading}
                onclick={() => void runSearch(true)}>Load more</button
              >{/if}
          {/if}
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .message-search {
    position: relative;
    min-width: 0;
  }
  .header-placement {
    z-index: 1301;
  }
  .header-search-box {
    width: clamp(12rem, 18vw, 17rem);
    height: 2rem;
    display: flex;
    align-items: center;
    border: 1px solid var(--border, #34363d);
    border-radius: 9px;
    background: color-mix(in srgb, var(--surface, #151619) 86%, #000);
    transition:
      border-color 120ms ease,
      box-shadow 120ms ease,
      width 160ms ease;
  }
  .header-search-box:focus-within,
  .header-search-box.open {
    border-color: color-mix(in srgb, var(--accent, #ff8068) 58%, var(--border, #34363d));
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent, #ff8068) 16%, transparent);
  }
  .search-composer {
    min-width: 0;
    height: 100%;
    flex: 1;
    display: flex;
    align-items: center;
    gap: 0.22rem;
    overflow: hidden;
    padding-left: 0.3rem;
  }
  .header-search-box input {
    min-width: 0;
    min-height: 0;
    flex: 1;
    height: 100%;
    padding: 0 0.2rem;
    border: 0;
    border-radius: inherit;
    outline: 0;
    box-shadow: none;
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: 0.86rem;
    font-weight: 650;
  }
  .header-search-box input:hover,
  .header-search-box input:focus,
  .header-search-box input:focus-visible {
    min-height: 0;
    border: 0;
    outline: 0;
    box-shadow: none;
  }
  .header-search-box input::placeholder {
    color: var(--muted, #aaa);
    font-weight: 600;
  }
  .header-search-box > button,
  .dialog-query button {
    width: 2rem;
    height: 100%;
    display: grid;
    place-items: center;
    flex: none;
    border: 0;
    background: transparent;
    color: var(--muted, #aaa);
  }
  .header-search-box > button:hover:not(:disabled),
  .dialog-query button:hover:not(:disabled) {
    color: var(--text, #fff);
  }
  .search-token {
    min-width: max-content;
    height: 1.45rem;
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    padding: 0 0.32rem;
    border: 0;
    border-radius: 4px;
    background: color-mix(in srgb, var(--accent, #ff8068) 18%, var(--surface-raised, #202126));
    color: var(--text, #fff);
    font-size: 0.72rem;
    white-space: nowrap;
  }
  .search-token:hover {
    background: color-mix(in srgb, var(--accent, #ff8068) 28%, var(--surface-raised, #202126));
  }
  .search-token span {
    color: var(--accent, #ff8068);
    font-weight: 800;
  }
  .search-token b {
    color: var(--muted, #aaa);
    font-size: 0.9rem;
  }
  .search-layer.header-layer {
    position: absolute;
    top: calc(100% + 0.35rem);
    right: 0;
    z-index: 1000;
  }
  .search-layer.header-layer.advanced-layer {
    position: fixed;
    inset: 0;
    z-index: 1100;
    display: grid;
    place-items: center;
    padding: 1rem;
    background: rgb(0 0 0 / 68%);
  }
  .search-layer.header-layer.results-layer {
    position: fixed;
    top: 3.75rem;
    right: 0;
    bottom: 0;
    z-index: 1200;
  }
  .search-layer.dialog-backdrop {
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: #000a;
    display: grid;
    place-items: center;
    padding: 1rem;
  }
  .backdrop-close {
    position: absolute;
    inset: 0;
    border: 0;
    background: transparent;
  }
  .search-panel {
    position: relative;
    width: min(620px, calc(100vw - 2rem));
    max-height: min(860px, calc(100dvh - 2rem));
    overflow: auto;
    background: var(--surface, #151619);
    border: 1px solid var(--border, #34363d);
    border-radius: 14px;
    padding: 1rem;
    color: inherit;
    box-shadow: 0 24px 80px #0009;
  }
  .search-panel.advanced {
    width: min(820px, calc(100vw - 2rem));
  }
  .search-panel.header-popover {
    width: min(410px, calc(100vw - 1rem));
    max-height: min(640px, calc(100dvh - 4.5rem));
    padding: 0.5rem;
    border-radius: 9px;
    box-shadow: 0 12px 32px #000a;
  }
  .search-panel.header-popover.advanced {
    width: min(520px, calc(100vw - 2rem));
    max-height: min(720px, calc(100dvh - 2rem));
    padding: 0.9rem;
    border-radius: 12px;
  }
  .results-layer .search-panel.header-popover {
    width: min(440px, 100vw);
    height: 100%;
    max-height: none;
    display: flex;
    flex-direction: column;
    border-radius: 0;
    border-top: 0;
    border-bottom: 0;
    padding: 0.8rem;
    box-shadow: -12px 20px 40px #0008;
  }
  .search-panel header {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: center;
    margin-bottom: 0.75rem;
  }
  .search-panel h2 {
    margin: 0;
    font-size: 1.05rem;
  }
  .search-panel header p {
    margin: 0.2rem 0 0;
    color: var(--muted, #aaa);
    font-size: 0.85rem;
  }
  .panel-tools {
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }
  .advanced-toggle {
    min-height: 2rem;
    padding: 0 0.7rem;
    border: 1px solid var(--border, #34363d);
    border-radius: 8px;
    background: transparent;
    color: var(--muted, #aaa);
    font-weight: 700;
  }
  .advanced-toggle:hover,
  .advanced-toggle.active {
    color: var(--text, #fff);
    border-color: color-mix(in srgb, var(--accent, #ff8068) 55%, var(--border, #34363d));
    background: color-mix(in srgb, var(--accent, #ff8068) 13%, transparent);
  }
  .close {
    width: 2rem;
    height: 2rem;
    border: 0;
    background: transparent;
    color: inherit;
    font-size: 1.65rem;
    line-height: 1;
  }
  .dialog-query {
    display: flex;
    align-items: center;
    min-height: 2.8rem;
    margin-bottom: 0.85rem;
    border: 1px solid var(--border, #444);
    border-radius: 10px;
    background: var(--surface-raised, #202126);
  }
  .dialog-query .query {
    flex: 1;
  }
  .query input {
    width: 100%;
    font-size: 1.05rem;
    padding: 0.75rem 0.9rem;
    border: 0;
    outline: 0;
    background: transparent;
    color: inherit;
  }
  .advanced-filters {
    padding-top: 0.15rem;
  }
  .header-popover.advanced .filters {
    grid-template-columns: 1fr;
    gap: 0.58rem;
    margin: 0.6rem 0 0.75rem;
  }
  .header-popover.advanced .filters label {
    gap: 0.25rem;
    font-size: 0.8rem;
  }
  .header-popover.advanced .filters select,
  .header-popover.advanced .filters input {
    padding: 0.55rem 0.65rem;
    border-radius: 8px;
  }
  .filters {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.75rem;
    margin: 1rem 0;
  }
  .filters label {
    display: grid;
    gap: 0.35rem;
    font-weight: 650;
  }
  .filters select,
  .filters input {
    min-width: 0;
    padding: 0.7rem;
    border-radius: 10px;
    border: 1px solid var(--border, #444);
    background: var(--surface-raised, #202126);
    color: inherit;
  }
  fieldset {
    border: 0;
    padding: 0;
    margin: 0;
  }
  legend {
    font-weight: 700;
    margin-bottom: 0.5rem;
  }
  .chips {
    display: flex;
    gap: 0.45rem;
    flex-wrap: wrap;
  }
  .chips button {
    padding: 0.5rem 0.75rem;
    border-radius: 999px;
    border: 1px solid var(--border, #444);
    background: transparent;
    color: inherit;
    text-transform: capitalize;
  }
  .chips button.active {
    background: var(--accent, #ff8068);
    color: #111;
  }
  .search-start {
    display: grid;
    gap: 0.05rem;
    padding: 0;
  }
  .search-start h3 {
    margin: 0.2rem 0.5rem 0.35rem;
    color: var(--muted, #aaa);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }
  .search-start > button {
    display: grid;
    grid-template-columns: 1.8rem minmax(0, 1fr);
    gap: 0.5rem;
    align-items: center;
    width: 100%;
    padding: 0.45rem 0.5rem;
    border: 0;
    border-radius: 9px;
    background: transparent;
    color: inherit;
    text-align: left;
  }
  .search-start > button:hover,
  .search-start > button:focus-visible,
  .search-start > button.highlighted {
    background: var(--surface-raised, #202126);
  }
  .quick-avatar {
    width: 1.65rem;
    height: 1.65rem;
    display: grid;
    place-items: center;
    border-radius: 50%;
    background: color-mix(in srgb, var(--accent, #ff8068) 30%, var(--surface-raised, #202126));
    color: var(--text, #fff);
    font-size: 0.76rem;
    font-weight: 850;
    overflow: hidden;
  }
  .quick-avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .suggestion-empty {
    margin: 0;
    padding: 0.8rem 0.55rem;
    color: var(--muted, #aaa);
    font-size: 0.85rem;
  }
  .search-start > button > span:last-child {
    display: grid;
    gap: 0;
  }
  .search-start strong {
    font-size: 0.86rem;
    line-height: 1.25;
  }
  .search-start small {
    color: var(--muted, #aaa);
    font-size: 0.74rem;
    line-height: 1.25;
  }
  .quick-icon {
    display: grid;
    place-items: center;
    color: var(--muted, #aaa);
    font-size: 1.15rem;
    font-weight: 800;
  }
  .submit,
  .more {
    margin-top: 1rem;
    padding: 0.7rem 1rem;
    border: 0;
    border-radius: 10px;
    background: var(--accent, #ff8068);
    color: #111;
    font-weight: 750;
  }
  .form-actions {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    align-items: center;
  }
  .header-popover.advanced .form-actions {
    position: sticky;
    bottom: -0.9rem;
    margin: 0.75rem -0.9rem -0.9rem;
    padding: 0.65rem 0.9rem 0.8rem;
    border-top: 1px solid var(--border, #34363d);
    background: var(--surface, #151619);
  }
  .clear-filters {
    margin-top: 1rem;
    padding: 0.7rem 0;
    border: 0;
    background: transparent;
    color: var(--accent, #ff8068);
    font-weight: 700;
  }
  .history {
    margin-top: 0.7rem;
    padding: 0.85rem 0.65rem 0;
    border-top: 1px solid var(--border, #34363d);
  }
  .history > div:first-child {
    display: flex;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }
  .history > div:first-child button {
    border: 0;
    background: transparent;
    color: var(--accent, #ff8068);
  }
  .history-list {
    display: grid;
    gap: 0.15rem;
  }
  .history-list button {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    width: 100%;
    padding: 0.5rem 0;
    border: 0;
    background: transparent;
    color: inherit;
    text-align: left;
  }
  .history-list button:hover {
    color: var(--accent, #ff8068);
  }
  .results {
    display: grid;
    grid-auto-rows: max-content;
    align-content: start;
    gap: 0.5rem;
    margin-top: 1rem;
  }
  .results-layer .results {
    min-height: 0;
    flex: 1;
    overflow-y: auto;
    margin-top: 0.35rem;
    padding: 0.1rem 0.2rem 0.5rem 0;
  }
  .results-layer .result {
    padding: 0;
    border-radius: 9px;
  }
  .result {
    display: grid;
    min-width: 0;
    gap: 0.15rem;
    border: 1px solid var(--border, #34363d);
    border-radius: 12px;
    background: var(--surface-raised, #202126);
    color: inherit;
    overflow: hidden;
  }
  .result:hover,
  .result:focus-within {
    border-color: var(--accent, #ff8068);
  }
  .result-context {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    width: 100%;
    padding: 0.48rem 0.65rem;
    border: 0;
    border-bottom: 1px solid var(--border, #34363d);
    background: color-mix(in srgb, var(--surface, #151619) 72%, transparent);
    color: var(--muted, #aaa);
    font-size: 0.75rem;
    text-align: left;
  }
  .result-context span:first-child {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .result-context span:last-child {
    flex: none;
    color: var(--accent, #ff8068);
    font-weight: 750;
  }
  .result-context:hover span:last-child {
    text-decoration: underline;
  }
  .result :global(.message-row) {
    padding: 0.55rem 0.65rem 0.65rem;
    background: transparent;
  }
  .result :global(.message-attachments img),
  .result :global(.message-attachments video),
  .result :global(.link-preview),
  .result :global(.invite-embed) {
    max-width: 100%;
    max-height: 240px;
  }
  .result :global(.link-preview),
  .result :global(.invite-embed) {
    overflow: auto;
  }
  .remote-snippet {
    margin: -0.35rem 0.65rem 0.7rem 3.55rem;
    color: var(--text, #fff);
    overflow-wrap: anywhere;
  }
  .partial {
    color: var(--muted, #aaa);
    font-size: 0.85rem;
  }
  .error,
  .encrypted-notice {
    padding: 0.85rem;
    border-radius: 12px;
    background: #5a2028;
    color: #ffd7dc;
  }
  .encrypted-notice {
    display: grid;
    gap: 0.25rem;
    background: #28254a;
    color: #e4e0ff;
  }
  .empty {
    text-align: center;
    color: var(--muted, #aaa);
    padding: 2rem;
  }
  @media (max-width: 600px) {
    .search-layer.dialog-backdrop {
      padding: 0;
    }
    .search-panel:not(.header-popover) {
      height: 100dvh;
      max-height: none;
      border-radius: 0;
    }
    .filters {
      grid-template-columns: 1fr;
    }
    .header-search-box {
      width: 8rem;
    }
    .search-layer.header-layer {
      position: fixed;
      top: 4rem;
      right: 0.5rem;
      left: 0.5rem;
    }
    .search-layer.header-layer.advanced-layer {
      inset: 0;
      padding: 0;
    }
    .search-layer.header-layer.results-layer {
      top: 4rem;
      right: 0;
      left: 0;
      bottom: 0;
    }
    .search-panel.header-popover,
    .search-panel.header-popover.advanced {
      width: 100%;
      max-height: calc(100dvh - 4.5rem);
    }
    .advanced-layer .search-panel.header-popover.advanced,
    .results-layer .search-panel.header-popover {
      height: 100%;
      max-height: none;
      border-radius: 0;
    }
  }
</style>
