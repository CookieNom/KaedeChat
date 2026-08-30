<script lang="ts">
  import type { Attachment, Role, UserSummary } from '$lib/chat/types';
  import type { EmbedMedia, MessageEmbed } from '$lib/chat/rich-content';
  import { embedAccent } from '$lib/chat/rich-content';
  import { attachmentMediaPath, authenticatedMedia } from '$lib/media/authenticated';
  import Markdown from './Markdown.svelte';
  import LinkPreview from './LinkPreview.svelte';

  let {
    embed,
    attachments = [],
    mentionUsers = [],
    mentionRoles = [],
    allowExternalMedia = true
  }: {
    embed: MessageEmbed;
    attachments?: Attachment[];
    mentionUsers?: UserSummary[];
    mentionRoles?: Role[];
    /** False for decrypted E2EE material: automatic previewing would disclose its URL. */
    allowExternalMedia?: boolean;
  } = $props();

  const accent = $derived(embedAccent(embed.color));

  function attachmentFor(media: EmbedMedia | null | undefined): Attachment | null {
    const filename = media?.url.startsWith('attachment://')
      ? media.url.slice('attachment://'.length)
      : null;
    if (!filename) return null;
    return attachments.find((attachment) => attachment.filename === filename) ?? null;
  }

  function formattedTimestamp(): string | null {
    if (!embed.timestamp) return null;
    const date = new Date(embed.timestamp);
    return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- embed author and title URLs are external -->

<article class="rich-embed" style:border-left-color={accent}>
  <div class="embed-copy">
    {#if embed.author}
      <div class="embed-author">
        {#if embed.author.icon_url}
          {#if allowExternalMedia}
            <LinkPreview url={embed.author.icon_url} compactMedia />
          {:else}
            <a
              class="external-media-placeholder compact"
              href={embed.author.icon_url}
              target="_blank"
              rel="noopener noreferrer nofollow"
              aria-label="Open external author image">↗</a
            >
          {/if}
        {/if}
        {#if embed.author.url}
          <a href={embed.author.url} target="_blank" rel="noopener noreferrer nofollow"
            >{embed.author.name}</a
          >
        {:else}<strong>{embed.author.name}</strong>{/if}
      </div>
    {/if}
    {#if embed.title}
      <h3>
        {#if embed.url}<a href={embed.url} target="_blank" rel="noopener noreferrer nofollow"
            >{embed.title}</a
          >{:else}{embed.title}{/if}
      </h3>
    {/if}
    {#if embed.description}
      <div class="embed-description">
        <Markdown content={embed.description} {mentionUsers} {mentionRoles} />
      </div>
    {/if}
    {#if embed.fields?.length}
      <div class="embed-fields">
        {#each embed.fields as field, index (`${index}:${field.name}`)}
          <section class:inline={field.inline}>
            <strong>{field.name}</strong>
            <Markdown content={field.value} {mentionUsers} {mentionRoles} />
          </section>
        {/each}
      </div>
    {/if}
    {#if embed.image}
      {@const attachment = attachmentFor(embed.image)}
      {#if attachment}
        <img
          class="embed-image"
          use:authenticatedMedia={{
            path: attachmentMediaPath(
              attachment.origin_domain,
              attachment.id,
              'thumbnail_512',
              null,
              attachment.private_media_url
            ),
            contentType: attachment.content_type
          }}
          alt={attachment.filename}
          loading="lazy"
        />
      {:else if embed.image.url}
        {#if allowExternalMedia}
          <LinkPreview url={embed.image.url} mediaOnly />
        {:else}
          <a
            class="external-media-placeholder"
            href={embed.image.url}
            target="_blank"
            rel="noopener noreferrer nofollow">Open external embed image</a
          >
        {/if}
      {/if}
    {/if}
    {#if embed.footer || formattedTimestamp()}
      <footer>
        {#if embed.footer?.icon_url}
          {#if allowExternalMedia}
            <LinkPreview url={embed.footer.icon_url} compactMedia />
          {:else}
            <a
              class="external-media-placeholder compact"
              href={embed.footer.icon_url}
              target="_blank"
              rel="noopener noreferrer nofollow"
              aria-label="Open external footer image">↗</a
            >
          {/if}
        {/if}
        {#if embed.footer?.text}<span>{embed.footer.text}</span>{/if}
        {#if embed.footer?.text && formattedTimestamp()}<span aria-hidden="true">•</span>{/if}
        {#if formattedTimestamp()}<time datetime={embed.timestamp ?? undefined}
            >{formattedTimestamp()}</time
          >{/if}
      </footer>
    {/if}
  </div>
  {#if embed.thumbnail}
    {@const attachment = attachmentFor(embed.thumbnail)}
    <aside>
      {#if attachment}
        <img
          use:authenticatedMedia={{
            path: attachmentMediaPath(
              attachment.origin_domain,
              attachment.id,
              'thumbnail_128',
              null,
              attachment.private_media_url
            ),
            contentType: attachment.content_type
          }}
          alt={attachment.filename}
          loading="lazy"
        />
      {:else if allowExternalMedia}<LinkPreview url={embed.thumbnail.url} compactMedia />
      {:else}
        <a
          class="external-media-placeholder thumbnail"
          href={embed.thumbnail.url}
          target="_blank"
          rel="noopener noreferrer nofollow">External image</a
        >
      {/if}
    </aside>
  {/if}
</article>

<style>
  .rich-embed {
    display: flex;
    width: min(540px, 100%);
    gap: 12px;
    margin-top: 8px;
    overflow: hidden;
    border: 1px solid var(--line);
    border-left-width: 4px;
    border-radius: 7px;
    padding: 12px;
    background: var(--surface-raised);
  }
  .embed-copy {
    min-width: 0;
    flex: 1;
  }
  .embed-author,
  footer {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .embed-author {
    margin-bottom: 7px;
    font-size: 0.78rem;
  }
  h3 {
    margin: 0 0 6px;
    font-size: 1rem;
  }
  a {
    color: var(--text-strong);
    text-decoration: none;
  }
  a:hover {
    text-decoration: underline;
  }
  .embed-description {
    font-size: 0.9rem;
  }
  .embed-fields {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 10px;
    margin-top: 10px;
  }
  .embed-fields section {
    grid-column: 1 / -1;
    min-width: 0;
    font-size: 0.84rem;
  }
  .embed-fields section.inline {
    grid-column: span 4;
  }
  .embed-fields strong {
    display: block;
    margin-bottom: 3px;
  }
  .embed-image {
    display: block;
    width: 100%;
    max-height: 420px;
    margin-top: 10px;
    border-radius: 6px;
    object-fit: contain;
  }
  aside {
    width: 82px;
    flex: 0 0 82px;
  }
  aside :global(img) {
    width: 82px;
    height: 82px;
    border-radius: 6px;
    object-fit: cover;
  }
  footer {
    margin-top: 10px;
    color: var(--text-muted);
    font-size: 0.7rem;
  }
  footer :global(img),
  .embed-author :global(img) {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    object-fit: cover;
  }
  .external-media-placeholder {
    display: inline-flex;
    min-height: 34px;
    align-items: center;
    justify-content: center;
    margin-top: 10px;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 7px 9px;
    color: var(--text-muted);
    font-size: 0.76rem;
  }
  .external-media-placeholder.compact {
    width: 20px;
    min-height: 20px;
    margin-top: 0;
    padding: 0;
    border-radius: 50%;
  }
  .external-media-placeholder.thumbnail {
    width: 82px;
    min-height: 82px;
    margin-top: 0;
    padding: 4px;
    text-align: center;
  }
  @media (max-width: 560px) {
    .embed-fields section.inline {
      grid-column: span 6;
    }
  }
</style>
