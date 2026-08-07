<script lang="ts">
  import { api } from '$lib/api/client';

  interface Preview {
    url: string;
    title: string | null;
    description: string | null;
    site_name: string | null;
    media_url: string | null;
    media_type: 'image' | 'video' | null;
  }

  let { url }: { url: string } = $props();
  let preview = $state<Preview | null>(null);

  $effect(() => {
    const target = url;
    const controller = new AbortController();
    preview = null;
    void api<Preview>('/link-previews', {
      method: 'POST',
      body: JSON.stringify({ url: target }),
      signal: controller.signal
    })
      .then((result) => {
        if (!controller.signal.aborted && target === url) preview = result;
      })
      .catch(() => undefined);
    return () => controller.abort();
  });
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- preview destinations are external URLs returned by the API -->
{#if preview}
  <article class="link-preview">
    {#if preview.media_url && preview.media_type === 'image'}
      <a href={preview.url} target="_blank" rel="noopener noreferrer nofollow">
        <img src={preview.media_url} alt="" loading="lazy" />
      </a>
    {:else if preview.media_url && preview.media_type === 'video'}
      <!-- svelte-ignore a11y_media_has_caption -->
      <video src={preview.media_url} controls preload="metadata"></video>
    {/if}
    {#if preview.title || preview.description || preview.site_name}
      <div>
        {#if preview.site_name}<small>{preview.site_name}</small>{/if}
        {#if preview.title}
          <a href={preview.url} target="_blank" rel="noopener noreferrer nofollow"
            >{preview.title}</a
          >
        {/if}
        {#if preview.description}<p>{preview.description}</p>{/if}
      </div>
    {/if}
  </article>
{/if}

<style>
  .link-preview {
    display: grid;
    width: min(520px, 100%);
    max-height: 520px;
    overflow: hidden;
    margin-top: 9px;
    border: 1px solid var(--line);
    border-left: 4px solid var(--accent);
    border-radius: 13px;
    background: var(--surface-raised);
    box-shadow: 0 8px 24px rgb(0 0 0 / 12%);
  }
  .link-preview > a {
    display: block;
    overflow: hidden;
  }
  img,
  video {
    display: block;
    width: 100%;
    max-height: 340px;
    object-fit: contain;
    background: rgb(0 0 0 / 20%);
  }
  .link-preview > div {
    display: grid;
    gap: 5px;
    padding: 12px 14px 14px;
  }
  small {
    color: var(--text-muted);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .link-preview > div > a {
    color: var(--text-strong);
    font-weight: 750;
    line-height: 1.25;
    text-decoration: none;
  }
  .link-preview > div > a:hover {
    text-decoration: underline;
  }
  p {
    display: -webkit-box;
    overflow: hidden;
    margin: 0;
    color: var(--text-muted);
    line-height: 1.35;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    line-clamp: 3;
  }
</style>
