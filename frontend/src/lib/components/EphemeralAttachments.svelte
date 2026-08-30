<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import type { Attachment } from '$lib/chat/types';
  import type { EncryptedFileManifest } from '$lib/e2ee/media';
  import { decryptEncryptedAttachment, downloadEncryptedFile } from '$lib/e2ee/media';
  import {
    attachmentMediaPath,
    authenticatedMedia,
    downloadAuthenticatedMedia
  } from '$lib/media/authenticated';
  import MediaViewer from './MediaViewer.svelte';
  import { onDestroy } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  let {
    attachments,
    encryptedManifests = {}
  }: { attachments: Attachment[]; encryptedManifests?: Record<string, EncryptedFileManifest> } =
    $props();

  let failures = $state<Record<string, string>>({});
  let attempts = $state<Record<string, number>>({});
  let downloadError = $state('');
  let viewer = $state<Attachment | null>(null);
  let encryptedUrls = $state<Record<string, string>>({});
  let encryptedLoading = $state<Record<string, boolean>>({});
  let encryptedGeneration = 0;
  const allocatedEncryptedUrls = new SvelteSet<string>();

  function key(attachment: Attachment): string {
    return `${attachment.id}@${attachment.origin_domain}`;
  }

  function path(attachment: Attachment, variant: 'original' | 'thumbnail_512'): string {
    return attachmentMediaPath(
      attachment.origin_domain,
      attachment.id,
      variant,
      null,
      attachment.private_media_url
    );
  }

  function failed(attachment: Attachment, event: Event): void {
    const target = event.currentTarget as HTMLImageElement | HTMLMediaElement;
    failures = {
      ...failures,
      [key(attachment)]:
        target.dataset.mediaErrorMessage ??
        `Could not load ${attachment.filename}. Check your connection and try again.`
    };
  }

  function retry(attachment: Attachment): void {
    const attachmentKey = key(attachment);
    const next = { ...failures };
    delete next[attachmentKey];
    failures = next;
    attempts = { ...attempts, [attachmentKey]: (attempts[attachmentKey] ?? 0) + 1 };
  }

  async function download(attachment: Attachment): Promise<void> {
    downloadError = '';
    try {
      const encrypted = encryptedManifests[key(attachment)];
      if (encrypted) {
        await downloadEncryptedFile(encrypted, null, attachment.private_media_url);
        return;
      }
      await downloadAuthenticatedMedia(
        { path: path(attachment, 'original'), contentType: attachment.content_type },
        attachment.filename
      );
    } catch (caught) {
      downloadError = userErrorMessage(
        caught,
        `Could not download ${attachment.filename}. Try again.`
      );
    }
  }

  async function loadEncrypted(
    attachment: Attachment,
    manifest: EncryptedFileManifest,
    generation: number
  ): Promise<void> {
    const attachmentKey = key(attachment);
    encryptedLoading = { ...encryptedLoading, [attachmentKey]: true };
    try {
      const plaintext = await decryptEncryptedAttachment(
        manifest,
        null,
        attachment.private_media_url
      );
      if (generation !== encryptedGeneration) return;
      const url = URL.createObjectURL(plaintext);
      allocatedEncryptedUrls.add(url);
      encryptedUrls = { ...encryptedUrls, [attachmentKey]: url };
      const nextFailures = { ...failures };
      delete nextFailures[attachmentKey];
      failures = nextFailures;
    } catch (caught) {
      if (generation !== encryptedGeneration) return;
      failures = {
        ...failures,
        [attachmentKey]: userErrorMessage(
          caught,
          `Could not decrypt ${attachment.filename} on this device.`
        )
      };
    } finally {
      if (generation === encryptedGeneration) {
        const nextLoading = { ...encryptedLoading };
        delete nextLoading[attachmentKey];
        encryptedLoading = nextLoading;
      }
    }
  }

  function revokeEncryptedUrls(): void {
    for (const url of allocatedEncryptedUrls) URL.revokeObjectURL(url);
    allocatedEncryptedUrls.clear();
    encryptedUrls = {};
  }

  $effect(() => {
    const candidates = attachments.flatMap((attachment) => {
      const manifest = encryptedManifests[key(attachment)];
      return manifest ? [{ attachment, manifest }] : [];
    });
    encryptedGeneration += 1;
    const generation = encryptedGeneration;
    revokeEncryptedUrls();
    encryptedLoading = {};
    for (const candidate of candidates) {
      void loadEncrypted(candidate.attachment, candidate.manifest, generation);
    }
  });

  onDestroy(() => {
    encryptedGeneration += 1;
    revokeEncryptedUrls();
  });

  function sizeLabel(size: number): string {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
    return `${(size / (1024 * 1024)).toFixed(size < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  }
</script>

<div class="private-attachments" aria-label="Private response attachments">
  {#each attachments as attachment (key(attachment))}
    <section class="private-attachment" aria-label={`Attachment ${attachment.filename}`}>
      {#if attachment.encryption_mode === 'e2ee' && !encryptedManifests[key(attachment)]}
        <p class="attachment-state" role="alert">
          <span aria-hidden="true">⚠</span> This encrypted private attachment is unavailable on this device.
        </p>
      {:else if attachment.encryption_mode === 'e2ee' && encryptedLoading[key(attachment)]}
        <p class="attachment-state" role="status">
          <span class="spinner" aria-hidden="true"></span> Decrypting {attachment.filename}…
        </p>
      {:else if attachment.encryption_mode === 'e2ee' && failures[key(attachment)]}
        <div class="attachment-state rejected" role="alert">
          <span>{failures[key(attachment)]}</span>
          <button
            type="button"
            onclick={() =>
              void loadEncrypted(
                attachment,
                encryptedManifests[key(attachment)],
                encryptedGeneration
              )}>Try again</button
          >
        </div>
      {:else if attachment.encryption_mode === 'e2ee' && encryptedUrls[key(attachment)]}
        {@const encryptedUrl = encryptedUrls[key(attachment)]}
        {#if attachment.content_type.startsWith('image/')}
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- this is a local blob URL, not an application route -->
          <a class="image-preview" href={encryptedUrl} target="_blank" rel="noopener">
            <img
              src={encryptedUrl}
              alt={attachment.filename}
              width={attachment.width ?? 512}
              height={attachment.height ?? 320}
            />
          </a>
        {:else if attachment.content_type.startsWith('video/')}
          <video
            src={encryptedUrl}
            controls
            playsinline
            preload="metadata"
            aria-label={attachment.filename}
          >
            <track kind="captions" />
          </video>
        {:else if attachment.content_type.startsWith('audio/')}
          <audio src={encryptedUrl} controls preload="metadata" aria-label={attachment.filename}>
            <track kind="captions" />
          </audio>
        {:else}
          <button class="file-download" type="button" onclick={() => void download(attachment)}>
            <span aria-hidden="true">📎</span>
            <span
              ><strong>{attachment.filename}</strong><small>{sizeLabel(attachment.size)}</small
              ></span
            >
          </button>
        {/if}
        <footer>
          <span>{attachment.filename} · {sizeLabel(attachment.size)}</span>
          <button type="button" onclick={() => void download(attachment)}>Download</button>
        </footer>
      {:else if attachment.scan_status === 'pending'}
        <p class="attachment-state" role="status">
          <span class="spinner" aria-hidden="true"></span> Preparing {attachment.filename}…
        </p>
      {:else if attachment.scan_status === 'rejected' || attachment.scan_status === 'infected'}
        <p class="attachment-state rejected" role="alert">
          <span aria-hidden="true">⚠</span>
          {attachment.filename} was rejected during server processing.
        </p>
      {:else if attachment.scan_status === 'failed' || attachment.scan_status === 'encrypted'}
        <p class="attachment-state rejected" role="alert">
          <span aria-hidden="true">⚠</span>
          {attachment.filename} could not be processed by the server.
        </p>
      {:else if failures[key(attachment)]}
        <div class="attachment-state rejected" role="alert">
          <span>{failures[key(attachment)]}</span>
          <button type="button" onclick={() => retry(attachment)}>Try again</button>
        </div>
      {:else}
        {#key `${key(attachment)}:${attempts[key(attachment)] ?? 0}`}
          {#if attachment.content_type.startsWith('image/')}
            <button
              type="button"
              class="image-preview"
              aria-label={`Open ${attachment.filename}`}
              onclick={() => (viewer = attachment)}
            >
              <img
                use:authenticatedMedia={{
                  path: path(attachment, 'thumbnail_512'),
                  contentType: attachment.content_type
                }}
                onerror={(event) => failed(attachment, event)}
                alt={attachment.filename}
                width={attachment.width ?? 512}
                height={attachment.height ?? 320}
                loading="lazy"
              />
            </button>
          {:else if attachment.content_type.startsWith('video/')}
            <video
              use:authenticatedMedia={{
                path: path(attachment, 'original'),
                contentType: attachment.content_type
              }}
              onerror={(event) => failed(attachment, event)}
              controls
              playsinline
              preload="metadata"
              aria-label={attachment.filename}
            >
              <track kind="captions" />
            </video>
          {:else if attachment.content_type.startsWith('audio/')}
            <audio
              use:authenticatedMedia={{
                path: path(attachment, 'original'),
                contentType: attachment.content_type
              }}
              onerror={(event) => failed(attachment, event)}
              controls
              preload="metadata"
              aria-label={attachment.filename}
            >
              <track kind="captions" />
            </audio>
          {:else}
            <button class="file-download" type="button" onclick={() => void download(attachment)}>
              <span aria-hidden="true">📎</span>
              <span
                ><strong>{attachment.filename}</strong><small>{sizeLabel(attachment.size)}</small
                ></span
              >
            </button>
          {/if}
        {/key}
        {#if attachment.content_type.startsWith('image/') || attachment.content_type.startsWith('video/') || attachment.content_type.startsWith('audio/')}
          <footer>
            <span>{attachment.filename} · {sizeLabel(attachment.size)}</span>
            <button type="button" onclick={() => void download(attachment)}>Download</button>
          </footer>
        {/if}
      {/if}
    </section>
  {/each}
  {#if downloadError}<p class="download-error" role="alert">{downloadError}</p>{/if}
</div>

{#if viewer}
  <MediaViewer attachment={viewer} onClose={() => (viewer = null)} />
{/if}

<style>
  .private-attachments {
    display: grid;
    gap: 8px;
    margin-top: 9px;
  }
  .private-attachment {
    min-width: 0;
  }
  .image-preview {
    display: block;
    max-width: 100%;
    overflow: hidden;
    border: 0;
    border-radius: 8px;
    padding: 0;
    background: var(--surface-raised);
    cursor: zoom-in;
  }
  img,
  video {
    display: block;
    width: min(100%, 480px);
    max-height: 360px;
    object-fit: contain;
  }
  audio {
    display: block;
    width: min(100%, 480px);
  }
  footer,
  .attachment-state,
  .file-download {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  footer {
    justify-content: space-between;
    width: min(100%, 480px);
    margin-top: 4px;
    color: var(--text-muted);
    font-size: 0.7rem;
  }
  footer button,
  .attachment-state button {
    border: 0;
    color: var(--accent);
    background: transparent;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }
  .attachment-state,
  .file-download {
    width: min(100%, 480px);
    min-height: 52px;
    margin: 0;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px;
    color: var(--text-muted);
    background: var(--surface-raised);
    font-size: 0.78rem;
  }
  .attachment-state.rejected {
    border-color: color-mix(in srgb, var(--danger) 55%, var(--line));
    color: var(--danger);
  }
  .attachment-state.rejected span:first-child {
    flex: 1;
  }
  .file-download {
    color: var(--text);
    text-align: left;
    cursor: pointer;
  }
  .file-download > span:last-child {
    display: grid;
    min-width: 0;
  }
  .file-download strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .file-download small {
    color: var(--text-muted);
  }
  .spinner {
    width: 14px;
    height: 14px;
    flex: 0 0 auto;
    border: 2px solid var(--line);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  .download-error {
    margin: 0;
    color: var(--danger);
    font-size: 0.76rem;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
