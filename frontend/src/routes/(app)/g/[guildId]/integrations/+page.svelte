<script lang="ts">
  import Toast from '$lib/components/Toast.svelte';
  import { t } from '$lib/ui/locale';

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
  let savedRestrictions = $state<Record<string, string>>({});
  const restrictionDraft = (installation: Installation) =>
    JSON.stringify([...installation.channel_restrictions].sort());
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
    error = $t('ui_this_guild_is_unavailable_or_you_no_longer_ha_70ba0e65');
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
          savedRestrictions = Object.fromEntries(
            loadedInstallations.map((item) => [item.id, restrictionDraft(item)])
          );
        } catch (caught) {
          if (loadIsCurrent(targetGuildRef, controller, generation)) {
            error = userErrorMessage(
              caught,
              $t('ui_could_not_load_bot_integrations_for_this_guil_c99c5da4')
            );
          }
        } finally {
          if (loadIsCurrent(targetGuildRef, controller, generation)) botsLoading = false;
        }
      }
    } catch (caught) {
      if (loadIsCurrent(targetGuildRef, controller, generation)) {
        error = userErrorMessage(
          caught,
          $t('ui_could_not_load_integrations_for_this_guild_db1bca6f')
        );
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
        error = userErrorMessage(caught, $t('ui_could_not_remove_the_bot_2d564666'));
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
    if (savedRestrictions[installation.id] === restrictionDraft(installation)) return;
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
      savedRestrictions[installation.id] = JSON.stringify([...updated.channel_restrictions].sort());
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
        error = userErrorMessage(
          caught,
          $t('ui_could_not_update_the_bot_s_channel_access_56720b8c')
        );
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

<Toast message={notice} onDismiss={() => (notice = '')} />

<svelte:head
  ><title
    >{$t('ui_integrations_value0_kaede_chat_20fe2f45', {
      value0: String(routeIsLoaded ? (guild?.name ?? 'Guild') : 'Guild')
    })}</title
  ></svelte:head
>
<main>
  <header>
    <div>
      <a href={resolve(`/g/${encodeURIComponent(guildRef)}/settings`)}
        >{$t('ui_guild_settings_30da895f')}</a
      ><span>{$t('ui_guild_integrations_ce0e63b1')}</span>
      <h1>{$t('ui_integrations_090512d9')}</h1>
      <p>{routeIsLoaded ? (guild?.name ?? guildRef) : guildRef}</p>
    </div>
  </header>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
      class="notice"
    >
      {notice}
    </div>{/if}
  {#if !guild || !routeIsLoaded}
    <p>{$t('ui_loading_integrations_191441df')}</p>
  {:else}
    <nav class="integration-nav" aria-label={$t('ui_integration_types_e5be4ada')}>
      <a href="#bots-apps">{$t('ui_bots_apps_2b96a588')}</a>
      <a href="#webhooks">{$t('ui_webhooks_45808d75')}</a>
      <a href="#channels-followed">{$t('ui_channels_followed_23902d9d')}</a>
    </nav>
    <section id="bots-apps" class="intro">
      <span>{$t('ui_bots_and_apps_75083834')}</span>
      <h2>{$t('ui_installed_bots_and_apps_6f59b877')}</h2>
      <p>{$t('ui_each_bot_keeps_only_the_scopes_event_intents__7155cc97')}</p>
    </section>
    {#if !canManageGuild}
      <section class="empty">
        <strong>{$t('ui_manage_server_is_required_151ee3c1')}</strong>
        <p>{$t('ui_bot_installation_details_are_visible_to_membe_90f8d61f')}</p>
      </section>
    {:else if botsLoading}
      <section class="empty" role="status">{$t('ui_loading_bots_and_apps_6ae74c6f')}</section>
    {:else if installations.length === 0}
      <section class="empty">
        <strong>{$t('ui_no_bots_or_apps_installed_3a39141a')}</strong>
        <p>{$t('ui_open_a_bot_invite_link_to_add_one_you_will_re_831fa648')}</p>
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
                <p>
                  {installation.application.description ??
                    $t('ui_no_description_provided_2527a18a')}
                </p>
              </div>
            </div>
            <div class="details">
              <span>{installation.status}</span><span
                >{installation.e2ee_mode.replaceAll('_', ' ')}</span
              ><span
                >{$t('ui_value0_scopes_776b226f', {
                  value0: String(installation.scopes.length)
                })}</span
              ><span
                >{$t('ui_value0_intents_650b668d', {
                  value0: String(installation.intents.length)
                })}</span
              >
            </div>
            <details>
              <summary>{$t('ui_approved_access_cc93c1b9')}</summary>
              <h3>{$t('ui_scopes_0d5644ff')}</h3>
              <div class="pills">
                {#each installation.scopes as scope (scope)}<span>{scope}</span>{/each}
              </div>
              <h3>{$t('ui_live_events_b7a9f551')}</h3>
              <div class="pills">
                {#each installation.intents as intent (intent)}<span>{intent}</span>{/each}
              </div>
              <h3>{$t('ui_bot_initial_permissions')}</h3>
              <p>{$t('ui_bot_edit_role_permissions')}</p>
              {#if selectedPermissionMetadata(installation.permissions).length}
                <div class="pills">
                  {#each selectedPermissionMetadata(installation.permissions) as permission (permission.permission)}
                    <span title={permission.description}>{permission.label}</span>
                  {/each}
                </div>
              {:else}
                <p>{$t('ui_no_guild_permissions_approved_81abf779')}</p>
              {/if}
            </details>
            <details class="channel-access">
              <summary>{$t('ui_channel_access_34e631c3')}</summary>
              <p>{$t('ui_this_is_an_installation_wide_ceiling_in_addit_eb7a998f')}</p>
              <div class="channel-access-heading">
                <strong
                  >{installation.channel_restrictions.length
                    ? `${installation.channel_restrictions.length} selected`
                    : $t('ui_all_role_permitted_channels_4115543b')}</strong
                >
                {#if installation.channel_restrictions.length}
                  <button
                    type="button"
                    disabled={busyRef !== ''}
                    onclick={() => {
                      installations = installations.map((item) =>
                        item.id === installation.id ? { ...item, channel_restrictions: [] } : item
                      );
                    }}>{$t('ui_allow_all_56ac845a')}</button
                  >
                {/if}
              </div>
              <fieldset disabled={busyRef !== ''}>
                <legend>{$t('ui_allowed_channels_and_categories_8ed2bf18')}</legend>
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
                    {channel.type === 4 ? $t('ui_category_292c06f0') : $t('ui_channel_ce4683e7')} · {channel.name ??
                      channel.id}
                  </label>
                {/each}
              </fieldset>
              <button
                type="button"
                disabled={busyRef !== '' ||
                  savedRestrictions[installation.id] === restrictionDraft(installation)}
                onclick={() => void saveChannelRestrictions(installation)}
              >
                {busyRef === `${loadedGuildRef}:${installation.application.ref}:channels`
                  ? $t('ui_saving_23e39291')
                  : $t('ui_save_channel_access_30bd74a5')}
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
              <small
                >{$t('ui_installed_value0_3d93ef12', {
                  value0: String(new Date(installation.installed_at).toLocaleString())
                })}</small
              ><button
                disabled={busyRef === `${loadedGuildRef}:${installation.application.ref}`}
                onclick={() => remove(installation)}
                >{busyRef === `${loadedGuildRef}:${installation.application.ref}`
                  ? $t('ui_removing_d4b09919')
                  : $t('ui_remove_bot_5c58ac60')}</button
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
