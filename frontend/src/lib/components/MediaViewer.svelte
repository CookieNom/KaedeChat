<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import type { Attachment } from '$lib/chat/types';
  import {
    attachmentMediaPath,
    authenticatedMedia,
    downloadAuthenticatedMedia
  } from '$lib/media/authenticated';
  import { portal } from '$lib/ui/portal';
  import { onDestroy, onMount, tick } from 'svelte';

  const ZOOM_LEVELS = [1, 1.25, 1.5, 2, 3, 4];

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
  let zoomIndex = $state(0);
  let stage: HTMLDivElement;

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

  async function setZoom(nextIndex: number, point?: { x: number; y: number }) {
    const clampedIndex = Math.max(0, Math.min(ZOOM_LEVELS.length - 1, nextIndex));
    if (isVideo || clampedIndex === zoomIndex || !stage) return;

    const x = point?.x ?? stage.clientWidth / 2;
    const y = point?.y ?? stage.clientHeight / 2;
    const relativeX = (stage.scrollLeft + x) / stage.scrollWidth;
    const relativeY = (stage.scrollTop + y) / stage.scrollHeight;
    zoomIndex = clampedIndex;
    await tick();
    stage.scrollLeft = relativeX * stage.scrollWidth - x;
    stage.scrollTop = relativeY * stage.scrollHeight - y;
  }

  function zoomWheel(event: WheelEvent) {
    if ((!event.ctrlKey && !event.metaKey) || isVideo) return;
    event.preventDefault();
    const bounds = stage.getBoundingClientRect();
    void setZoom(zoomIndex + (event.deltaY < 0 ? 1 : -1), {
      x: event.clientX - bounds.left,
      y: event.clientY - bounds.top
    });
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      onClose();
    } else if (!isVideo && (event.key === '+' || event.key === '=')) {
      event.preventDefault();
      void setZoom(zoomIndex + 1);
    } else if (!isVideo && event.key === '-') {
      event.preventDefault();
      void setZoom(zoomIndex - 1);
    } else if (!isVideo && event.key === '0') {
      event.preventDefault();
      void setZoom(0);
    }
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
    <div
      bind:this={stage}
      class:image-stage={!isVideo}
      class="media-viewer-stage"
      onwheel={zoomWheel}
    >
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
            <div
              class="media-viewer-image-canvas"
              style:width={`${ZOOM_LEVELS[zoomIndex] * 100}%`}
              style:height={`${ZOOM_LEVELS[zoomIndex] * 100}%`}
            >
              <img
                use:authenticatedMedia={{ path: originalUrl, contentType: attachment.content_type }}
                onerror={mediaFailed}
                alt={attachment.filename}
              />
            </div>
          {/if}
        {/key}
      {/if}
    </div>
    {#if !isVideo && !loadError}
      <div class="media-viewer-zoom" aria-label="Image zoom controls">
        <button
          type="button"
          aria-label="Zoom out"
          title="Zoom out (−)"
          disabled={zoomIndex === 0}
          onclick={() => void setZoom(zoomIndex - 1)}>−</button
        >
        <button
          type="button"
          class="zoom-level"
          aria-label="Fit image to window"
          title="Fit image to window (0)"
          disabled={zoomIndex === 0}
          onclick={() => void setZoom(0)}
          >{zoomIndex === 0 ? 'Fit' : `${ZOOM_LEVELS[zoomIndex] * 100}%`}</button
        >
        <button
          type="button"
          aria-label="Zoom in"
          title="Zoom in (+)"
          disabled={zoomIndex === ZOOM_LEVELS.length - 1}
          onclick={() => void setZoom(zoomIndex + 1)}>+</button
        >
      </div>
    {/if}
  </dialog>
</div>
