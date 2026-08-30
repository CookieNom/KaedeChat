<script lang="ts">
  /* eslint-disable svelte/no-navigation-without-resolve -- the Directory helper validates and resolves this internal product route. */
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import type { DirectoryProductReference } from '$lib/chat/application-product-links';
  import {
    applicationInstallPath,
    directoryDetailPath,
    type DirectoryApplicationDetail
  } from '$lib/chat/application-directory';
  import { assetUrl } from '$lib/media/assets';
  import { resolveApplicationDirectoryPath } from '$lib/navigation/routes';
  import { onDestroy } from 'svelte';

  let { reference }: { reference: DirectoryProductReference } = $props();
  let application = $state<DirectoryApplicationDetail | null>(null);
  let unavailable = $state('');
  let controller = new AbortController();
  let generation = 0;
  let loadedRef = $state('');

  const productPath = $derived(
    application
      ? directoryDetailPath(application.ref)
      : directoryDetailPath(reference.applicationRef)
  );
  const installPath = $derived(
    application ? applicationInstallPath(application, productPath) : null
  );
  const iconUrl = $derived(
    application?.icon_hash
      ? assetUrl(application.icon_hash, 'thumbnail_128', application.origin_domain)
      : ''
  );

  function requestIsCurrent(targetRef: string, signal: AbortSignal, request: number): boolean {
    return (
      !signal.aborted &&
      generation === request &&
      loadedRef === targetRef &&
      reference.applicationRef === targetRef
    );
  }

  $effect(() => {
    const targetRef = reference.applicationRef;
    if (targetRef === loadedRef) return;
    loadedRef = targetRef;
    application = null;
    unavailable = '';
    controller.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const request = ++generation;
    void api<DirectoryApplicationDetail>(
      `/application-directory/${encodeURIComponent(targetRef)}`,
      { signal }
    )
      .then((value) => {
        if (!requestIsCurrent(targetRef, signal, request)) return;
        if (value.ref !== targetRef) {
          unavailable = 'This App Directory listing returned a different application.';
          return;
        }
        application = value;
      })
      .catch((caught) => {
        if (requestIsCurrent(targetRef, signal, request)) {
          unavailable = userErrorMessage(caught, 'This App Directory listing is unavailable.');
        }
      });
  });

  onDestroy(() => {
    generation += 1;
    controller.abort();
  });
</script>

{#if application}
  <aside class="directory-app" aria-label={`App Directory listing for ${application.name}`}>
    <div class="identity">
      <span class="avatar">
        {#if iconUrl}<img src={iconUrl} alt="" />{:else}{application.name
            .slice(0, 1)
            .toUpperCase()}{/if}
      </span>
      <span
        ><small>APP DIRECTORY</small><strong>{application.name}</strong><em
          >{application.origin_domain}</em
        ></span
      >
    </div>
    <p>{application.summary}</p>
    <div class="details">
      <span>{application.category}</span>
      {#each application.tags.slice(0, 3) as tag (tag)}<span>{tag}</span>{/each}
    </div>
    <div class="actions">
      <a href={resolveApplicationDirectoryPath(productPath)}>View in Directory</a>
      {#if installPath}<a
          class="add"
          href={resolve(installPath as `/applications/${string}/install/${string}`)}>Add App</a
        >{/if}
    </div>
  </aside>
{:else if unavailable}
  <aside class="directory-app unavailable">
    <strong>App listing unavailable</strong><small>{unavailable}</small>
  </aside>
{:else}
  <aside class="directory-app loading" aria-label="Loading App Directory listing">
    <span></span>
  </aside>
{/if}

<style>
  .directory-app {
    display: grid;
    width: min(430px, 100%);
    box-sizing: border-box;
    gap: 11px;
    margin-top: 10px;
    border: 1px solid var(--line);
    border-left: 4px solid var(--pine, var(--accent));
    border-radius: 14px;
    padding: 16px;
    background: color-mix(in srgb, var(--surface-raised) 92%, var(--accent));
  }
  .identity {
    display: flex;
    gap: 11px;
    align-items: center;
  }
  .identity > span:last-child {
    display: grid;
    min-width: 0;
  }
  .identity small {
    color: var(--pine, var(--accent));
    font-size: 0.69rem;
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .identity strong,
  .identity em {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .identity em {
    color: var(--muted);
    font-size: 0.82rem;
    font-style: normal;
  }
  .avatar {
    display: grid;
    width: 48px;
    height: 48px;
    flex: 0 0 48px;
    overflow: hidden;
    place-items: center;
    border-radius: 15px;
    color: white;
    background: var(--accent-deep);
    font-weight: 800;
  }
  .avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  p {
    margin: 0;
    color: var(--text-muted);
  }
  .details,
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }
  .details span {
    border-radius: 999px;
    padding: 4px 8px;
    color: var(--muted);
    background: var(--surface);
    font-size: 0.75rem;
  }
  .actions a {
    border-radius: 9px;
    padding: 8px 12px;
    color: var(--text);
    background: var(--surface-hover);
    font-weight: 750;
    text-decoration: none;
  }
  .actions a.add {
    color: white;
    background: var(--accent);
  }
  .unavailable small {
    color: var(--muted);
  }
  .loading {
    min-height: 80px;
  }
  .loading span {
    width: 55%;
    height: 15px;
    border-radius: 6px;
    background: var(--surface);
  }
</style>
