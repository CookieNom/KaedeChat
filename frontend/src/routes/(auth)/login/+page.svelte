<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import { safeReturnPath } from '$lib/auth/return-path';
  import { onMount, tick } from 'svelte';

  interface LoginResult {
    mfa_required: boolean;
    mfa_ticket?: string;
  }

  let identifier = $state('');
  let password = $state('');
  let code = $state('');
  let ticket = $state<string | null>(null);
  let error = $state('');
  let busy = $state(false);
  let recoveryEnabled = $state<boolean | null>(null);
  let codeInput = $state<HTMLInputElement | null>(null);

  onMount(() => {
    const controller = new AbortController();
    void loadAuthConfiguration(controller.signal)
      .then((configuration) => {
        recoveryEnabled = configuration.password_recovery_enabled;
      })
      .catch(() => {
        // Keep the recovery link when capability discovery is temporarily
        // unavailable; the recovery endpoint remains enumeration-safe.
        recoveryEnabled = true;
      });
    return () => controller.abort();
  });

  async function submit() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      if (ticket) {
        await api('/auth/mfa', {
          method: 'POST',
          body: JSON.stringify({ ticket, code })
        });
      } else {
        const result = await api<LoginResult>('/auth/login', {
          method: 'POST',
          body: JSON.stringify({ identifier, password })
        });
        if (result.mfa_required) {
          ticket = result.mfa_ticket ?? null;
          password = '';
          await tick();
          codeInput?.focus();
          return;
        }
      }
      password = '';
      code = '';
      ticket = null;
      const returnTo = sessionStorage.getItem('kaede.return-to');
      sessionStorage.removeItem('kaede.return-to');
      window.location.replace(safeReturnPath(returnTo, window.location.origin) ?? resolve('/home'));
    } catch (caught) {
      error = caught instanceof ApiError ? caught.message : 'Could not sign in.';
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Sign in · Kaede Chat</title></svelte:head>

<p class="eyebrow">Welcome back</p>
<h1 class="auth-title">Pick up the thread.</h1>
<p class="auth-intro">Sign in with your local username, email address, or full federated handle.</p>
<form
  onsubmit={(event) => {
    event.preventDefault();
    submit();
  }}
>
  {#if ticket}
    <label
      >Authenticator or recovery code <input
        bind:this={codeInput}
        bind:value={code}
        autocomplete="one-time-code"
        minlength="6"
        maxlength="32"
        required
        disabled={busy}
      /></label
    >
    <p class="field-note">Enter the current code from your authenticator or one recovery code.</p>
  {:else}
    <label
      >Email, username, or handle <input
        bind:value={identifier}
        autocomplete="username"
        minlength="2"
        maxlength="320"
        required
        disabled={busy}
      /></label
    >
    <label
      >Password <input
        bind:value={password}
        type="password"
        autocomplete="current-password"
        maxlength="256"
        required
        disabled={busy}
      /></label
    >
  {/if}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  <button class="primary-button" disabled={busy}
    >{busy ? 'One moment…' : ticket ? 'Verify' : 'Sign in'}</button
  >
</form>
<p class="form-foot">
  {#if recoveryEnabled === true}<a href={resolve('/forgot-password')}>Forgot password?</a> ·
  {/if}New here?
  <a href={resolve('/register')}>Create an account</a>
</p>
