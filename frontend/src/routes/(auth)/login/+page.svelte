<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import {
    loadPasswordKdfContext,
    preparePassword,
    savePreparedVaultKey
  } from '$lib/auth/password-kdf';
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
  let verificationEmailInput = $state<HTMLInputElement | null>(null);
  let instanceField = $state<NativeInstanceField | null>(null);
  let verificationResendAvailable = $state(false);
  let verificationEmail = $state('');
  let verificationResendBusy = $state(false);
  let verificationResendStatus = $state('');
  let preparedVaultKey: CryptoKey | null = null;

  function emailCandidate(value: string): string {
    const candidate = value.trim().toLowerCase();
    if (candidate.length > 320 || /\s/.test(candidate)) return '';
    const separator = candidate.lastIndexOf('@');
    const domain = candidate.slice(separator + 1);
    return separator > 0 && domain.includes('.') ? candidate : '';
  }

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
    verificationResendAvailable = false;
    verificationResendStatus = '';
    try {
      if (!(await instanceField?.apply())) return;
      if (ticket) {
        await api('/auth/mfa', {
          method: 'POST',
          body: JSON.stringify({ ticket, code })
        });
      } else {
        const prepared = await preparePassword(
          password,
          await loadPasswordKdfContext(identifier.trim())
        );
        preparedVaultKey = prepared.vaultKey;
        const result = await api<LoginResult>('/auth/login', {
          method: 'POST',
          body: JSON.stringify({
            identifier,
            password: prepared.authenticationSecret,
            password_kdf_version: prepared.context.version,
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
      if (!preparedVaultKey) {
        throw new Error('Encryption keys could not be unlocked. Start sign-in again.');
      }
      await savePreparedVaultKey(preparedVaultKey);
      preparedVaultKey = null;
      password = '';
      code = '';
      ticket = null;
      const returnTo = sessionStorage.getItem('kaede.return-to');
      sessionStorage.removeItem('kaede.return-to');
      window.location.replace(safeReturnPath(returnTo, window.location.origin) ?? resolve('/home'));
    } catch (caught) {
      if (!ticket) preparedVaultKey = null;
      if (caught instanceof ApiError) {
        error = caught.message;
        if (!ticket && caught.code === 'EMAIL_NOT_VERIFIED') {
          verificationResendAvailable = true;
          verificationEmail ||= emailCandidate(identifier);
          await tick();
          if (!verificationEmail) verificationEmailInput?.focus();
        }
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
        error = userErrorMessage(caught, 'Could not sign in. Check your details and try again.');
      }
    } finally {
      busy = false;
    }
  }

  async function resendVerification() {
    if (verificationResendBusy) return;
    if (!verificationEmailInput?.reportValidity()) return;
    verificationResendBusy = true;
    verificationResendStatus = '';
    try {
      await api('/auth/verify-email/resend', {
        method: 'POST',
        body: JSON.stringify({ email: verificationEmail.trim() })
      });
      verificationResendStatus =
        'If that address belongs to an unverified account, a new verification email is on its way.';
    } catch (caught) {
      verificationResendStatus = userErrorMessage(
        caught,
        'The verification email could not be requested. Try again shortly.'
      );
    } finally {
      verificationResendBusy = false;
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
{#if verificationResendAvailable}
  <div class="verification-resend-panel">
    <p class="field-note">
      Didn’t receive the message? Confirm the email address used to create this account.
    </p>
    <label
      >Verification email <input
        bind:this={verificationEmailInput}
        bind:value={verificationEmail}
        type="email"
        autocomplete="email"
        maxlength="320"
        required
        disabled={verificationResendBusy}
      /></label
    >
    <button
      type="button"
      class="secondary-button"
      disabled={verificationResendBusy}
      onclick={resendVerification}
      >{verificationResendBusy ? 'Sending…' : 'Resend verification email'}</button
    >
    {#if verificationResendStatus}
      <p class="field-note" role="status" aria-live="polite">{verificationResendStatus}</p>
    {/if}
  </div>
{/if}
<p class="form-foot">
  {#if recoveryEnabled === true}<a href={resolve('/forgot-password')}>Forgot password?</a> ·
  {/if}New here?
  <a href={resolve('/register')}>Create an account</a>
</p>
