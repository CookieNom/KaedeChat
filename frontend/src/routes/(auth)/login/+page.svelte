<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import { safeReturnPath } from '$lib/auth/return-path';
  import TurnstileWidget from '$lib/components/TurnstileWidget.svelte';
  import NativeInstanceField from '$lib/components/NativeInstanceField.svelte';
  import { initializeNativeInstance } from '$lib/platform/native';
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
  let turnstileEnabled = $state(false);
  let turnstileSiteKey = $state<string | null>(null);
  let turnstileRequired = $state(false);
  let turnstileToken = $state<string | null>(null);
  let turnstileWidget = $state<TurnstileWidget | null>(null);
  let codeInput = $state<HTMLInputElement | null>(null);
  let instanceField = $state<NativeInstanceField | null>(null);

  onMount(() => {
    const controller = new AbortController();
    void initializeNativeInstance()
      .then(() => loadAuthConfiguration(controller.signal))
      .then((configuration) => {
        recoveryEnabled = configuration.password_recovery_enabled;
        turnstileEnabled = configuration.turnstile.enabled;
        turnstileSiteKey = configuration.turnstile.site_key;
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
      if (!(await instanceField?.apply())) return;
      if (ticket) {
        await api('/auth/mfa', {
          method: 'POST',
          body: JSON.stringify({ ticket, code })
        });
      } else {
        const result = await api<LoginResult>('/auth/login', {
          method: 'POST',
          body: JSON.stringify({
            identifier,
            password,
            ...(turnstileRequired ? { turnstile_token: turnstileToken } : {})
          })
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
      if (caught instanceof ApiError) {
        error = caught.message;
        const needsChallenge =
          turnstileEnabled &&
          (caught.detail.turnstile_required === true ||
            caught.code === 'TURNSTILE_REQUIRED' ||
            caught.code === 'TURNSTILE_INVALID');
        if (needsChallenge) {
          const alreadyVisible = turnstileRequired;
          turnstileRequired = true;
          turnstileToken = null;
          await tick();
          if (alreadyVisible) turnstileWidget?.reset();
        }
      } else {
        error = 'Could not sign in.';
      }
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
    <NativeInstanceField bind:this={instanceField} disabled={busy} />
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
    {#if turnstileRequired && turnstileSiteKey}
      <div class="adaptive-challenge">
        <p class="field-note">Please verify this sign-in attempt before trying again.</p>
        <TurnstileWidget
          bind:this={turnstileWidget}
          siteKey={turnstileSiteKey}
          action="kaede-login"
          onToken={(token) => (turnstileToken = token)}
        />
      </div>
    {/if}
  {/if}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  <button class="primary-button" disabled={busy || (turnstileRequired && !turnstileToken)}
    >{busy ? 'One moment…' : ticket ? 'Verify' : 'Sign in'}</button
  >
</form>
<p class="form-foot">
  {#if recoveryEnabled === true}<a href={resolve('/forgot-password')}>Forgot password?</a> ·
  {/if}New here?
  <a href={resolve('/register')}>Create an account</a>
</p>
