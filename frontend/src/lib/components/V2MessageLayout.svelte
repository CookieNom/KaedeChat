<script lang="ts">
  import type { Attachment, Role, UserSummary } from '$lib/chat/types';
  import type {
    ActionRow,
    ContainerChild,
    MessageLayoutComponent,
    UnfurledMediaItem
  } from '$lib/chat/rich-content';
  import {
    attachmentMediaPath,
    authenticatedMedia,
    downloadAuthenticatedMedia
  } from '$lib/media/authenticated';
  import type { Snippet } from 'svelte';
  import Markdown from './Markdown.svelte';

  let {
    layout,
    layoutKey,
    attachments = [],
    mentionUsers = [],
    mentionRoles = [],
    allowExternalMedia = true,
    actionRow
  }: {
    layout: MessageLayoutComponent;
    layoutKey: string;
    attachments?: Attachment[];
    mentionUsers?: UserSummary[];
    mentionRoles?: Role[];
    allowExternalMedia?: boolean;
    actionRow: Snippet<[ActionRow, string]>;
  } = $props();

  let downloadError = $state('');

  function attachmentFor(media: UnfurledMediaItem): Attachment | null {
    if (!media.url.startsWith('attachment://')) return null;
    const filename = media.url.slice('attachment://'.length);
    return attachments.find((item) => item.filename === filename) ?? null;
  }

  function externalUrl(media: UnfurledMediaItem): string | null {
    return /^https?:\/\//i.test(media.url) ? media.url : null;
  }

  async function download(attachment: Attachment) {
    downloadError = '';
    try {
      await downloadAuthenticatedMedia(
        {
          path: attachmentMediaPath(
            attachment.origin_domain,
            attachment.id,
            'original',
            attachment.history_media_url,
            attachment.private_media_url
          ),
          contentType: attachment.content_type
        },
        attachment.filename
      );
    } catch {
      downloadError = `Could not download ${attachment.filename}. Try again.`;
    }
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- sanitized component media URLs are external resources, not application routes -->

{#snippet media(media: UnfurledMediaItem, description: string | null | undefined, spoiler = false)}
  {@const attachment = attachmentFor(media)}
  {@const remote = externalUrl(media)}
  <figure class:spoiler aria-label={spoiler ? 'Spoiler media' : description || 'Media'}>
    {#if attachment && attachment.scan_status === 'clean'}
      <img
        use:authenticatedMedia={{
          path: attachmentMediaPath(
            attachment.origin_domain,
            attachment.id,
            'thumbnail_512',
            attachment.history_media_url,
            attachment.private_media_url
          ),
          contentType: attachment.content_type
        }}
        alt={description ?? attachment.filename}
        loading="lazy"
      />
    {:else if remote && allowExternalMedia}
      <img src={remote} alt={description ?? ''} loading="lazy" referrerpolicy="no-referrer" />
    {:else if remote}
      <a
        class="external-media-placeholder"
        href={remote}
        target="_blank"
        rel="noopener noreferrer nofollow">Open external component media</a
      >
    {:else}
      <span class="unavailable">Media unavailable</span>
    {/if}
    {#if description}<figcaption>{description}</figcaption>{/if}
  </figure>
{/snippet}

{#snippet render(component: MessageLayoutComponent | ContainerChild, key: string)}
  {#if component.type === 1}
    {@render actionRow(component, key)}
  {:else if component.type === 10}
    <div class="text-display">
      <Markdown content={component.content} {mentionUsers} {mentionRoles} />
    </div>
  {:else if component.type === 9}
    <section class="section">
      <div class="section-copy">
        {#each component.components as text, index (`${key}:text:${text.id ?? index}`)}
          <Markdown content={text.content} {mentionUsers} {mentionRoles} />
        {/each}
      </div>
      <div class="section-accessory">
        {#if component.accessory.type === 2}
          {@render actionRow({ type: 1, components: [component.accessory] }, `${key}:accessory`)}
        {:else}
          {@render media(
            component.accessory.media,
            component.accessory.description,
            component.accessory.spoiler
          )}
        {/if}
      </div>
    </section>
  {:else if component.type === 12}
    <div class="gallery" class:single={component.items.length === 1}>
      {#each component.items as item, index (`${key}:gallery:${index}`)}
        {@render media(item.media, item.description, item.spoiler)}
      {/each}
    </div>
  {:else if component.type === 13}
    {@const attachment = attachmentFor(component.file)}
    <div class="file" class:spoiler={component.spoiler}>
      <span aria-hidden="true">📄</span>
      <span>{attachment?.filename ?? component.file.url.replace('attachment://', '')}</span>
      {#if attachment}
        <button type="button" onclick={() => void download(attachment)}>Download</button>
      {:else}
        <span class="unavailable">Unavailable</span>
      {/if}
    </div>
  {:else if component.type === 14}
    <div
      class="separator"
      class:divider={component.divider !== false}
      class:large={component.spacing === 2}
      aria-hidden="true"
    ></div>
  {:else if component.type === 17}
    <section
      class="container"
      class:spoiler={component.spoiler}
      style:--container-accent={component.accent_color == null
        ? 'transparent'
        : `#${component.accent_color.toString(16).padStart(6, '0')}`}
    >
      {#each component.components as child, index (`${key}:child:${child.id ?? index}`)}
        {@render render(child, `${key}:child:${child.id ?? index}`)}
      {/each}
    </section>
  {/if}
{/snippet}

<div class="v2-layout">
  {@render render(layout, layoutKey)}
  {#if downloadError}<small role="alert">{downloadError}</small>{/if}
</div>

<style>
  .v2-layout,
  .container,
  .section-copy {
    display: grid;
    gap: 8px;
  }
  .container {
    overflow: hidden;
    border: 1px solid var(--line);
    border-left: 4px solid var(--container-accent);
    border-radius: 8px;
    padding: 12px;
    background: color-mix(in srgb, var(--surface-raised) 70%, transparent);
  }
  .section {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
  }
  .section-accessory {
    max-width: 160px;
  }
  .gallery {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    overflow: hidden;
    gap: 4px;
    border-radius: 8px;
  }
  .gallery.single {
    grid-template-columns: 1fr;
  }
  figure {
    display: grid;
    min-width: 96px;
    margin: 0;
    gap: 4px;
  }
  figure img {
    display: block;
    width: 100%;
    max-height: 360px;
    border-radius: 6px;
    object-fit: cover;
  }
  .external-media-placeholder {
    display: flex;
    min-height: 72px;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 8px;
    color: var(--text-muted);
    font-size: 0.75rem;
    text-align: center;
  }
  figcaption,
  small,
  .unavailable {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  .file {
    display: flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px;
    background: var(--surface-raised);
  }
  .file button {
    margin-left: auto;
  }
  .separator {
    min-height: 8px;
  }
  .separator.large {
    min-height: 16px;
  }
  .separator.divider {
    border-top: 1px solid var(--line);
  }
  .spoiler {
    filter: blur(18px);
    transition: filter 120ms ease;
  }
  .spoiler:hover,
  .spoiler:focus-within {
    filter: none;
  }
</style>
