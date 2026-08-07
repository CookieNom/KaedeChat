<script lang="ts">
  import { page } from '$app/state';
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import { formatDateTime } from '$lib/ui/locale';
  import { invitedChannel } from '$lib/chat/invite-preview';
  import { entityRef } from '$lib/chat/refs';
  import type { Guild } from '$lib/chat/types';
  import { guildChannelPath } from '$lib/navigation/routes';

  interface InvitePreview {
    code: string;
    guild: Guild;
    uses?: number;
    max_uses?: number | null;
    expires_at: string | null;
    channel_id: string | null;
  }

  const code = $derived(page.params.code ?? '');
  let preview = $state<InvitePreview | null>(null);
  let error = $state('');
  let busy = $state(false);

  let loadGeneration = 0;

  $effect(() => {
    const targetCode = code;
    const generation = ++loadGeneration;
    const controller = new AbortController();
    preview = null;
    error = '';
    busy = false;
    if (!targetCode) {
      error = 'This invite is unavailable.';
      return;
    }
    void api<InvitePreview>(`/invites/${encodeURIComponent(targetCode)}`, {
      signal: controller.signal
    })
      .then((value) => {
        if (generation === loadGeneration && targetCode === code) preview = value;
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || generation !== loadGeneration || targetCode !== code)
          return;
        error = caught instanceof ApiError ? caught.message : 'This invite is unavailable.';
      });
    return () => controller.abort();
  });

  async function accept() {
    if (busy) return;
    const targetCode = code;
    const generation = loadGeneration;
    busy = true;
    error = '';
    try {
      const guild = await api<Guild>(`/invites/${encodeURIComponent(targetCode)}`, {
        method: 'POST'
      });
      if (generation !== loadGeneration || targetCode !== code) return;
      const hydrated = await api<Guild>(`/guilds/${encodeURIComponent(entityRef(guild))}`);
      if (generation !== loadGeneration || targetCode !== code) return;
      const channel = invitedChannel(hydrated, preview?.channel_id ?? null);
      if (!channel) {
        window.location.assign(resolve('/home'));
        return;
      }
      window.location.assign(guildChannelPath(hydrated, channel));
    } catch (caught) {
      if (generation !== loadGeneration || targetCode !== code) return;
      if (caught instanceof ApiError && caught.status === 401) {
        sessionStorage.setItem('kaede.return-to', window.location.pathname);
        window.location.assign(resolve('/login'));
      } else {
        error = caught instanceof ApiError ? caught.message : 'Could not accept this invite.';
      }
    } finally {
      if (generation === loadGeneration && targetCode === code) busy = false;
    }
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- the typed guild route is resolved before parameters are inserted -->

<svelte:head><title>Guild invitation · Kaede Chat</title></svelte:head>

<div class="auth-page">
  <section class="auth-card invite-card">
    <span class="invite-mark" aria-hidden="true"
      >{preview?.guild.name.slice(0, 2).toUpperCase() ?? 'K'}</span
    >
    <p class="eyebrow">You’re invited</p>
    <h1>{preview?.guild.name ?? 'Opening invitation…'}</h1>
    {#if preview?.guild.description}<p>{preview.guild.description}</p>{/if}
    {#if preview}
      <p>
        Hosted by <strong>{preview.guild.origin_domain}</strong>
        {#if preview.max_uses != null}
          · {preview.max_uses - (preview.uses ?? 0)} uses remain{/if}
      </p>
      {#if preview.expires_at}
        <p class="field-note">Expires {formatDateTime(preview.expires_at)}</p>
      {/if}
      <button class="primary-button" disabled={busy} onclick={accept}>
        {busy ? 'Joining…' : 'Accept invitation'}
      </button>
    {/if}
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  </section>
</div>
