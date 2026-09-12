<script lang="ts">
  import { t } from '$lib/ui/locale';

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
      error = $t('ui_this_invite_is_unavailable_e8c97148');
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
          $t('ui_this_invite_is_unavailable_ask_for_a_new_invi_3932878a')
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
        error = userErrorMessage(caught, $t('ui_could_not_accept_this_invite_try_again_fc4d2e30'));
      }
    } finally {
      if (generation === loadGeneration && targetCode === code) busy = false;
    }
  }

  function openOnHome() {
    if (!preview) return;
    const target = federatedInviteHomeUrl(preview.code, preview.guild.origin_domain, homeDomain);
    if (!target) {
      homeError = $t('ui_enter_a_valid_home_instance_domain_such_as_ch_c756a84d');
      return;
    }
    homeError = '';
    window.location.assign(target);
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- the typed guild route is resolved before parameters are inserted -->

<svelte:head><title>{$t('ui_guild_invitation_kaede_chat_356f4cb7')}</title></svelte:head>

<div class="auth-page">
  <section class="auth-card invite-card">
    <span class="invite-mark" aria-hidden="true"
      >{preview?.guild.name.slice(0, 2).toUpperCase() ?? 'K'}</span
    >
    <p class="eyebrow">{$t('ui_you_re_invited_3b3fc7fd')}</p>
    <h1>{preview?.guild.name ?? $t('ui_opening_invitation_451845cf')}</h1>
    {#if preview?.guild.description}<p>{preview.guild.description}</p>{/if}
    {#if preview}
      <p>{$t('ui_hosted_by_f772bf27')} <strong>{preview.guild.origin_domain}</strong></p>
      {#if preview.expires_at}
        <p class="field-note">
          {$t('ui_expires_value0_8c3e7e71', { value0: String(formatDateTime(preview.expires_at)) })}
        </p>
      {/if}
      {#if destination?.name}<p class="field-note">
          {$t('ui_destination_value0_fdd67332', { value0: String(destination.name) })}
        </p>{/if}
      {#each details as detail (detail)}
        <p class="field-note">{detail}</p>
      {/each}
      <button class="primary-button" disabled={busy} onclick={accept}>
        {busy ? $t('ui_joining_6bbb89ee') : $t('ui_accept_invitation_7e17aadc')}
      </button>
      <details>
        <summary>{$t('ui_use_an_account_from_another_instance_b9ac912c')}</summary>
        <form
          onsubmit={(event) => {
            event.preventDefault();
            openOnHome();
          }}
        >
          <label>
            <span>{$t('ui_your_home_instance_496f3dc5')}</span>
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
            {$t('ui_you_ll_review_this_same_invite_on_your_home_i_697d544b')}
          </p>
          {#if homeError}<p class="form-error" role="alert">{homeError}</p>{/if}
          <button class="secondary-button" type="submit"
            >{$t('ui_continue_to_my_instance_d4434687')}</button
          >
        </form>
      </details>
    {/if}
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  </section>
</div>
