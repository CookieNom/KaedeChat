<script lang="ts">
  import { resolve } from '$app/paths';
  import { page } from '$app/state';
  import { api, userErrorMessage } from '$lib/api/client';
  import { selectedPermissionMetadata } from '$lib/chat/permission-selection';
  import { hasAllPermissions } from '$lib/chat/permissions';
  import { entityRef } from '$lib/chat/refs';
  import { isThreadChannel } from '$lib/chat/threads';
  import type { Guild } from '$lib/chat/types';
  import ApplicationCommandPermissions from '$lib/components/ApplicationCommandPermissions.svelte';
  import BotE2eeParticipation from '$lib/components/BotE2eeParticipation.svelte';
  import GuildAnnouncementFollows from '$lib/components/GuildAnnouncementFollows.svelte';
  import GuildWebhooks from '$lib/components/GuildWebhooks.svelte';
  import { Permission } from '$lib/generated/permissions';
  import { chatEntities as entities } from '$lib/stores/entities.svelte';
  import { onDestroy, untrack } from 'svelte';
  import { SvelteMap, SvelteSet } from 'svelte/reactivity';

  interface Installation {
    id: string;
    status: string;
    scopes: string[];
    intents: string[];
    permissions: string;
    channel_restrictions: string[];
    e2ee_mode: string;
    grant_revision: string;
    installed_at: string;
    application: {
      ref: string;
      name: string;
      description: string | null;
      origin_domain: string;
      bot_user: { username: string; display_name: string | null; handle: string };
    };
  }

  const guildRef = $derived(page.params.guildId ?? '');
  let guild = $state<Guild | null>(null);
  let installations = $state<Installation[]>([]);
  let error = $state('');
  let notice = $state('');
  let busyRef = $state('');
  let botsLoading = $state(false);
  let loadedGuildRef = $state('');
  let loadController = new AbortController();
  let loadGeneration = 0;
  let observedGuildProjectionRef = '';
  let revokedGuildAccessRef = '';
  const routeIsLoaded = $derived(loadedGuildRef === guildRef);
  const normalizedGuild = $derived(entities.guilds.get(guildRef) ?? null);
  const canManageCommandPermissions = $derived(
    hasAllPermissions(
      permissionBits(guild?.permissions),
      Permission.MANAGE_GUILD | Permission.MANAGE_ROLES
    )
  );
  const canManageGuild = $derived(
    hasAllPermissions(permissionBits(guild?.permissions), Permission.MANAGE_GUILD)
  );
  const canManageWebhooks = $derived(
    hasAllPermissions(permissionBits(guild?.permissions), Permission.MANAGE_WEBHOOKS)
  );
  const availableGuilds = $derived.by(() => {
    const byRef = new SvelteMap(entities.guilds.values.map((item) => [entityRef(item), item]));
    if (guild) byRef.set(entityRef(guild), guild);
    return [...byRef.values()];
  });
  const restrictionChannels = $derived(
    (guild?.channels ?? [])
      .filter((channel) => !isThreadChannel(channel))
      .toSorted((left, right) => left.position - right.position || left.id.localeCompare(right.id))
  );

  function permissionBits(value: string | undefined): bigint {
    try {
      return BigInt(value ?? '0');
    } catch {
      return 0n;
    }
  }

  function loadIsCurrent(
    targetGuildRef: string,
    controller: AbortController,
    generation: number
  ): boolean {
    return (
      !controller.signal.aborted &&
      loadedGuildRef === targetGuildRef &&
      guildRef === targetGuildRef &&
      loadGeneration === generation
    );
  }

  function revokeGuildIntegrationsAccess(targetGuildRef: string) {
    if (revokedGuildAccessRef === targetGuildRef) return;
    revokedGuildAccessRef = targetGuildRef;
    loadGeneration += 1;
    loadController.abort();
    guild = null;
    installations = [];
    busyRef = '';
    botsLoading = false;
    notice = '';
    error = 'This guild is unavailable or you no longer have access.';
    window.location.assign(resolve('/home'));
  }

  async function load(targetGuildRef: string) {
    loadController.abort();
    const controller = new AbortController();
    loadController = controller;
    const generation = ++loadGeneration;
    error = '';
    botsLoading = false;
    try {
      const loadedGuild = await api<Guild>(`/guilds/${encodeURIComponent(targetGuildRef)}`, {
        signal: controller.signal
      });
      if (!loadIsCurrent(targetGuildRef, controller, generation)) return;
      entities.ingestGuilds([loadedGuild]);
      guild = loadedGuild;
      installations = [];
      if (hasAllPermissions(permissionBits(loadedGuild.permissions), Permission.MANAGE_GUILD)) {
        botsLoading = true;
        try {
          const loadedInstallations = await api<Installation[]>(
            `/guilds/${encodeURIComponent(targetGuildRef)}/integrations/bots`,
            { signal: controller.signal }
          );
          if (!loadIsCurrent(targetGuildRef, controller, generation)) return;
          installations = loadedInstallations;
        } catch (caught) {
          if (loadIsCurrent(targetGuildRef, controller, generation)) {
            error = userErrorMessage(caught, 'Could not load bot integrations for this guild.');
          }
        } finally {
          if (loadIsCurrent(targetGuildRef, controller, generation)) botsLoading = false;
        }
      }
    } catch (caught) {
      if (loadIsCurrent(targetGuildRef, controller, generation)) {
        error = userErrorMessage(caught, 'Could not load integrations for this guild.');
      }
    }
  }
  async function remove(installation: Installation) {
    const targetGuildRef = loadedGuildRef;
    const loadedGuild = guild;
    if (
      !loadedGuild ||
      busyRef ||
      targetGuildRef !== guildRef ||
      !installations.includes(installation) ||
      !confirm(`Remove ${installation.application.name} from ${loadedGuild.name}?`)
    )
      return;
    const operation = `${targetGuildRef}:${installation.application.ref}`;
    const signal = loadController.signal;
    busyRef = operation;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(targetGuildRef)}/integrations/bots/${encodeURIComponent(installation.application.ref)}`,
        { method: 'DELETE', signal }
      );
      if (
        signal.aborted ||
        loadedGuildRef !== targetGuildRef ||
        guildRef !== targetGuildRef ||
        guild !== loadedGuild
      )
        return;
      installations = installations.filter((item) => item.id !== installation.id);
      notice = `${installation.application.name} was removed. Its future access is revoked.`;
    } catch (caught) {
      if (
        !signal.aborted &&
        loadedGuildRef === targetGuildRef &&
        guildRef === targetGuildRef &&
        guild === loadedGuild
      ) {
        error = userErrorMessage(caught, 'Could not remove the bot.');
      }
    } finally {
      if (
        !signal.aborted &&
        loadedGuildRef === targetGuildRef &&
        guildRef === targetGuildRef &&
        busyRef === operation
      )
        busyRef = '';
    }
  }

  function setChannelRestriction(installation: Installation, ref: string, enabled: boolean) {
    const selected = new SvelteSet(installation.channel_restrictions);
    if (enabled) selected.add(ref);
    else selected.delete(ref);
    const channel_restrictions = [...selected];
    installations = installations.map((item) =>
      item.id === installation.id ? { ...item, channel_restrictions } : item
    );
  }

  async function saveChannelRestrictions(installation: Installation) {
    const targetGuildRef = loadedGuildRef;
    const loadedGuild = guild;
    if (!loadedGuild || busyRef || targetGuildRef !== guildRef) return;
    const operation = `${targetGuildRef}:${installation.application.ref}:channels`;
    const signal = loadController.signal;
    busyRef = operation;
    error = '';
    try {
      const updated = await api<{
        status: Installation['status'];
        channel_restrictions: string[];
        grant_revision: string;
      }>(
        `/guilds/${encodeURIComponent(targetGuildRef)}/integrations/bots/${encodeURIComponent(installation.application.ref)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            channel_restrictions: installation.channel_restrictions
          }),
          signal
        }
      );
      if (
        signal.aborted ||
        loadedGuildRef !== targetGuildRef ||
        guildRef !== targetGuildRef ||
        guild !== loadedGuild
      )
        return;
      installations = installations.map((item) =>
        item.id === installation.id
          ? {
              ...item,
              status: updated.status,
              channel_restrictions: updated.channel_restrictions,
              grant_revision: updated.grant_revision
            }
          : item
      );
      notice = updated.channel_restrictions.length
        ? `${installation.application.name} is now limited to the selected channels and categories.`
        : `${installation.application.name} can now use every channel allowed by its role.`;
    } catch (caught) {
      if (
        !signal.aborted &&
        loadedGuildRef === targetGuildRef &&
        guildRef === targetGuildRef &&
        guild === loadedGuild
      ) {
        error = userErrorMessage(caught, 'Could not update the bot’s channel access.');
      }
    } finally {
      if (
        !signal.aborted &&
        loadedGuildRef === targetGuildRef &&
        guildRef === targetGuildRef &&
        busyRef === operation
      )
        busyRef = '';
    }
  }

  $effect(() => {
    const targetGuildRef = guildRef;
    const projection = normalizedGuild;
    if (!projection) {
      if (observedGuildProjectionRef === targetGuildRef) {
        untrack(() => revokeGuildIntegrationsAccess(targetGuildRef));
      }
      return;
    }
    observedGuildProjectionRef = targetGuildRef;
    if (revokedGuildAccessRef === targetGuildRef) revokedGuildAccessRef = '';
  });

  $effect(() => {
    const targetGuildRef = guildRef;
    if (targetGuildRef === loadedGuildRef) return;
    loadedGuildRef = targetGuildRef;
    guild = null;
    installations = [];
    error = '';
    notice = '';
    busyRef = '';
    botsLoading = false;
    void load(targetGuildRef);
  });

  onDestroy(() => {
    loadGeneration += 1;
    loadController.abort();
  });
</script>

<svelte:head
  ><title>Integrations · {routeIsLoaded ? (guild?.name ?? 'Guild') : 'Guild'} · Kaede Chat</title
  ></svelte:head
>
<main>
  <header>
    <div>
      <a href={resolve(`/g/${encodeURIComponent(guildRef)}/settings`)}>← Guild settings</a><span
        >Guild integrations</span
      >
      <h1>Integrations</h1>
      <p>{routeIsLoaded ? (guild?.name ?? guildRef) : guildRef}</p>
    </div>
  </header>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
      class="notice"
    >
      {notice}
    </div>{/if}
  {#if !guild || !routeIsLoaded}
    <p>Loading integrations…</p>
  {:else}
    <nav class="integration-nav" aria-label="Integration types">
      <a href="#bots-apps">Bots &amp; Apps</a>
      <a href="#webhooks">Webhooks</a>
      <a href="#channels-followed">Channels Followed</a>
    </nav>
    <section id="bots-apps" class="intro">
      <span>Bots and apps</span>
      <h2>Installed bots and apps</h2>
      <p>
        Each bot keeps only the scopes, event intents, role permissions, channel access, and
        encryption mode approved for this guild. Removing a bot revokes future API and Gateway
        access.
      </p>
    </section>
    {#if !canManageGuild}
      <section class="empty">
        <strong>Manage Server is required</strong>
        <p>Bot installation details are visible to members who can manage this server.</p>
      </section>
    {:else if botsLoading}
      <section class="empty" role="status">Loading bots and apps…</section>
    {:else if installations.length === 0}
      <section class="empty">
        <strong>No bots or apps installed</strong>
        <p>Open a bot invite link to add one. You will review its access before installation.</p>
      </section>
    {:else}<div class="list">
        {#each installations as installation (installation.id)}<article>
            <div class="identity">
              <span class="avatar">{installation.application.name.slice(0, 1).toUpperCase()}</span>
              <div>
                <strong>{installation.application.name}</strong><small
                  >{installation.application.bot_user.handle} · {installation.application
                    .origin_domain}</small
                >
                <p>{installation.application.description ?? 'No description provided.'}</p>
              </div>
            </div>
            <div class="details">
              <span>{installation.status}</span><span
                >{installation.e2ee_mode.replaceAll('_', ' ')}</span
              ><span>{installation.scopes.length} scopes</span><span
                >{installation.intents.length} intents</span
              >
            </div>
            <details>
              <summary>Approved access</summary>
              <h3>Scopes</h3>
              <div class="pills">
                {#each installation.scopes as scope (scope)}<span>{scope}</span>{/each}
              </div>
              <h3>Live events</h3>
              <div class="pills">
                {#each installation.intents as intent (intent)}<span>{intent}</span>{/each}
              </div>
              <h3>Guild permissions</h3>
              {#if selectedPermissionMetadata(installation.permissions).length}
                <div class="pills">
                  {#each selectedPermissionMetadata(installation.permissions) as permission (permission.permission)}
                    <span title={permission.description}>{permission.label}</span>
                  {/each}
                </div>
              {:else}
                <p>No guild permissions approved.</p>
              {/if}
            </details>
            <details class="channel-access">
              <summary>Channel access</summary>
              <p>
                This is an installation-wide ceiling in addition to the bot role and channel
                overrides. Selecting a category includes its child channels. Leave every option
                clear to allow all channels permitted by the role.
              </p>
              <div class="channel-access-heading">
                <strong
                  >{installation.channel_restrictions.length
                    ? `${installation.channel_restrictions.length} selected`
                    : 'All role-permitted channels'}</strong
                >
                {#if installation.channel_restrictions.length}
                  <button
                    type="button"
                    disabled={busyRef !== ''}
                    onclick={() => {
                      installations = installations.map((item) =>
                        item.id === installation.id ? { ...item, channel_restrictions: [] } : item
                      );
                    }}>Allow all</button
                  >
                {/if}
              </div>
              <fieldset disabled={busyRef !== ''}>
                <legend>Allowed channels and categories</legend>
                {#each restrictionChannels as channel (entityRef(channel))}
                  <label>
                    <input
                      type="checkbox"
                      checked={installation.channel_restrictions.includes(entityRef(channel))}
                      onchange={(event) =>
                        setChannelRestriction(
                          installation,
                          entityRef(channel),
                          event.currentTarget.checked
                        )}
                    />
                    {channel.type === 4 ? 'Category' : 'Channel'} · {channel.name ?? channel.id}
                  </label>
                {/each}
              </fieldset>
              <button
                type="button"
                disabled={busyRef !== ''}
                onclick={() => void saveChannelRestrictions(installation)}
              >
                {busyRef === `${loadedGuildRef}:${installation.application.ref}:channels`
                  ? 'Saving…'
                  : 'Save channel access'}
              </button>
            </details>
            <ApplicationCommandPermissions
              {guildRef}
              applicationRef={installation.application.ref}
              roles={guild?.roles ?? []}
              channels={guild?.channels ?? []}
              canManage={canManageCommandPermissions}
            />
            {#if installation.e2ee_mode === 'participant'}
              <BotE2eeParticipation
                {guildRef}
                applicationRef={installation.application.ref}
                applicationName={installation.application.name}
                channels={guild?.channels ?? []}
                canManage={canManageGuild}
              />
            {/if}
            <footer>
              <small>Installed {new Date(installation.installed_at).toLocaleString()}</small><button
                disabled={busyRef === `${loadedGuildRef}:${installation.application.ref}`}
                onclick={() => remove(installation)}
                >{busyRef === `${loadedGuildRef}:${installation.application.ref}`
                  ? 'Removing…'
                  : 'Remove bot'}</button
              >
            </footer>
          </article>{/each}
      </div>
    {/if}
    <GuildWebhooks {guild} canManage={canManageWebhooks} />
    <GuildAnnouncementFollows {guild} guilds={availableGuilds} />
  {/if}
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  main {
    box-sizing: border-box;
    width: min(960px, 100%);
    min-height: 100dvh;
    margin: auto;
    padding: clamp(1rem, 5vw, 4rem);
    color: var(--text);
  }
  main > header span {
    display: block;
    margin-top: 1.2rem;
    color: var(--accent);
    font-size: 0.75rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  h1,
  h2,
  p {
    margin: 0.25rem 0;
  }
  a {
    color: var(--accent);
  }
  main > header p,
  .intro p,
  small,
  article p,
  .empty p {
    color: var(--text-muted);
  }
  .intro,
  article,
  .empty,
  .notice {
    border: 1px solid var(--line);
    border-radius: 13px;
    padding: 1rem;
    background: var(--surface);
  }
  .intro {
    margin: 1.5rem 0;
  }
  .intro > span {
    color: var(--text-muted);
    font-size: 0.75rem;
    font-weight: 750;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .integration-nav {
    display: flex;
    gap: 0.65rem;
    flex-wrap: wrap;
    margin-top: 1.4rem;
  }
  .integration-nav a {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.48rem 0.75rem;
    color: var(--text);
    background: var(--surface);
    font-weight: 750;
    text-decoration: none;
  }
  .list {
    display: grid;
    gap: 0.8rem;
  }
  .identity,
  article footer,
  .details,
  .pills {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }
  .identity {
    align-items: flex-start;
  }
  .identity div {
    min-width: 0;
  }
  .identity strong,
  .identity small {
    display: block;
  }
  .avatar {
    display: grid;
    flex: 0 0 52px;
    height: 52px;
    place-items: center;
    border-radius: 13px;
    color: white;
    background: var(--accent);
    font-size: 1.3rem;
    font-weight: 850;
  }
  .details {
    margin: 0.9rem 0;
  }
  .details span,
  .pills span {
    border-radius: 999px;
    padding: 0.25rem 0.55rem;
    background: var(--surface-hover);
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  details {
    border-top: 1px solid var(--line);
    padding-top: 0.7rem;
  }
  .channel-access p {
    margin: 0.65rem 0;
  }
  .channel-access-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin: 0.65rem 0;
  }
  .channel-access fieldset {
    display: grid;
    max-height: 18rem;
    gap: 0.45rem;
    margin: 0.65rem 0;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.75rem;
  }
  .channel-access label {
    display: flex;
    align-items: center;
    gap: 0.55rem;
  }
  summary {
    cursor: pointer;
    font-weight: 750;
  }
  h3 {
    margin: 0.8rem 0 0.35rem;
    font-size: 0.8rem;
  }
  article footer {
    justify-content: space-between;
    margin-top: 1rem;
  }
  article button {
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 0.65rem 0.8rem;
    color: var(--accent);
    background: transparent;
    font: inherit;
    font-weight: 800;
  }
  article footer button {
    border-color: var(--danger, #d84a4a);
    color: var(--danger, #ef6767);
  }
  article button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  .notice {
    margin-bottom: 1rem;
  }
  .error {
    color: var(--danger, #ef6767);
  }
</style>
