<script lang="ts">
  import { resolve } from '$app/paths';
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { consumeUrlToken } from '$lib/auth/url-token';

  let state = $state<'working' | 'done' | 'failed'>('working');

  onMount(async () => {
    const token = consumeUrlToken();
    if (!token) {
      state = 'failed';
      return;
    }
    try {
      await api('/auth/verify-email', { method: 'POST', body: JSON.stringify({ token }) });
      state = 'done';
    } catch {
      state = 'failed';
    }
  });
</script>

<svelte:head><title>Verify email · Kaede Chat</title></svelte:head>
<div aria-live="polite">
  <p class="eyebrow">Email verification</p>
  {#if state === 'working'}<h1 class="auth-title">Following the link…</h1>
  {:else if state === 'done'}<h1 class="auth-title">You’re verified.</h1>
    <p><a class="primary-button" href={resolve('/login')}>Continue to sign in</a></p>
  {:else}<h1 class="auth-title">This link has faded.</h1>
    <p class="lede">It may be invalid, expired, or already used.</p>
    <p class="form-foot"><a href={resolve('/login')}>Return to sign in</a></p>{/if}
</div>
