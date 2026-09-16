<script lang="ts">
  import { onMount } from 'svelte';
  import { api, userErrorMessage } from '$lib/api/client';
  import { loadPasswordKdfContext, preparePassword } from '$lib/auth/password-kdf';

  let {
    handle,
    mfaEnabled,
    onDeleted
  }: {
    handle: string;
    mfaEnabled: boolean;
    onDeleted: () => Promise<void>;
  } = $props();
  let action = $state<'content' | 'account' | null>(null);
  let password = $state('');
  let code = $state('');
  let confirmation = $state('');
  let busy = $state(false);
  let pending = $state(false);
  let notice = $state('');
  let error = $state('');

  onMount(() => {
    const controller = new AbortController();
    async function refresh() {
      try {
        const result = await api<{ status: string; remote_failures?: number }>(
          '/users/@me/content-deletion',
          { signal: controller.signal }
        );
        pending = result.status === 'pending';
        if (pending)
          notice = 'Deleting your content. You can close the app; cleanup will continue.';
        else if (result.status === 'complete')
          notice = result.remote_failures
            ? 'Local content deleted. Some remote servers could not confirm deletion.'
            : 'Content deletion complete. Remote servers may retain copies.';
      } catch (caught) {
        if (!controller.signal.aborted)
          error = userErrorMessage(caught, 'Could not check deletion progress.');
      }
    }
    void refresh();
    const interval = window.setInterval(() => {
      if (pending) void refresh();
    }, 5000);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  });

  function choose(value: 'content' | 'account' | null) {
    action = value;
    password = '';
    code = '';
    confirmation = '';
    error = '';
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!action || busy || pending || confirmation !== 'DELETE') return;
    busy = true;
    error = '';
    try {
      const prepared = await preparePassword(password, await loadPasswordKdfContext(handle));
      await api(action === 'account' ? '/users/@me' : '/users/@me/content', {
        method: 'DELETE',
        body: JSON.stringify({
          password: prepared.authenticationSecret,
          password_kdf_version: prepared.context.version,
          current_code: mfaEnabled ? code.trim() : null
        })
      });
      const accountDeleted = action === 'account';
      choose(null);
      pending = true;
      notice = 'Deleting your content. You can close the app; cleanup will continue.';
      if (accountDeleted) await onDeleted();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not start deletion. Please try again.');
    } finally {
      busy = false;
    }
  }
</script>

<section class="deletion" aria-labelledby="delete-account-heading">
  <h3 id="delete-account-heading">Delete content or account</h3>
  <p>
    These actions are permanent. Cleanup continues even if you close the app. Deletion on other
    servers is best effort.
  </p>
  <div class="actions">
    <button
      type="button"
      class="danger"
      disabled={busy || pending}
      onclick={() => choose('content')}>Delete all content</button
    >
    <button
      type="button"
      class="danger"
      disabled={busy || pending}
      onclick={() => choose('account')}>Delete account</button
    >
  </div>
  {#if action}
    <form onsubmit={submit}>
      <h4>
        {action === 'account'
          ? 'Permanently delete your account?'
          : 'Permanently delete all your content?'}
      </h4>
      <p>
        {action === 'account'
          ? 'You will be signed out on every device. Your content, profile, and private account data will be removed. Your email and username stay reserved so nobody can recreate your account. Transfer or delete any guilds you own first.'
          : 'Your messages, uploaded files, and profile content will be removed. Your account stays active. Content posted after this request is not included.'}
      </p>
      <label
        >Current password<input
          type="password"
          autocomplete="current-password"
          bind:value={password}
          required
          disabled={busy}
        /></label
      >
      {#if mfaEnabled}<label
          >Authenticator or recovery code<input
            autocomplete="one-time-code"
            bind:value={code}
            required
            disabled={busy}
          /></label
        >{/if}
      <label
        >Type DELETE to confirm<input
          bind:value={confirmation}
          autocomplete="off"
          spellcheck="false"
          required
          disabled={busy}
        /></label
      >
      <div class="actions">
        <button
          class="danger"
          disabled={busy || confirmation !== 'DELETE' || !password || (mfaEnabled && !code.trim())}
          >{busy
            ? 'Starting deletion…'
            : action === 'account'
              ? 'Permanently delete account'
              : 'Permanently delete content'}</button
        >
        <button type="button" disabled={busy} onclick={() => choose(null)}>Cancel</button>
      </div>
    </form>
  {/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if error}<p role="alert">{error}</p>{/if}
</section>

<style>
  .deletion {
    padding: 1.25rem;
    border: 1px solid var(--line);
    border-radius: 12px;
  }
  h3,
  h4 {
    margin: 0 0 0.75rem;
  }
  p {
    margin: 0 0 1rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  button {
    padding: 0.7rem 1rem;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface);
    color: inherit;
    font: inherit;
    cursor: pointer;
  }
  button.danger {
    border-color: var(--danger);
    color: var(--danger);
  }
  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  form {
    display: grid;
    gap: 1rem;
    margin-top: 1.5rem;
  }
  label {
    display: grid;
    gap: 0.4rem;
  }
  input {
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
  }
  [role='alert'] {
    color: var(--danger);
  }
  @media (max-width: 480px) {
    .actions > button {
      width: 100%;
    }
  }
</style>
