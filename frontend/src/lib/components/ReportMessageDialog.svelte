<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import type { Attachment, Message } from '$lib/chat/types';
  import { entityRef } from '$lib/chat/refs';
  import { decryptEncryptedAttachment, type EncryptedFileManifest } from '$lib/e2ee/media';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';
  import { encryptedReportDisclosure } from '$lib/reports/message-evidence';
  import { portal } from '$lib/ui/portal';

  let {
    message,
    attachment = null,
    attachmentLabel,
    attachmentManifest,
    onClose,
    onSubmitted
  }: {
    message: Message;
    attachment?: Attachment | null;
    attachmentLabel?: string;
    attachmentManifest?: EncryptedFileManifest;
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
  let progress = $state(0);
  let activity = $state('');
  let createdReportId = $state<string | null>(null);
  let evidenceTicket = $state<UploadTicket | null>(null);
  let decryptedEvidence = $state<Blob | null>(null);
  let evidenceUploaded = $state(false);
  const encrypted = $derived(Boolean(message.e2ee));
  const reportingAttachment = $derived(attachment !== null);
  const requiresMessageDisclosure = $derived(encrypted && !reportingAttachment);
  const requiresAttachmentDisclosure = $derived(
    reportingAttachment && attachment?.encryption_mode === 'e2ee'
  );
  const requiresDisclosure = $derived(requiresMessageDisclosure || requiresAttachmentDisclosure);
  const attachmentDisclosureAvailable = $derived(
    Boolean(
      attachmentManifest &&
      attachment &&
      attachmentManifest.attachment_id === attachment.id &&
      attachmentManifest.attachment_domain === attachment.origin_domain
    )
  );
  const disclosure = $derived(encryptedReportDisclosure(message));

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    if (
      (requiresMessageDisclosure && (!disclosure.available || !disclosureAcknowledged)) ||
      (requiresAttachmentDisclosure && (!attachmentDisclosureAvailable || !disclosureAcknowledged))
    )
      return;
    busy = true;
    error = '';
    try {
      const ref = entityRef(message);
      if (requiresAttachmentDisclosure && !decryptedEvidence) {
        activity = 'Decrypting selected attachment…';
        decryptedEvidence = await decryptEncryptedAttachment(
          attachmentManifest!,
          attachment?.history_media_url
        );
      }
      if (!createdReportId) {
        activity = 'Creating report…';
        const created = await api<{ id: string }>('/reports', {
          method: 'POST',
          body: JSON.stringify({
            target_type: reportingAttachment ? 'attachment' : 'message',
            target_ref: reportingAttachment ? entityRef(attachment!) : ref,
            message_ref: ref,
            category,
            description: description.trim() || null,
            ...(requiresMessageDisclosure
              ? {
                  disclosed_content: disclosure.content,
                  disclosure_acknowledged: true
                }
              : {})
          })
        });
        createdReportId = created.id;
      }
      if (requiresAttachmentDisclosure) {
        if (!evidenceTicket) {
          activity = 'Preparing secure evidence upload…';
          evidenceTicket = await api<UploadTicket>(
            `/reports/${encodeURIComponent(createdReportId)}/attachment-evidence`,
            {
              method: 'POST',
              body: JSON.stringify({
                filename: attachmentManifest!.filename,
                content_type: attachmentManifest!.content_type,
                size: decryptedEvidence!.size,
                disclosure_acknowledged: true
              })
            }
          );
        }
        if (!evidenceUploaded) {
          activity = 'Uploading decrypted evidence…';
          const file = new File([decryptedEvidence!], attachmentManifest!.filename, {
            type: attachmentManifest!.content_type
          });
          await uploadObject(evidenceTicket, file, (next) => (progress = next));
          evidenceUploaded = true;
        }
        activity = 'Finalizing evidence…';
        await api(`/reports/${encodeURIComponent(createdReportId)}/attachment-evidence`, {
          method: 'PUT',
          body: JSON.stringify({
            attachment_id: evidenceTicket.id,
            disclosure_acknowledged: true
          })
        });
      }
      onSubmitted?.();
      onClose();
    } catch (caught) {
      error = userErrorMessage(
        caught,
        createdReportId
          ? 'The report was submitted, but its decrypted evidence could not be attached. Keep this dialog open and try again.'
          : 'Could not submit this report. Try again.'
      );
    } finally {
      busy = false;
      activity = '';
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
        <h2 id="report-title">Report {reportingAttachment ? 'attachment' : 'message'}</h2>
      </div>
      <button type="button" class="close" aria-label="Close report" onclick={onClose}>×</button>
    </header>
    {#if reportingAttachment}
      <div
        class:encrypted-disclosure={attachment?.encryption_mode === 'e2ee'}
        class="attachment-report-summary"
      >
        <strong>{attachmentLabel ?? attachment?.filename ?? 'Attachment'}</strong>
        <small>{attachment?.content_type ?? 'Unknown file type'}</small>
        {#if attachment?.encryption_mode === 'e2ee'}
          {#if attachmentDisclosureAvailable}
            <strong>Share this decrypted attachment?</strong>
            <p>
              This attachment is end-to-end encrypted. Reporting it decrypts the selected file on
              this device, then uploads an unencrypted evidence copy and its filename to this
              instance's Trust &amp; Safety team. The copy is scanned and stored with the report. It
              does not share the encryption key, other attachments, or other messages.
            </p>
          {:else}
            <strong>Attachment not decrypted on this device</strong>
            <p>
              Kaede cannot disclose this attachment until its authenticated file manifest is
              available here. Close this dialog, wait for the attachment to decrypt, and try again.
            </p>
          {/if}
        {:else}
          <p>
            This specific attachment, its stored metadata, and basic message context will be sent to
            this instance's Trust &amp; Safety team. Guild moderators do not receive this report.
          </p>
        {/if}
      </div>
    {:else if encrypted}
      <div class="encrypted-disclosure">
        {#if disclosure.available}
          <strong>Share decrypted message evidence?</strong>
          <p>
            This message is end-to-end encrypted. Reporting it sends the decrypted text shown on
            this device and basic message context to this instance's Trust &amp; Safety team. For an
            attachment-only message, the disclosed text is empty but the message can still be
            reported. It does not send your encryption keys, decrypted file contents, or other
            messages. The server labels this as reporter-supplied evidence.
          </p>
        {:else}
          <strong>Message not decrypted on this device</strong>
          <p>
            Kaede cannot submit this encrypted message until its authenticated message evidence has
            decrypted here. Close this dialog, wait for the message to decrypt, and try again.
          </p>
        {/if}
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
        <select bind:value={category} disabled={busy || Boolean(createdReportId)}>
          {#each categories as [value, label] (value)}
            <option {value}>{label}</option>
          {/each}
        </select>
      </label>
      <label>
        Additional details <span>(optional)</span>
        <textarea
          bind:value={description}
          maxlength="2000"
          rows="4"
          disabled={busy || Boolean(createdReportId)}
        ></textarea>
      </label>
      {#if requiresDisclosure}
        <label class="disclosure-consent">
          <input
            type="checkbox"
            bind:checked={disclosureAcknowledged}
            disabled={busy ||
              (requiresMessageDisclosure && !disclosure.available) ||
              (requiresAttachmentDisclosure && !attachmentDisclosureAvailable)}
          />
          <span>
            {requiresAttachmentDisclosure
              ? 'I understand this attachment will be decrypted and an unencrypted copy will be shared with Trust & Safety.'
              : 'I understand the decrypted message evidence will be shared with Trust & Safety.'}
          </span>
        </label>
      {/if}
      {#if busy && activity}
        <div class="report-progress" role="status">
          <span>{activity}</span>
          {#if activity.startsWith('Uploading') && progress > 0}<progress max="100" value={progress}
            ></progress>{/if}
        </div>
      {/if}
      {#if error}<div class="report-error" role="alert">{error}</div>{/if}
      <footer>
        <button type="button" class="secondary" disabled={busy} onclick={onClose}>Cancel</button>
        <button
          type="submit"
          class="danger"
          disabled={busy ||
            (requiresMessageDisclosure && (!disclosure.available || !disclosureAcknowledged)) ||
            (requiresAttachmentDisclosure &&
              (!attachmentDisclosureAvailable || !disclosureAcknowledged))}
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
  .attachment-report-summary {
    display: grid;
    gap: 0.25rem;
    margin: 1rem 0;
    border-radius: 10px;
    padding: 0.85rem;
    background: var(--surface-hover);
  }
  .attachment-report-summary small {
    color: var(--text-muted);
  }
  .attachment-report-summary p {
    margin: 0.45rem 0 0;
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
  .report-progress {
    display: grid;
    gap: 0.4rem;
    color: var(--text-muted);
    font-size: 0.85rem;
  }
  .report-progress progress {
    width: 100%;
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
