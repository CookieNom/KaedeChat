<script lang="ts">
  import { resolve } from '$app/paths';
  import type { Guild } from '$lib/chat/types';
  import { entityKey } from '$lib/chat/refs';
  import {
    placeGuild,
    placeGuildAtTopLevel,
    placeGuildInGroup,
    moveGuildGroup,
    reconcileGuildNavigation,
    ungroupGuilds,
    updateGuildGroup,
    type GuildDropPosition,
    type GuildNavigationGroupItem
  } from '$lib/guild-navigation';
  import { assetUrl } from '$lib/media/assets';
  import { guildNavigation } from '$lib/stores/guild-navigation.svelte';
  import CreateGuildDialog from './CreateGuildDialog.svelte';
  import Icon from './Icon.svelte';

  let {
    guilds,
    homeHref,
    guildHref,
    mentionCount,
    homeActive = false,
    homeUnreadCount = 0,
    activeGuildKey = null
  }: {
    guilds: Guild[];
    homeHref: string;
    guildHref: (guild: Guild) => string;
    mentionCount: (guild: Guild) => number;
    homeActive?: boolean;
    homeUnreadCount?: number;
    activeGuildKey?: string | null;
  } = $props();

  const guildByRef = $derived(new Map(guilds.map((guild) => [entityKey(guild), guild])));
  const navigation = $derived(reconcileGuildNavigation(guildNavigation.navigation, guilds));
  let draggedGuild = $state<string | null>(null);
  let draggedGroup = $state<string | null>(null);
  let editingGroup = $state<GuildNavigationGroupItem | null>(null);
  let editingName = $state('');
  let railDropActive = $state(false);
  let createGuildOpen = $state(false);

  function compactBadge(count: number): string {
    return count > 99 ? '99+' : String(count);
  }

  function guildDragStart(event: DragEvent, guild: string) {
    if (guildNavigation.saving) return;
    draggedGuild = guild;
    event.dataTransfer?.setData('text/plain', guild);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function groupDragStart(event: DragEvent, groupId: string) {
    if (guildNavigation.saving) return;
    draggedGroup = groupId;
    event.dataTransfer?.setData('text/plain', `group:${groupId}`);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function guildDragEnd() {
    draggedGuild = null;
    draggedGroup = null;
    railDropActive = false;
  }

  function allowDrop(event: DragEvent) {
    if ((!draggedGuild && !draggedGroup) || guildNavigation.saving) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
  }

  function allowRailDrop(event: DragEvent) {
    if (event.target !== event.currentTarget || !draggedGuild || guildNavigation.saving) return;
    event.preventDefault();
    railDropActive = true;
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
  }

  function railDropIndex(event: DragEvent): number {
    const rail = event.currentTarget as HTMLElement;
    const items = rail.querySelectorAll<HTMLElement>(':scope > [data-navigation-index]');
    for (const item of items) {
      const bounds = item.getBoundingClientRect();
      if (event.clientY < bounds.top + bounds.height / 2) {
        return Number(item.dataset.navigationIndex ?? 0);
      }
    }
    return navigation.items.length;
  }

  function dropOnRail(event: DragEvent) {
    if (event.target !== event.currentTarget || !draggedGuild || guildNavigation.saving) return;
    event.preventDefault();
    const next = placeGuildAtTopLevel(navigation, draggedGuild, railDropIndex(event));
    guildDragEnd();
    void guildNavigation.save(next.items);
  }

  function dropPosition(event: DragEvent): GuildDropPosition {
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const ratio = bounds.height ? (event.clientY - bounds.top) / bounds.height : 0.5;
    if (ratio < 0.27) return 'before';
    if (ratio > 0.73) return 'after';
    return 'inside';
  }

  function dropOnGuild(event: DragEvent, target: string) {
    if (draggedGroup) {
      event.preventDefault();
      const next = moveGuildGroup(
        navigation,
        draggedGroup,
        target,
        dropPosition(event) !== 'before'
      );
      guildDragEnd();
      void guildNavigation.save(next.items);
      return;
    }
    if (!draggedGuild || draggedGuild === target || guildNavigation.saving) return;
    event.preventDefault();
    const next = placeGuild(
      navigation,
      draggedGuild,
      target,
      dropPosition(event),
      crypto.randomUUID()
    );
    guildDragEnd();
    void guildNavigation.save(next.items);
  }

  function dropOnGroup(event: DragEvent, groupId: string) {
    if (draggedGroup) {
      if (draggedGroup === groupId) return;
      event.preventDefault();
      const next = moveGuildGroup(navigation, draggedGroup, groupId, false);
      guildDragEnd();
      void guildNavigation.save(next.items);
      return;
    }
    if (!draggedGuild || guildNavigation.saving) return;
    event.preventDefault();
    const next = placeGuildInGroup(navigation, draggedGuild, groupId);
    guildDragEnd();
    void guildNavigation.save(next.items);
  }

  function toggleGroup(group: GuildNavigationGroupItem) {
    if (guildNavigation.saving) return;
    const next = updateGuildGroup(navigation, group.id, { collapsed: !group.collapsed });
    void guildNavigation.save(next.items);
  }

  function openGroupEditor(event: MouseEvent, group: GuildNavigationGroupItem) {
    event.preventDefault();
    editingGroup = group;
    editingName = group.name;
  }

  function closeGroupEditor() {
    editingGroup = null;
    editingName = '';
  }

  function saveGroupName() {
    if (!editingGroup || !editingName.trim() || guildNavigation.saving) return;
    const next = updateGuildGroup(navigation, editingGroup.id, {
      name: editingName.trim().slice(0, 32)
    });
    closeGroupEditor();
    void guildNavigation.save(next.items);
  }

  function removeGroup() {
    if (!editingGroup || guildNavigation.saving) return;
    const next = ungroupGuilds(navigation, editingGroup.id);
    closeGroupEditor();
    void guildNavigation.save(next.items);
  }
</script>

<nav
  class:rail-drop-active={railDropActive}
  class="guild-spine"
  aria-label="Guilds"
  ondragover={allowRailDrop}
  ondragleave={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) railDropActive = false;
  }}
  ondrop={dropOnRail}
>
  <a
    class:active={homeActive}
    class="spine-home"
    href={resolve(homeHref as '/home')}
    aria-label={homeUnreadCount ? `Home, ${homeUnreadCount} unread direct messages` : 'Home'}
    aria-current={homeActive ? 'page' : undefined}
    title="Home"
  >
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 10.5 12 4l8 6.5v8a1.5 1.5 0 0 1-1.5 1.5h-4v-6h-5v6h-4A1.5 1.5 0 0 1 4 18.5z" />
    </svg>
    {#if homeUnreadCount}
      <small class="rail-unread">{compactBadge(homeUnreadCount)}</small>
    {/if}
  </a>
  <div class="spine-separator" aria-hidden="true"></div>

  {#each navigation.items as item, itemIndex (item.kind === 'guild' ? item.guild : item.id)}
    {#if item.kind === 'guild'}
      {@const guild = guildByRef.get(item.guild)}
      {#if guild}
        {@const mentions = mentionCount(guild)}
        <a
          data-navigation-index={itemIndex}
          class:active={activeGuildKey === item.guild}
          class:dragging={draggedGuild === item.guild}
          href={resolve(guildHref(guild) as `/g/${string}/${string}`)}
          draggable={!guildNavigation.saving}
          aria-label={mentions ? `${guild.name}, ${mentions} mentions` : guild.name}
          aria-current={activeGuildKey === item.guild ? 'page' : undefined}
          title={`${guild.name} · drag to reorder or group`}
          ondragstart={(event) => guildDragStart(event, item.guild)}
          ondragend={guildDragEnd}
          ondragover={allowDrop}
          ondrop={(event) => dropOnGuild(event, item.guild)}
        >
          {#if guild.icon_hash}
            <img src={assetUrl(guild.icon_hash, 'thumbnail_128', guild)} alt="" />
          {:else}
            {guild.name.slice(0, 2).toUpperCase()}
          {/if}
          {#if mentions}<small class="rail-unread">{compactBadge(mentions)}</small>{/if}
        </a>
      {/if}
    {:else}
      {@const groupGuilds = item.guilds.flatMap((ref) => {
        const guild = guildByRef.get(ref);
        return guild ? [{ ref, guild }] : [];
      })}
      <section
        data-navigation-index={itemIndex}
        class:active={item.guilds.includes(activeGuildKey ?? '')}
        class:collapsed={item.collapsed}
        class="guild-folder"
        role="group"
        draggable={!guildNavigation.saving}
        aria-label={item.name}
        ondragover={allowDrop}
        ondrop={(event) => dropOnGroup(event, item.id)}
        oncontextmenu={(event) => openGroupEditor(event, item)}
        ondragstart={(event) => groupDragStart(event, item.id)}
        ondragend={guildDragEnd}
      >
        <button
          class="guild-folder-toggle"
          type="button"
          aria-expanded={!item.collapsed}
          aria-label={`${item.collapsed ? 'Open' : 'Close'} ${item.name}`}
          title={`${item.name} · right-click to rename or remove`}
          onclick={() => toggleGroup(item)}
        >
          <span class="folder-preview" aria-hidden="true">
            {#each groupGuilds.slice(0, 4) as member (member.ref)}
              {#if member.guild.icon_hash}
                <img src={assetUrl(member.guild.icon_hash, 'thumbnail_128', member.guild)} alt="" />
              {:else}
                <i>{member.guild.name.slice(0, 1).toUpperCase()}</i>
              {/if}
            {/each}
          </span>
        </button>
        {#if !item.collapsed}
          <div class="folder-members">
            {#each groupGuilds as member (member.ref)}
              {@const mentions = mentionCount(member.guild)}
              <a
                class:active={activeGuildKey === member.ref}
                class:dragging={draggedGuild === member.ref}
                href={resolve(guildHref(member.guild) as `/g/${string}/${string}`)}
                draggable={!guildNavigation.saving}
                aria-label={mentions
                  ? `${member.guild.name}, ${mentions} mentions`
                  : member.guild.name}
                aria-current={activeGuildKey === member.ref ? 'page' : undefined}
                title={`${member.guild.name} · drag into a rail gap to remove from folder`}
                ondragstart={(event) => {
                  event.stopPropagation();
                  guildDragStart(event, member.ref);
                }}
                ondragend={guildDragEnd}
                ondragover={allowDrop}
                ondrop={(event) => dropOnGuild(event, member.ref)}
              >
                {#if member.guild.icon_hash}
                  <img
                    src={assetUrl(member.guild.icon_hash, 'thumbnail_128', member.guild)}
                    alt=""
                  />
                {:else}
                  {member.guild.name.slice(0, 2).toUpperCase()}
                {/if}
                {#if mentions}<small class="rail-unread">{compactBadge(mentions)}</small>{/if}
              </a>
            {/each}
          </div>
        {/if}
      </section>
    {/if}
  {/each}

  <button
    class="spine-create"
    type="button"
    aria-label="Create a guild"
    title="Create a guild"
    onclick={() => (createGuildOpen = true)}
  >
    <Icon name="plus" size={23} strokeWidth={2.2} />
  </button>

  {#if guildNavigation.error}
    <button
      class="guild-navigation-error"
      type="button"
      title={guildNavigation.error}
      aria-label={`${guildNavigation.error} Retry loading guild organization.`}
      onclick={() => void guildNavigation.load(true)}>!</button
    >
  {/if}
</nav>

<CreateGuildDialog bind:open={createGuildOpen} />

{#if editingGroup}
  <dialog
    open
    class="guild-group-dialog-backdrop"
    aria-label="Edit guild group"
    onclick={(event) => {
      if (event.target === event.currentTarget) closeGroupEditor();
    }}
  >
    <section class="guild-group-dialog" aria-labelledby="guild-group-title">
      <h2 id="guild-group-title">Edit guild group</h2>
      <label>
        Group name
        <input
          bind:value={editingName}
          maxlength="32"
          onkeydown={(event) => {
            if (event.key === 'Enter') saveGroupName();
            if (event.key === 'Escape') closeGroupEditor();
          }}
        />
      </label>
      <p>Drag guilds onto the folder to add them. Drag them outside the folder to remove them.</p>
      <div>
        <button class="danger-button" type="button" onclick={removeGroup}>Ungroup guilds</button>
        <button class="secondary-button" type="button" onclick={closeGroupEditor}>Cancel</button>
        <button type="button" disabled={!editingName.trim()} onclick={saveGroupName}>Save</button>
      </div>
    </section>
  </dialog>
{/if}

<style>
  .guild-folder {
    position: relative;
    display: grid;
    flex: 0 0 auto;
    justify-items: center;
    gap: 0.55rem;
    width: 58px;
    border-radius: 20px;
    padding: 5px;
    background: color-mix(in srgb, var(--rail-hover) 82%, transparent);
  }

  .guild-spine.rail-drop-active {
    box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--accent) 70%, transparent);
  }

  .spine-create {
    display: grid;
    width: 48px;
    height: 48px;
    min-width: 48px;
    min-height: 48px;
    flex: 0 0 auto;
    place-items: center;
    padding: 0;
    color: var(--accent-text);
    border: 0;
    border-radius: 17px;
    background: var(--rail-hover);
    cursor: pointer;
    transition:
      border-radius 150ms ease,
      color 150ms ease,
      background-color 150ms ease,
      transform 150ms ease;
  }

  .spine-create:hover {
    color: var(--on-accent);
    border-radius: 13px;
    background: var(--accent);
    transform: translateY(-1px);
  }

  .spine-create:focus-visible {
    outline: 2px solid var(--rail-text);
    outline-offset: 2px;
  }

  .spine-create :global(svg) {
    fill: none;
  }

  .guild-folder.active {
    box-shadow: inset 3px 0 var(--rail-text);
  }

  .guild-folder.collapsed {
    padding: 5px;
  }

  .guild-folder-toggle {
    display: grid;
    width: 48px;
    height: 48px;
    place-items: center;
    border: 0;
    border-radius: 15px;
    padding: 6px;
    color: var(--rail-text);
    background: color-mix(in srgb, var(--accent) 18%, var(--rail-hover));
    cursor: pointer;
  }

  .folder-preview {
    display: grid;
    width: 36px;
    height: 36px;
    grid-template-columns: repeat(2, 1fr);
    grid-template-rows: repeat(2, 1fr);
    gap: 2px;
    overflow: hidden;
  }

  .folder-preview img,
  .folder-preview i {
    position: static;
    display: grid;
    width: 17px;
    height: 17px;
    min-width: 17px;
    min-height: 17px;
    border-radius: 5px;
    place-items: center;
    object-fit: cover;
    font-size: 0.55rem;
    font-style: normal;
    background: var(--rail);
  }

  .folder-members {
    display: grid;
    gap: 0.55rem;
  }

  .folder-members a {
    position: relative;
    display: grid;
    width: 48px;
    height: 48px;
    place-items: center;
    overflow: visible;
    border-radius: 17px;
    color: var(--rail-text);
    background: var(--rail-hover);
    font-family: var(--font-display);
    font-size: 0.76rem;
    font-weight: 820;
    text-decoration: none;
  }

  .folder-members a.active {
    border-radius: 13px;
    outline: 2px solid var(--accent);
  }

  .folder-members a img {
    position: absolute;
    inset: 0;
    display: block;
    width: 100%;
    height: 100%;
    min-width: 100%;
    min-height: 100%;
    max-width: 100%;
    max-height: 100%;
    border-radius: inherit;
    object-fit: cover;
    object-position: center;
  }

  .dragging {
    opacity: 0.42;
  }

  .guild-navigation-error {
    width: 34px;
    height: 34px;
    flex: 0 0 auto;
    border: 1px solid var(--danger);
    border-radius: 999px;
    color: var(--danger);
    background: transparent;
    font-weight: 900;
    cursor: pointer;
  }

  .guild-group-dialog-backdrop {
    position: fixed;
    z-index: 200;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 20px;
    width: 100%;
    height: 100%;
    max-width: none;
    max-height: none;
    border: 0;
    margin: 0;
    background: rgb(0 0 0 / 62%);
  }

  .guild-group-dialog {
    width: min(420px, 100%);
    border: 1px solid var(--line);
    border-radius: 20px;
    padding: 22px;
    background: var(--panel);
    box-shadow: var(--shadow-lg);
  }

  .guild-group-dialog h2 {
    margin: 0 0 1rem;
  }

  .guild-group-dialog label {
    display: grid;
    gap: 0.45rem;
    font-weight: 760;
  }

  .guild-group-dialog input {
    width: 100%;
  }

  .guild-group-dialog p {
    color: var(--muted);
    line-height: 1.45;
  }

  .guild-group-dialog > div {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.65rem;
  }
</style>
