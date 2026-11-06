<script lang="ts">
  import type { Channel, Guild } from '$lib/chat/types';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { assetUrl } from '$lib/media/assets';
  import Icon from './Icon.svelte';

  let {
    channel,
    guild,
    compact = false
  }: {
    channel: Channel;
    guild?: Guild;
    compact?: boolean;
  } = $props();
  const server = $derived(
    guild ?? chatEntities.guilds.get(`${channel.guild_id}@${channel.guild_domain}`)
  );
  const isDm = $derived(channel.guild_id === null);
  const group = $derived(isDm && (channel.type === 3 || channel.conversation_type === 'group'));
  const recipients = $derived((channel.recipients ?? []).slice(0, group ? 2 : 1));
  const pictures = $derived(
    isDm
      ? recipients.map((user) =>
          user.avatar_hash ? assetUrl(user.avatar_hash, 'thumbnail_128', user) : ''
        )
      : [server?.icon_hash ? assetUrl(server.icon_hash, 'thumbnail_128', server) : '']
  );
  const badge = $derived(
    isDm
      ? group
        ? 'users'
        : 'message'
      : [2, 13].includes(channel.type)
        ? 'volume'
        : [10, 11, 12].includes(channel.type)
          ? 'threads'
          : 'hash'
  );
</script>

<span
  class="conversation-icon"
  class:compact
  class:stacked={group && pictures.length > 1}
  aria-hidden="true"
>
  {#each pictures.length ? pictures : [''] as picture, index (index + picture)}
    <span class="picture" class:second={index === 1}>
      <Icon name={isDm ? (group ? 'users' : 'user') : 'server'} size={compact ? 18 : 22} />
      {#if picture}
        <img
          src={picture}
          alt=""
          loading="lazy"
          onerror={(event) => {
            event.currentTarget.setAttribute('hidden', '');
          }}
        />
      {/if}
    </span>
  {/each}
  <span class="badge"><Icon name={badge} size={compact ? 12 : 14} /></span>
</span>

<style>
  .conversation-icon {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 44px;
    flex: 0 0 44px;
  }
  .picture {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    overflow: hidden;
    border-radius: 50%;
    color: var(--text-muted);
    background: var(--surface-hover);
  }
  img {
    position: absolute;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .stacked .picture {
    width: 30px;
    height: 30px;
    inset: 0 auto auto 0;
  }
  .stacked .second {
    inset: auto 0 0 auto;
    border: 2px solid var(--surface-raised);
  }
  .badge {
    position: absolute;
    right: -2px;
    bottom: -2px;
    display: grid;
    place-items: center;
    width: 21px;
    height: 21px;
    border: 2px solid var(--surface-raised);
    border-radius: 50%;
    color: var(--text-soft);
    background: var(--surface-subtle);
  }
  .compact {
    width: 32px;
    height: 32px;
    flex-basis: 32px;
  }
  .compact .badge {
    width: 17px;
    height: 17px;
  }
  .compact.stacked .picture {
    width: 23px;
    height: 23px;
  }
</style>
