<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import { entityRef } from '$lib/chat/refs';
  import type { Channel, Message } from '$lib/chat/types';
  import { portal } from '$lib/ui/portal';
  import { onMount, tick } from 'svelte';

  let {
    message,
    channels,
    onForward,
    onClose
  }: {
    message: Message;
    channels: Channel[];
    onForward: (channels: Channel[], note: string) => Promise<void>;
    onClose: () => void;
  } = $props();

  let selectedRefs = $state<string[]>([]);
  let note = $state('');
  let busy = $state(false);
  let error = $state('');
  let plaintextDisclosureAcknowledged = $state(false);
  let form = $state<HTMLFormElement | null>(null);
  const requiresPlaintextDisclosure = $derived(
    Boolean(
      message.e2ee &&
      channels.some(
        (channel) => selectedRefs.includes(entityRef(channel)) && channel.encryption_mode !== 'e2ee'
      )
    )
  );

  onMount(() => {
    if (channels.length === 1) selectedRefs = [entityRef(channels[0])];
    void tick().then(() =>
      form?.querySelector<HTMLInputElement>('input[type="checkbox"]')?.focus()
    );
  });

  function toggle(channel: Channel, checked: boolean) {
    const ref = entityRef(channel);
    if (checked) {
      if (selectedRefs.length >= 5 || selectedRefs.includes(ref)) return;
      selectedRefs = [...selectedRefs, ref];
    } else {
      selectedRefs = selectedRefs.filter((item) => item !== ref);
    }
    if (!requiresPlaintextDisclosure) plaintextDisclosureAcknowledged = false;
    error = '';
  }

  async function submit() {
    const targets = channels.filter((channel) => selectedRefs.includes(entityRef(channel)));
    if (
      !targets.length ||
      busy ||
      (requiresPlaintextDisclosure && !plaintextDisclosureAcknowledged)
    )
      return;
    busy = true;
    error = '';
    try {
      await onForward(targets, note);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not forward this message. Try again.');
    } finally {
      busy = false;
    }
  }
</script>

<div use:portal class="forward-dialog-layer" role="presentation">
  <button class="backdrop" type="button" aria-label="Cancel forward" onclick={onClose}></button>
  <form
    bind:this={form}
    class="forward-dialog"
    aria-label="Forward message"
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    <header>
      <div>
        <small>Message action</small>
        <h2>Forward</h2>
      </div>
    </header>
    <p>
      Forwarding creates an immutable, author-free snapshot. A source link is shown only to people
      who still have access to the original channel.
    </p>
    <fieldset disabled={busy}>
      <legend>Send to · {selectedRefs.length}/5</legend>
      <div class="destinations">
        {#each channels as channel (entityRef(channel))}
          {@const ref = entityRef(channel)}
          {@const selected = selectedRefs.includes(ref)}
          <label class:selected>
            <input
              type="checkbox"
              checked={selected}
              disabled={!selected && selectedRefs.length >= 5}
              onchange={(event) => toggle(channel, event.currentTarget.checked)}
            />
            <span>#{channel.name ?? 'direct message'}</span>
          </label>
        {/each}
      </div>
    </fieldset>
    {#if requiresPlaintextDisclosure}
      <label class="disclosure">
        <input type="checkbox" bind:checked={plaintextDisclosureAcknowledged} disabled={busy} />
        <span
          >I understand that forwarding to an unencrypted destination decrypts this snapshot and its
          files on this device before uploading them there.</span
        >
      </label>
    {/if}
    <label class="note">
      Add a note <span>Optional</span>
      <textarea
        bind:value={note}
        maxlength="4000"
        rows="3"
        placeholder="Say something about this message"
        disabled={busy}
      ></textarea>
    </label>
    <small class="source"
      >Original: {message.content?.slice(0, 120) || 'rich message or attachment'}</small
    >
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    <footer>
      <button type="button" disabled={busy} onclick={onClose}>Cancel</button>
      <button
        class="primary"
        type="submit"
        disabled={busy ||
          selectedRefs.length === 0 ||
          (requiresPlaintextDisclosure && !plaintextDisclosureAcknowledged)}
        >{busy
          ? 'Sending…'
          : `Send${selectedRefs.length > 1 ? ` (${selectedRefs.length})` : ''}`}</button
      >
    </footer>
  </form>
</div>

<style>
  .forward-dialog-layer {
    position: fixed;
    z-index: 4000;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 1rem;
  }
  .backdrop {
    position: absolute;
    inset: 0;
    border: 0;
    background: rgb(0 0 0 / 58%);
  }
  .forward-dialog {
    position: relative;
    width: min(480px, 100%);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1rem;
    color: var(--text);
    background: var(--surface-raised);
  }
  header h2,
  header small {
    margin: 0;
  }
  p,
  .source {
    color: var(--text-muted);
  }
  .note {
    display: grid;
    gap: 0.35rem;
    margin: 1rem 0;
    font-size: 0.78rem;
    font-weight: 700;
  }
  .note span {
    color: var(--text-muted);
    font-weight: 500;
  }
  .disclosure {
    display: flex;
    align-items: flex-start;
    gap: 0.55rem;
    border: 1px solid color-mix(in srgb, var(--warning) 55%, var(--line));
    border-radius: 8px;
    padding: 0.7rem;
    color: var(--text-soft);
    font-size: 0.78rem;
  }
  fieldset {
    min-width: 0;
    margin: 1rem 0;
    border: 0;
    padding: 0;
  }
  legend {
    margin-bottom: 0.4rem;
    font-size: 0.78rem;
    font-weight: 700;
  }
  .destinations {
    display: grid;
    max-height: 240px;
    overflow: auto;
    gap: 4px;
  }
  .destinations label {
    display: flex;
    align-items: center;
    gap: 9px;
    border-radius: 7px;
    padding: 8px 9px;
    background: var(--surface-subtle);
    cursor: pointer;
  }
  .destinations label.selected {
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-subtle));
  }
  .destinations input {
    width: 17px;
    height: 17px;
    margin: 0;
    accent-color: var(--accent);
  }
  textarea,
  button {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.6rem;
    color: var(--text);
    background: var(--surface-subtle);
  }
  textarea {
    width: 100%;
    resize: vertical;
    font: inherit;
  }
  footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
    margin-top: 1rem;
  }
  .primary {
    border-color: var(--accent);
    background: var(--accent);
    color: white;
  }
  .error {
    color: var(--danger);
  }
</style>
