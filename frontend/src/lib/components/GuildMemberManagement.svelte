<script lang="ts">
  import { onMount, onDestroy, untrack, type Snippet } from 'svelte';
  import { assetUrl } from '$lib/media/assets';
  import { entityRef } from '$lib/chat/refs';
  import { memberDisplayName, roleColorCss } from '$lib/chat/members';
  import { isApplicationUser, userPublicHandle } from '$lib/chat/users';
  import { formatDateTime } from '$lib/ui/locale';
  import type { GuildMemberSummary, Role } from '$lib/chat/types';
  import Icon from './Icon.svelte';

  let {
    members,
    roles,
    hasMore,
    loading,
    busy,
    loadMore,
    canManageMember,
    canManageRole,
    toggleRole,
    actions,
    canPrune = false
  }: {
    members: GuildMemberSummary[];
    roles: Role[];
    hasMore: boolean;
    loading: boolean;
    busy: boolean;
    loadMore: () => Promise<boolean>;
    canManageMember: (member: GuildMemberSummary) => boolean;
    canManageRole: (role: Role) => boolean;
    toggleRole: (
      member: GuildMemberSummary,
      role: Role,
      enabled: boolean
    ) => Promise<boolean> | undefined;
    actions: Snippet<[GuildMemberSummary, boolean]>;
    canPrune?: boolean;
  } = $props();

  let search = $state('');
  let sort = $state('newest');
  let roleFilter = $state('');
  let signalFilter = $state('');
  let pageSize = $state(12);
  let page = $state(0);
  let selected = $state<string[]>([]);
  let bulkRole = $state('');
  let bulkBusy = $state(false);
  let bulkStatus = $state('');
  let loadingDirectory = $state(false);
  let now = $state(Date.now());
  let disposed = false;
  onDestroy(() => {
    disposed = true;
  });
  onMount(() => {
    const timer = window.setInterval(() => {
      now = Date.now();
    }, 60_000);
    return () => window.clearInterval(timer);
  });
  const timedOut = (member: GuildMemberSummary) =>
    Boolean(
      member.timeout_indefinite || (member.timeout_until && Date.parse(member.timeout_until) > now)
    );
  const manageableRoles = $derived(roles.filter(canManageRole));
  const filtered = $derived.by(() => {
    const query = search.trim().toLocaleLowerCase();
    return members
      .filter((member) => {
        const identity =
          `${memberDisplayName(member)} ${member.user.username} ${userPublicHandle(member.user) ?? ''} ${entityRef(member.user)}`.toLocaleLowerCase();
        return (
          (!query || identity.includes(query)) &&
          (!roleFilter ||
            (roleFilter === 'none'
              ? !member.role_ids.some((id) => roles.some((role) => role.id === id))
              : member.role_ids.includes(roleFilter))) &&
          (!signalFilter ||
            (signalFilter === 'timeout'
              ? timedOut(member)
              : signalFilter === 'app'
                ? isApplicationUser(member.user)
                : member.temporary))
        );
      })
      .sort((a, b) => {
        if (sort === 'name')
          return memberDisplayName(a).localeCompare(memberDisplayName(b), undefined, {
            sensitivity: 'base',
            numeric: true
          });
        const left = Date.parse(a.joined_at ?? '');
        const right = Date.parse(b.joined_at ?? '');
        if (!Number.isFinite(left)) return Number.isFinite(right) ? 1 : 0;
        if (!Number.isFinite(right)) return -1;
        return sort === 'oldest' ? left - right : right - left;
      });
  });
  const pageCount = $derived(Math.max(1, Math.ceil(filtered.length / pageSize)));
  const currentPage = $derived(Math.min(page, pageCount - 1));
  const visible = $derived(filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize));
  const selectable = $derived(visible.filter(canManageMember));
  const selectedMembers = $derived(
    members.filter((member) => selected.includes(entityRef(member.user)) && canManageMember(member))
  );
  const allSelected = $derived(
    selectable.length > 0 && selectable.every((member) => selected.includes(entityRef(member.user)))
  );
  const someSelected = $derived(
    selectable.some((member) => selected.includes(entityRef(member.user)))
  );
  const pageNumbers = $derived(
    Array.from(
      { length: Math.min(5, pageCount) },
      (_, i) => Math.max(0, Math.min(currentPage - 2, pageCount - 5)) + i
    )
  );

  $effect(() => {
    void [search, roleFilter, signalFilter, sort, pageSize];
    page = 0;
    selected = [];
    bulkStatus = '';
  });

  // ponytail: sorting/filtering use the loaded directory; move them server-side if guild size makes full loading costly.
  $effect(() => {
    if (hasMore) void untrack(loadDirectory);
  });

  async function loadDirectory() {
    if (loadingDirectory || loading) return;
    loadingDirectory = true;
    try {
      while (!disposed && (await loadMore())) {
        /* Fetch the next cursor page. */
      }
    } finally {
      loadingDirectory = false;
    }
  }

  function selectMember(ref: string, checked: boolean) {
    selected = checked
      ? [...new Set([...selected, ref])]
      : selected.filter((value) => value !== ref);
  }

  function relativeDate(value?: string): string {
    const timestamp = Date.parse(value ?? '');
    if (!Number.isFinite(timestamp)) return 'Unknown';
    const days = Math.max(0, Math.floor((now - timestamp) / 86400000));
    if (days === 0) return 'Today';
    if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`;
    const count = days < 365 ? Math.floor(days / 30) : Math.floor(days / 365);
    return `${count} ${days < 365 ? 'month' : 'year'}${count === 1 ? '' : 's'} ago`;
  }

  async function applyBulkRole() {
    const role = manageableRoles.find((role) => role.id === bulkRole);
    if (!role || busy || bulkBusy) return;
    bulkBusy = true;
    bulkStatus = '';
    const targets = selectedMembers.filter((member) => !member.role_ids.includes(role.id));
    let completed = 0;
    try {
      for (const member of targets) {
        if (disposed || !(await toggleRole(member, role, true))) break;
        completed += 1;
      }
      bulkStatus =
        targets.length === 0
          ? 'Selected members already have this role.'
          : `Assigned ${role.name} to ${completed} of ${targets.length} members.${completed < targets.length ? ' Stopped after a failure; review the error and retry.' : ''}`;
    } finally {
      bulkBusy = false;
    }
  }
</script>

<div class="member-directory" aria-busy={loading || loadingDirectory}>
  <header class="toolbar">
    <h3>Recent Members <span>{members.length}{hasMore ? '+' : ''}</span></h3>
    <label class="search"
      ><Icon name="search" size={16} /><input
        type="search"
        aria-label="Search members"
        placeholder="Search by username or ID"
        bind:value={search}
      /></label
    >
    <select aria-label="Sort members" bind:value={sort}>
      <option value="newest">Newest members</option><option value="oldest">Oldest members</option
      ><option value="name">Name A–Z</option>
    </select>
    {#if canPrune}<a class="prune" href="#bulk-moderation">Prune</a>{/if}
  </header>
  <div class="filters">
    <label
      >Roles <select aria-label="Filter by role" bind:value={roleFilter}
        ><option value="">All roles</option><option value="none">No roles</option
        >{#each roles as role (role.id)}<option value={role.id}>{role.name}</option>{/each}</select
      ></label
    >
    <label
      >Signals <select aria-label="Filter by signal" bind:value={signalFilter}
        ><option value="">All members</option><option value="timeout">Timed out</option><option
          value="app">Apps</option
        ><option value="temporary">Temporary members</option></select
      ></label
    >
    {#if search || roleFilter || signalFilter}<button
        onclick={() => {
          search = '';
          roleFilter = '';
          signalFilter = '';
        }}>Clear filters</button
      >{/if}
    {#if hasMore}<span role="status"
        >{loading || loadingDirectory
          ? `Loading members… ${members.length} loaded`
          : 'Directory incomplete. Filters apply to loaded members.'}</span
      >
      {#if !loading && !loadingDirectory}<button onclick={() => void loadDirectory()}
          >Retry loading</button
        >{/if}
    {/if}
  </div>
  {#if selectedMembers.length}
    <div class="bulk-bar">
      <strong>{selectedMembers.length} selected</strong>
      <select
        aria-label="Role to assign to selected members"
        bind:value={bulkRole}
        disabled={busy || bulkBusy}
        ><option value="">Choose a role</option>{#each manageableRoles as role (role.id)}<option
            value={role.id}>{role.name}</option
          >{/each}</select
      >
      <button disabled={!bulkRole || busy || bulkBusy} onclick={() => void applyBulkRole()}
        >{bulkBusy ? 'Assigning…' : 'Add role'}</button
      >
      <button disabled={bulkBusy} onclick={() => (selected = [])}>Clear selection</button>
    </div>
  {/if}
  {#if bulkStatus}<p class="bulk-status" role="status">{bulkStatus}</p>{/if}
  <div class="table-scroll">
    <table>
      <thead
        ><tr>
          <th class="selection"
            ><input
              type="checkbox"
              aria-label="Select all manageable members on this page"
              checked={allSelected}
              indeterminate={someSelected && !allSelected}
              disabled={!selectable.length || !manageableRoles.length || busy || bulkBusy}
              onchange={(event) => {
                for (const member of selectable)
                  selectMember(entityRef(member.user), event.currentTarget.checked);
              }}
            /></th
          >
          <th scope="col">Name</th><th scope="col">Member since</th><th scope="col">Roles</th><th
            scope="col">Signals</th
          ><th scope="col"><span class="sr-only">Actions</span></th>
        </tr></thead
      >
      <tbody>
        {#each visible as member (entityRef(member.user))}
          {@const ref = entityRef(member.user)}
          {@const assignedRoles = roles.filter((role) => member.role_ids.includes(role.id))}
          <tr class:selected={selected.includes(ref)}>
            <td class="selection"
              ><input
                type="checkbox"
                aria-label={`Select ${memberDisplayName(member)}`}
                checked={selected.includes(ref)}
                disabled={!canManageMember(member) || !manageableRoles.length || busy || bulkBusy}
                onchange={(event) => selectMember(ref, event.currentTarget.checked)}
              /></td
            >
            <td
              ><div class="identity">
                <span class="member-avatar" aria-hidden="true"
                  >{#if member.user.avatar_hash}<img
                      src={assetUrl(member.user.avatar_hash, 'thumbnail_128', member.user)}
                      alt=""
                    />{:else}{memberDisplayName(member).slice(0, 1).toUpperCase()}{/if}</span
                >
                <div>
                  <strong>{memberDisplayName(member)}</strong><small title={ref}
                    >{userPublicHandle(member.user) ?? ref}</small
                  >
                </div>
              </div></td
            >
            <td class="date"
              ><span
                title={member.joined_at
                  ? formatDateTime(member.joined_at)
                  : 'Membership date unavailable'}>{relativeDate(member.joined_at)}</span
              ></td
            >
            <td
              ><div class="role-chips">
                {#each assignedRoles as role (role.id)}<span class="role-chip"
                    ><i style:background={roleColorCss(role.color) ?? 'var(--text-muted)'}
                    ></i>{role.name}</span
                  >{:else}<span class="muted">—</span>{/each}
              </div></td
            >
            <td
              ><div class="signals">
                {#if timedOut(member)}<span
                    title={member.timeout_indefinite
                      ? 'Timed out indefinitely'
                      : `Timed out until ${formatDateTime(member.timeout_until!)}`}>Timed out</span
                  >{/if}{#if isApplicationUser(member.user)}<span>APP</span
                  >{/if}{#if member.temporary}<span>Temporary</span
                  >{/if}{#if !timedOut(member) && !isApplicationUser(member.user) && !member.temporary}<span
                    class="muted">—</span
                  >{/if}
              </div></td
            >
            <td class="actions-cell"
              ><details>
                <summary
                  aria-label={`Actions for ${memberDisplayName(member)}`}
                  title="Member actions">⋮</summary
                >
                <div class="actions-panel">
                  <strong>{memberDisplayName(member)}</strong>
                  {#if manageableRoles.length && canManageMember(member)}<fieldset
                      disabled={busy || bulkBusy}
                    >
                      <legend>Manage roles</legend>{#each manageableRoles as role (role.id)}<label
                          ><input
                            type="checkbox"
                            checked={member.role_ids.includes(role.id)}
                            onchange={(event) =>
                              void toggleRole(member, role, event.currentTarget.checked)}
                          />{role.name}</label
                        >{/each}
                    </fieldset>{/if}
                  <div class="moderation-actions">{@render actions(member, timedOut(member))}</div>
                  <small class="muted">ID: {ref}</small>
                </div>
              </details></td
            >
          </tr>
        {:else}<tr
            ><td colspan="6" class="empty"
              ><Icon name="users" /><strong
                >{loading || loadingDirectory ? 'Loading members…' : 'No matching members'}</strong
              ><span>Try a different name, user ID, or filter.</span></td
            ></tr
          >{/each}
      </tbody>
    </table>
  </div>
  <footer>
    <div class="page-size">
      Showing <select aria-label="Members per page" bind:value={pageSize}
        ><option value={12}>12</option><option value={25}>25</option><option value={50}>50</option
        ><option value={100}>100</option></select
      ><span
        >per page · {filtered.length} member{filtered.length === 1 ? '' : 's'}{hasMore
          ? ' loaded'
          : ''}</span
      >
    </div>
    <nav aria-label="Guild member pages">
      <button disabled={currentPage === 0} onclick={() => (page = currentPage - 1)}
        >‹ Previous</button
      >{#each pageNumbers as number (number)}<button
          class:active={currentPage === number}
          aria-current={currentPage === number ? 'page' : undefined}
          aria-label={`Page ${number + 1}`}
          onclick={() => (page = number)}>{number + 1}</button
        >{/each}<button
        disabled={currentPage >= pageCount - 1}
        onclick={() => (page = currentPage + 1)}>Next ›</button
      >
    </nav>
  </footer>
</div>

<style>
  .member-directory {
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface);
    overflow: hidden;
  }
  .toolbar,
  .filters,
  .bulk-bar,
  footer,
  nav,
  .page-size {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .toolbar {
    padding: 16px;
    border-bottom: 1px solid var(--line-soft);
  }
  h3 {
    margin: 0 auto 0 0;
    font-size: 1rem;
    font-weight: 650;
  }
  h3 span {
    margin-left: 8px;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  input,
  select,
  button,
  a,
  summary {
    font: inherit;
  }
  select,
  button,
  .prune {
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--surface);
    color: var(--text-soft);
    padding: 8px 10px;
    font-size: 0.8rem;
  }
  button,
  summary,
  select {
    cursor: pointer;
  }
  button:hover:not(:disabled),
  summary:hover {
    background: var(--surface-hover);
    color: var(--text);
  }
  button:disabled,
  input:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .prune {
    color: var(--danger);
    text-decoration: none;
  }
  .search {
    display: flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 8px 12px;
    color: var(--text-muted);
  }
  .search input {
    width: 220px;
    max-width: 100%;
    border: 0;
    padding: 0;
    color: var(--text);
    background: transparent;
    font-size: 0.85rem;
  }
  .search:focus-within {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .search input:focus {
    outline: none;
  }
  .filters {
    padding: 10px 16px;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .filters label {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .filters select {
    padding: 5px 8px;
  }
  .bulk-bar {
    padding: 12px 16px;
    border-block: 1px solid var(--line);
    background: var(--surface-hover);
    font-size: 0.8rem;
  }
  .bulk-status {
    padding: 0 16px;
    font-size: 0.8rem;
  }
  .table-scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    text-align: left;
    font-size: 0.85rem;
  }
  th {
    padding: 16px 12px;
    color: var(--text-muted);
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  td {
    padding: 16px 12px;
    border-top: 1px solid var(--line-soft);
    vertical-align: middle;
  }
  tr.selected,
  tbody tr:hover {
    background: var(--surface-hover);
  }
  .selection {
    width: 40px;
    padding-right: 0;
    padding-left: 18px;
  }
  input[type='checkbox'] {
    width: 17px;
    height: 17px;
    accent-color: var(--accent);
    vertical-align: middle;
  }
  .identity {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 190px;
  }
  .identity > div {
    display: grid;
    gap: 4px;
  }
  .identity strong,
  .identity small {
    max-width: 250px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .identity strong {
    font-weight: 600;
  }
  .identity small,
  .muted {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  .member-avatar {
    width: 36px;
    height: 36px;
    flex-shrink: 0;
    border-radius: 50%;
    background: var(--pine);
    color: var(--on-pine);
    display: grid;
    place-items: center;
    font-weight: 700;
  }
  .member-avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    border-radius: inherit;
  }
  .date {
    white-space: nowrap;
    color: var(--text-soft);
  }
  .role-chips,
  .signals {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    min-width: 110px;
  }
  .role-chip,
  .signals > span:not(.muted) {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border-radius: 5px;
    padding: 4px 7px;
    background: var(--surface-hover);
    font-size: 0.73rem;
  }
  .role-chip i {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .actions-cell {
    width: 42px;
  }
  summary {
    display: grid;
    place-items: center;
    list-style: none;
    border-radius: 5px;
    width: 30px;
    height: 32px;
    font-size: 1.4rem;
    color: var(--text-muted);
  }
  summary::-webkit-details-marker {
    display: none;
  }
  .actions-panel {
    width: 210px;
    padding: 12px 0 0;
    display: grid;
    gap: 12px;
  }
  .actions-panel > strong {
    font-size: 0.8rem;
  }
  fieldset {
    border: 0;
    margin: 0;
    padding: 0;
    max-height: 200px;
    overflow-y: auto;
  }
  legend {
    color: var(--text-muted);
    font-size: 0.7rem;
    margin-bottom: 6px;
  }
  fieldset label {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 5px 0;
    overflow-wrap: anywhere;
  }
  .moderation-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .actions-panel small {
    overflow-wrap: anywhere;
  }
  footer {
    justify-content: space-between;
    padding: 16px;
    border-top: 1px solid var(--line);
    color: var(--text-muted);
    font-size: 0.8rem;
  }
  nav {
    gap: 4px;
  }
  nav button {
    border-color: transparent;
    background: transparent;
  }
  nav button.active {
    border-radius: 50%;
    background: var(--accent);
    color: var(--on-accent, white);
  }
  .empty {
    text-align: center;
    padding: 50px 20px;
    color: var(--text-muted);
  }
  .empty strong,
  .empty span {
    display: block;
    margin-top: 10px;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  @media (max-width: 700px) {
    .search {
      flex: 1;
      min-width: 180px;
    }
    .search input {
      width: 100%;
    }
    h3 {
      width: 100%;
    }
    footer {
      justify-content: center;
    }
  }
</style>
