<script module lang="ts">
  export interface InstanceStats {
    domain: string;
    display_name: string | null;
    software_version: string | null;
    last_seen_at: string | null;
    users: number;
    messages: number;
    guilds: number;
    dm_conversations: number;
    cached_files: number;
    retained_events: number;
    guild_storage_bytes: number;
    dm_storage_bytes: number;
    media_storage_bytes: number;
    event_storage_bytes: number;
    pending_deliveries: number;
    failed_deliveries: number;
  }
</script>

<script lang="ts">
  import { t } from '$lib/ui/locale';
  import Icon from './Icon.svelte';

  let { instances }: { instances: InstanceStats[] } = $props();
  let search = $state('');
  let sort = $state('contact');
  function storage(peer: InstanceStats): number {
    return (
      peer.guild_storage_bytes +
      peer.dm_storage_bytes +
      peer.media_storage_bytes +
      peer.event_storage_bytes
    );
  }
  function bytes(value: number): string {
    if (value < 1024) return `${value.toLocaleString()} B`;
    const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
    return `${(value / 1024 ** unit).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit]}`;
  }
  const totals = $derived(
    instances.reduce(
      (sum, peer) => ({
        users: sum.users + peer.users,
        messages: sum.messages + peer.messages,
        storage: sum.storage + storage(peer)
      }),
      { users: 0, messages: 0, storage: 0 }
    )
  );
  const visible = $derived(
    instances
      .filter((peer) =>
        `${peer.domain} ${peer.display_name ?? ''}`
          .toLowerCase()
          .includes(search.trim().toLowerCase())
      )
      .sort((a, b) =>
        sort === 'storage'
          ? storage(b) - storage(a)
          : sort === 'messages'
            ? b.messages - a.messages
            : sort === 'domain'
              ? a.domain.localeCompare(b.domain)
              : (b.last_seen_at ?? '').localeCompare(a.last_seen_at ?? '')
      )
  );
</script>

<section class="instance-statistics" aria-label={$t('federation_stats_title')}>
  <div class="summary">
    {#each [[$t('federation_stats_peers'), instances.length.toLocaleString()], [$t('federation_stats_users'), totals.users.toLocaleString()], [$t('federation_stats_messages'), totals.messages.toLocaleString()], [$t('federation_stats_storage'), bytes(totals.storage)]] as [label, value] (label)}
      <article><span>{label}</span><strong>{value}</strong></article>
    {/each}
  </div>
  <div class="peer-panel">
    <header>
      <div>
        <h2>{$t('federation_stats_title')}</h2>
        <p>{$t('federation_stats_scope')}</p>
      </div>
      <div class="controls">
        <label class="search"
          ><Icon name="search" size={18} /><input
            type="search"
            bind:value={search}
            aria-label={$t('federation_stats_search')}
            placeholder={$t('federation_stats_search')}
          /></label
        >
        <select bind:value={sort} aria-label={$t('federation_stats_sort')}>
          <option value="contact">{$t('federation_stats_contact')}</option>
          <option value="storage">{$t('federation_stats_storage')}</option>
          <option value="messages">{$t('federation_stats_messages')}</option>
          <option value="domain">{$t('ui_instance_domain_57b0b406')}</option>
        </select>
      </div>
    </header>
    <div class="peers">
      {#each visible as peer (peer.domain)}
        <details>
          <summary>
            <div class="peer-name">
              <span class="globe"><Icon name="globe" size={20} /></span>
              <div>
                <strong>{peer.domain}</strong><small
                  >{peer.display_name ?? $t('federation_stats_remote')}</small
                >
              </div>
            </div>
            <div class="figure">
              <span>{$t('federation_stats_users')}</span><strong
                >{peer.users.toLocaleString()}</strong
              >
            </div>
            <div class="figure">
              <span>{$t('federation_stats_messages')}</span><strong
                >{peer.messages.toLocaleString()}</strong
              >
            </div>
            <div class="figure">
              <span>{$t('federation_stats_storage')}</span><strong>{bytes(storage(peer))}</strong>
            </div>
            <span class="expand"><Icon name="chevron-down" size={18} /></span>
          </summary>
          <div class="peer-details">
            <dl class="storage-grid">
              {#each [[$t('federation_stats_guild_storage'), bytes(peer.guild_storage_bytes)], [$t('federation_stats_dm_storage'), bytes(peer.dm_storage_bytes)], [$t('federation_stats_media_storage'), bytes(peer.media_storage_bytes)], [$t('federation_stats_event_storage'), bytes(peer.event_storage_bytes)]] as [label, value] (label)}
                <div>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              {/each}
            </dl>
            <dl class="metadata-grid">
              {#each [[$t('federation_stats_contact'), peer.last_seen_at ? new Date(peer.last_seen_at).toLocaleString() : $t('federation_stats_never')], [$t('federation_stats_version'), peer.software_version ?? '—'], [$t('federation_stats_guilds'), peer.guilds.toLocaleString()], [$t('federation_stats_dms'), peer.dm_conversations.toLocaleString()], [$t('federation_stats_files'), peer.cached_files.toLocaleString()], [$t('federation_stats_events'), peer.retained_events.toLocaleString()], [$t('federation_stats_pending'), peer.pending_deliveries.toLocaleString()], [$t('federation_stats_failed'), peer.failed_deliveries.toLocaleString()]] as [label, value] (label)}
                <div>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              {/each}
            </dl>
          </div>
        </details>
      {:else}
        <div class="empty">
          <Icon name="globe" size={28} />
          <h3>{search ? $t('federation_stats_no_matches') : $t('federation_stats_empty')}</h3>
          <p>{search ? $t('federation_stats_try_search') : $t('federation_stats_empty_hint')}</p>
        </div>
      {/each}
    </div>
    <p class="accounting-note">{$t('federation_stats_accounting')}</p>
  </div>
</section>

<style>
  .instance-statistics {
    min-width: 0;
  }
  .summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.85rem;
  }
  .summary article,
  .peer-panel {
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    background: var(--surface);
    box-shadow: var(--shadow-sm);
  }
  .summary article {
    padding: 1.15rem;
  }
  .summary span,
  dt,
  .figure span {
    color: var(--text-muted);
    font-size: 0.78rem;
  }
  .summary strong {
    display: block;
    font: 750 clamp(1.5rem, 2.5vw, 2.25rem) var(--font-display);
    margin-top: 0.4rem;
    overflow-wrap: anywhere;
  }
  .peer-panel {
    margin-top: 1rem;
    overflow: hidden;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    padding: 1.25rem;
  }
  header > div:first-child {
    flex: 1 1 22rem;
  }
  h2 {
    font-size: 1.15rem;
    margin: 0 0 0.35rem;
  }
  p {
    color: var(--text-muted);
    font-size: 0.82rem;
    line-height: 1.6;
    margin: 0;
  }
  .controls {
    display: flex;
    flex: 1 1 26rem;
    gap: 0.6rem;
  }
  .search {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex: 1;
    min-width: 0;
    padding: 0 0.75rem;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface-raised);
    color: var(--text-muted);
  }
  input {
    width: 100%;
    min-width: 0;
    border: 0;
    background: transparent;
    color: var(--text);
    padding: 0.75rem 0;
    font: inherit;
  }
  select {
    min-width: 0;
    max-width: 50%;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface-raised);
    color: var(--text);
    padding: 0.7rem;
    font: inherit;
    font-size: 0.82rem;
  }
  input:focus-visible {
    outline: none;
  }
  .search:focus-within,
  select:focus-visible,
  summary:focus-visible {
    outline: 2px solid var(--accent-text);
    outline-offset: 2px;
  }
  details {
    border-top: 1px solid var(--line-soft);
  }
  summary {
    display: grid;
    grid-template-columns: minmax(0, 2fr) repeat(3, minmax(0, 1fr)) 18px;
    align-items: center;
    gap: 1rem;
    padding: 1.1rem 1.25rem;
    cursor: pointer;
    list-style: none;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  summary:hover {
    background: var(--surface-raised);
  }
  .peer-name {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    min-width: 0;
  }
  .peer-name > div {
    min-width: 0;
  }
  .peer-name strong,
  .peer-name small {
    display: block;
    overflow-wrap: anywhere;
  }
  .peer-name small {
    margin-top: 0.2rem;
    color: var(--text-muted);
    font-size: 0.78rem;
  }
  .globe {
    display: grid;
    place-items: center;
    width: 2.5rem;
    height: 2.5rem;
    flex-shrink: 0;
    border-radius: 10px;
    color: var(--accent-text);
    background: var(--accent-soft);
  }
  .figure {
    align-self: stretch;
    grid-template-rows: 1fr auto;
    display: grid;
    gap: 0.3rem;
    text-align: right;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .figure strong,
  dd {
    font-variant-numeric: tabular-nums;
  }
  .expand {
    color: var(--text-muted);
    transition: transform 0.15s;
  }
  details[open] .expand {
    transform: rotate(180deg);
  }
  .peer-details {
    padding: 0 1.25rem 1.25rem;
  }
  dl {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1rem;
  }
  dd {
    margin: 0.35rem 0 0;
    font-size: 0.9rem;
    overflow-wrap: anywhere;
  }
  .storage-grid {
    padding: 1rem;
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    background: var(--surface-raised);
  }
  .metadata-grid {
    padding-top: 1.25rem;
  }
  .accounting-note {
    padding: 1rem 1.25rem;
    border-top: 1px solid var(--line-soft);
  }
  .empty {
    display: grid;
    justify-items: center;
    text-align: center;
    gap: 0.5rem;
    padding: 2.5rem 1rem;
    color: var(--text-muted);
  }
  .empty h3 {
    color: var(--text);
    margin: 0;
    font-size: 1rem;
  }
  @media (max-width: 1100px) {
    .summary,
    dl {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  @media (max-width: 600px) {
    summary {
      grid-template-columns: repeat(3, minmax(0, 1fr)) 18px;
      gap: 0.9rem 0.5rem;
      padding: 1rem;
    }
    .peer-name {
      grid-column: 1 / 4;
    }
    .expand {
      grid-column: 4;
      grid-row: 1;
    }
    .figure {
      grid-row: 2;
      text-align: left;
    }
    header,
    .peer-details {
      padding: 1rem;
    }
    .controls {
      flex-basis: 100%;
      flex-wrap: wrap;
    }
    .search {
      flex-basis: 100%;
    }
    select {
      max-width: 100%;
      width: 100%;
    }
    .summary article {
      padding: 0.9rem;
    }
  }
</style>
