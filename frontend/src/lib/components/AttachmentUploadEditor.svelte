<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { onMount } from 'svelte';
  import type { PendingUpload } from '$lib/media/uploads';
  import { isAttachmentSpoiler } from '$lib/media/spoilers';
  import FilePreview from './FilePreview.svelte';
  let {
    upload,
    onSpoiler,
    onClose,
    disabled = false
  }: {
    upload: PendingUpload;
    onSpoiler?: (key: string, spoiler: boolean) => void;
    onClose: () => void;
    disabled?: boolean;
  } = $props();
  let dialog: HTMLDialogElement;
  onMount(() => dialog.showModal());
</script>

<dialog bind:this={dialog} onclose={onClose} aria-labelledby="attachment-editor-title">
  <h2 id="attachment-editor-title">{$t('ui_attachment_040d2b36')}</h2>
  <div class="preview"><FilePreview file={upload.file} /></div>
  <p>{upload.file.name}</p>
  {#if onSpoiler}
    <label
      ><input
        type="checkbox"
        checked={isAttachmentSpoiler(upload.file.name)}
        disabled={disabled || upload.updating}
        onchange={(event) => onSpoiler?.(upload.key, event.currentTarget.checked)}
      />
      {$t('ui_mark_as_spoiler_a954e083')}</label
    >
  {/if}
  {#if upload.error}<p role="alert">{upload.error}</p>{/if}
  <button type="button" onclick={() => dialog.close()}>{$t('ui_done_11a6767d')}</button>
</dialog>

<style>
  dialog {
    width: min(480px, 90vw);
    border: 1px solid var(--border);
    border-radius: 16px;
    background: var(--surface);
    color: var(--text);
    padding: 24px;
  }
  dialog::backdrop {
    background: #0009;
  }
  .preview {
    height: 280px;
    overflow: hidden;
    border-radius: 8px;
  }
  p {
    overflow-wrap: anywhere;
  }
  label {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
  }
</style>
