<script lang="ts">
  import { t } from '$lib/ui/locale';

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
        throw new Error($t('ui_encryption_keys_could_not_be_unlocked_start_s_29db2800'));
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
        error = userErrorMessage(
          caught,
          $t('ui_could_not_sign_in_check_your_details_and_try__5102b372')
        );
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
        $t('ui_the_verification_email_could_not_be_requested_e9581ccc')
      );
    } finally {
      verificationResendBusy = false;
    }
  }
</script>

<svelte:head><title>{$t('ui_sign_in_kaede_chat_52b8ba2a')}</title></svelte:head>

<p class="eyebrow">{$t('ui_welcome_back_66212495')}</p>
<h1 class="auth-title">{$t('ui_pick_up_the_thread_767b206c')}</h1>
<p class="auth-intro">{$t('ui_sign_in_with_your_local_username_email_addres_2bbde8e4')}</p>
<form
  onsubmit={(event) => {
    event.preventDefault();
    submit();
  }}
>
  {#if ticket}
    <label
      >{$t('ui_authenticator_or_recovery_code_322eeecc')}
      <input
        bind:this={codeInput}
        bind:value={code}
        autocomplete="one-time-code"
        minlength="6"
        maxlength="32"
        required
        disabled={busy}
      /></label
    >
    <p class="field-note">{$t('ui_enter_the_current_code_from_your_authenticato_6a921309')}</p>
  {:else}
    <NativeInstanceField bind:this={instanceField} disabled={busy} />
    <label
      >{$t('ui_email_username_or_handle_e502b7c9')}
      <input
        bind:value={identifier}
        autocomplete="username"
        minlength="2"
        maxlength="320"
        required
        disabled={busy}
      /></label
    >
    <label
      >{$t('ui_password_e7cf3ef4')}
      <input
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
        <p class="field-note">{$t('ui_please_verify_this_sign_in_attempt_before_try_8f0e84e1')}</p>
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
    >{busy
      ? $t('ui_one_moment_7ce83d45')
      : ticket
        ? $t('ui_verify_eea2745e')
        : $t('ui_sign_in_bfd402b2')}</button
  >
</form>
{#if verificationResendAvailable}
  <div class="verification-resend-panel">
    <p class="field-note">{$t('ui_didn_t_receive_the_message_confirm_the_email__255653b3')}</p>
    <label
      >{$t('ui_verification_email_23fac8fb')}
      <input
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
      >{verificationResendBusy
        ? $t('ui_sending_b8ed5279')
        : $t('ui_resend_verification_email_900852de')}</button
    >
    {#if verificationResendStatus}
      <p class="field-note" role="status" aria-live="polite">{verificationResendStatus}</p>
    {/if}
  </div>
{/if}
<p class="form-foot">
  {#if recoveryEnabled === true}<a href={resolve('/forgot-password')}
      >{$t('ui_forgot_password_30c1d8d3')}</a
    > ·
  {/if}{$t('ui_new_here_38c1c445')}
  <a href={resolve('/register')}>{$t('ui_create_an_account_86033f75')}</a>
</p>
