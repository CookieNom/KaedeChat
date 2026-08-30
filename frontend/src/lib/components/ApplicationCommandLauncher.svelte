<script lang="ts">
  /* eslint-disable svelte/no-navigation-without-resolve -- Directory route helpers validate the path and resolve the configured base path. */
  import { goto } from '$app/navigation';
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import {
    applicationCommandLauncherGroups,
    localizedCommandName,
    localizedCommandText,
    type ApplicationCommand
  } from '$lib/chat/application-commands';
  import {
    activeLauncherInstallations,
    launcherCollectionGroups,
    launcherInstallationDestination,
    launcherRecentApplications,
    rememberLauncherCommand,
    uninstalledCatalogApplications,
    type LauncherRecentApplication
  } from '$lib/chat/application-launcher';
  import {
    canonicalDirectoryDomain,
    directoryDetailPath,
    directoryQuery,
    EMPTY_DIRECTORY_FILTERS,
    parseDirectoryBotProfileApplication,
    type DirectoryApplicationSummary,
    type DirectoryPage
  } from '$lib/chat/application-directory';
  import {
    listUserApplicationInstallations,
    type UserApplicationInstallation
  } from '$lib/chat/application-installations';
  import { assetUrl } from '$lib/media/assets';
  import { resolveApplicationDirectoryPath } from '$lib/navigation/routes';
  import { preferredLocale } from '$lib/ui/locale';
  import { onDestroy } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';

  let {
    commands,
    accountRef,
    disabled = false,
    locale = preferredLocale(),
    open = $bindable(false),
    showTrigger = true,
    onSelect
  }: {
    commands: ApplicationCommand[];
    accountRef: string | null;
    disabled?: boolean;
    locale?: string;
    open?: boolean;
    showTrigger?: boolean;
    onSelect: (command: ApplicationCommand) => void;
  } = $props();

  let query = $state('');
  let directoryDomain = $state('');
  let searchInput: HTMLInputElement | null = $state(null);
  let launcherElement: HTMLDivElement | null = $state(null);
  let triggerElement: HTMLButtonElement | null = $state(null);
  let recentApplications = $state<LauncherRecentApplication[]>([]);
  let installations = $state<UserApplicationInstallation[]>([]);
  let installationsError = $state('');
  let recentActionError = $state('');
  let recentActionBusy = $state<string | null>(null);
  let catalogPage = $state<DirectoryPage | null>(null);
  let catalogLoading = $state(false);
  let catalogError = $state('');
  let catalogController = new AbortController();
  let catalogGeneration = 0;
  let installationController = new AbortController();
  let installationGeneration = 0;
  let destinationController = new AbortController();
  let destinationGeneration = 0;
  let wasOpen = false;
  let returnFocusElement: HTMLElement | null = null;
  const destinationRequests = new SvelteMap<string, Promise<string>>();
  const destinationCache = new SvelteMap<string, string>();

  const trimmedQuery = $derived(query.trim());
  const groups = $derived(applicationCommandLauncherGroups(commands, query, locale));
  const collectionGroups = $derived(
    !trimmedQuery && catalogPage ? launcherCollectionGroups(catalogPage, commands) : []
  );
  const searchedApplications = $derived(
    trimmedQuery && catalogPage
      ? uninstalledCatalogApplications(catalogPage.items, commands)
      : ([] as DirectoryApplicationSummary[])
  );
  const resultStatus = $derived(
    catalogLoading
      ? 'Searching the App Directory.'
      : catalogError
        ? catalogError
        : trimmedQuery
          ? `${groups.reduce((count, group) => count + group.commands.length, 0)} installed commands and ${searchedApplications.length} directory apps found.`
          : ''
  );

  function refreshRecents(): void {
    recentApplications =
      accountRef && typeof localStorage !== 'undefined'
        ? launcherRecentApplications(accountRef, commands, installations, localStorage)
        : [];
  }

  function show(): void {
    if (disabled) return;
    open = true;
  }

  function close(restoreFocus = true): void {
    const focusTarget = returnFocusElement ?? triggerElement;
    open = false;
    query = '';
    if (restoreFocus) window.setTimeout(() => focusTarget?.focus(), 0);
  }

  function choose(command: ApplicationCommand): void {
    if (accountRef && typeof localStorage !== 'undefined') {
      rememberLauncherCommand(accountRef, command, localStorage);
    }
    close(false);
    onSelect(command);
  }

  function trapDialogKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== 'Tab' || !launcherElement) return;
    const focusable = Array.from(
      launcherElement.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((element) => !element.hasAttribute('hidden'));
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function commandButton(command: ApplicationCommand, prefix = ''): string {
    const name = localizedCommandName(command, locale);
    return prefix ? `${prefix}: /${name}` : `/${name}`;
  }

  function appIcon(application: DirectoryApplicationSummary): string {
    return application.icon_hash
      ? assetUrl(application.icon_hash, 'thumbnail_128', application.origin_domain)
      : '';
  }

  function catalogAppLink(application: DirectoryApplicationSummary): string {
    return resolveApplicationDirectoryPath(directoryDetailPath(application.ref));
  }

  function destinationKey(installation: UserApplicationInstallation): string {
    return `${installation.id}\u0000${installation.application_ref}\u0000${installation.bot_user_ref}`;
  }

  function resolvedDestination(path: string): string {
    return path.startsWith('/application-directory/')
      ? resolveApplicationDirectoryPath(path)
      : resolve(path as `/applications/${string}/install/${string}`);
  }

  function loadInstalledDestination(
    installation: UserApplicationInstallation,
    expectedAccountRef: string,
    controller: AbortController,
    generation: number
  ): Promise<string> {
    const key = destinationKey(installation);
    const cached = destinationCache.get(key);
    if (cached) return Promise.resolve(cached);
    const existing = destinationRequests.get(key);
    if (existing) return existing;
    const request = api<unknown>(
      `/application-directory/bot-profiles/${encodeURIComponent(installation.bot_user_ref)}`,
      { signal: controller.signal }
    )
      .then((value) => {
        if (
          controller.signal.aborted ||
          generation !== destinationGeneration ||
          accountRef !== expectedAccountRef ||
          installation.user_ref !== expectedAccountRef
        ) {
          throw new DOMException('The app destination request is stale.', 'AbortError');
        }
        const profile = parseDirectoryBotProfileApplication(value, installation.bot_user_ref);
        const destination = profile && launcherInstallationDestination(installation, profile);
        if (!destination) throw new Error('The installed app destination did not match this app.');
        destinationCache.set(key, destination);
        return destination;
      })
      .finally(() => {
        if (destinationRequests.get(key) === request) destinationRequests.delete(key);
      });
    destinationRequests.set(key, request);
    return request;
  }

  async function openRecentApplication(recent: LauncherRecentApplication): Promise<void> {
    if (recent.command) {
      choose(recent.command);
      return;
    }
    const installation = recent.installation;
    const expectedAccountRef = accountRef;
    if (!installation || !expectedAccountRef || installation.user_ref !== expectedAccountRef) {
      recentActionError = 'This installed app is no longer available for this account.';
      return;
    }
    const controller = destinationController;
    const generation = destinationGeneration;
    recentActionBusy = recent.applicationRef;
    recentActionError = '';
    try {
      const destination = await loadInstalledDestination(
        installation,
        expectedAccountRef,
        controller,
        generation
      );
      if (
        controller.signal.aborted ||
        generation !== destinationGeneration ||
        accountRef !== expectedAccountRef
      ) {
        return;
      }
      await goto(resolvedDestination(destination));
      close(false);
    } catch (caught) {
      if (
        !controller.signal.aborted &&
        generation === destinationGeneration &&
        accountRef === expectedAccountRef
      ) {
        recentActionError = userErrorMessage(caught, 'Could not open this installed app.');
      }
    } finally {
      if (generation === destinationGeneration && recentActionBusy === recent.applicationRef) {
        recentActionBusy = null;
      }
    }
  }

  async function loadCatalog(
    search: string,
    domain: string,
    controller: AbortController,
    generation: number
  ): Promise<void> {
    try {
      const page = await api<DirectoryPage>(
        directoryQuery({ ...EMPTY_DIRECTORY_FILTERS, query: search, domain }),
        { signal: controller.signal }
      );
      if (controller.signal.aborted || generation !== catalogGeneration || !open) return;
      catalogPage = page;
    } catch (caught) {
      if (!controller.signal.aborted && generation === catalogGeneration && open) {
        catalogPage = null;
        catalogError = userErrorMessage(caught, 'Could not search the App Directory.');
      }
    } finally {
      if (!controller.signal.aborted && generation === catalogGeneration && open) {
        catalogLoading = false;
      }
    }
  }

  $effect(() => {
    if (open && !wasOpen) {
      returnFocusElement =
        typeof document !== 'undefined' && document.activeElement instanceof HTMLElement
          ? document.activeElement
          : triggerElement;
      query = '';
      refreshRecents();
      window.setTimeout(() => searchInput?.focus(), 0);
    }
    if (!open && wasOpen) query = '';
    wasOpen = open;
  });

  $effect(() => {
    const shouldLoad = open;
    const search = trimmedQuery;
    const requestedDomain = directoryDomain.trim();
    const domain = canonicalDirectoryDomain(requestedDomain);
    catalogController.abort();
    const controller = new AbortController();
    catalogController = controller;
    const generation = ++catalogGeneration;
    if (!shouldLoad) {
      catalogPage = null;
      catalogLoading = false;
      catalogError = '';
      return;
    }
    if (requestedDomain && !domain) {
      catalogPage = null;
      catalogLoading = false;
      catalogError = 'Enter a valid Directory instance domain.';
      return;
    }
    catalogPage = null;
    catalogLoading = true;
    catalogError = '';
    const timer = window.setTimeout(
      () => void loadCatalog(search, domain, controller, generation),
      search || domain ? 250 : 0
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  });

  $effect(() => {
    const shouldLoad = open;
    const expectedAccountRef = accountRef;
    installationController.abort();
    destinationController.abort();
    const controller = new AbortController();
    const lookupController = new AbortController();
    installationController = controller;
    destinationController = lookupController;
    const generation = ++installationGeneration;
    destinationGeneration += 1;
    destinationRequests.clear();
    destinationCache.clear();
    installations = [];
    installationsError = '';
    recentActionError = '';
    recentActionBusy = null;
    if (!shouldLoad || !expectedAccountRef) return;
    void listUserApplicationInstallations(controller.signal)
      .then((items) => {
        if (
          controller.signal.aborted ||
          generation !== installationGeneration ||
          !open ||
          accountRef !== expectedAccountRef
        ) {
          return;
        }
        installations = activeLauncherInstallations(expectedAccountRef, items);
      })
      .catch((caught: unknown) => {
        if (
          !controller.signal.aborted &&
          generation === installationGeneration &&
          open &&
          accountRef === expectedAccountRef
        ) {
          installationsError = userErrorMessage(
            caught,
            'Could not load installed apps. Commands and Directory discovery are still available.'
          );
        }
      });
    return () => {
      installationGeneration += 1;
      destinationGeneration += 1;
      controller.abort();
      lookupController.abort();
    };
  });

  $effect(() => {
    if (!open) return;
    const currentAccountRef = accountRef;
    const currentCommands = commands;
    const currentInstallations = installations;
    recentApplications =
      currentAccountRef && typeof localStorage !== 'undefined'
        ? launcherRecentApplications(
            currentAccountRef,
            currentCommands,
            currentInstallations,
            localStorage
          )
        : [];
  });

  onDestroy(() => {
    catalogGeneration += 1;
    catalogController.abort();
    installationGeneration += 1;
    installationController.abort();
    destinationGeneration += 1;
    destinationController.abort();
    destinationRequests.clear();
    destinationCache.clear();
  });
</script>

{#if showTrigger}
  <button
    bind:this={triggerElement}
    class="apps-button"
    type="button"
    {disabled}
    aria-label="Open Apps"
    aria-haspopup="dialog"
    aria-expanded={open}
    title="Apps"
    onclick={show}>Apps</button
  >
{/if}

{#if open}
  <div
    class="launcher-backdrop"
    role="presentation"
    onclick={(event) => {
      if (event.target === event.currentTarget) close();
    }}
    onkeydown={(event) => {
      if (event.key === 'Escape') close();
    }}
  >
    <div
      bind:this={launcherElement}
      class="launcher"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-labelledby="apps-launcher-title"
      onkeydown={trapDialogKeydown}
    >
      <header>
        <div>
          <small>Choose an app</small>
          <h2 id="apps-launcher-title">Apps</h2>
        </div>
        <button type="button" aria-label="Close Apps" onclick={() => close()}>×</button>
      </header>
      <div class="launcher-search-controls">
        <input
          bind:this={searchInput}
          bind:value={query}
          type="search"
          aria-label="Search installed commands and the App Directory"
          placeholder="Search apps and commands"
        />
        <label class="directory-instance">
          <span>Directory instance</span>
          <input
            bind:value={directoryDomain}
            aria-invalid={directoryDomain.trim() !== '' &&
              !canonicalDirectoryDomain(directoryDomain)}
            maxlength="253"
            placeholder="Local"
            autocapitalize="none"
            autocomplete="off"
            spellcheck="false"
          />
        </label>
      </div>
      <p class="visually-hidden" role="status" aria-live="polite">{resultStatus}</p>
      <div class="launcher-content">
        {#if !trimmedQuery && recentApplications.length}
          <section class="recents" aria-labelledby="launcher-recents-title">
            <h3 id="launcher-recents-title">Recents</h3>
            <div class="recent-grid">
              {#each recentApplications as recent (recent.applicationRef)}
                <button
                  type="button"
                  disabled={recentActionBusy === recent.applicationRef}
                  aria-busy={recentActionBusy === recent.applicationRef}
                  onclick={() => void openRecentApplication(recent)}
                >
                  <strong>{recent.applicationName}</strong>
                  <span
                    >{recent.command
                      ? commandButton(recent.command)
                      : recentActionBusy === recent.applicationRef
                        ? 'Opening app…'
                        : 'Installed app'}</span
                  >
                </button>
              {/each}
            </div>
            {#if recentActionError}<p class="catalog-error" role="alert">
                {recentActionError}
              </p>{/if}
          </section>
        {/if}

        {#if installationsError}<p class="catalog-error" role="status">{installationsError}</p>{/if}

        {#if groups.length}
          <section aria-labelledby="launcher-installed-title">
            <h3 id="launcher-installed-title">Your apps</h3>
            <div class="groups">
              {#each groups as group (group.applicationRef)}
                <section class="app-group" aria-label={group.applicationName}>
                  <h4>{group.applicationName}</h4>
                  <small>{group.applicationRef}</small>
                  {#each group.commands as command (`${command.application_ref}:${command.id}:${command.integration_type}:${command.interaction_context}`)}
                    <button type="button" onclick={() => choose(command)}>
                      <strong>{commandButton(command)}</strong>
                      <span
                        >{localizedCommandText(
                          command.description ?? 'Run command',
                          command.description_localizations,
                          locale
                        )}</span
                      >
                    </button>
                  {/each}
                </section>
              {/each}
            </div>
          </section>
        {/if}

        {#if trimmedQuery && searchedApplications.length}
          <section class="catalog" aria-labelledby="launcher-search-title">
            <h3 id="launcher-search-title">Apps from the Directory</h3>
            <div class="app-cards">
              {#each searchedApplications as application (application.ref)}
                {@const icon = appIcon(application)}
                <a href={catalogAppLink(application)} onclick={() => close(false)}>
                  <span class="app-icon">
                    {#if icon}<img src={icon} alt="" />{:else}{application.name
                        .slice(0, 1)
                        .toUpperCase()}{/if}
                  </span>
                  <span
                    ><strong>{application.name}</strong><small>{application.summary}</small></span
                  >
                </a>
              {/each}
            </div>
          </section>
        {:else if !trimmedQuery}
          {#each collectionGroups as group, index (group.collection?.slug ?? 'explore')}
            <section class="catalog" aria-labelledby={`launcher-collection-${index}`}>
              <div class="collection-heading">
                <h3 id={`launcher-collection-${index}`}>
                  {group.collection?.name ?? 'Explore apps'}
                </h3>
                {#if group.collection}<small>{group.collection.description}</small>{/if}
              </div>
              <div class="app-cards">
                {#each group.applications as application (application.ref)}
                  {@const icon = appIcon(application)}
                  <a href={catalogAppLink(application)} onclick={() => close(false)}>
                    <span class="app-icon">
                      {#if icon}<img src={icon} alt="" />{:else}{application.name
                          .slice(0, 1)
                          .toUpperCase()}{/if}
                    </span>
                    <span
                      ><strong>{application.name}</strong><small>{application.summary}</small></span
                    >
                  </a>
                {/each}
              </div>
            </section>
          {/each}
        {/if}

        {#if catalogLoading}<p class="catalog-state" role="status">Loading Directory apps…</p>{/if}
        {#if catalogError}<p class="catalog-error" role="status">{catalogError}</p>{/if}
        {#if !catalogLoading && !catalogError && trimmedQuery && !groups.length && !searchedApplications.length}
          <p class="catalog-state" role="status">No apps or commands match “{query}”.</p>
        {/if}
        {#if !catalogLoading && !catalogError && !trimmedQuery && !groups.length && !collectionGroups.length}
          <p class="catalog-state">No apps are available yet.</p>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .apps-button {
    min-width: 44px;
    height: 32px;
    flex: 0 0 auto;
    border: 0;
    border-radius: 8px;
    padding: 0 0.5rem;
    color: var(--text-muted);
    background: transparent;
    font-size: 0.72rem;
    font-weight: 800;
    cursor: pointer;
  }
  .apps-button:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
  }
  .apps-button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .launcher-backdrop {
    position: fixed;
    z-index: 250;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 1rem;
    background: color-mix(in srgb, #000 60%, transparent);
  }
  .launcher {
    width: min(38rem, 100%);
    max-height: min(46rem, 88vh);
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr);
    gap: 0.85rem;
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 1rem;
    color: var(--text);
    background: var(--surface);
    box-shadow: 0 24px 80px rgb(0 0 0 / 45%);
  }
  header,
  .collection-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  header h2,
  header small,
  h3,
  h4,
  .app-group > small,
  .catalog-state,
  .catalog-error,
  .collection-heading small {
    margin: 0;
  }
  header small,
  .app-group > small,
  .catalog-state,
  .collection-heading small {
    color: var(--text-muted);
  }
  header button {
    border: 0;
    border-radius: 8px;
    padding: 0.35rem 0.65rem;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.25rem;
    cursor: pointer;
  }
  input {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.7rem 0.8rem;
    color: var(--text);
    background: var(--surface-subtle);
  }
  .launcher-search-controls {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(10rem, 14rem);
    gap: 0.65rem;
    align-items: end;
  }
  .directory-instance {
    display: grid;
    gap: 0.25rem;
    color: var(--text-muted);
    font-size: 0.72rem;
    font-weight: 700;
  }
  .directory-instance input {
    padding-block: 0.62rem;
  }
  .launcher-content {
    display: grid;
    gap: 1rem;
    min-height: 8rem;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .launcher-content > section + section {
    border-top: 1px solid var(--line);
    padding-top: 1rem;
  }
  .recent-grid,
  .app-cards {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.5rem;
    margin-top: 0.55rem;
  }
  .recent-grid button,
  .app-cards a {
    display: grid;
    gap: 0.15rem;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.65rem 0.75rem;
    color: var(--text);
    background: var(--surface-subtle);
    text-align: left;
    text-decoration: none;
  }
  .recent-grid button {
    cursor: pointer;
    font: inherit;
  }
  .recent-grid span,
  .app-cards small,
  .app-group button span {
    overflow: hidden;
    color: var(--text-muted);
    font-size: 0.78rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .groups {
    margin-top: 0.35rem;
  }
  .app-group {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0.2rem 0.6rem;
    padding: 0.6rem 0;
  }
  .app-group + .app-group {
    border-top: 1px solid var(--line);
  }
  .app-group button {
    grid-column: 1 / -1;
    display: grid;
    gap: 0.15rem;
    border: 0;
    border-radius: 9px;
    padding: 0.65rem 0.75rem;
    color: var(--text);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }
  .app-group button:hover,
  .app-group button:focus-visible,
  .recent-grid button:hover,
  .recent-grid button:focus-visible,
  .app-cards a:hover,
  .app-cards a:focus-visible {
    background: var(--surface-hover);
  }
  .collection-heading {
    align-items: baseline;
  }
  .collection-heading small {
    overflow: hidden;
    max-width: 60%;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .app-cards a {
    grid-template-columns: 40px minmax(0, 1fr);
    align-items: center;
  }
  .app-cards a > span:last-child {
    display: grid;
    min-width: 0;
  }
  .app-icon {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    overflow: hidden;
    border-radius: 10px;
    color: white;
    background: var(--accent);
    font-weight: 800;
  }
  .app-icon img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .catalog-error {
    color: var(--danger);
  }
  @media (max-width: 520px) {
    .launcher-backdrop {
      align-items: end;
      padding: 0;
    }
    .launcher {
      width: 100%;
      max-height: 88vh;
      border-radius: 16px 16px 0 0;
    }
    .launcher-search-controls {
      grid-template-columns: 1fr;
    }
    .recent-grid,
    .app-cards {
      grid-template-columns: 1fr;
    }
  }
</style>
