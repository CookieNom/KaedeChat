<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import { onMount, tick } from 'svelte';

  let email = $state('');
  let sent = $state(false);
  let busy = $state(false);
  let error = $state('');
  let recoveryEnabled = $state<boolean | null>(null);
  let successPanel = $state<HTMLElement | null>(null);

  onMount(() => {
    const controller = new AbortController();
    void loadAuthConfiguration(controller.signal)
      .then((configuration) => {
        recoveryEnabled = configuration.password_recovery_enabled;
      })
      .catch(() => {
        recoveryEnabled = true;
      });
    return () => controller.abort();
  });

  async function submit() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await api('/auth/password/forgot', { method: 'POST', body: JSON.stringify({ email }) });
      sent = true;
      await tick();
      successPanel?.focus();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not request a reset link. Try again.');
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Reset password · Kaede Chat</title></svelte:head>
<p class="eyebrow">Account recovery</p>
<h1 class="auth-title">Find your way back.</h1>
{#if recoveryEnabled === false}
  <p class="lede">
    This instance does not use email, so self-service password recovery is unavailable. Contact the
    instance operator if you lose access.
  </p>
{:else if recoveryEnabled === null}
  <p class="lede">Checking recovery options…</p>
{:else if sent}<div
    bind:this={successPanel}
    class="auth-success"
    role="status"
    aria-live="polite"
    aria-atomic="true"
    tabindex="-1"
  >
    <p class="lede">If that address has an account, a reset link is on its way.</p>
    <p class="form-foot"><a href={resolve('/login')}>Return to sign in</a></p>
  </div>
{:else}<form
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
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
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    <button class="primary-button" disabled={busy}>{busy ? 'Sending…' : 'Send reset link'}</button>
  </form>
  <p class="form-foot"><a href={resolve('/login')}>Back to sign in</a></p>{/if}
