<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { guildMemberSearchPath, mergeGuildMemberPage } from '$lib/chat/members';
  import { entityRef } from '$lib/chat/refs';
  import type { GuildMemberSummary, UserSummary } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';

  export interface StaticEntityOption {
    value: string;
    label: string;
    group?: string;
  }

  let {
    guildRef = null,
    fallbackUsers = [],
    staticOptions = [],
    value = [],
    multiple = false,
    maxValues = 1,
    optional = false,
    placeholder = 'Choose a member',
    disabled = false,
    filterUser = () => true,
    onChange
  }: {
    guildRef?: string | null;
    fallbackUsers?: UserSummary[];
    staticOptions?: StaticEntityOption[];
    value?: string[];
    multiple?: boolean;
    maxValues?: number;
    optional?: boolean;
    placeholder?: string | null;
    disabled?: boolean;
    filterUser?: (user: UserSummary) => boolean;
    onChange: (values: string[], users: UserSummary[]) => void;
  } = $props();

  let active = $state(false);
  let query = $state('');
  let results = $state<GuildMemberSummary[]>([]);
  let seen = $state<GuildMemberSummary[]>([]);
  let loading = $state(false);
  let loadingMore = $state(false);
  let hasMore = $state(false);
  let cursor = $state('');
  let error = $state('');
  let loadedGuildRef = '';
  let requestGeneration = 0;
  let pageController: AbortController | null = null;
  let picker = $state<HTMLDivElement | null>(null);
  let highlighted = $state(0);
  const memberResultsId = $props.id();
  const pageSize = 25;

  function userOption(user: UserSummary): StaticEntityOption {
    return {
      value: entityRef(user),
      label: `${userDisplayName(user)} · @${user.handle}`,
      group: 'Guild members'
    };
  }

  const options = $derived.by(() => {
    const needle = query.trim().toLowerCase();
    const users = (guildRef ? results.map((member) => member.user) : fallbackUsers).filter(
      (user) =>
        filterUser(user) &&
        (!needle ||
          `${userDisplayName(user)} ${user.handle} ${user.origin_domain}`
            .toLowerCase()
            .includes(needle))
    );
    const selected = new Set(value);
    const selectedFallbackUsers = fallbackUsers.filter(
      (user) => filterUser(user) && selected.has(entityRef(user))
    );
    const selectedUsers = seen.filter((member) => selected.has(entityRef(member.user)));
    const candidates = [
      ...staticOptions,
      ...selectedFallbackUsers.map(userOption),
      ...selectedUsers.map((item) => userOption(item.user)),
      ...users.map(userOption)
    ];
    for (const missing of value) {
      if (!candidates.some((option) => option.value === missing)) {
        candidates.push({
          value: missing,
          label: `Selected user · ${missing}`,
          group: 'Guild members'
        });
      }
    }
    return candidates.filter(
      (option, index) => candidates.findIndex((item) => item.value === option.value) === index
    );
  });
  const availableOptions = $derived(options.filter((option) => !value.includes(option.value)));
  const selectedOptions = $derived(options.filter((option) => value.includes(option.value)));

  async function loadPage(
    targetGuild: string,
    search: string,
    after: string,
    append: boolean,
    generation: number,
    controller: AbortController
  ) {
    const page = await api<GuildMemberSummary[]>(
      guildMemberSearchPath(targetGuild, search, pageSize + 1, after),
      { signal: controller.signal }
    );
    if (
      controller.signal.aborted ||
      generation !== requestGeneration ||
      targetGuild !== (guildRef?.trim() ?? '') ||
      search !== query.trim()
    )
      return;
    const next = mergeGuildMemberPage(append ? results : [], page, pageSize);
    const known = mergeGuildMemberPage(seen, page, pageSize);
    results = next.members;
    seen = known.members;
    hasMore = next.hasMore;
    cursor = next.cursor;
  }

  $effect(() => {
    const targetGuild = guildRef?.trim() ?? '';
    const search = query.trim();
    const shouldLoad = active && Boolean(targetGuild);
    if (loadedGuildRef !== targetGuild) {
      loadedGuildRef = targetGuild;
      results = [];
      seen = [];
      cursor = '';
      hasMore = false;
      error = '';
    }
    if (!shouldLoad) {
      loading = false;
      loadingMore = false;
      return;
    }
    const generation = ++requestGeneration;
    pageController?.abort();
    const controller = new AbortController();
    pageController = controller;
    loading = true;
    loadingMore = false;
    cursor = '';
    hasMore = false;
    error = '';
    const timer = window.setTimeout(() => {
      void loadPage(targetGuild, search, '', false, generation, controller)
        .catch((caught: unknown) => {
          if (controller.signal.aborted) return;
          results = [];
          error = userErrorMessage(caught, 'Could not search guild members. Try again.');
        })
        .finally(() => {
          if (!controller.signal.aborted) loading = false;
        });
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      if (pageController !== controller) pageController?.abort();
    };
  });

  async function loadMoreMembers() {
    const targetGuild = guildRef?.trim() ?? '';
    const search = query.trim();
    if (!targetGuild || !cursor || !hasMore || loading || loadingMore) return;
    const generation = requestGeneration;
    pageController?.abort();
    const controller = new AbortController();
    pageController = controller;
    loadingMore = true;
    error = '';
    try {
      await loadPage(targetGuild, search, cursor, true, generation, controller);
    } catch (caught) {
      if (!controller.signal.aborted && generation === requestGeneration) {
        error = userErrorMessage(caught, 'Could not load more guild members. Try again.');
      }
    } finally {
      if (!controller.signal.aborted && generation === requestGeneration) loadingMore = false;
    }
  }

  function usersFor(values: string[]): UserSummary[] {
    return [...fallbackUsers, ...seen.map((member) => member.user)].filter((user) =>
      values.includes(entityRef(user))
    );
  }

  function choose(option: StaticEntityOption) {
    const next = multiple ? [...value, option.value].slice(0, maxValues) : [option.value];
    onChange(next, usersFor(next));
    query = '';
    highlighted = 0;
    if (!multiple) active = false;
  }

  function remove(option: StaticEntityOption) {
    const next = value.filter((item) => item !== option.value);
    onChange(next, usersFor(next));
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      active = false;
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      active = true;
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      highlighted = Math.max(0, Math.min(availableOptions.length - 1, highlighted + direction));
      return;
    }
    if (event.key === 'Enter' && active && availableOptions[highlighted]) {
      event.preventDefault();
      choose(availableOptions[highlighted]);
    }
  }

  function focusout(event: FocusEvent) {
    if (!picker?.contains(event.relatedTarget as Node | null)) active = false;
  }
</script>

<div class="guild-member-picker" bind:this={picker} onfocusout={focusout}>
  <div class="member-search">
    <input
      bind:value={query}
      {disabled}
      aria-label="Search people"
      placeholder="Search by name or federated username"
      aria-busy={loading}
      role="combobox"
      aria-expanded={active}
      aria-controls={memberResultsId}
      aria-autocomplete="list"
      aria-activedescendant={active && availableOptions[highlighted]
        ? `${memberResultsId}-${highlighted}`
        : undefined}
      onfocus={() => {
        active = true;
        highlighted = 0;
      }}
      onkeydown={keydown}
      autocomplete="off"
    />
  </div>
  {#if selectedOptions.length}
    <div class="selected-options" aria-label="Selected people">
      {#each selectedOptions as option (option.value)}
        <span
          >{option.label}<button type="button" {disabled} onclick={() => remove(option)}>×</button
          ></span
        >
      {/each}
    </div>
  {:else if !active}
    <small>{placeholder ?? 'Choose a member'}</small>
  {/if}
  {#if active}
    <div id={memberResultsId} class="member-results" role="listbox" aria-multiselectable={multiple}>
      {#if optional && !multiple && value.length}
        <button type="button" role="option" aria-selected="false" onclick={() => onChange([], [])}
          >{placeholder ?? 'Clear selection'}</button
        >
      {/if}
      {#each availableOptions as option, index (option.value)}
        <button
          id={`${memberResultsId}-${index}`}
          type="button"
          role="option"
          aria-selected="false"
          class:highlighted={index === highlighted}
          disabled={disabled || (multiple && value.length >= maxValues)}
          onmouseenter={() => (highlighted = index)}
          onclick={() => choose(option)}>{option.label}</button
        >
      {:else}
        {#if !loading}<small>No matching people.</small>{/if}
      {/each}
    </div>
  {/if}
  {#if guildRef && !active}<small>Search to choose any member of this guild.</small>{/if}
  {#if loading}<small role="status">Searching members…</small>{/if}
  {#if hasMore}
    <button type="button" disabled={disabled || loadingMore} onclick={() => void loadMoreMembers()}
      >{loadingMore ? 'Loading more members…' : 'Load more matching members'}</button
    >
  {/if}
  {#if error}<small class="error" role="alert">{error}</small>{/if}
  {#if multiple}<small>{value.length} of {maxValues} selected</small>{/if}
</div>

<style>
  .guild-member-picker {
    display: grid;
    gap: 4px;
  }
  input {
    min-height: 36px;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 6px 9px;
    color: var(--text);
    background: var(--surface-raised);
  }
  small {
    color: var(--text-muted);
    font-size: 0.72rem;
  }
  small.error {
    color: var(--danger);
  }
  .selected-options {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .selected-options span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 4px 7px;
    background: var(--surface-raised);
  }
  .selected-options button {
    min-height: 0;
    border: 0;
    padding: 0;
    background: transparent;
  }
  .member-results {
    display: grid;
    max-height: 240px;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 4px;
    background: var(--surface-raised);
  }
  .member-results button {
    border: 0;
    text-align: left;
    background: transparent;
  }
  .member-results button:hover,
  .member-results button.highlighted {
    background: var(--surface-hover);
  }
  button {
    min-height: 30px;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 5px 9px;
    color: var(--text);
    background: var(--surface-raised);
    font: inherit;
    font-size: 0.72rem;
    font-weight: 750;
    cursor: pointer;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
</style>
