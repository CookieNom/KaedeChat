<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import type { Message } from '$lib/chat/types';
  import { entityRef } from '$lib/chat/refs';
  import { portal } from '$lib/ui/portal';

  let {
    message,
    onClose,
    onSubmitted
  }: {
    message: Message;
    onClose: () => void;
    onSubmitted?: () => void;
  } = $props();

  const categories = [
    ['spam', 'Spam'],
    ['harassment', 'Harassment'],
    ['hate', 'Hate'],
    ['sexual_content', 'Sexual content'],
    ['violence', 'Violence'],
    ['self_harm', 'Self-harm'],
    ['impersonation', 'Impersonation'],
    ['privacy', 'Privacy'],
    ['malware', 'Malware'],
    ['illegal_content', 'Illegal content'],
    ['other', 'Other']
  ] as const;

  let category = $state<(typeof categories)[number][0]>('spam');
  let description = $state('');
  let disclosureAcknowledged = $state(false);
  let busy = $state(false);
  let error = $state('');
  const encrypted = $derived(Boolean(message.e2ee));
  const disclosedContent = $derived(message.decrypted_content ?? '');
  const hasDisclosedContent = $derived(Boolean(disclosedContent.trim()));

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    if (encrypted && (!hasDisclosedContent || !disclosureAcknowledged)) return;
    busy = true;
    error = '';
    try {
      const ref = entityRef(message);
      await api('/reports', {
        method: 'POST',
        body: JSON.stringify({
          target_type: 'message',
          target_ref: ref,
          message_ref: ref,
          category,
          description: description.trim() || null,
          ...(encrypted
            ? {
                disclosed_content: disclosedContent,
                disclosure_acknowledged: true
              }
            : {})
        })
      });
      onSubmitted?.();
      onClose();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not submit this report. Try again.');
    } finally {
      busy = false;
    }
  }

  function backdrop(event: MouseEvent) {
    if (event.currentTarget === event.target && !busy) onClose();
  }
</script>

<div use:portal class="report-backdrop" role="presentation" onclick={backdrop}>
  <div class="report-dialog" role="dialog" aria-modal="true" aria-labelledby="report-title">
    <header>
      <div>
        <span>Trust &amp; Safety</span>
        <h2 id="report-title">Report message</h2>
      </div>
      <button type="button" class="close" aria-label="Close report" onclick={onClose}>×</button>
    </header>
    {#if encrypted}
      <div class="encrypted-disclosure">
        <strong>Share decrypted message?</strong>
        <p>
          This message is end-to-end encrypted. Reporting it sends only the decrypted text shown on
          this device and basic message context to this instance's Trust &amp; Safety team. It does
          not send your encryption keys or other messages. The server will label the text as
          reporter-supplied evidence.
        </p>
      </div>
    {:else}
      <p>
        The message text and basic context will be sent to this instance's Trust &amp; Safety team.
        Guild moderators do not receive this report.
      </p>
    {/if}
    <form onsubmit={submit}>
      <label>
        Reason
        <select bind:value={category}>
          {#each categories as [value, label] (value)}
            <option {value}>{label}</option>
          {/each}
        </select>
      </label>
      <label>
        Additional details <span>(optional)</span>
        <textarea bind:value={description} maxlength="2000" rows="4"></textarea>
      </label>
      {#if encrypted}
        <label class="disclosure-consent">
          <input type="checkbox" bind:checked={disclosureAcknowledged} />
          <span>I understand the decrypted text will be shared with Trust &amp; Safety.</span>
        </label>
      {/if}
      {#if error}<div class="report-error" role="alert">{error}</div>{/if}
      <footer>
        <button type="button" class="secondary" disabled={busy} onclick={onClose}>Cancel</button>
        <button
          type="submit"
          class="danger"
          disabled={busy || (encrypted && (!hasDisclosedContent || !disclosureAcknowledged))}
          >{busy ? 'Submitting…' : 'Submit report'}</button
        >
      </footer>
    </form>
  </div>
</div>

<style>
  .report-backdrop {
    position: fixed;
    inset: 0;
    z-index: 1500;
    display: grid;
    place-items: center;
    padding: 1rem;
    background: rgb(0 0 0 / 0.6);
  }
  .report-dialog {
    box-sizing: border-box;
    width: min(480px, 100%);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1.2rem;
    color: var(--text);
    background: var(--surface);
    box-shadow: 0 24px 70px rgb(0 0 0 / 0.45);
  }
  .encrypted-disclosure {
    border: 1px solid color-mix(in srgb, var(--danger, #d84a4a) 45%, var(--line));
    border-radius: 10px;
    padding: 0.85rem;
    background: color-mix(in srgb, var(--danger, #d84a4a) 8%, var(--surface));
  }
  .encrypted-disclosure p {
    margin: 0.4rem 0 0;
  }
  header,
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  header span,
  label span,
  p {
    color: var(--text-muted);
  }
  header span {
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  h2 {
    margin: 0.15rem 0 0;
  }
  .close {
    border: 0;
    padding: 0.2rem 0.5rem;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.8rem;
  }
  form,
  label {
    display: grid;
    gap: 0.55rem;
  }
  form {
    gap: 1rem;
  }
  label {
    font-weight: 750;
  }
  .disclosure-consent {
    display: flex;
    align-items: flex-start;
    grid-template-columns: none;
    gap: 0.65rem;
    font-weight: 650;
  }
  .disclosure-consent input {
    width: 1rem;
    height: 1rem;
    margin-top: 0.15rem;
  }
  select,
  textarea {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.7rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
  }
  textarea {
    resize: vertical;
  }
  footer {
    justify-content: flex-end;
    margin-top: 0.25rem;
  }
  footer button {
    border: 0;
    border-radius: 8px;
    padding: 0.7rem 0.9rem;
    font: inherit;
    font-weight: 800;
  }
  .secondary {
    color: var(--text);
    background: var(--surface-hover);
  }
  .danger {
    color: white;
    background: var(--danger, #d84a4a);
  }
  .report-error {
    color: var(--danger, #ef6767);
  }
</style>
