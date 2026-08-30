<script lang="ts">
  import { localizedCommandName } from '$lib/chat/application-commands';
  import {
    appContextCommandHistory,
    appContextCommandMenuModel,
    type AppContextCommandEntry
  } from '$lib/chat/context-commands';
  import Icon from './Icon.svelte';
  import { tick } from 'svelte';

  let {
    id,
    entries,
    accountRef = null,
    onSelect,
    menuItem = false
  }: {
    id: string;
    entries: AppContextCommandEntry[];
    accountRef?: string | null;
    onSelect: (entry: AppContextCommandEntry) => void;
    menuItem?: boolean;
  } = $props();

  let open = $state(false);
  let root = $state<HTMLElement | null>(null);
  let trigger = $state<HTMLButtonElement | null>(null);
  let submenu = $state<HTMLElement | null>(null);
  let searchInput = $state<HTMLInputElement | null>(null);
  let query = $state('');
  let history = $state<string[]>([]);
  const menuModel = $derived(appContextCommandMenuModel(entries, query, history));

  function items(): HTMLButtonElement[] {
    return Array.from(submenu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
  }

  function openMenu(focusSearch = false) {
    if (!entries.length) return;
    open = true;
    query = '';
    history = accountRef ? appContextCommandHistory(accountRef) : [];
    if (focusSearch) void tick().then(() => searchInput?.focus());
  }

  function closeMenu(focusTrigger = false) {
    open = false;
    query = '';
    if (focusTrigger) void tick().then(() => trigger?.focus());
  }

  function triggerKeydown(event: KeyboardEvent) {
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      event.stopPropagation();
      openMenu(true);
    } else if (event.key === 'Escape' && open) {
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
    }
  }

  function submenuKeydown(event: KeyboardEvent) {
    const buttons = items();
    const fromSearch = event.target === searchInput;
    if (fromSearch) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeMenu(true);
      } else if (buttons.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        event.stopPropagation();
        buttons[event.key === 'ArrowDown' ? 0 : buttons.length - 1]?.focus();
      }
      return;
    }
    if (!buttons.length) {
      if (event.key === 'Escape' || event.key === 'ArrowLeft') {
        event.preventDefault();
        event.stopPropagation();
        closeMenu(true);
      }
      return;
    }
    const current = buttons.findIndex((button) => button === document.activeElement);
    let next = current;
    if (event.key === 'ArrowDown') next = current < 0 ? 0 : (current + 1) % buttons.length;
    else if (event.key === 'ArrowUp')
      next = current < 0 ? buttons.length - 1 : (current - 1 + buttons.length) % buttons.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = buttons.length - 1;
    else if (event.key === 'ArrowLeft' || event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
      return;
    } else return;
    event.preventDefault();
    event.stopPropagation();
    buttons[next]?.focus();
  }
</script>

{#snippet commandButton(entry: AppContextCommandEntry)}
  <button
    type="button"
    role="menuitem"
    tabindex="-1"
    onclick={(event) => {
      event.stopPropagation();
      closeMenu(false);
      onSelect(entry);
    }}
  >
    <span>{localizedCommandName(entry.command)}</span>
    <small>{entry.detail}</small>
  </button>
{/snippet}

<svelte:window
  onpointerdown={(event) => {
    if (open && !root?.contains(event.target as Node)) closeMenu(false);
  }}
/>

{#if entries.length}
  <div bind:this={root} class:profile={!menuItem} class="app-context-command-menu">
    <button
      bind:this={trigger}
      type="button"
      role={menuItem ? 'menuitem' : undefined}
      tabindex={menuItem ? -1 : 0}
      aria-haspopup="menu"
      aria-expanded={open}
      aria-controls={`${id}-submenu`}
      onclick={(event) => {
        event.stopPropagation();
        if (open) closeMenu(false);
        else openMenu(false);
      }}
      onkeydown={triggerKeydown}
    >
      <Icon name="sparkles" size={17} />
      <span>Apps</span>
      <span class="submenu-arrow" aria-hidden="true">›</span>
    </button>
    {#if open}
      <div
        bind:this={submenu}
        id={`${id}-submenu`}
        class="app-command-submenu"
        role="menu"
        tabindex="-1"
        aria-label="Apps"
        onkeydown={submenuKeydown}
      >
        <label class="command-search">
          <Icon name="search" size={15} />
          <input
            bind:this={searchInput}
            bind:value={query}
            type="search"
            autocomplete="off"
            aria-label="Search app commands"
            placeholder="Search commands"
          />
        </label>
        {#if menuModel.frequent.length}
          <section
            class="app-command-group frequent"
            role="group"
            aria-labelledby={`${id}-frequent`}
          >
            <h3 id={`${id}-frequent`}>Frequently Used</h3>
            {#each menuModel.frequent as entry (entry.key)}
              {@render commandButton(entry)}
            {/each}
          </section>
        {/if}
        {#each menuModel.groups as group, groupIndex (group.applicationRef)}
          <section
            class="app-command-group"
            role="group"
            aria-labelledby={`${id}-app-${groupIndex}`}
          >
            <h3 id={`${id}-app-${groupIndex}`}>{group.applicationName}</h3>
            {#each group.entries as entry (entry.key)}
              {@render commandButton(entry)}
            {/each}
          </section>
        {/each}
        {#if !menuModel.frequent.length && !menuModel.groups.length}
          <p class="no-command-results" role="status">No commands match “{query}”.</p>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .app-context-command-menu {
    position: relative;
    display: grid;
    width: 100%;
  }

  .submenu-arrow {
    margin-left: auto;
    font-size: 1.15rem;
    line-height: 1;
  }

  .app-command-submenu {
    display: grid;
    gap: 0.15rem;
    margin: 0.1rem 0 0.15rem 0.6rem;
    border-left: 1px solid var(--line-soft);
    padding-left: 0.35rem;
  }

  .command-search {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 7px;
    margin: 0.15rem 0.2rem 0.35rem;
    padding: 0.35rem 0.45rem;
    color: var(--text-muted);
    background: var(--surface-subtle);
  }

  .command-search:focus-within {
    border-color: color-mix(in srgb, var(--accent) 65%, var(--line));
  }

  .command-search input {
    width: 100%;
    min-width: 0;
    border: 0;
    outline: 0;
    padding: 0;
    color: var(--text);
    background: transparent;
    font: inherit;
  }

  .app-command-group {
    display: grid;
    gap: 0.15rem;
  }

  .app-command-group + .app-command-group {
    border-top: 1px solid var(--line-soft);
    margin-top: 0.25rem;
    padding-top: 0.25rem;
  }

  .app-command-group h3,
  .no-command-results {
    margin: 0;
    padding: 0.25rem 0.5rem;
    color: var(--text-muted);
    font-size: 0.62rem;
    font-weight: 760;
  }

  .app-command-submenu button {
    display: grid;
    justify-items: start;
  }

  .app-command-submenu small {
    overflow: hidden;
    width: 100%;
    color: var(--text-muted);
    font-size: 0.62rem;
    font-weight: 560;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .profile > button,
  .profile .app-command-submenu button {
    display: flex;
    width: 100%;
    min-width: 0;
    min-height: 38px;
    align-items: center;
    justify-content: center;
    gap: 7px;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0 12px;
    color: var(--text);
    background: var(--surface-subtle);
    font-size: 0.7rem;
    font-weight: 720;
    white-space: nowrap;
    cursor: pointer;
  }

  .profile .app-command-submenu {
    grid-column: 1 / -1;
    margin: 6px 0 0;
    border: 0;
    padding: 0;
  }

  .profile .command-search {
    margin-right: 0;
    margin-left: 0;
  }

  .profile .app-command-submenu button {
    display: grid;
    justify-items: start;
  }

  .profile > button:hover,
  .profile > button:focus-visible,
  .profile .app-command-submenu button:hover,
  .profile .app-command-submenu button:focus-visible {
    border-color: color-mix(in srgb, var(--text-muted) 52%, var(--line));
    background: var(--surface-hover);
  }
</style>
