<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { firstNavigableChannel } from '$lib/chat/channels';
  import type { Guild } from '$lib/chat/types';
  import { guildChannelPath } from '$lib/navigation/routes';
  import Icon from './Icon.svelte';
  import { tick } from 'svelte';

  let { open = $bindable(false) }: { open?: boolean } = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let nameInput = $state<HTMLInputElement | null>(null);
  let name = $state('');
  let submitting = $state(false);
  let error = $state('');
  let submissionGeneration = 0;

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      void tick().then(() => nameInput?.focus());
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  function close() {
    if (submitting) return;
    open = false;
  }

  function reset() {
    submissionGeneration += 1;
    open = false;
    name = '';
    submitting = false;
    error = '';
  }

  async function createGuild() {
    const guildName = name.trim();
    if (submitting || guildName.length < 2) return;

    const generation = ++submissionGeneration;
    submitting = true;
    error = '';
    try {
      const guild = await api<Guild>('/guilds', {
        method: 'POST',
        body: JSON.stringify({ name: guildName })
      });
      if (generation !== submissionGeneration || !open) return;
      const channel = firstNavigableChannel(guild.channels);
      window.location.assign(channel ? guildChannelPath(guild, channel) : resolve('/home'));
    } catch (caught) {
      if (generation !== submissionGeneration || !open) return;
      error = userErrorMessage(caught, 'Could not create the guild. Check its name and try again.');
    } finally {
      if (generation === submissionGeneration) submitting = false;
    }
  }
</script>

<dialog
  bind:this={dialog}
  class="create-guild-dialog"
  aria-labelledby="create-guild-title"
  onclose={reset}
  oncancel={(event) => {
    event.preventDefault();
    close();
  }}
  onclick={(event) => {
    if (event.target === dialog) close();
  }}
>
  <form
    onsubmit={(event) => {
      event.preventDefault();
      void createGuild();
    }}
  >
    <header>
      <span class="dialog-icon" aria-hidden="true"><Icon name="plus" size={24} /></span>
      <div>
        <p>New community</p>
        <h2 id="create-guild-title">Create a guild</h2>
      </div>
      <button
        class="close-button"
        type="button"
        aria-label="Close"
        disabled={submitting}
        onclick={close}>×</button
      >
    </header>

    <div class="dialog-body">
      <p>Give your community a name. You can customize its icon, channels, and roles next.</p>
      <label>
        <span>Guild name</span>
        <input
          bind:this={nameInput}
          bind:value={name}
          minlength="2"
          maxlength="100"
          autocomplete="off"
          placeholder="My community"
          required
        />
      </label>
      {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    </div>

    <footer>
      <button class="secondary-button" type="button" disabled={submitting} onclick={close}
        >Cancel</button
      >
      <button class="primary-button" disabled={submitting || name.trim().length < 2}>
        {submitting ? 'Creating…' : 'Create guild'}
      </button>
    </footer>
  </form>
</dialog>

<style>
  .create-guild-dialog {
    width: min(480px, calc(100vw - 32px));
    padding: 0;
    overflow: hidden;
    color: var(--text);
    border: 1px solid var(--line-strong);
    border-radius: 22px;
    background: var(--surface-raised);
    box-shadow: 0 24px 80px rgb(0 0 0 / 55%);
  }

  .create-guild-dialog::backdrop {
    background: rgb(0 0 0 / 68%);
    backdrop-filter: blur(2px);
  }

  form {
    display: flex;
    flex-direction: column;
  }

  header {
    display: grid;
    grid-template-columns: 48px minmax(0, 1fr) 32px;
    align-items: center;
    gap: 14px;
    padding: 25px 26px 20px;
    border-bottom: 1px solid var(--line-soft);
  }

  .dialog-icon {
    display: grid;
    width: 48px;
    height: 48px;
    place-items: center;
    color: var(--on-accent);
    border-radius: 16px;
    background: var(--accent);
  }

  header p,
  header h2,
  .dialog-body p,
  .form-error {
    margin: 0;
  }

  header p {
    color: var(--accent-text);
    font-size: 0.7rem;
    font-weight: 800;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  header h2 {
    margin-top: 3px;
    font-size: 1.5rem;
  }

  .close-button {
    padding: 0;
    color: var(--text-muted);
    border: 0;
    background: transparent;
    font-size: 2rem;
    line-height: 1;
    cursor: pointer;
  }

  .dialog-body {
    display: grid;
    gap: 18px;
    padding: 24px 26px 26px;
  }

  .dialog-body > p:not(.form-error) {
    color: var(--text-muted);
    line-height: 1.5;
  }

  label {
    display: grid;
    gap: 8px;
    font-weight: 760;
  }

  label span {
    font-size: 0.82rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  input {
    width: 100%;
  }

  footer {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    padding: 18px 26px;
    border-top: 1px solid var(--line-soft);
    background: var(--surface-subtle);
  }

  @media (max-width: 520px) {
    header,
    .dialog-body,
    footer {
      padding-right: 20px;
      padding-left: 20px;
    }
  }
</style>
