<script lang="ts">
  import { onMount } from 'svelte';
  import { resolve } from '$app/paths';
  import { initializeNativeInstance, isNativeDesktop, nativeInvoke } from '$lib/platform/native';
  import { userErrorMessage } from '$lib/api/client';

  type SavedAccount = { account_key: string; instance: string; label: string };
  let accounts = $state<SavedAccount[]>([]);
  let active = $state<string | null>(null);
  let busy = $state(true);
  let error = $state('');

  async function load() {
    const result = await nativeInvoke<{ accounts: SavedAccount[]; active: string | null }>(
      'native_saved_accounts'
    );
    accounts = result.accounts;
    active = result.active;
  }

  onMount(() => {
    if (!isNativeDesktop()) {
      busy = false;
      return;
    }
    void initializeNativeInstance()
      .then(load)
      .catch((caught) => {
        error = userErrorMessage(caught, 'Could not load saved accounts.');
      })
      .finally(() => {
        busy = false;
      });
  });

  async function choose(account: SavedAccount) {
    busy = true;
    error = '';
    try {
      const instance = await nativeInvoke<string>('native_switch_account', {
        accountKey: account.account_key
      });
      localStorage.setItem('kaede.native.instance', instance);
      sessionStorage.removeItem('kaede.gateway.session');
      sessionStorage.removeItem('kaede.gateway.sequence');
      sessionStorage.removeItem('kaede.return-to');
      localStorage.removeItem('kaede.native.last-route');
      window.location.replace(resolve('/home'));
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not switch accounts. Try again or sign in again.');
      busy = false;
    }
  }

  async function forget(account: SavedAccount) {
    busy = true;
    error = '';
    try {
      await nativeInvoke('native_forget_account', { accountKey: account.account_key });
      await load();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not remove this saved account.');
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Switch accounts · Kaede Chat</title></svelte:head>
<h1 class="auth-title">Switch accounts</h1>
{#if isNativeDesktop()}
  <p class="auth-intro">
    Choose a saved sign-in or add another account. Switching disconnects your current call.
  </p>
  <div class="accounts" aria-busy={busy}>
    {#each accounts as account (account.account_key)}
      <div class="account">
        <button class="secondary-button" disabled={busy} onclick={() => choose(account)}>
          <strong>{account.label}</strong><small
            >{account.instance}{active === account.account_key ? ' · Current account' : ''}</small
          >
        </button>
        {#if active !== account.account_key}
          <button
            class="secondary-button"
            disabled={busy}
            aria-label={`Remove saved sign-in for ${account.label}`}
            onclick={() => forget(account)}>Remove</button
          >
        {/if}
      </div>
    {:else}
      <p>{busy ? 'Loading accounts…' : 'No saved accounts yet.'}</p>
    {/each}
  </div>
  <a class="primary-button" href={resolve('/login?add-account=1')}>Add an account / sign in again</a
  >
  <p class="field-note">
    Remove forgets the sign-in on this device. Revoke sessions in account settings to sign out other
    devices.
  </p>
{:else}
  <p>Saved-account switching is available in the desktop app.</p>
{/if}
{#if error}<p class="form-error" role="alert">{error}</p>{/if}
{#if active && accounts.some((account) => account.account_key === active)}
  <button
    class="secondary-button"
    disabled={busy}
    onclick={() => choose(accounts.find((account) => account.account_key === active)!)}
    >Back to current account</button
  >
{:else}
  <a href={resolve('/home')}>Back to chat</a>
{/if}

<style>
  .accounts {
    display: grid;
    gap: 12px;
    margin-bottom: 20px;
  }
  .account {
    display: flex;
    gap: 8px;
  }
  .account > button:first-child {
    display: grid;
    flex: 1;
    text-align: left;
    overflow-wrap: anywhere;
  }
  small {
    color: var(--text-muted);
  }
</style>
