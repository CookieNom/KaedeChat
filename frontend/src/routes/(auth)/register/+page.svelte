<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import TurnstileWidget from '$lib/components/TurnstileWidget.svelte';
  import NativeInstanceField from '$lib/components/NativeInstanceField.svelte';
  import {
    initializeNativeInstance,
    isNativeDesktop,
    storedNativeInstance
  } from '$lib/platform/native';
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
  let instanceField = $state<NativeInstanceField | null>(null);
  let nativeDesktop = $state(false);
  let registrationStep = $state<1 | 2>(1);
  let selectedInstance = $state('');

  function applyConfiguration(configuration: Awaited<ReturnType<typeof loadAuthConfiguration>>) {
    emailRequired = configuration.email_required;
    turnstileEnabled = configuration.turnstile.enabled;
    turnstileSiteKey = configuration.turnstile.site_key;
    if (!emailRequired) email = '';
  }

  async function continueFromServer() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      if (!(await instanceField?.apply())) return;
      selectedInstance = storedNativeInstance();
      applyConfiguration(await loadAuthConfiguration());
      registrationStep = 2;
    } catch (caught) {
      error = userErrorMessage(
        caught,
        'Could not reach that Kaede server. Check the domain and try again.'
      );
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    nativeDesktop = isNativeDesktop();
    selectedInstance = storedNativeInstance();
    const controller = new AbortController();
    void initializeNativeInstance()
      .then(() => loadAuthConfiguration(controller.signal))
      .then(applyConfiguration)
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
      error = userErrorMessage(
        caught,
        'Could not create your account. Check the form and try again.'
      );
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
{:else if nativeDesktop && registrationStep === 1}
  <p class="eyebrow">Create account</p>
  <h1 class="auth-title">Choose your server.</h1>
  <p class="auth-intro">
    Your server stores your account and connects it to communities across the Kaede network.
  </p>
  <form
    class="registration-server-form"
    onsubmit={(event) => {
      event.preventDefault();
      void continueFromServer();
    }}
  >
    <NativeInstanceField bind:this={instanceField} disabled={busy} suggestedInstance="kaede.chat" />
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    <button class="primary-button" disabled={busy}>
      {busy ? 'Checking server…' : 'Continue'}
    </button>
  </form>
{:else if emailRequired === null}
  <p class="eyebrow">Create account</p>
  <h1 class="auth-title">Checking registration options…</h1>
{:else}
  <p class="eyebrow">Your place, your name</p>
  <h1 class="auth-title">Create your account.</h1>
  {#if nativeDesktop}
    <div class="registration-server-summary">
      <span>Account server</span>
      <strong>{selectedInstance}</strong>
      <button
        type="button"
        class="quiet-button"
        disabled={busy}
        onclick={() => {
          error = '';
          registrationStep = 1;
        }}>Change</button
      >
    </div>
  {/if}
  <form
    class="registration-details-form"
    onsubmit={(event) => {
      event.preventDefault();
      submit();
    }}
  >
    <div class="registration-details-grid" class:single-email-column={!emailRequired}>
      <label
        >Username
        <input
          bind:value={username}
          pattern={'[a-z0-9_.]{2,32}'}
          maxlength="32"
          autocomplete="username"
          required
          disabled={busy}
        />
        <small>Lowercase letters, numbers, dots, and underscores.</small></label
      >
      {#if emailRequired}
        <label
          >Email
          <input
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
        >Password
        <input
          bind:value={password}
          type="password"
          minlength="10"
          maxlength="256"
          autocomplete="new-password"
          required
          disabled={busy}
        />
        <small>At least 10 characters; a password manager is recommended.</small></label
      >
      <label
        >Confirm password
        <input
          bind:value={confirmPassword}
          type="password"
          minlength="10"
          maxlength="256"
          autocomplete="new-password"
          required
          disabled={busy}
        /></label
      >
    </div>
    {#if turnstileEnabled && turnstileSiteKey}
      <TurnstileWidget
        bind:this={turnstileWidget}
        siteKey={turnstileSiteKey}
        action="kaede-register"
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
