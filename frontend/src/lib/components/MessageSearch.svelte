<script lang="ts">
  import { t } from '$lib/ui/locale';

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
  import GuildMemberPicker from './GuildMemberPicker.svelte';
  import MessageRow from './MessageRow.svelte';

  let {
    open = $bindable(false),
    scope,
    scopeRef,
    accountRef,
    channel,
    channels = [],
    users = [],
    onJump,
    placement = 'dialog'
  }: {
    open: boolean;
    scope: 'channel' | 'guild' | 'dms';
    scopeRef: string | null;
    accountRef: string | null;
    channel?: Channel | null;
    channels?: Channel[];
    users?: UserSummary[];
    onJump?: (result: MessageSearchResult) => void | Promise<void>;
    placement?: 'dialog' | 'header';
  } = $props();

  let selectedChannelRef = $state('');
  let channelPickerOpen = $state(false);
  let searchGeneration = 0;
  const filterChannelRef = $derived(scope === 'guild' && channel ? entityRef(channel) : '');
  const channelOptions = $derived([
    ...new Map(
      [...channels, ...(channel ? [channel] : [])].map((item) => [entityRef(item), item])
    ).values()
  ]);
  const searchScope = $derived(scope === 'guild' && selectedChannelRef ? 'channel' : scope);
  const searchScopeRef = $derived(
    scope === 'guild' && selectedChannelRef ? selectedChannelRef : scopeRef
  );
  const searchChannel = $derived(
    scope === 'guild'
      ? channelOptions.find((item) => entityRef(item) === selectedChannelRef)
      : channel
  );

  $effect(() => {
    // Start each search session in the channel it was opened from.
    if (open) {
      selectedChannelRef = filterChannelRef;
      searchGeneration += 1;
      loading = false;
      response = null;
      cursor = null;
    }
  });

  function toggleChannelPicker() {
    channelPickerOpen = !channelPickerOpen;
    if (channelPickerOpen)
      queueMicrotask(() =>
        searchRoot?.querySelector<HTMLInputElement>('[aria-label="Search channels"]')?.focus()
      );
  }

  function changeChannelFilter(reference: string) {
    selectedChannelRef = reference;
    channelPickerOpen = false;
    searchGeneration += 1;
    loading = false;
    response = null;
    cursor = null;
    error = '';
    suggestionsOpen = true;
  }

  let query = $state('');
  let authorRef = $state('');
  let mentionRef = $state('');
  let selectedUsers = $state<UserSummary[]>([]);
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
    searchScope === 'channel' &&
      (searchChannel?.encryption_mode === 'e2ee' || searchChannel?.search_available === false)
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
  const memberPickerGuildRef = $derived(
    scope === 'guild'
      ? scopeRef
      : channel?.guild_id && channel.guild_domain
        ? entityRef({ id: channel.guild_id, origin_domain: channel.guild_domain })
        : null
  );
  const authorUser = $derived(authorRef ? userForRef(authorRef) : null);
  const mentionedUser = $derived(mentionRef ? userForRef(mentionRef) : null);
  let suggestions = $derived.by((): Suggestion[] => {
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
        label: $t('ui_from_a_specific_user_f21f0f9e'),
        hint: $t('ui_from_user_acd6cbb9')
      },
      {
        kind: 'operator',
        operator: 'has',
        label: $t('ui_includes_a_specific_type_of_data_79fc28c2'),
        hint: $t('ui_has_link_embed_or_file_a0333b27')
      },
      {
        kind: 'operator',
        operator: 'mentions',
        label: $t('ui_mentions_a_specific_user_dd1b1451'),
        hint: $t('ui_mentions_user_3e71b2c0')
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
    changeChannelFilter('');
    authorRef = '';
    mentionRef = '';
    has = [];
    pinned = 'any';
    authorType = 'any';
    before = '';
    after = '';
  }

  function closeSearch() {
    channelPickerOpen = false;
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
    return (
      selectedUsers.find((user) => entityRef(user) === reference) ??
      uniqueUsers.find((user) => entityRef(user) === reference) ??
      null
    );
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
    const generation = ++searchGeneration;
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
          scope: searchScope,
          scope_ref: searchScopeRef,
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
      if (generation !== searchGeneration) return;
      response =
        next && response
          ? { ...result, results: [...response.results, ...result.results] }
          : result;
      cursor = result.next_cursor;
      rememberSearch();
    } catch (caught) {
      if (generation !== searchGeneration) return;
      error = userErrorMessage(caught, $t('ui_could_not_search_messages_try_again_ec9dac18'));
    } finally {
      if (generation === searchGeneration) loading = false;
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

{#snippet channelPicker()}
  <GuildMemberPicker
    staticOptions={channelOptions.map((item) => ({
      value: entityRef(item),
      label: `#${item.name ?? 'Unnamed channel'}`
    }))}
    value={selectedChannelRef ? [selectedChannelRef] : []}
    optional
    entityName="channels"
    searchPlaceholder="Search channels"
    placeholder={$t('ui_all_channels_4b33d5e0')}
    onChange={(values) => changeChannelFilter(values[0] ?? '')}
  />
{/snippet}

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
            aria-label={$t('ui_remove_sender_filter_d4de0bf0')}
            onclick={() => removeFilter('author')}
          >
            <span>from:</span>{userDisplayName(authorUser)} <b>×</b>
          </button>
        {/if}
        {#if mentionedUser}
          <button
            class="search-token"
            type="button"
            aria-label={$t('ui_remove_mention_filter_c142a6dd')}
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
          aria-label={$t('ui_search_messages_ddf0602b')}
          placeholder={searchScope === 'channel' && searchChannel?.name
            ? `Search ${searchChannel.name}`
            : scope === 'guild'
              ? $t('ui_search_guild_1d1ee794')
              : $t('ui_search_49c266ba')}
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
        <button
          class="search-clear"
          type="button"
          aria-label={$t('ui_clear_search_3b7ea517')}
          onclick={resetSearch}>×</button
        >
      {:else}
        <button
          type="submit"
          aria-label={$t('ui_run_message_search_ca1a30b4')}
          disabled={loading || !hasCriteria}
        >
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
            aria-label={advancedOpen
              ? $t('ui_close_advanced_filters_6ddf487e')
              : $t('ui_close_message_search_b4a727bc')}
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
          aria-label={$t('ui_search_messages_ddf0602b')}
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
                    ? $t('ui_filters_546ebb8e')
                    : response
                      ? `${response.results.length}${cursor ? '+' : ''} results`
                      : $t('ui_search_messages_ddf0602b')}
                </h2>
                {#if placement === 'dialog'}
                  <p>
                    {$t('ui_typo_tolerant_search_across_value0_387e40f9', {
                      value0: String(
                        scope === 'guild'
                          ? 'this guild'
                          : scope === 'dms'
                            ? 'your direct messages'
                            : 'this conversation'
                      )
                    })}
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
                    {$t('ui_filters_value0_94a4d25c', {
                      value0: String(activeFilterCount ? ` · ${activeFilterCount}` : '')
                    })}
                  </button>
                {/if}
                <button
                  class="close"
                  type="button"
                  aria-label={advancedOpen
                    ? $t('ui_close_advanced_filters_6ddf487e')
                    : $t('ui_close_search_55656b5e')}
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
                ><span class="visually-hidden">{$t('ui_search_text_38d268a1')}</span><input
                  bind:value={query}
                  maxlength="512"
                  placeholder={$t('ui_search_messages_ddf0602b')}
                /></label
              >
              <button
                type="submit"
                aria-label={$t('ui_run_message_search_ca1a30b4')}
                disabled={loading || !hasCriteria}><Icon name="search" size={18} /></button
              >
            </form>
          {/if}

          {#if scope === 'guild' && !advancedOpen}
            <div class="search-scope">
              <button
                type="button"
                class="scope-choice"
                aria-label={$t('ui_change_search_channel_1202412c')}
                aria-expanded={channelPickerOpen}
                onclick={toggleChannelPicker}
              >
                <Icon name="hash" size={16} />
                <span>{searchChannel?.name ?? $t('ui_all_channels_4b33d5e0')}</span>
                <Icon name="chevron-down" size={14} />
              </button>
              {#if selectedChannelRef}
                <button type="button" class="scope-all" onclick={() => changeChannelFilter('')}
                  >{$t('ui_search_all_channels_d25f3bfd')}</button
                >
              {/if}
            </div>
            {#if channelPickerOpen}
              <div class="scope-picker">
                {@render channelPicker()}
              </div>
            {/if}
          {/if}

          {#if encrypted && !advancedOpen}
            <div class="encrypted-notice" role="status">
              <strong>{$t('ui_search_is_unavailable_for_this_encrypted_conv_3c49113e')}</strong>
              <span>{$t('ui_end_to_end_encrypted_message_bodies_never_lea_3555c50f')}</span>
            </div>
          {:else if disabledByInstance}
            <div class="encrypted-notice" role="status">
              <strong>{$t('ui_message_search_is_disabled_on_this_instance_969b507d')}</strong>
              <span>{$t('ui_your_instance_administrator_can_enable_the_pr_74e3fc7a')}</span>
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
                  {#if scope === 'guild'}
                    <label class="person-filter"
                      >{$t('ui_channel_ce4683e7')} {@render channelPicker()}
                    </label>
                  {/if}
                  <label class="person-filter"
                    >{$t('ui_from_21819769')}
                    <GuildMemberPicker
                      guildRef={memberPickerGuildRef}
                      fallbackUsers={uniqueUsers}
                      value={authorRef ? [authorRef] : []}
                      optional
                      placeholder={$t('ui_anyone_8d486bb2')}
                      onChange={(values, users) => {
                        authorRef = values[0] ?? '';
                        selectedUsers = [
                          ...selectedUsers.filter(
                            (user) =>
                              !users.some((selected) => entityRef(selected) === entityRef(user))
                          ),
                          ...users
                        ];
                      }}
                    />
                  </label>
                  <label class="person-filter"
                    >{$t('ui_mentions_6f32e692')}
                    <GuildMemberPicker
                      guildRef={memberPickerGuildRef}
                      fallbackUsers={uniqueUsers}
                      value={mentionRef ? [mentionRef] : []}
                      optional
                      placeholder={$t('ui_anyone_8d486bb2')}
                      onChange={(values, users) => {
                        mentionRef = values[0] ?? '';
                        selectedUsers = [
                          ...selectedUsers.filter(
                            (user) =>
                              !users.some((selected) => entityRef(selected) === entityRef(user))
                          ),
                          ...users
                        ];
                      }}
                    />
                  </label>
                  <label
                    >{$t('ui_sort_bec69036')}<select bind:value={sort}
                      ><option value="relevance">{$t('ui_most_relevant_6e9317d8')}</option><option
                        value="newest">{$t('ui_newest_d15efa17')}</option
                      ><option value="oldest">{$t('ui_oldest_505dc450')}</option></select
                    ></label
                  >
                  <label
                    >{$t('ui_pinned_f20c8794')}<select bind:value={pinned}
                      ><option value="any">{$t('ui_either_b5969c3e')}</option><option value="yes"
                        >{$t('ui_pinned_f20c8794')}</option
                      ><option value="no">{$t('ui_not_pinned_47bce0ad')}</option></select
                    ></label
                  >
                  <label
                    >{$t('ui_author_type_e5b40d84')}<select bind:value={authorType}
                      ><option value="any">{$t('ui_anyone_8d486bb2')}</option><option value="user"
                        >{$t('ui_people_7db20897')}</option
                      ><option value="bot">{$t('ui_bots_f17c0acb')}</option><option value="webhook"
                        >{$t('ui_webhooks_45808d75')}</option
                      ></select
                    ></label
                  >
                  <label>{$t('ui_after_7b68fe55')}<input type="date" bind:value={after} /></label>
                  <label>{$t('ui_before_9bb72500')}<input type="date" bind:value={before} /></label>
                </div>
                <fieldset>
                  <legend>{$t('ui_contains_2eaecb3d')}</legend>
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
                    >{$t('ui_clear_filters_7179ea00')}</button
                  >
                  <button class="submit" type="submit" disabled={loading || !hasCriteria}
                    >{loading ? $t('ui_searching_c31723ab') : $t('ui_search_49c266ba')}</button
                  >
                </div>
              </form>
            {:else if suggestionsOpen && !channelPickerOpen}
              <section class="search-start" aria-label={$t('ui_search_options_556a0850')}>
                <h3>
                  {activeOperator === 'from'
                    ? $t('ui_from_user_fd588027')
                    : activeOperator === 'mentions'
                      ? $t('ui_mentions_user_85697ac7')
                      : activeOperator === 'has'
                        ? $t('ui_message_contains_772edfbc')
                        : $t('ui_filters_546ebb8e')}
                </h3>
                {#if suggestions.length === 0}
                  <p class="suggestion-empty">{$t('ui_no_matching_options_cd6bf89d')}</p>
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
                        ><strong>{$t('ui_more_filters_b1ab49a6')}</strong><small
                          >{$t('ui_dates_author_type_pins_and_more_6d123231')}</small
                        ></span
                      >
                    {/if}
                  </button>
                {/each}
              </section>
            {/if}

            {#if !channelPickerOpen && history.length && suggestionsOpen && !activeOperator}
              <section class="history" aria-label={$t('ui_recent_searches_228c84b5')}>
                <div>
                  <strong>{$t('ui_recent_searches_228c84b5')}</strong><button
                    type="button"
                    onclick={() => {
                      history = [];
                      if (storageKey) sessionStorage.removeItem(storageKey);
                    }}>{$t('ui_clear_83b12c22')}</button
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
                {$t('ui_search_is_catching_up_with_recent_messages_re_c79a234b')}
              </p>{/if}
            {#if response.coverage.authority === 'unavailable' || response.coverage.authority === 'unsupported'}<p
                class="partial"
                role="status"
              >
                {$t('ui_showing_locally_cached_matches_the_conversati_819ba8bc')}
              </p>{:else if response.coverage.local === 'cached' && response.coverage.authority === 'not_queried'}<p
                class="partial"
                role="status"
              >
                {$t('ui_account_wide_direct_message_search_uses_this__c97610d2')}
              </p>{/if}
            {#if response.encrypted_channel_refs.length}<p class="partial">
                {$t('ui_encrypted_channels_were_excluded_from_these_r_7504f46e')}
              </p>{/if}
            <div class="results" aria-live="polite">
              {#if response.results.length === 0}<p class="empty">
                  {$t('ui_no_messages_matched_those_filters_e2e6b23d')}
                </p>{/if}
              {#each response.results as result (entityRef(result.message))}
                <article class="result">
                  <button class="result-context" type="button" onclick={() => jump(result)}>
                    <span
                      >{result.guild?.name ??
                        result.channel.recipients?.map(userDisplayName).join(', ') ??
                        $t('ui_direct_message_cd3e1605')} · {result.channel.name ??
                        'conversation'}</span
                    >
                    <span>{$t('ui_jump_9ee0ca3e')}</span>
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
                onclick={() => void runSearch(true)}>{$t('ui_load_more_ac8991ef')}</button
              >{/if}
          {/if}
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .search-scope {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.65rem 0.75rem;
    border-bottom: 1px solid var(--line, #34363d);
  }
  .scope-choice {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    min-width: 0;
    border: 0;
    background: transparent;
    color: var(--text);
    font: inherit;
    font-size: 0.8rem;
    cursor: pointer;
    padding: 0.25rem;
  }
  .scope-choice span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .scope-all {
    flex-shrink: 0;
    border: 0;
    background: transparent;
    color: var(--text-muted);
    font: inherit;
    font-size: 0.72rem;
    cursor: pointer;
    padding: 0.25rem;
  }
  .scope-all:hover {
    color: var(--text);
  }
  .scope-picker {
    padding: 0.75rem;
  }
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
