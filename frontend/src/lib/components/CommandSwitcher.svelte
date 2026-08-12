<script lang="ts">
  import { afterNavigate, goto } from '$app/navigation';
  import { api, userErrorMessage } from '$lib/api/client';
  import type { Channel, Guild } from '$lib/chat/types';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import { recordNavigation } from '$lib/navigation/history';
  import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
  import { onMount, tick } from 'svelte';

  interface Destination {
    path: string;
    label: string;
    detail: string;
  }

  let open = $state(false);
  let query = $state('');
  let destinations = $state<Destination[]>([]);
  let active = $state(0);
  let input = $state<HTMLInputElement | null>(null);
  let dialog = $state<HTMLElement | null>(null);
  let optionsElement = $state<HTMLElement | null>(null);
  let loading = $state(false);
  let loaded = $state(false);
  let loadError = $state('');
  let requestGeneration = 0;
  let destinationAbort: AbortController | null = null;
  let previousFocus: HTMLElement | null = null;
  const filtered = $derived(
    destinations
      .filter((item) =>
        `${item.label} ${item.detail}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())
      )
      .slice(0, 15)
  );

  async function show() {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    open = true;
    query = '';
    active = 0;
    destinations = [];
    loaded = false;
    loadError = '';
    const destinationRequest = loadDestinations();
    await tick();
    input?.focus();
    await destinationRequest;
  }

  async function loadDestinations() {
    destinationAbort?.abort();
    const controller = new AbortController();
    destinationAbort = controller;
    const generation = ++requestGeneration;
    loading = true;
    loaded = false;
    loadError = '';
    try {
      const [guilds, dms] = await Promise.all([
        api<Guild[]>('/users/@me/guilds', { signal: controller.signal }),
        api<Channel[]>('/users/@me/channels', { signal: controller.signal })
      ]);
      if (controller.signal.aborted || generation !== requestGeneration) return;
      destinations = [
        ...guilds.flatMap((guild) =>
          (guild.channels ?? [])
            .filter((channel) => channel.type === 0 || channel.type === 2 || channel.type === 5)
            .map((channel) => ({
              path: guildChannelPath(guild, channel),
              label: `${channel.type === 2 ? 'Voice' : '#'} ${channel.name ?? 'channel'}`,
              detail: guild.name
            }))
        ),
        ...dms.map((channel) => ({
          path: directMessagePath(channel),
          label: userDisplayName(channel.recipients?.[0]) || 'Direct message',
          detail: channel.recipients?.[0]
            ? (userPublicHandle(channel.recipients[0]) ?? 'Profile unavailable')
            : 'Direct message'
        }))
      ];
      loaded = true;
    } catch (caught) {
      if (!controller.signal.aborted && generation === requestGeneration) {
        destinations = [];
        loaded = true;
        loadError = userErrorMessage(
          caught,
          'Could not load your channels and conversations. Try again.'
        );
      }
    } finally {
      if (generation === requestGeneration) loading = false;
    }
  }

  function hide(restoreFocus = true) {
    if (!open) return;
    const focusTarget = restoreFocus ? previousFocus : null;
    open = false;
    destinationAbort?.abort();
    destinationAbort = null;
    requestGeneration += 1;
    loading = false;
    previousFocus = null;
    if (focusTarget) {
      void tick().then(() => {
        if (focusTarget.isConnected) focusTarget.focus({ preventScroll: true });
      });
    }
  }

  function choose(destination: Destination) {
    hide(false);
    // eslint-disable-next-line svelte/no-navigation-without-resolve -- destinations are built exclusively from resolved route templates above
    void goto(destination.path);
  }

  function globalKeydown(event: KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === 'k') {
      event.preventDefault();
      if (open) hide();
      else void show();
    } else if (event.altKey && event.key === 'ArrowLeft') {
      event.preventDefault();
      history.back();
    } else if (event.altKey && event.key === 'ArrowRight') {
      event.preventDefault();
      history.forward();
    } else if (event.key === 'Escape' && open) {
      event.preventDefault();
      hide();
    }
  }

  function dialogKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      hide();
      return;
    }
    if (event.key === 'Tab') {
      const focusable = Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'input, button:not([disabled]):not([tabindex="-1"])'
        ) ?? []
      );
      if (!focusable.length) return;
      const current = focusable.indexOf(document.activeElement as HTMLElement);
      const next = event.shiftKey
        ? current <= 0
          ? focusable.length - 1
          : current - 1
        : current < 0 || current === focusable.length - 1
          ? 0
          : current + 1;
      focusable[next].focus();
      event.preventDefault();
      return;
    }
    if (event.isComposing) return;
    if (!filtered.length) return;
    if (event.key === 'ArrowDown') setActive((active + 1) % filtered.length);
    else if (event.key === 'ArrowUp') setActive((active - 1 + filtered.length) % filtered.length);
    else if (event.key === 'Home') setActive(0);
    else if (event.key === 'End') setActive(filtered.length - 1);
    else if (event.key === 'Enter') {
      const destination = filtered[active] ?? filtered[0];
      if (destination) choose(destination);
    } else return;
    event.preventDefault();
  }

  function setActive(index: number) {
    const next = filtered.length ? Math.max(0, Math.min(index, filtered.length - 1)) : 0;
    active = next;
    if (filtered.length) void revealActiveOption(next);
  }

  async function revealActiveOption(index: number) {
    await tick();
    const option = optionsElement?.querySelector<HTMLElement>(`[data-option-index="${index}"]`);
    option?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }

  function retryLoad() {
    input?.focus();
    void loadDestinations();
  }

  $effect(() => {
    void query;
    void filtered.length;
    setActive(0);
  });

  afterNavigate(({ to }) => {
    if (to?.url) recordNavigation(localStorage, to.url.pathname);
  });

  onMount(() => {
    const openFromControl = () => void show();
    window.addEventListener('keydown', globalKeydown);
    window.addEventListener('kaede:open-command-switcher', openFromControl);
    return () => {
      window.removeEventListener('keydown', globalKeydown);
      window.removeEventListener('kaede:open-command-switcher', openFromControl);
      destinationAbort?.abort();
    };
  });
</script>

{#if open}
  <div class="command-backdrop" role="presentation" onclick={() => hide()}>
    <div
      bind:this={dialog}
      class="command-switcher"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label="Switch channel"
      onclick={(event) => event.stopPropagation()}
      onkeydown={dialogKeydown}
    >
      <input
        bind:this={input}
        bind:value={query}
        aria-label="Find a channel"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded="true"
        aria-controls="command-switcher-options"
        aria-activedescendant={filtered[active] ? `command-switcher-option-${active}` : undefined}
        aria-describedby={loading || loadError || (loaded && !filtered.length)
          ? 'command-switcher-status'
          : undefined}
        placeholder="Jump to a channel…"
      />
      <div
        bind:this={optionsElement}
        id="command-switcher-options"
        role="listbox"
        aria-label="Channels"
        aria-busy={loading}
      >
        {#each filtered as destination, index (destination.path)}
          <button
            type="button"
            id={`command-switcher-option-${index}`}
            data-option-index={index}
            role="option"
            tabindex="-1"
            aria-selected={index === active}
            class:active={index === active}
            onmouseenter={() => setActive(index)}
            onclick={() => choose(destination)}
          >
            <strong>{destination.label}</strong><small>{destination.detail}</small>
          </button>
        {/each}
      </div>
      {#if loading}
        <p id="command-switcher-status" role="status" aria-live="polite">Loading channels…</p>
      {:else if loadError}
        <div id="command-switcher-status" role="alert">
          <p>{loadError}</p>
          <button type="button" onclick={retryLoad}>Try again</button>
        </div>
      {:else if loaded && !filtered.length}
        <p id="command-switcher-status" role="status">
          {destinations.length
            ? `No results for “${query}”.`
            : 'You do not have any channels or conversations yet.'}
        </p>
      {/if}
    </div>
  </div>
{/if}
