<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import TurnstileWidget from '$lib/components/TurnstileWidget.svelte';
  import { onMount, tick } from 'svelte';

  interface RegistrationResult {
    id: string;
    handle: string;
    email_verification_required: boolean;
  }

  let username = $state('');
  let email = $state('');
  let password = $state('');
  let confirmPassword = $state('');
  let error = $state('');
  let sent = $state(false);
  let busy = $state(false);
  let emailRequired = $state<boolean | null>(null);
  let verificationRequired = $state(true);
  let turnstileEnabled = $state(false);
  let turnstileSiteKey = $state<string | null>(null);
  let turnstileToken = $state<string | null>(null);
  let turnstileWidget = $state<TurnstileWidget | null>(null);
  let successPanel = $state<HTMLElement | null>(null);

  onMount(() => {
    const controller = new AbortController();
    void loadAuthConfiguration(controller.signal)
      .then((configuration) => {
        emailRequired = configuration.email_required;
        turnstileEnabled = configuration.turnstile.enabled;
        turnstileSiteKey = configuration.turnstile.site_key;
        if (!emailRequired) email = '';
      })
      .catch(() => {
        // Registration still fails closed on the server if this discovery call
        // is unavailable, so requiring email is the safe fallback.
        emailRequired = true;
      });
    return () => controller.abort();
  });

  async function submit() {
    if (busy || emailRequired === null) return;
    if (password !== confirmPassword) {
      error = 'Passwords do not match.';
      return;
    }
    busy = true;
    error = '';
    try {
      const result = await api<RegistrationResult>('/auth/register', {
        method: 'POST',
        body: JSON.stringify({
          username,
          ...(emailRequired ? { email } : {}),
          password,
          ...(turnstileEnabled ? { turnstile_token: turnstileToken } : {})
        })
      });
      password = '';
      confirmPassword = '';
      verificationRequired = result.email_verification_required;
      sent = true;
      await tick();
      successPanel?.focus();
    } catch (caught) {
      error = caught instanceof ApiError ? caught.message : 'Could not create your account.';
      if (turnstileEnabled) turnstileWidget?.reset();
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Create account · Kaede Chat</title></svelte:head>

{#if sent}
  <div
    bind:this={successPanel}
    class="auth-success"
    role="status"
    aria-live="polite"
    aria-atomic="true"
    tabindex="-1"
  >
    {#if verificationRequired}
      <p class="eyebrow">Almost there</p>
      <h1 class="auth-title">Check your inbox.</h1>
      <p class="lede">We sent a verification link to {email}. It remains valid for 48 hours.</p>
    {:else}
      <p class="eyebrow">Account ready</p>
      <h1 class="auth-title">Welcome to Kaede.</h1>
      <p class="lede">Your account was created. You can sign in with your username and password.</p>
      <p class="form-foot"><a href={resolve('/login')}>Continue to sign in</a></p>
    {/if}
  </div>
{:else if emailRequired === null}
  <p class="eyebrow">Create account</p>
  <h1 class="auth-title">Checking registration options…</h1>
{:else}
  <p class="eyebrow">Your place, your name</p>
  <h1 class="auth-title">Join this instance.</h1>
  <form
    onsubmit={(event) => {
      event.preventDefault();
      submit();
    }}
  >
    <label
      >Username <input
        bind:value={username}
        pattern={'[a-z0-9_.]{2,32}'}
        maxlength="32"
        autocomplete="username"
        required
        disabled={busy}
      /></label
    >
    <p class="field-note">
      This becomes your permanent federated address. Usernames use lowercase letters, numbers, dots,
      and underscores.
    </p>
    {#if emailRequired}
      <label
        >Email <input
          bind:value={email}
          type="email"
          autocomplete="email"
          maxlength="320"
          required
          disabled={busy}
        /></label
      >
    {/if}
    <label
      >Password <input
        bind:value={password}
        type="password"
        minlength="10"
        maxlength="256"
        autocomplete="new-password"
        required
        disabled={busy}
      /></label
    >
    <p class="field-note">Use at least 10 characters. A password manager is recommended.</p>
    <label
      >Confirm password <input
        bind:value={confirmPassword}
        type="password"
        minlength="10"
        maxlength="256"
        autocomplete="new-password"
        required
        disabled={busy}
      /></label
    >
    {#if turnstileEnabled && turnstileSiteKey}
      <TurnstileWidget
        bind:this={turnstileWidget}
        siteKey={turnstileSiteKey}
        onToken={(token) => (turnstileToken = token)}
      />
    {/if}
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    <button class="primary-button" disabled={busy || (turnstileEnabled && !turnstileToken)}
      >{busy ? 'Creating…' : 'Create account'}</button
    >
  </form>
  <p class="form-foot">Already have an account? <a href={resolve('/login')}>Sign in</a></p>
{/if}
