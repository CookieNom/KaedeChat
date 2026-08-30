<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';

  interface Preview {
    url: string;
    title: string | null;
    description: string | null;
    site_name: string | null;
    media_url: string | null;
    media_type: 'image' | 'video' | null;
  }

  let {
    url,
    mediaOnly = false,
    compactMedia = false
  }: { url: string; mediaOnly?: boolean; compactMedia?: boolean } = $props();
  let preview = $state<Preview | null>(null);
  let loadError = $state('');
  let loadAttempt = $state(0);

  $effect(() => {
    const target = url;
    void loadAttempt;
    const controller = new AbortController();
    preview = null;
    loadError = '';
    void api<Preview>('/link-previews', {
      method: 'POST',
      body: JSON.stringify({ url: target }),
      signal: controller.signal
    })
      .then((result) => {
        if (!controller.signal.aborted && target === url) preview = result;
      })
      .catch((caught) => {
        if (!controller.signal.aborted && target === url) {
          loadError = userErrorMessage(
            caught,
            'Could not load this link preview. Open the link directly or try again.'
          );
        }
      });
    return () => controller.abort();
  });
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- preview destinations are external URLs returned by the API -->
{#if loadError && !mediaOnly && !compactMedia}
  <aside class="link-preview link-preview-error" role="alert">
    <p>{loadError}</p>
    <div>
      <a href={url} target="_blank" rel="noopener noreferrer nofollow">Open link</a>
      <button type="button" onclick={() => (loadAttempt += 1)}>Try again</button>
    </div>
  </aside>
{:else if preview && compactMedia && preview.media_url && preview.media_type === 'image'}
  <img class="compact-media" src={preview.media_url} alt="" loading="lazy" />
{:else if preview && mediaOnly && preview.media_url}
  {#if preview.media_type === 'image'}
    <img class="media-only" src={preview.media_url} alt="" loading="lazy" />
  {:else if preview.media_type === 'video'}
    <!-- svelte-ignore a11y_media_has_caption -->
    <video class="media-only" src={preview.media_url} controls preload="metadata"></video>
  {/if}
{:else if preview}
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
  .media-only {
    display: block;
    width: 100%;
    max-height: 420px;
    margin-top: 10px;
    border-radius: 6px;
    object-fit: contain;
  }
  .compact-media {
    display: block;
    width: 100%;
    height: 100%;
    border-radius: inherit;
    object-fit: cover;
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
  .link-preview-error {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border-left-color: var(--danger);
  }
  .link-preview-error > div {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 8px;
    padding: 0;
  }
</style>
