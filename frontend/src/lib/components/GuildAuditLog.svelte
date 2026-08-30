<script lang="ts">
  import { SvelteMap } from 'svelte/reactivity';

  import { api, userErrorMessage } from '$lib/api/client';
  import {
    AUDIT_ACTION_OPTIONS,
    auditActionFilterValue,
    auditActionLabel,
    auditActorName,
    auditActorRef,
    auditChangeDescription,
    auditFieldLabel,
    auditLogQueryString,
    auditRelativeTime,
    auditSummary,
    auditTargetName,
    canonicalAuditActorRef,
    parseAuditActionFilter,
    type AuditActionOption,
    type AuditLogEntry
  } from '$lib/chat/audit';
  import { entityRef } from '$lib/chat/refs';
  import type { Guild, GuildMemberSummary } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';
  import { formatDateTime } from '$lib/ui/locale';
  import GuildMemberPicker from './GuildMemberPicker.svelte';
  import Icon from './Icon.svelte';

  let { guild, members }: { guild: Guild; members: GuildMemberSummary[] } = $props();

  let entries = $state<AuditLogEntry[]>([]);
  let loading = $state(false);
  let loadingMore = $state(false);
  let error = $state('');
  let retryAppend = $state(false);
  let hasMore = $state(true);
  let actorFilter = $state('');
  let actorReferenceInput = $state('');
  let actorReferenceError = $state('');
  let actionFilter = $state('');
  let loadedQuery = $state('');
  let requestSerial = 0;

  function actorName(member: GuildMemberSummary): string {
    return member.nickname?.trim() || userDisplayName(member.user);
  }

  const actorOptions = $derived.by(() => {
    const available = new SvelteMap<string, { value: string; label: string }>();
    for (const member of members) {
      available.set(entityRef(member.user), {
        value: entityRef(member.user),
        label: actorName(member)
      });
    }
    for (const entry of entries) {
      const ref = auditActorRef(entry);
      if (!available.has(ref)) {
        available.set(ref, { value: ref, label: auditActorName(entry, members) });
      }
    }
    if (actorFilter && !available.has(actorFilter)) {
      available.set(actorFilter, { value: actorFilter, label: actorFilter });
    }
    return [...available.values()].sort((a, b) => a.label.localeCompare(b.label));
  });

  function chooseActor(values: string[]): void {
    actorFilter = values[0] ?? '';
    actorReferenceError = '';
  }

  function applyExactActor(event: SubmitEvent): void {
    event.preventDefault();
    const reference = canonicalAuditActorRef(actorReferenceInput);
    if (!reference) {
      actorReferenceError = 'Enter a complete user reference such as 123@instance.example.';
      return;
    }
    actorFilter = reference;
    actorReferenceInput = '';
    actorReferenceError = '';
  }

  const actions = $derived.by(() => {
    const available = new SvelteMap<string, AuditActionOption>(
      AUDIT_ACTION_OPTIONS.map((option) => [auditActionFilterValue(option), option])
    );
    for (const entry of entries) {
      const key = auditActionFilterValue(entry);
      if (!available.has(key)) {
        available.set(key, {
          action_type: entry.action_type,
          target_type: entry.target_type,
          label: auditActionLabel(entry),
          verb: 'performed an action on'
        });
      }
    }
    return [...available.values()].sort((a, b) => a.label.localeCompare(b.label));
  });

  async function loadEntries(append: boolean): Promise<void> {
    if (append && (loading || loadingMore || !hasMore)) return;

    const existingEntries = entries;
    const selectedAction = parseAuditActionFilter(actionFilter);
    const query = auditLogQueryString({
      limit: 50,
      before: append && existingEntries.length ? existingEntries.at(-1)!.id : undefined,
      userId: actorFilter || undefined,
      actionType: selectedAction?.action_type,
      targetType: selectedAction?.target_type
    });
    const request = ++requestSerial;

    if (append) loadingMore = true;
    else loading = true;
    error = '';
    retryAppend = false;

    try {
      const page = await api<AuditLogEntry[]>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/audit-logs?${query}`
      );
      if (request !== requestSerial) return;
      const merged = append ? [...existingEntries, ...page] : page;
      entries = [...new Map(merged.map((entry) => [entry.id, entry])).values()];
      hasMore = page.length === 50;
    } catch (caught) {
      if (request !== requestSerial) return;
      error = userErrorMessage(caught, 'Could not load the guild audit log. Try again.');
      retryAppend = append;
    } finally {
      if (request === requestSerial) {
        loading = false;
        loadingMore = false;
      }
    }
  }

  function refresh(): void {
    entries = [];
    hasMore = true;
    void loadEntries(false);
  }

  function retry(): void {
    if (retryAppend && entries.length) void loadEntries(true);
    else refresh();
  }

  $effect(() => {
    const currentGuild = entityRef(guild);
    const currentQuery = `${currentGuild}|${actorFilter}|${actionFilter}`;
    if (currentQuery === loadedQuery) return;

    loadedQuery = currentQuery;
    entries = [];
    hasMore = true;
    void loadEntries(false);
  });
</script>

<div class="audit-toolbar">
  <div class="actor-picker">
    <span>Moderator</span>
    <GuildMemberPicker
      guildRef={entityRef(guild)}
      staticOptions={actorOptions}
      value={actorFilter ? [actorFilter] : []}
      optional
      placeholder="Everyone"
      disabled={loading}
      onChange={chooseActor}
    />
  </div>
  <form class="exact-actor" onsubmit={applyExactActor}>
    <label>
      <span>Departed moderator ID</span>
      <div class="exact-actor-row">
        <input
          bind:value={actorReferenceInput}
          disabled={loading}
          placeholder="123@instance.example"
          aria-label="Filter by an exact moderator reference"
          aria-describedby={actorReferenceError ? 'audit-actor-reference-error' : undefined}
        />
        <button type="submit" disabled={loading}>Apply</button>
      </div>
    </label>
    {#if actorReferenceError}
      <small id="audit-actor-reference-error" role="alert">{actorReferenceError}</small>
    {:else}
      <small>Use an exact reference for an account that has left the guild.</small>
    {/if}
  </form>
  <label>
    <span>Action</span>
    <select bind:value={actionFilter} disabled={loading} aria-label="Filter by action">
      <option value="">All actions</option>
      {#each actions as action (auditActionFilterValue(action))}
        <option value={auditActionFilterValue(action)}>{action.label}</option>
      {/each}
    </select>
  </label>
  <button class="audit-refresh" type="button" disabled={loading || loadingMore} onclick={refresh}>
    <Icon name="clock" size={16} />{loading ? 'Loading…' : 'Refresh'}
  </button>
</div>

{#if error}
  <div class="audit-error" role="alert">
    <span>{error}</span>
    <button type="button" disabled={loading || loadingMore} onclick={retry}>Try again</button>
  </div>
{/if}

{#if loading && !entries.length}
  <p class="audit-state" role="status">Loading audit log…</p>
{:else if error && !entries.length}
  <div class="audit-state">
    <Icon name="shield" size={24} />
    <strong>Audit log unavailable</strong>
    <span>Try again when the connection is available.</span>
  </div>
{:else if !entries.length}
  <div class="audit-state">
    <Icon name="shield" size={24} />
    <strong
      >{actorFilter || actionFilter ? 'No matching audit entries' : 'No audit entries yet'}</strong
    >
    <span
      >{actorFilter || actionFilter
        ? 'Try another moderator or action.'
        : 'Administrative actions will appear here.'}</span
    >
  </div>
{:else}
  <div class="audit-list" aria-busy={loadingMore}>
    {#each entries as entry (entry.id)}
      {@const actor = auditActorName(entry, members)}
      {@const target = auditTargetName(entry, guild, members)}
      <article class="audit-entry">
        <span class="audit-icon" aria-hidden="true"><Icon name="shield" size={17} /></span>
        <div class="audit-copy">
          <div class="audit-title">
            <strong>{auditActionLabel(entry)}</strong>
            <time datetime={entry.created_at} title={formatDateTime(entry.created_at)}
              >{auditRelativeTime(entry.created_at)}</time
            >
          </div>
          <p>{auditSummary(actor, entry, target)}</p>
          {#if entry.reason}<blockquote>Reason: {entry.reason}</blockquote>{/if}
          {#if entry.changes.length}
            <dl>
              {#each entry.changes as change, index (`${change.key}-${index}`)}
                <div>
                  <dt>{auditFieldLabel(change.key)}</dt>
                  <dd>{auditChangeDescription(change)}</dd>
                </div>
              {/each}
            </dl>
          {/if}
        </div>
      </article>
    {/each}
  </div>
  {#if hasMore}
    <button
      class="load-more"
      type="button"
      disabled={loading || loadingMore}
      onclick={() => void loadEntries(true)}
      >{loadingMore ? 'Loading older entries…' : 'Load older entries'}</button
    >
  {/if}
{/if}

<style>
  .audit-toolbar {
    display: grid;
    grid-template-columns: minmax(10rem, 1fr) minmax(14rem, 1.35fr) minmax(10rem, 1fr) auto;
    gap: 0.75rem;
    align-items: end;
    margin-bottom: 1rem;
  }
  .audit-toolbar label {
    display: grid;
    gap: 0.35rem;
    color: var(--text-muted);
    font-size: 0.78rem;
    font-weight: 700;
  }
  .actor-picker,
  .exact-actor {
    display: grid;
    gap: 0.35rem;
  }
  .actor-picker > span {
    color: var(--text-muted);
    font-size: 0.78rem;
    font-weight: 700;
  }
  select,
  input,
  .audit-refresh,
  .load-more,
  .audit-error button,
  .exact-actor button {
    min-height: 2.5rem;
    border: 1px solid var(--line-soft);
    border-radius: 0.6rem;
    background: var(--surface-raised);
    color: var(--text);
    padding: 0 0.75rem;
  }
  .exact-actor-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.4rem;
  }
  .exact-actor input {
    width: 100%;
    min-width: 0;
  }
  .exact-actor small {
    color: var(--text-muted);
    font-size: 0.72rem;
    font-weight: 500;
  }
  .audit-refresh {
    display: inline-flex;
    gap: 0.4rem;
    align-items: center;
    justify-content: center;
    cursor: pointer;
  }
  .audit-list {
    display: grid;
    gap: 0.65rem;
  }
  .audit-entry {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.75rem;
    padding: 0.9rem;
    border: 1px solid var(--line-soft);
    border-radius: 0.75rem;
    background: var(--surface-raised);
  }
  .audit-icon {
    display: grid;
    place-items: center;
    width: 2rem;
    height: 2rem;
    border-radius: 50%;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
  }
  .audit-title {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
  }
  .audit-title time,
  .audit-copy p,
  .audit-copy dd {
    color: var(--text-muted);
  }
  .audit-copy p {
    margin: 0.25rem 0 0;
  }
  blockquote {
    margin: 0.65rem 0 0;
    padding-left: 0.7rem;
    border-left: 2px solid var(--accent);
    color: var(--text-soft);
  }
  dl {
    display: grid;
    gap: 0.35rem;
    margin: 0.65rem 0 0;
  }
  dl div {
    display: grid;
    grid-template-columns: minmax(7rem, 0.35fr) minmax(0, 1fr);
    gap: 0.6rem;
  }
  dt {
    font-weight: 700;
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .audit-state,
  .audit-error {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    justify-content: center;
    min-height: 7rem;
    color: var(--text-muted);
  }
  .audit-error {
    min-height: auto;
    justify-content: space-between;
    margin-bottom: 0.75rem;
    padding: 0.75rem;
    border: 1px solid color-mix(in srgb, var(--danger) 35%, var(--line-soft));
    border-radius: 0.65rem;
    background: color-mix(in srgb, var(--danger) 8%, var(--surface-raised));
  }
  .audit-state {
    flex-direction: column;
  }
  .load-more {
    display: block;
    margin: 1rem auto 0;
    cursor: pointer;
  }
  button:disabled,
  select:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
  @media (max-width: 720px) {
    .audit-toolbar {
      grid-template-columns: 1fr;
    }
    .audit-title {
      flex-direction: column;
      gap: 0.2rem;
    }
    dl div {
      grid-template-columns: 1fr;
      gap: 0.1rem;
    }
  }
</style>
