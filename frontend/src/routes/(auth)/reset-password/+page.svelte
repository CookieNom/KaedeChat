<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { consumeUrlToken } from '$lib/auth/url-token';
  import { prepareResetPassword } from '$lib/auth/password-kdf';
  import { rebaseDeviceStateAfterPasswordReset } from '$lib/e2ee/store';
  import { tick } from 'svelte';
  let password = $state('');
  let confirmPassword = $state('');
  let token = $state<string | null>(null);
  let done = $state(false);
  let error = $state('');
  let busy = $state(false);
  let successPanel = $state<HTMLElement | null>(null);
  let localStateRebased = $state(false);
  interface PasswordResetResult {
    status: 'password_updated';
    account_ref: string;
  }

  function canonicalAccountRef(value: string): boolean {
    const separator = value.lastIndexOf('@');
    if (separator <= 0) return false;
    const id = value.slice(0, separator);
    const domain = value.slice(separator + 1);
    return (
      /^(?:0|[1-9][0-9]{0,18})$/u.test(id) &&
      BigInt(id) <= 9_223_372_036_854_775_807n &&
      /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/u.test(
        domain
      )
    );
  }
  $effect(() => {
    token ??= consumeUrlToken();
  });
  async function submit() {
    if (busy) return;
    if (!token) {
      error = $t('ui_this_reset_link_is_invalid_ddca8f10');
      return;
    }
    if (password !== confirmPassword) {
      error = $t('ui_passwords_do_not_match_6c6e178a');
      return;
    }
    busy = true;
    error = '';
    try {
      const prepared = await prepareResetPassword(password);
      const result = await api<PasswordResetResult>('/auth/password/reset', {
        method: 'POST',
        body: JSON.stringify({
          token,
          password: prepared.authenticationSecret,
          password_kdf: prepared.authKdf
        })
      });
      if (result.status !== 'password_updated' || !canonicalAccountRef(result.account_ref)) {
        throw new Error($t('ui_the_password_reset_response_was_invalid_8a9e6c1b'));
      }
      // Only the authenticated, one-time reset response may lower this
      // browser's rollback checkpoint. A merely missing server vault never can.
      localStateRebased = await rebaseDeviceStateAfterPasswordReset(result.account_ref);
      password = '';
      confirmPassword = '';
      token = null;
      done = true;
      await tick();
      successPanel?.focus();
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_reset_your_password_try_again_059288d9'));
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>{$t('ui_choose_password_kaede_chat_50b171c2')}</title></svelte:head>
<p class="eyebrow">{$t('ui_account_recovery_2b291cd5')}</p>
<h1 class="auth-title">{$t('ui_choose_a_new_key_291be8ac')}</h1>
{#if done}<div
    bind:this={successPanel}
    class="auth-success"
    role="status"
    aria-live="polite"
    aria-atomic="true"
    tabindex="-1"
  >
    <p class="lede">
      {$t('ui_your_password_has_been_updated_cc244a90')}
      <a href={resolve('/login')}>{$t('ui_sign_in_bfd402b2')}</a>.
    </p>
    <p class="auth-warning">
      {localStateRebased
        ? $t('ui_this_browser_kept_its_trusted_encrypted_histo_6817ba78')
        : $t('ui_restore_an_encrypted_recovery_backup_after_si_76da14aa')}
    </p>
  </div>
{:else}<form
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    <p class="auth-warning">{$t('ui_resetting_your_password_replaces_the_key_that_86ed745e')}</p>
    <label
      >{$t('ui_new_password_3dd9df44')}
      <input
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
      >{$t('ui_confirm_new_password_bf000421')}
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
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    <button class="primary-button" disabled={busy}>
      {busy ? $t('ui_updating_dfe40efe') : $t('ui_update_password_fe45b401')}
    </button>
  </form>{/if}
