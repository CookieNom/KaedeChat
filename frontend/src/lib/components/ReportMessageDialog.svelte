<script lang="ts">
  import { t } from '$lib/ui/locale';

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
  const focusedAttachment = $derived(attachment !== null);
  const requiresMessageDisclosure = $derived(encrypted);
  const requiresAttachmentDisclosure = $derived(
    focusedAttachment && attachment?.encryption_mode === 'e2ee'
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
            target_type: 'message',
            target_ref: ref,
            message_ref: ref,
            ...(focusedAttachment ? { focused_attachment_ref: entityRef(attachment!) } : {}),
            category,
            description: description.trim() || null,
            ...(encrypted
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
          ? $t('ui_the_report_was_submitted_but_its_decrypted_ev_b1a96ba1')
          : $t('ui_could_not_submit_this_report_try_again_1364fabc')
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
        <span>{$t('ui_trust_safety_5b9c374d')}</span>
        <h2 id="report-title">{$t('ui_report_message_0a1e3c52')}</h2>
      </div>
      <button
        type="button"
        class="close"
        aria-label={$t('ui_close_report_a7d7787b')}
        onclick={onClose}>×</button
      >
    </header>
    {#if focusedAttachment}
      <div
        class:encrypted-disclosure={attachment?.encryption_mode === 'e2ee'}
        class="attachment-report-summary"
      >
        <strong>{attachmentLabel ?? attachment?.filename ?? $t('ui_attachment_040d2b36')}</strong>
        <small>{attachment?.content_type ?? $t('ui_unknown_file_type_6a4ec9bd')}</small>
        {#if attachment?.encryption_mode === 'e2ee'}
          {#if attachmentDisclosureAvailable}
            <strong>{$t('ui_include_this_decrypted_attachment_with_the_me_2cc67680')}</strong>
            <p>{$t('ui_this_report_covers_the_entire_message_it_send_cb7052ab')}</p>
          {:else}
            <strong>{$t('ui_attachment_not_decrypted_on_this_device_6e608aef')}</strong>
            <p>{$t('ui_kaede_cannot_disclose_this_attachment_until_i_fb39fcc7')}</p>
          {/if}
        {:else}
          <p>{$t('ui_the_entire_message_its_text_and_metadata_for__45463903')}</p>
        {/if}
      </div>
    {:else if encrypted}
      <div class="encrypted-disclosure">
        {#if disclosure.available}
          <strong>{$t('ui_share_decrypted_message_evidence_3d4964b6')}</strong>
          <p>{$t('ui_this_message_is_end_to_end_encrypted_reportin_8687e45c')}</p>
        {:else}
          <strong>{$t('ui_message_not_decrypted_on_this_device_eaa691ea')}</strong>
          <p>{$t('ui_kaede_cannot_submit_this_encrypted_message_un_258b34b2')}</p>
        {/if}
      </div>
    {:else}
      <p>{$t('ui_the_message_text_basic_context_and_metadata_f_7c11101a')}</p>
    {/if}
    <form onsubmit={submit}>
      <label>
        {$t('ui_reason_f81ab834')}
        <select bind:value={category} disabled={busy || Boolean(createdReportId)}>
          {#each categories as [value, label] (value)}
            <option {value}>{label}</option>
          {/each}
        </select>
      </label>
      <label>
        {$t('ui_additional_details_00fcfc37')} <span>{$t('ui_optional_0059798b')}</span>
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
              ? $t('ui_i_understand_the_message_text_and_this_attach_90c49a8d')
              : $t('ui_i_understand_the_decrypted_message_evidence_w_ac997686')}
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
        <button type="button" class="secondary" disabled={busy} onclick={onClose}
          >{$t('ui_cancel_19766ed6')}</button
        >
        <button
          type="submit"
          class="danger"
          disabled={busy ||
            (requiresMessageDisclosure && (!disclosure.available || !disclosureAcknowledged)) ||
            (requiresAttachmentDisclosure &&
              (!attachmentDisclosureAvailable || !disclosureAcknowledged))}
          >{busy ? $t('ui_submitting_49195f55') : $t('ui_submit_report_b41fd589')}</button
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
