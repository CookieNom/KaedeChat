<script lang="ts">
  import type { Message } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';

  let {
    message,
    busy = false,
    error = '',
    onCreate,
    onClose
  }: {
    message: Message;
    busy?: boolean;
    error?: string;
    onCreate: (name: string) => Promise<void> | void;
    onClose: () => void;
  } = $props();

  let name = $state('');
</script>

<div class="thread-dialog-layer" role="presentation">
  <button class="thread-dialog-backdrop" type="button" aria-label="Close" onclick={onClose}
  ></button>
  <div class="thread-dialog" role="dialog" aria-modal="true" aria-labelledby="thread-dialog-title">
    <header>
      <div>
        <span>Start a conversation</span>
        <h2 id="thread-dialog-title">Create Thread</h2>
      </div>
      <button type="button" disabled={busy} aria-label="Close" onclick={onClose}>×</button>
    </header>
    <p class="starter">
      <strong>{userDisplayName(message.author)}</strong>
      <span
        >{message.e2ee
          ? message.e2ee_verified === true
            ? (message.decrypted_content ?? 'Message')
            : 'Encrypted message'
          : (message.content ?? 'Message')}</span
      >
    </p>
    <form
      onsubmit={(event) => {
        event.preventDefault();
        if (name.trim()) void onCreate(name.trim());
      }}
    >
      <label>
        Thread Name
        <input bind:value={name} maxlength="100" required disabled={busy} />
      </label>
      {#if error}<p role="alert">{error}</p>{/if}
      <footer>
        <button type="button" disabled={busy} onclick={onClose}>Cancel</button>
        <button class="primary" disabled={busy || !name.trim()}>
          {busy ? 'Creating…' : 'Create Thread'}
        </button>
      </footer>
    </form>
  </div>
</div>

<style>
  .thread-dialog-layer {
    position: fixed;
    z-index: 1200;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 1rem;
  }

  .thread-dialog-backdrop {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: rgb(0 0 0 / 72%);
  }

  .thread-dialog {
    position: relative;
    width: min(440px, 100%);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1rem;
    background: var(--surface);
    box-shadow: var(--shadow-lg);
  }

  header,
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  header span {
    color: var(--text-muted);
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  h2 {
    margin: 0.1rem 0 0;
  }

  header > button {
    border: 0;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.4rem;
  }

  .starter {
    display: grid;
    gap: 0.2rem;
    margin: 1rem 0;
    border-left: 3px solid var(--line);
    padding-left: 0.75rem;
    color: var(--text-muted);
    font-size: 0.8rem;
  }

  .starter span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  form,
  label {
    display: grid;
    gap: 0.55rem;
  }

  label {
    color: var(--text-soft);
    font-size: 0.75rem;
    font-weight: 750;
  }

  input {
    min-height: 42px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0 0.75rem;
    color: var(--text);
    background: var(--surface-subtle);
  }

  form > p {
    margin: 0;
    color: var(--danger);
    font-size: 0.75rem;
  }

  footer {
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  footer button {
    min-height: 38px;
    border: 0;
    border-radius: 7px;
    padding: 0 0.9rem;
    color: var(--text-soft);
    background: transparent;
    font-weight: 750;
  }

  footer .primary {
    color: var(--on-accent);
    background: var(--accent);
  }
</style>
