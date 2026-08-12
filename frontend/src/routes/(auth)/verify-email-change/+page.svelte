<script lang="ts">
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
      error = 'This confirmation link is missing its token. Request another email change.';
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
        'This confirmation link may be invalid, expired, or already used. Request another email change.'
      );
    }
  });
</script>

<svelte:head><title>Confirm email · Kaede Chat</title></svelte:head>
<div aria-live="polite">
  <p class="eyebrow">Email change</p>
  {#if confirmationState === 'working'}
    <h1 class="auth-title">Confirming your new address…</h1>
  {:else if confirmationState === 'done'}
    <h1 class="auth-title">Your address is updated.</h1>
    <p><a class="primary-button" href={resolve('/settings')}>Return to settings</a></p>
  {:else}
    <h1 class="auth-title">This link has faded.</h1>
    <p class="lede form-error" role="alert">{error}</p>
    <p class="form-foot"><a href={resolve('/settings')}>Return to settings</a></p>
  {/if}
</div>
