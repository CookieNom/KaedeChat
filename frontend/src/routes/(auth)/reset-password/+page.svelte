<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import { consumeUrlToken } from '$lib/auth/url-token';
  import { tick } from 'svelte';
  let password = $state('');
  let confirmPassword = $state('');
  let token = $state<string | null>(null);
  let done = $state(false);
  let error = $state('');
  let busy = $state(false);
  let successPanel = $state<HTMLElement | null>(null);
  $effect(() => {
    token ??= consumeUrlToken();
  });
  async function submit() {
    if (busy) return;
    if (!token) {
      error = 'This reset link is invalid.';
      return;
    }
    if (password !== confirmPassword) {
      error = 'Passwords do not match.';
      return;
    }
    busy = true;
    error = '';
    try {
      await api('/auth/password/reset', {
        method: 'POST',
        body: JSON.stringify({ token, password })
      });
      password = '';
      confirmPassword = '';
      token = null;
      done = true;
      await tick();
      successPanel?.focus();
    } catch (caught) {
      error = caught instanceof ApiError ? caught.message : 'Could not reset password.';
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Choose password · Kaede Chat</title></svelte:head>
<p class="eyebrow">Account recovery</p>
<h1 class="auth-title">Choose a new key.</h1>
{#if done}<div
    bind:this={successPanel}
    class="auth-success"
    role="status"
    aria-live="polite"
    aria-atomic="true"
    tabindex="-1"
  >
    <p class="lede">
      Your password has been updated. <a href={resolve('/login')}>Sign in</a>.
    </p>
  </div>
{:else}<form
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    <label
      >New password <input
        bind:value={password}
        type="password"
        minlength="10"
        maxlength="256"
        autocomplete="new-password"
        required
        disabled={busy}
      /></label
    >
    <label
      >Confirm new password <input
        bind:value={confirmPassword}
        type="password"
        minlength="10"
        maxlength="256"
        autocomplete="new-password"
        required
        disabled={busy}
      /></label
    >
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    <button class="primary-button" disabled={busy}>
      {busy ? 'Updating…' : 'Update password'}
    </button>
  </form>{/if}
