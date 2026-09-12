<script lang="ts">
  import { isAttachmentSpoiler } from '$lib/media/spoilers';
  import AttachmentUploadEditor from './AttachmentUploadEditor.svelte';
  import type { PendingUpload } from '$lib/media/uploads';
  import FilePreview from './FilePreview.svelte';

  let {
    uploads,
    onRemove,
    onSpoiler,
    disabled = false
  }: {
    uploads: PendingUpload[];
    onRemove: (key: string) => void;
    onSpoiler?: (key: string, spoiler: boolean) => void;
    disabled?: boolean;
  } = $props();

  let editing = $state<string | null>(null);
  const selected = $derived(uploads.find((item) => item.key === editing));

  function formatSize(size: number): string {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
</script>

<div class="upload-preview-tray" aria-label="Message attachments">
  {#each uploads as upload (upload.key)}
    <article class:failed={upload.status === 'failed'} class="upload-preview-card">
      <button
        type="button"
        class="upload-preview-media"
        class:spoiler={isAttachmentSpoiler(upload.file.name)}
        aria-label={`Edit attachment ${upload.file.name}`}
        disabled={disabled || upload.updating}
        onclick={() => (editing = upload.key)}><FilePreview file={upload.file} /></button
      >
      <button
        type="button"
        class="upload-preview-remove"
        aria-label={`Remove ${upload.file.name}`}
        title="Remove attachment"
        onclick={() => onRemove(upload.key)}>×</button
      >
      <div class="upload-preview-copy">
        {#if onSpoiler}
          <button
            type="button"
            disabled={disabled || upload.updating}
            aria-pressed={isAttachmentSpoiler(upload.file.name)}
            onclick={() => onSpoiler?.(upload.key, !isAttachmentSpoiler(upload.file.name))}
            >{isAttachmentSpoiler(upload.file.name) ? 'Spoiler ✓' : 'Mark as spoiler'}</button
          >
        {/if}
        <strong title={upload.file.name}>{upload.file.name}</strong>
        <small>
          {upload.status === 'failed'
            ? upload.error
            : upload.status === 'ready'
              ? `${formatSize(upload.file.size)} · Ready`
              : `${formatSize(upload.file.size)} · ${upload.progress}%`}
        </small>
        {#if upload.status === 'uploading'}
          <progress
            max="100"
            value={upload.progress}
            aria-label={`${upload.file.name} upload: ${upload.progress}%`}
          ></progress>
        {/if}
      </div>
    </article>
  {/each}
</div>

{#if selected}
  <AttachmentUploadEditor
    upload={selected}
    {disabled}
    {onSpoiler}
    onClose={() => (editing = null)}
  />
{/if}

<style>
  .upload-preview-media {
    display: block;
    width: 100%;
    padding: 0;
    border: 0;
    cursor: pointer;
  }
  .upload-preview-media.spoiler {
    filter: blur(8px);
  }
</style>
