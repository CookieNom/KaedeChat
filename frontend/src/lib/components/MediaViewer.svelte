<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import type { Attachment } from '$lib/chat/types';
  import {
    attachmentMediaPath,
    authenticatedMedia,
    downloadAuthenticatedMedia
  } from '$lib/media/authenticated';
  import { portal } from '$lib/ui/portal';
  import { onDestroy, onMount } from 'svelte';

  let {
    attachment,
    onClose,
    onReport
  }: { attachment: Attachment; onClose: () => void; onReport?: () => void } = $props();
  const originalUrl = $derived(
    attachmentMediaPath(
      attachment.origin_domain,
      attachment.id,
      'original',
      attachment.history_media_url,
      attachment.private_media_url
    )
  );
  // Media is an authenticated API route rather than a Svelte page. The cast
  // keeps SvelteKit's base-path handling without pretending this dynamic URL
  // is part of the generated page-route union.
  const isVideo = $derived(attachment.content_type.startsWith('video/'));
  let loadError = $state('');
  let downloadError = $state('');
  let mediaAttempt = $state(0);

  function mediaFailed(event: Event) {
    const target = event.currentTarget as HTMLImageElement | HTMLVideoElement;
    loadError =
      target.dataset.mediaErrorMessage ??
      `Could not load ${attachment.filename}. Check your connection and try again.`;
  }

  function retryMedia() {
    loadError = '';
    mediaAttempt += 1;
  }

  async function download() {
    downloadError = '';
    try {
      await downloadAuthenticatedMedia(
        { path: originalUrl, contentType: attachment.content_type },
        attachment.filename
      );
    } catch (caught) {
      downloadError = userErrorMessage(
        caught,
        `Could not download ${attachment.filename}. Try again.`
      );
    }
  }

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
      {#if onReport}<button type="button" class="report-media" onclick={onReport}
          >Report message</button
        >{/if}
      <button type="button" onclick={() => void download()}>Download</button>
      <button type="button" aria-label="Close media viewer" onclick={onClose}>×</button>
    </header>
    {#if downloadError}<p class="form-error" role="alert">{downloadError}</p>{/if}
    <div class="media-viewer-stage">
      {#if loadError}
        <div class="attachment-load-error" role="alert">
          <span>{loadError}</span>
          <button type="button" onclick={retryMedia}>Try again</button>
        </div>
      {:else}
        {#key mediaAttempt}
          {#if isVideo}
            <video
              use:authenticatedMedia={{ path: originalUrl, contentType: attachment.content_type }}
              onerror={mediaFailed}
              controls
              playsinline
              preload="metadata"
            >
              <track kind="captions" />
            </video>
          {:else}
            <img
              use:authenticatedMedia={{ path: originalUrl, contentType: attachment.content_type }}
              onerror={mediaFailed}
              alt={attachment.filename}
            />
          {/if}
        {/key}
      {/if}
    </div>
  </dialog>
</div>
