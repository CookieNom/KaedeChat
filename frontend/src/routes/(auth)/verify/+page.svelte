<script lang="ts">
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
      error = 'This verification link is missing its token. Request a new verification email.';
      return;
    }
    try {
      await api('/auth/verify-email', { method: 'POST', body: JSON.stringify({ token }) });
      verificationState = 'done';
    } catch (caught) {
      verificationState = 'failed';
      error = userErrorMessage(
        caught,
        'This verification link may be invalid, expired, or already used. Request a new verification email.'
      );
    }
  });
</script>

<svelte:head><title>Verify email · Kaede Chat</title></svelte:head>
<div aria-live="polite">
  <p class="eyebrow">Email verification</p>
  {#if verificationState === 'working'}<h1 class="auth-title">Following the link…</h1>
  {:else if verificationState === 'done'}<h1 class="auth-title">You’re verified.</h1>
    <p><a class="primary-button" href={resolve('/login')}>Continue to sign in</a></p>
  {:else}<h1 class="auth-title">This link has faded.</h1>
    <p class="lede form-error" role="alert">{error}</p>
    <p class="form-foot"><a href={resolve('/login')}>Return to sign in</a></p>{/if}
</div>
