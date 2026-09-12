<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { consumeUrlToken } from '$lib/auth/url-token';
  import { onMount } from 'svelte';

  let confirmationState = $state<'working' | 'done' | 'failed'>('working');
  let error = $state('');

  onMount(async () => {
    const token = consumeUrlToken();
    if (!token) {
      confirmationState = 'failed';
      error = $t('ui_this_confirmation_link_is_missing_its_token_r_9a32d1dc');
      return;
    }
    try {
      await api('/auth/email/change/confirm', {
        method: 'POST',
        body: JSON.stringify({ token })
      });
      confirmationState = 'done';
    } catch (caught) {
      confirmationState = 'failed';
      error = userErrorMessage(
        caught,
        $t('ui_this_confirmation_link_may_be_invalid_expired_324c7ecb')
      );
    }
  });
</script>

<svelte:head><title>{$t('ui_confirm_email_kaede_chat_88b09029')}</title></svelte:head>
<div aria-live="polite">
  <p class="eyebrow">{$t('ui_email_change_f6aa2134')}</p>
  {#if confirmationState === 'working'}
    <h1 class="auth-title">{$t('ui_confirming_your_new_address_1a28ab15')}</h1>
  {:else if confirmationState === 'done'}
    <h1 class="auth-title">{$t('ui_your_address_is_updated_a6bac80f')}</h1>
    <p>
      <a class="primary-button" href={resolve('/settings')}
        >{$t('ui_return_to_settings_cab0aa32')}</a
      >
    </p>
  {:else}
    <h1 class="auth-title">{$t('ui_this_link_has_faded_c21c2905')}</h1>
    <p class="lede form-error" role="alert">{error}</p>
    <p class="form-foot">
      <a href={resolve('/settings')}>{$t('ui_return_to_settings_cab0aa32')}</a>
    </p>
  {/if}
</div>
