<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import { firstNavigableChannel } from '$lib/chat/channels';
  import { loadInvitePreview, type InvitePreview } from '$lib/chat/invite-preview';
  import type { Guild } from '$lib/chat/types';
  import { assetUrl } from '$lib/media/assets';
  import { guildChannelPath } from '$lib/navigation/routes';
  import Icon from './Icon.svelte';

  let { reference }: { reference: string } = $props();
  let preview = $state<InvitePreview | null>(null);
  let unavailable = $state(false);
  let busy = $state(false);
  let error = $state('');
  let loadGeneration = 0;

  $effect(() => {
    const target = reference;
    const generation = ++loadGeneration;
    preview = null;
    unavailable = false;
    error = '';
    void loadInvitePreview(target)
      .then((result) => {
        if (generation === loadGeneration) preview = result;
      })
      .catch(() => {
        if (generation === loadGeneration) unavailable = true;
      });
  });

  async function join() {
    if (busy) return;
    const generation = loadGeneration;
    busy = true;
    error = '';
    try {
      const guild = await api<Guild>(`/invites/${encodeURIComponent(reference)}`, {
        method: 'POST'
      });
      if (generation !== loadGeneration) return;
      const channel = firstNavigableChannel(guild.channels);
      window.location.assign(channel ? guildChannelPath(guild, channel) : resolve('/home'));
    } catch (caught) {
      if (generation !== loadGeneration) return;
      error = caught instanceof ApiError ? caught.message : 'Could not join this guild.';
    } finally {
      if (generation === loadGeneration) busy = false;
    }
  }
</script>

{#if preview}
  <aside class="invite-embed" aria-label={`Invitation to ${preview.guild.name}`}>
    <div class="invite-embed-copy">
      <p>Guild invitation</p>
      <div>
        <span class="invite-embed-icon" aria-hidden="true">
          {#if preview.guild.icon_hash}
            <img src={assetUrl(preview.guild.icon_hash, 'thumbnail_128', preview.guild)} alt="" />
          {:else}
            {preview.guild.name.slice(0, 2).toUpperCase()}
          {/if}
        </span>
        <span>
          <strong>{preview.guild.name}</strong>
          <small>{preview.guild.origin_domain}</small>
        </span>
      </div>
      {#if preview.guild.description}<p class="invite-description">
          {preview.guild.description}
        </p>{/if}
    </div>
    <button class="invite-join" type="button" disabled={busy} onclick={join}>
      {busy ? 'Joining…' : 'Join guild'}
    </button>
    {#if error}<p class="invite-error" role="alert">{error}</p>{/if}
  </aside>
{:else if unavailable}
  <aside class="invite-embed unavailable" aria-label="Unavailable guild invitation">
    <Icon name="server" size={20} />
    <span
      ><strong>Invitation unavailable</strong><small>It may have expired or been revoked.</small
      ></span
    >
  </aside>
{:else}
  <aside class="invite-embed loading" aria-label="Loading guild invitation">
    <span></span><span></span>
  </aside>
{/if}

<style>
  .invite-embed {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 14px;
    width: min(430px, 100%);
    margin-top: 10px;
    padding: 16px;
    border: 1px solid var(--line);
    border-left: 4px solid var(--pine);
    border-radius: 14px;
    background: color-mix(in srgb, var(--surface-raised) 92%, var(--pine));
    box-shadow: 0 10px 28px rgb(0 0 0 / 14%);
  }

  .invite-embed-copy {
    min-width: 0;
  }

  .invite-embed-copy > p:first-child {
    margin: 0 0 10px;
    color: var(--muted);
    font-size: 0.69rem;
    font-weight: 800;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .invite-embed-copy > div {
    display: flex;
    align-items: center;
    gap: 11px;
  }

  .invite-embed-copy > div > span:last-child {
    display: grid;
    min-width: 0;
  }

  .invite-embed-copy strong,
  .invite-embed-copy small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .invite-embed-copy small {
    color: var(--muted);
  }

  .invite-embed-icon {
    display: grid;
    flex: 0 0 48px;
    width: 48px;
    height: 48px;
    place-items: center;
    overflow: hidden;
    border-radius: 15px;
    color: white;
    background: var(--accent-deep);
    font-weight: 800;
  }

  .invite-embed-icon img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .invite-description {
    display: -webkit-box;
    margin: 11px 0 0;
    overflow: hidden;
    color: var(--muted);
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .invite-join {
    align-self: center;
    min-width: 104px;
    padding: 10px 14px;
    border: 0;
    border-radius: 10px;
    color: white;
    background: var(--pine);
    font: inherit;
    font-weight: 800;
    cursor: pointer;
  }

  .invite-join:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .invite-join:disabled {
    cursor: wait;
    opacity: 0.7;
  }

  .invite-error {
    grid-column: 1 / -1;
    margin: 0;
    color: var(--danger);
    font-size: 0.82rem;
  }

  .unavailable {
    grid-template-columns: auto 1fr;
    align-items: center;
    color: var(--muted);
  }

  .unavailable span {
    display: grid;
  }

  .unavailable strong {
    color: var(--text);
  }

  .loading {
    display: flex;
    height: 80px;
    align-items: center;
  }

  .loading span {
    width: 46px;
    height: 46px;
    border-radius: 14px;
    background: var(--surface-soft);
  }

  .loading span:last-child {
    width: 150px;
    height: 18px;
    border-radius: 6px;
  }

  @media (max-width: 520px) {
    .invite-embed {
      grid-template-columns: 1fr;
    }

    .invite-join {
      width: 100%;
    }
  }
</style>
