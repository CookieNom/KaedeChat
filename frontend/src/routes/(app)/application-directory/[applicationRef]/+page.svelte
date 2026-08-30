<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import {
    applicationInstallPath,
    directoryDetailPath,
    type DirectoryApplicationDetail,
    type DirectoryLocale
  } from '$lib/chat/application-directory';
  import { DIRECTORY_LOCALES, youtubeEmbedUrl } from '$lib/chat/application-directory-editor';
  import { directoryProductShareUrl } from '$lib/chat/application-product-links';
  import { assetUrl } from '$lib/media/assets';
  import { resolveApplicationDirectoryPath } from '$lib/navigation/routes';
  import { onDestroy, onMount } from 'svelte';

  let { data } = $props<{ data: { applicationRef: string; returnTo: string } }>();
  let application = $state<DirectoryApplicationDetail | null>(null);
  let loading = $state(true);
  let error = $state('');
  let mediaIndex = $state(0);
  let selectedLocale = $state<DirectoryLocale | ''>('');
  let loadedRef = $state('');
  let controller = new AbortController();
  let requestGeneration = 0;
  let copyStatus = $state('');
  let playingVideoId = $state('');
  let carouselHovered = $state(false);
  let carouselFocused = $state(false);
  let carouselUserPaused = $state(false);
  let reducedMotion = $state(true);
  let documentVisible = $state(true);

  const detailPath = $derived(
    application ? directoryDetailPath(application.ref, data.returnTo) : null
  );
  const installPath = $derived(
    application && detailPath ? applicationInstallPath(application, detailPath) : null
  );
  const bannerUrl = $derived(
    application?.banner_hash
      ? assetUrl(application.banner_hash, 'original', application.origin_domain)
      : ''
  );
  const iconUrl = $derived(
    application?.icon_hash
      ? assetUrl(application.icon_hash, 'thumbnail_128', application.origin_domain)
      : ''
  );
  const currentMedia = $derived(application?.media[mediaIndex] ?? application?.media[0] ?? null);
  const currentEmbedUrl = $derived(
    currentMedia?.type === 'youtube' && playingVideoId === currentMedia.video_id
      ? youtubeEmbedUrl(currentMedia.video_id)
      : null
  );
  const displayedDescription = $derived(
    (selectedLocale && application?.description_localizations[selectedLocale]) ||
      application?.description ||
      ''
  );

  function requestIsCurrent(signal: AbortSignal, generation: number): boolean {
    return !signal.aborted && requestGeneration === generation && loadedRef === data.applicationRef;
  }

  async function load(applicationRef: string, signal: AbortSignal, generation: number) {
    loading = true;
    error = '';
    try {
      const loadedApplication = await api<DirectoryApplicationDetail>(
        `/application-directory/${encodeURIComponent(applicationRef)}`,
        { signal }
      );
      if (!requestIsCurrent(signal, generation)) return;
      if (loadedApplication.ref !== applicationRef) {
        throw new Error('The directory returned a different application.');
      }
      application = loadedApplication;
    } catch (caught) {
      if (requestIsCurrent(signal, generation)) {
        application = null;
        error = userErrorMessage(caught, 'This directory listing is unavailable.');
      }
    } finally {
      if (requestIsCurrent(signal, generation)) loading = false;
    }
  }

  function localeName(locale: DirectoryLocale): string {
    return DIRECTORY_LOCALES.find(([value]) => value === locale)?.[1] ?? locale;
  }

  function selectMedia(index: number): void {
    if (!application?.media[index]) return;
    mediaIndex = index;
    playingVideoId = '';
  }

  function stepMedia(offset: -1 | 1): void {
    const count = application?.media.length ?? 0;
    if (count < 2) return;
    selectMedia((mediaIndex + offset + count) % count);
  }

  async function copyProductLink(): Promise<void> {
    if (!application) return;
    const shareUrl = directoryProductShareUrl(application);
    if (!shareUrl) {
      copyStatus = 'This app does not have a canonical product link.';
      return;
    }
    try {
      await navigator.clipboard.writeText(shareUrl);
      copyStatus = 'App link copied.';
    } catch {
      copyStatus = 'Clipboard access was denied. Copy the address from your browser instead.';
    }
  }

  $effect(() => {
    if (data.applicationRef === loadedRef) return;
    loadedRef = data.applicationRef;
    application = null;
    mediaIndex = 0;
    selectedLocale = '';
    playingVideoId = '';
    carouselHovered = false;
    carouselFocused = false;
    carouselUserPaused = false;
    copyStatus = '';
    controller.abort();
    controller = new AbortController();
    const generation = ++requestGeneration;
    void load(data.applicationRef, controller.signal, generation);
  });

  $effect(() => {
    const count = application?.media.length ?? 0;
    if (
      count < 2 ||
      reducedMotion ||
      carouselHovered ||
      carouselFocused ||
      carouselUserPaused ||
      playingVideoId ||
      !documentVisible ||
      loadedRef !== data.applicationRef
    ) {
      return;
    }
    const timer = window.setInterval(() => stepMedia(1), 7_000);
    return () => window.clearInterval(timer);
  });

  onMount(() => {
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const updateMotion = () => (reducedMotion = motion.matches);
    const updateVisibility = () => (documentVisible = document.visibilityState === 'visible');
    updateMotion();
    updateVisibility();
    motion.addEventListener('change', updateMotion);
    document.addEventListener('visibilitychange', updateVisibility);
    return () => {
      motion.removeEventListener('change', updateMotion);
      document.removeEventListener('visibilitychange', updateVisibility);
    };
  });

  onDestroy(() => {
    requestGeneration += 1;
    controller.abort();
  });
</script>

<svelte:head><title>{application?.name ?? 'App Directory'} · Kaede Chat</title></svelte:head>
<!-- eslint-disable svelte/no-navigation-without-resolve -- product-page links are validated HTTPS URLs from the strict directory contract -->
<main>
  <nav>
    <a href={resolveApplicationDirectoryPath(data.returnTo)}>← App Directory</a>
  </nav>
  {#if loading}
    <p class="state">Loading app…</p>
  {:else if error || !application || loadedRef !== data.applicationRef}
    <p class="notice" role="alert">{error || 'This directory listing is unavailable.'}</p>
  {:else}
    <article>
      <div class="banner">
        {#if bannerUrl}<img src={bannerUrl} alt="" />{/if}
      </div>
      <header>
        <span class="icon">
          {#if iconUrl}<img src={iconUrl} alt="" />{:else}{application.name
              .slice(0, 1)
              .toUpperCase()}{/if}
        </span>
        <div class="identity">
          <span>{application.category}</span>
          <h1>
            {application.name}{#if application.verified}<small aria-label="Reviewed application"
                ><span aria-hidden="true">✓</span></small
              >{/if}
          </h1>
          <p>{application.summary}</p>
        </div>
        <div class="product-actions">
          <button class="share" type="button" onclick={() => void copyProductLink()}
            >Copy link</button
          >
          {#if installPath}<a
              class="add"
              href={resolve(installPath as `/applications/${string}/install/${string}`)}>Add App</a
            >{/if}
        </div>
      </header>
      <p class="visually-hidden" role="status" aria-live="polite">{copyStatus}</p>
      <div class="layout">
        <div class="main-column">
          <section>
            <div class="section-heading">
              <h2>About this app</h2>
              {#if application.supported_locales.length}
                <label class="language"
                  ><span>Language</span><select bind:value={selectedLocale}>
                    <option value="">Default</option>
                    {#each application.supported_locales as locale (locale)}<option value={locale}
                        >{localeName(locale)}</option
                      >{/each}
                  </select></label
                >
              {/if}
            </div>
            <p class="description">{displayedDescription}</p>
            {#if application.media.length}
              <section
                class="media-carousel"
                aria-label="App media"
                onmouseenter={() => (carouselHovered = true)}
                onmouseleave={() => (carouselHovered = false)}
                onfocusin={() => (carouselFocused = true)}
                onfocusout={(event) => {
                  if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node)) {
                    carouselFocused = false;
                  }
                }}
              >
                <div class="media-stage">
                  {#if currentMedia?.type === 'image'}
                    <img
                      src={assetUrl(currentMedia.media_hash, 'original', application.origin_domain)}
                      alt={currentMedia.name}
                    />
                  {:else if currentMedia?.type === 'youtube'}
                    {#if currentEmbedUrl}
                      <iframe
                        src={currentEmbedUrl}
                        title={`${application.name} video`}
                        loading="lazy"
                        referrerpolicy="no-referrer"
                        sandbox="allow-scripts allow-same-origin allow-presentation"
                        allow="accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture"
                        allowfullscreen
                      ></iframe>
                    {:else}
                      <button
                        class="video-poster"
                        type="button"
                        aria-label={`Play video for ${application.name}`}
                        onclick={() => (playingVideoId = currentMedia.video_id)}
                      >
                        <span aria-hidden="true">▶</span><strong>Play video</strong>
                      </button>
                    {/if}
                  {/if}
                  {#if application.media.length > 1}
                    <button
                      class="carousel-arrow previous"
                      type="button"
                      aria-label="Previous media"
                      onclick={() => stepMedia(-1)}>‹</button
                    >
                    <button
                      class="carousel-arrow next"
                      type="button"
                      aria-label="Next media"
                      onclick={() => stepMedia(1)}>›</button
                    >
                  {/if}
                </div>
                {#if application.media.length > 1}
                  <div class="carousel-controls">
                    <button
                      class="rotation-control"
                      type="button"
                      aria-pressed={carouselUserPaused}
                      onclick={() => (carouselUserPaused = !carouselUserPaused)}
                      >{carouselUserPaused ? 'Play carousel' : 'Pause carousel'}</button
                    >
                    <div class="media-dots" aria-label="Choose app media">
                      {#each application.media as item, index (`${item.type}:${item.type === 'image' ? item.asset_id : item.video_id}`)}
                        <button
                          class:active={index === mediaIndex}
                          type="button"
                          aria-label={`Show media ${index + 1} of ${application.media.length}`}
                          aria-current={index === mediaIndex ? 'true' : undefined}
                          onclick={() => selectMedia(index)}
                        ></button>
                      {/each}
                    </div>
                  </div>
                {/if}
              </section>
            {/if}
            {#if application.tags.length}<div class="tags">
                {#each application.tags as tag (tag)}<span>{tag}</span>{/each}
              </div>{/if}
          </section>

          {#if application.popular_commands.length}
            <section class="commands">
              <h2>Popular commands</h2>
              <div>
                {#each application.popular_commands as command (command.id)}
                  <article>
                    <code>/{command.name}</code>
                    <p>{command.description}</p>
                  </article>
                {/each}
              </div>
            </section>
          {/if}

          {#if application.similar_apps.length}
            <section class="similar">
              <h2>Similar apps</h2>
              <div class="similar-grid">
                {#each application.similar_apps as similar (similar.ref)}
                  {@const similarIcon = similar.icon_hash
                    ? assetUrl(similar.icon_hash, 'thumbnail_128', similar.origin_domain)
                    : ''}
                  <a
                    href={resolveApplicationDirectoryPath(
                      directoryDetailPath(similar.ref, data.returnTo)
                    )}
                  >
                    <span class="similar-icon">
                      {#if similarIcon}<img src={similarIcon} alt="" />{:else}{similar.name
                          .slice(0, 1)
                          .toUpperCase()}{/if}
                    </span>
                    <span><strong>{similar.name}</strong><small>{similar.summary}</small></span>
                  </a>
                {/each}
              </div>
            </section>
          {/if}
        </div>

        <aside>
          <h2>Details</h2>
          <dl>
            <dt>Publisher</dt>
            <dd>{application.origin_domain}</dd>
            <dt>Installation</dt>
            <dd>
              {application.install_template.install_types
                .map((item) => (item === 'guild_install' ? 'Servers' : 'Your account'))
                .join(', ')}
            </dd>
          </dl>
          <div class="links">
            <a href={application.support_url} target="_blank" rel="noopener noreferrer nofollow"
              >Support ↗</a
            >
            <a
              href={application.privacy_policy_url}
              target="_blank"
              rel="noopener noreferrer nofollow">Privacy policy ↗</a
            >
            <a href={application.terms_url} target="_blank" rel="noopener noreferrer nofollow"
              >Terms of service ↗</a
            >
            {#each application.external_links as link (link.url)}
              <a href={link.url} target="_blank" rel="noopener noreferrer nofollow">{link.name} ↗</a
              >
            {/each}
          </div>
        </aside>
      </div>
    </article>
  {/if}
</main>

<style>
  :global(body) {
    overflow: auto;
    background: var(--app-bg);
  }
  main {
    min-height: 100dvh;
    padding: 26px clamp(20px, 5vw, 72px) 72px;
    color: var(--text);
  }
  nav,
  article,
  .state,
  .notice {
    max-width: 1040px;
    margin-inline: auto;
  }
  nav {
    margin-bottom: 22px;
  }
  nav a,
  .links a {
    color: var(--text-muted);
    text-decoration: none;
  }
  article {
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--surface);
  }
  .banner {
    height: clamp(160px, 28vw, 300px);
    background: linear-gradient(135deg, var(--surface-hover), var(--surface-subtle));
  }
  .banner img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  header {
    display: grid;
    grid-template-columns: 88px 1fr auto;
    gap: 20px;
    align-items: center;
    padding: 0 34px 26px;
  }
  .icon,
  .similar-icon {
    display: grid;
    place-items: center;
    overflow: hidden;
    background: var(--accent);
    font-weight: 800;
  }
  .icon {
    width: 88px;
    height: 88px;
    margin-top: -30px;
    border: 6px solid var(--surface);
    border-radius: 22px;
    font-size: 38px;
  }
  .icon img,
  .similar-icon img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .identity > span {
    color: var(--text-muted);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
  }
  h1 {
    margin: 4px 0;
  }
  h1 small {
    margin-left: 8px;
    color: var(--pine);
  }
  .identity p {
    margin: 0;
    color: var(--text-muted);
  }
  .product-actions {
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .add,
  .share {
    box-sizing: border-box;
    padding: 12px 20px;
    border: 0;
    border-radius: 8px;
    font: inherit;
    font-weight: 750;
    text-decoration: none;
  }
  .add {
    color: var(--text-inverse);
    background: var(--pine);
  }
  .share {
    cursor: pointer;
    color: var(--text);
    background: var(--surface-raised);
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 280px;
    gap: 44px;
    padding: 30px 34px 44px;
    border-top: 1px solid var(--line);
  }
  .main-column {
    display: grid;
    gap: 34px;
    min-width: 0;
  }
  h2 {
    margin-top: 0;
    font-size: 18px;
  }
  .section-heading {
    display: flex;
    justify-content: space-between;
    gap: 18px;
    align-items: start;
  }
  .language {
    display: grid;
    gap: 4px;
    min-width: 150px;
    color: var(--text-muted);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .language select {
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 7px;
    color: var(--text);
    background: var(--surface-subtle);
  }
  .description {
    white-space: pre-wrap;
    color: var(--text-soft);
    line-height: 1.65;
  }
  .media-carousel {
    margin: 24px 0;
  }
  .media-stage {
    position: relative;
    display: grid;
    place-items: center;
    width: 100%;
    aspect-ratio: 16 / 9;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: #111;
  }
  .media-stage > img,
  .media-stage iframe,
  .video-poster {
    width: 100%;
    height: 100%;
    border: 0;
  }
  .media-stage > img {
    object-fit: contain;
  }
  .video-poster {
    display: grid;
    place-content: center;
    gap: 12px;
    cursor: pointer;
    color: white;
    background: linear-gradient(135deg, #26262b, #111114);
    font: inherit;
  }
  .video-poster span {
    display: grid;
    place-items: center;
    width: 64px;
    height: 46px;
    margin: auto;
    border-radius: 13px;
    background: #d91f2a;
    font-size: 24px;
  }
  .carousel-arrow {
    position: absolute;
    top: 50%;
    width: 38px;
    height: 54px;
    border: 0;
    border-radius: 8px;
    transform: translateY(-50%);
    cursor: pointer;
    color: white;
    background: rgb(0 0 0 / 65%);
    font-size: 32px;
  }
  .carousel-arrow.previous {
    left: 10px;
  }
  .carousel-arrow.next {
    right: 10px;
  }
  .carousel-controls {
    display: flex;
    position: relative;
    justify-content: center;
    align-items: center;
    margin-top: 10px;
  }
  .rotation-control {
    position: absolute;
    left: 0;
    border: 0;
    padding: 4px 6px;
    cursor: pointer;
    color: var(--text-muted);
    background: transparent;
    font: inherit;
    font-size: 11px;
  }
  .rotation-control:hover,
  .rotation-control:focus-visible {
    color: var(--text);
  }
  .media-dots {
    display: flex;
    gap: 7px;
  }
  .media-dots button {
    width: 9px;
    height: 9px;
    border: 0;
    border-radius: 999px;
    padding: 0;
    cursor: pointer;
    background: var(--text-muted);
    opacity: 0.45;
  }
  .media-dots button.active {
    width: 22px;
    opacity: 1;
    background: var(--accent);
  }
  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }
  .tags span {
    padding: 5px 9px;
    border-radius: 999px;
    color: var(--text-muted);
    background: var(--surface-subtle);
    font-size: 12px;
  }
  .commands > div {
    display: grid;
    gap: 8px;
  }
  .commands article {
    display: grid;
    grid-template-columns: minmax(110px, 0.35fr) 1fr;
    gap: 12px;
    align-items: baseline;
    border-radius: 8px;
    padding: 11px 13px;
    background: var(--surface-subtle);
  }
  .commands code {
    color: var(--accent);
    font-weight: 750;
  }
  .commands p {
    margin: 0;
    color: var(--text-muted);
    font-size: 13px;
  }
  .similar-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }
  .similar-grid a {
    display: grid;
    grid-template-columns: 44px minmax(0, 1fr);
    gap: 10px;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 10px;
    color: var(--text);
    text-decoration: none;
  }
  .similar-grid a:hover {
    background: var(--surface-hover);
  }
  .similar-icon {
    width: 44px;
    height: 44px;
    border-radius: 11px;
  }
  .similar-grid a > span:last-child {
    display: grid;
    min-width: 0;
  }
  .similar-grid small {
    overflow: hidden;
    color: var(--text-muted);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  dl {
    display: grid;
    gap: 5px;
  }
  dt {
    margin-top: 10px;
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .links {
    display: grid;
    gap: 10px;
    margin-top: 22px;
  }
  .links a:hover {
    color: var(--text);
  }
  .state,
  .notice {
    padding: 20px;
    border-radius: 8px;
    background: var(--surface);
  }
  .notice {
    color: var(--danger);
  }
  @media (max-width: 760px) {
    header {
      grid-template-columns: 70px 1fr;
      padding-inline: 20px;
    }
    .icon {
      width: 70px;
      height: 70px;
    }
    .product-actions {
      grid-column: 1 / -1;
    }
    .layout {
      grid-template-columns: 1fr;
      padding-inline: 20px;
    }
    aside {
      border-top: 1px solid var(--line);
      padding-top: 24px;
    }
  }
  @media (max-width: 520px) {
    .section-heading,
    .product-actions {
      align-items: stretch;
      flex-direction: column;
    }
    .similar-grid,
    .commands article {
      grid-template-columns: 1fr;
    }
  }
</style>
