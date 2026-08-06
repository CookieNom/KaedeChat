<script lang="ts">
  import { resolve } from '$app/paths';
  import { api } from '$lib/api/client';
  import { consumeUrlToken } from '$lib/auth/url-token';
  import { onMount } from 'svelte';

  let state = $state<'working' | 'done' | 'failed'>('working');

  onMount(async () => {
    const token = consumeUrlToken();
    if (!token) {
      state = 'failed';
      return;
    }
    try {
      await api('/auth/email/change/confirm', {
        method: 'POST',
        body: JSON.stringify({ token })
      });
      state = 'done';
    } catch {
      state = 'failed';
    }
  });
</script>

<svelte:head><title>Confirm email · Kaede Chat</title></svelte:head>
<div aria-live="polite">
  <p class="eyebrow">Email change</p>
  {#if state === 'working'}
    <h1 class="auth-title">Confirming your new address…</h1>
  {:else if state === 'done'}
    <h1 class="auth-title">Your address is updated.</h1>
    <p><a class="primary-button" href={resolve('/settings')}>Return to settings</a></p>
  {:else}
    <h1 class="auth-title">This link has faded.</h1>
    <p class="lede">It may be invalid, expired, or already used.</p>
    <p class="form-foot"><a href={resolve('/settings')}>Return to settings</a></p>
  {/if}
</div>
