<script lang="ts">
  import { api } from '$lib/api/client';
  import type { TrackerAttachment } from '$lib/task-tracker/types';
  let {
    attachment,
    channelRef,
    disabled,
    onRemove
  }: {
    attachment: TrackerAttachment;
    channelRef: string;
    disabled: boolean;
    onRemove: () => void;
  } = $props();
  let url = $state('');
  let busy = $state(false);
  let error = $state('');
  let preview = $state(false);
  function safeUrl(value: string): string {
    try {
      const parsed = new URL(value);
      return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password
        ? parsed.href
        : '';
    } catch {
      return '';
    }
  }
  async function open(showPreview: boolean) {
    busy = true;
    error = '';
    try {
      url = safeUrl(
        attachment.id
          ? (
              await api<{ url: string }>(
                `/channels/${encodeURIComponent(channelRef)}/tracker/attachments/read`,
                {
                  method: 'POST',
                  body: JSON.stringify({ attachment_id: attachment.id })
                }
              )
            ).url
          : (attachment.url ?? '')
      );
      if (!url) throw new Error('The attachment link is unavailable.');
      preview = showPreview;
    } catch (caught) {
      error = caught instanceof Error ? caught.message : 'Could not load the attachment.';
    } finally {
      busy = false;
    }
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- attachments use external URLs, not application routes -->
<div class="attachment">
  <div class="heading">
    <strong>{attachment.name}</strong>
    {#if attachment.type !== 'file'}<button
        type="button"
        disabled={busy}
        onclick={() => (preview ? (preview = false) : void open(true))}
        >{preview ? 'Hide preview' : 'Preview'}</button
      >{/if}
    {#if url}<a href={url} target="_blank" rel="noopener noreferrer">Open file</a>{:else}<button
        type="button"
        disabled={busy}
        onclick={() => void open(false)}>{busy ? 'Loading…' : 'Load file'}</button
      >{/if}
    {#if !disabled}<button type="button" onclick={onRemove} aria-label={`Remove ${attachment.name}`}
        >Remove</button
      >{/if}
  </div>
  {#if preview && url}
    {#if attachment.type === 'image'}<img
        src={url}
        alt={attachment.name}
        referrerpolicy="no-referrer"
      />
    {:else if attachment.type === 'video'}<!-- svelte-ignore a11y_media_has_caption --><video
        src={url}
        controls
        preload="metadata"
      ></video>{/if}
  {/if}
  {#if error}<p role="alert">{error}</p>{/if}
</div>

<style>
  .attachment {
    min-width: 0;
    border: 1px solid var(--line-soft);
    border-radius: 8px;
    padding: 0.65rem;
  }
  .heading {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  strong {
    flex: 1;
    min-width: 100px;
    overflow-wrap: anywhere;
    font-size: 0.8rem;
  }
  button,
  a {
    min-height: 36px;
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.4rem 0.65rem;
    background: var(--surface-raised);
    color: var(--text);
    cursor: pointer;
    font: inherit;
    font-size: 0.75rem;
    text-decoration: none;
  }
  img,
  video {
    display: block;
    width: 100%;
    max-height: 300px;
    object-fit: contain;
    margin-top: 0.5rem;
    border-radius: 6px;
  }
  p {
    color: var(--danger);
    font-size: 0.75rem;
  }
</style>
