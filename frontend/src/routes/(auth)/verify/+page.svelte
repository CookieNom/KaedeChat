<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { onMount } from 'svelte';
  import { api, userErrorMessage } from '$lib/api/client';
  import { consumeUrlToken } from '$lib/auth/url-token';

  let verificationState = $state<'working' | 'done' | 'failed'>('working');
  let error = $state('');

  onMount(async () => {
    const token = consumeUrlToken();
    if (!token) {
      verificationState = 'failed';
      error = $t('ui_this_verification_link_is_missing_its_token_r_bdd6c03f');
      return;
    }
    try {
      await api('/auth/verify-email', { method: 'POST', body: JSON.stringify({ token }) });
      verificationState = 'done';
    } catch (caught) {
      verificationState = 'failed';
      error = userErrorMessage(
        caught,
        $t('ui_this_verification_link_may_be_invalid_expired_b4c83a51')
      );
    }
  });
</script>

<svelte:head><title>{$t('ui_verify_email_kaede_chat_2e421d23')}</title></svelte:head>
<div aria-live="polite">
  <p class="eyebrow">{$t('ui_email_verification_4d225196')}</p>
  {#if verificationState === 'working'}<h1 class="auth-title">
      {$t('ui_following_the_link_69a9723d')}
    </h1>
  {:else if verificationState === 'done'}<h1 class="auth-title">
      {$t('ui_you_re_verified_a3ed9e86')}
    </h1>
    <p>
      <a class="primary-button" href={resolve('/login')}>{$t('ui_continue_to_sign_in_d398cd0a')}</a>
    </p>
  {:else}<h1 class="auth-title">{$t('ui_this_link_has_faded_c21c2905')}</h1>
    <p class="lede form-error" role="alert">{error}</p>
    <p class="form-foot">
      <a href={resolve('/login')}>{$t('ui_return_to_sign_in_8504054f')}</a>
    </p>{/if}
</div>
