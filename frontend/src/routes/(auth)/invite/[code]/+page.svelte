<script lang="ts">
  import { page } from '$app/state';
  import { resolve } from '$app/paths';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import { formatDateTime } from '$lib/ui/locale';
  import {
    invitedChannel,
    invitePreviewDetails,
    type InvitePreview
  } from '$lib/chat/invite-preview';
  import { entityRef } from '$lib/chat/refs';
  import { federatedInviteHomeUrl } from '$lib/chat/invites';
  import type { Guild } from '$lib/chat/types';
  import { guildChannelPath } from '$lib/navigation/routes';

  const code = $derived(page.params.code ?? '');
  let preview = $state<InvitePreview | null>(null);
  let error = $state('');
  let busy = $state(false);
  let homeDomain = $state('');
  let homeError = $state('');
  const destination = $derived(preview ? invitedChannel(preview.guild, preview.channel_id) : null);
  const details = $derived(preview ? invitePreviewDetails(preview) : []);

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
        error = userErrorMessage(
          caught,
          'This invite is unavailable. Ask for a new invite and try again.'
        );
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
        error = userErrorMessage(caught, 'Could not accept this invite. Try again.');
      }
    } finally {
      if (generation === loadGeneration && targetCode === code) busy = false;
    }
  }

  function openOnHome() {
    if (!preview) return;
    const target = federatedInviteHomeUrl(preview.code, preview.guild.origin_domain, homeDomain);
    if (!target) {
      homeError = 'Enter a valid home instance domain, such as chat.example.';
      return;
    }
    homeError = '';
    window.location.assign(target);
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
      </p>
      {#if preview.expires_at}
        <p class="field-note">Expires {formatDateTime(preview.expires_at)}</p>
      {/if}
      {#if destination?.name}<p class="field-note">Destination: {destination.name}</p>{/if}
      {#each details as detail (detail)}
        <p class="field-note">{detail}</p>
      {/each}
      <button class="primary-button" disabled={busy} onclick={accept}>
        {busy ? 'Joining…' : 'Accept invitation'}
      </button>
      <details>
        <summary>Use an account from another instance</summary>
        <form
          onsubmit={(event) => {
            event.preventDefault();
            openOnHome();
          }}
        >
          <label>
            <span>Your home instance</span>
            <input
              bind:value={homeDomain}
              inputmode="url"
              autocomplete="url"
              placeholder="chat.example"
              maxlength="253"
              oninput={() => (homeError = '')}
            />
          </label>
          <p class="field-note">
            You’ll review this same invite on your home instance, where your account is signed in.
          </p>
          {#if homeError}<p class="form-error" role="alert">{homeError}</p>{/if}
          <button class="secondary-button" type="submit">Continue to my instance</button>
        </form>
      </details>
    {/if}
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  </section>
</div>
