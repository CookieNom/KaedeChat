<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import { prepareRegistrationPassword } from '$lib/auth/password-kdf';
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
        $t('ui_could_not_reach_that_kaede_server_check_the_d_4a2c6f64')
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
      error = $t('ui_passwords_do_not_match_6c6e178a');
      return;
    }
    busy = true;
    error = '';
    try {
      const prepared = await prepareRegistrationPassword(password);
      const result = await api<RegistrationResult>('/auth/register', {
        method: 'POST',
        body: JSON.stringify({
          username,
          ...(emailRequired ? { email } : {}),
          password: prepared.authenticationSecret,
          password_kdf: prepared.context,
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
        $t('ui_could_not_create_your_account_check_the_form__346aba6e')
      );
      if (turnstileEnabled) turnstileWidget?.reset();
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>{$t('ui_create_account_kaede_chat_0d05deff')}</title></svelte:head>

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
      <p class="eyebrow">{$t('ui_almost_there_750a358a')}</p>
      <h1 class="auth-title">{$t('ui_check_your_inbox_75a9fda6')}</h1>
      <p class="lede">
        {$t('ui_we_sent_a_verification_link_to_value0_it_rema_0e84b4f9', { value0: String(email) })}
      </p>
    {:else}
      <p class="eyebrow">{$t('ui_account_ready_ff66ee6c')}</p>
      <h1 class="auth-title">{$t('ui_welcome_to_kaede_7ed4553a')}</h1>
      <p class="lede">{$t('ui_your_account_was_created_you_can_sign_in_with_c8ac6468')}</p>
      <p class="form-foot">
        <a href={resolve('/login')}>{$t('ui_continue_to_sign_in_d398cd0a')}</a>
      </p>
    {/if}
  </div>
{:else if nativeDesktop && registrationStep === 1}
  <p class="eyebrow">{$t('ui_create_account_798ca2ce')}</p>
  <h1 class="auth-title">{$t('ui_choose_your_server_1830677f')}</h1>
  <p class="auth-intro">{$t('ui_your_server_stores_your_account_and_connects__a05ec64f')}</p>
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
      {busy ? $t('ui_checking_server_7cb17add') : $t('ui_continue_31fbef16')}
    </button>
  </form>
{:else if emailRequired === null}
  <p class="eyebrow">{$t('ui_create_account_798ca2ce')}</p>
  <h1 class="auth-title">{$t('ui_checking_registration_options_42d5f667')}</h1>
{:else}
  <p class="eyebrow">{$t('ui_your_place_your_name_6a69c87e')}</p>
  <h1 class="auth-title">{$t('ui_create_your_account_9bedde90')}</h1>
  {#if nativeDesktop}
    <div class="registration-server-summary">
      <span>{$t('ui_account_server_670c81da')}</span>
      <strong>{selectedInstance}</strong>
      <button
        type="button"
        class="quiet-button"
        disabled={busy}
        onclick={() => {
          error = '';
          registrationStep = 1;
        }}>{$t('ui_change_c0bf75bd')}</button
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
        >{$t('ui_username_e3b89e9d')}
        <input
          bind:value={username}
          pattern={'[a-z0-9_.]{2,32}'}
          maxlength="32"
          autocomplete="username"
          required
          disabled={busy}
        />
        <small>{$t('ui_lowercase_letters_numbers_dots_and_underscore_239d8c4b')}</small></label
      >
      {#if emailRequired}
        <label
          >{$t('ui_email_969ccbd3')}
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
        >{$t('ui_password_e7cf3ef4')}
        <input
          bind:value={password}
          type="password"
          minlength="10"
          maxlength="256"
          autocomplete="new-password"
          required
          disabled={busy}
        />
        <small>{$t('ui_at_least_10_characters_a_password_manager_is__ea3a2829')}</small></label
      >
      <label
        >{$t('ui_confirm_password_5ac265f3')}
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
      >{busy ? $t('ui_creating_c79ed949') : $t('ui_create_account_798ca2ce')}</button
    >
  </form>
  <p class="form-foot">
    {$t('ui_already_have_an_account_e77fea93')}
    <a href={resolve('/login')}>{$t('ui_sign_in_bfd402b2')}</a>
  </p>
{/if}
