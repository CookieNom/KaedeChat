<script lang="ts">
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
        throw new Error('The password-reset response was invalid.');
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
      error = userErrorMessage(caught, 'Could not reset your password. Try again.');
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
    <p class="auth-warning">
      {localStateRebased
        ? 'This browser kept its trusted encrypted history and will seal it under the new account-vault key after sign-in.'
        : 'Restore an encrypted recovery backup after sign-in if you need history that is not already available on another trusted client.'}
    </p>
  </div>
{:else}<form
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    <p class="auth-warning">
      Resetting your password replaces the key that unlocks your end-to-end encrypted account vault.
      Your encrypted history will remain available only if this browser still has its local
      encryption state or you restore a recovery backup. Kaede cannot recover it from the server.
    </p>
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
