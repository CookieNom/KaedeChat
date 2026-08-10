<script lang="ts">
  import type { Attachment } from '$lib/chat/types';
  import {
    attachmentMediaPath,
    authenticatedMedia,
    downloadAuthenticatedMedia
  } from '$lib/media/authenticated';
  import { portal } from '$lib/ui/portal';
  import { onDestroy, onMount } from 'svelte';

  let { attachment, onClose }: { attachment: Attachment; onClose: () => void } = $props();
  const originalUrl = $derived(
    attachmentMediaPath(attachment.origin_domain, attachment.id, 'original')
  );
  // Media is an authenticated API route rather than a Svelte page. The cast
  // keeps SvelteKit's base-path handling without pretending this dynamic URL
  // is part of the generated page-route union.
  const isVideo = $derived(attachment.content_type.startsWith('video/'));

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onClose();
  }

  onMount(() => {
    window.addEventListener('keydown', keydown);
    document.body.classList.add('media-viewer-open');
  });

  onDestroy(() => {
    window.removeEventListener('keydown', keydown);
    document.body.classList.remove('media-viewer-open');
  });
</script>

<div
  use:portal
  class="media-viewer-backdrop"
  role="presentation"
  onclick={(event) => {
    if (event.target === event.currentTarget) onClose();
  }}
>
  <dialog open class="media-viewer" aria-label={attachment.filename}>
    <header>
      <div>
        <strong>{attachment.filename}</strong>
        <small>{attachment.content_type}</small>
      </div>
      <button
        type="button"
        onclick={() =>
          downloadAuthenticatedMedia(
            { path: originalUrl, contentType: attachment.content_type },
            attachment.filename
          )}>Download</button
      >
      <button type="button" aria-label="Close media viewer" onclick={onClose}>×</button>
    </header>
    <div class="media-viewer-stage">
      {#if isVideo}
        <video
          use:authenticatedMedia={{ path: originalUrl, contentType: attachment.content_type }}
          controls
          playsinline
          preload="metadata"
        >
          <track kind="captions" />
        </video>
      {:else}
        <img
          use:authenticatedMedia={{ path: originalUrl, contentType: attachment.content_type }}
          alt={attachment.filename}
        />
      {/if}
    </div>
  </dialog>
</div>
