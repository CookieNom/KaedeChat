<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { canReadAnnouncementChannel } from '$lib/chat/announcements';
  import { entityRef } from '$lib/chat/refs';
  import type { Guild } from '$lib/chat/types';
  import AnnouncementFollowers from './AnnouncementFollowers.svelte';
  import Icon from './Icon.svelte';
  import { SvelteMap } from 'svelte/reactivity';

  let { guild, guilds }: { guild: Guild; guilds: Guild[] } = $props();

  let selectedSourceRef = $state('');
  const sources = $derived(
    (guild.channels ?? [])
      .filter((channel) => canReadAnnouncementChannel(channel, guild))
      .sort((left, right) => left.position - right.position)
  );
  const selectedSource = $derived(
    sources.find((channel) => entityRef(channel) === selectedSourceRef) ?? sources[0] ?? null
  );
  const availableGuilds = $derived.by(() => {
    const byRef = new SvelteMap(guilds.map((item) => [entityRef(item), item]));
    byRef.set(entityRef(guild), guild);
    return [...byRef.values()];
  });

  $effect(() => {
    const references = sources.map((channel) => entityRef(channel));
    if (!references.includes(selectedSourceRef)) selectedSourceRef = references[0] ?? '';
  });
</script>

<section
  id="channels-followed"
  class="integration-section"
  aria-labelledby="channels-followed-title"
>
  <header>
    <span class="section-icon" aria-hidden="true"><Icon name="bell" size={19} /></span>
    <div>
      <span>{$t('ui_integrations_090512d9')}</span>
      <h2 id="channels-followed-title">{$t('ui_announcement_distribution_21b34f29')}</h2>
      <p>{$t('ui_choose_where_published_announcement_posts_are_3b902e67')}</p>
    </div>
  </header>

  {#if sources.length === 0}
    <div class="empty-state">
      <strong>{$t('ui_no_readable_announcement_channels_0b785bd7')}</strong>
      <p>{$t('ui_view_channel_is_required_to_manage_follower_d_e650ff8c')}</p>
    </div>
  {:else}
    <label class="source-picker">
      <span>{$t('ui_announcement_channel_c75895c9')}</span>
      <select bind:value={selectedSourceRef}>
        {#each sources as source (entityRef(source))}
          <option value={entityRef(source)}>#{source.name ?? 'announcement'}</option>
        {/each}
      </select>
    </label>
    {#if selectedSource}
      <AnnouncementFollowers
        sourceChannel={selectedSource}
        guilds={availableGuilds}
        canRead={true}
        manageTitle={`Follower destinations for #${selectedSource.name ?? 'announcements'}`}
        manageDescription="Add or remove text channels that receive posts when a message is deliberately published from this announcement channel."
      />
    {/if}
  {/if}
</section>

<style>
  .integration-section {
    display: grid;
    gap: 16px;
    margin-top: 22px;
    border: 1px solid var(--line);
    border-radius: 13px;
    padding: 1rem;
    background: var(--surface);
  }
  header {
    display: flex;
    align-items: flex-start;
    gap: 11px;
  }
  header > div {
    min-width: 0;
  }
  header span,
  .source-picker > span {
    color: var(--text-muted);
    font-size: 0.75rem;
    font-weight: 750;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  h2,
  p {
    margin: 0.2rem 0;
  }
  header p,
  .empty-state p {
    color: var(--text-muted);
    line-height: 1.45;
  }
  .section-icon {
    display: grid;
    flex: 0 0 36px;
    height: 36px;
    place-items: center;
    border-radius: 10px;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
  }
  .source-picker {
    display: grid;
    gap: 6px;
    max-width: 440px;
  }
  select {
    min-height: 42px;
  }
  .empty-state {
    border: 1px dashed var(--line);
    border-radius: 10px;
    padding: 14px;
  }
</style>
