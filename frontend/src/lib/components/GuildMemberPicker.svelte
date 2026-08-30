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
    onChange: (values: string[]) => void;
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
  const pageSize = 25;

  function userOption(user: UserSummary): StaticEntityOption {
    return {
      value: entityRef(user),
      label: `${userDisplayName(user)} · @${user.handle}`,
      group: 'Guild members'
    };
  }

  const options = $derived.by(() => {
    const users = guildRef ? results.map((member) => member.user) : fallbackUsers;
    const selected = new Set(value);
    const selectedUsers = seen.filter((member) => selected.has(entityRef(member.user)));
    const candidates = [
      ...staticOptions,
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

  function selectedValues(event: Event): string[] {
    return [...(event.currentTarget as HTMLSelectElement).selectedOptions]
      .map((option) => option.value)
      .filter(Boolean);
  }
</script>

<div class="guild-member-picker">
  {#if guildRef}
    <div class="member-search">
      <input
        bind:value={query}
        {disabled}
        aria-label="Search guild members"
        placeholder="Search guild members"
        aria-busy={loading}
        onfocus={() => (active = true)}
      />
    </div>
  {/if}
  <select
    {multiple}
    size={multiple ? Math.min(Math.max(options.length, 2), 5) : 1}
    {disabled}
    aria-label={placeholder ?? 'Choose a member'}
    onfocus={() => (active = true)}
    onchange={(event) => onChange(selectedValues(event))}
  >
    {#if !multiple}
      <option value="" disabled={!optional} selected={value.length === 0}
        >{placeholder ?? 'Choose a member'}</option
      >
    {/if}
    {#each options as option (option.value)}
      <option value={option.value} selected={value.includes(option.value)}>{option.label}</option>
    {/each}
  </select>
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
  input,
  select {
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
