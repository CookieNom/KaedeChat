<script lang="ts">
  import type { PendingUpload } from '$lib/media/uploads';
  import FilePreview from './FilePreview.svelte';

  let { uploads, onRemove }: { uploads: PendingUpload[]; onRemove: (key: string) => void } =
    $props();

  function formatSize(size: number): string {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
</script>

<div class="upload-preview-tray" aria-label="Message attachments">
  {#each uploads as upload (upload.key)}
    <article class:failed={upload.status === 'failed'} class="upload-preview-card">
      <div class="upload-preview-media"><FilePreview file={upload.file} /></div>
      <button
        type="button"
        class="upload-preview-remove"
        aria-label={`Remove ${upload.file.name}`}
        title="Remove attachment"
        onclick={() => onRemove(upload.key)}>×</button
      >
      <div class="upload-preview-copy">
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
