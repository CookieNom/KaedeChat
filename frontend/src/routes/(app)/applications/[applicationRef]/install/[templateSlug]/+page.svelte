<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { api, userErrorMessage } from '$lib/api/client';
  import { resolve } from '$app/paths';
  import { Permission } from '$lib/generated/permissions';
  import { selectedPermissionMetadata } from '$lib/chat/permission-selection';
  import {
    installUserApplication,
    userApplicationGrantFromPolicy,
    type UserApplicationContext
  } from '$lib/chat/application-installations';
  import { onDestroy } from 'svelte';
  let { data } = $props<{
    data: { applicationRef: string; templateSlug: string; returnTo: string };
  }>();
  interface Guild {
    id: string;
    origin_domain: string;
    name: string;
    icon_hash: string | null;
    permissions?: string;
  }
  interface Invite {
    application: {
      id: string;
      origin_domain: string;
      ref?: string;
      name: string;
      description: string | null;
      support_url?: string | null;
      privacy_url?: string | null;
      supported_install_types: Array<'guild_install' | 'user_install'>;
      user_install_scopes: string[];
      user_install_contexts: UserApplicationContext[];
      bot_user: { username: string; display_name: string | null; handle?: string };
    };
    template: {
      name: string;
      description: string | null;
      scopes: string[];
      intents: string[];
      permissions: string;
      e2ee_mode: string;
    };
  }
  let invite = $state<Invite | null>(null);
  let guilds = $state<Guild[]>([]);
  let selected = $state('');
  let error = $state('');
  let busy = $state(false);
  let installed = $state(false);
  let userInstalled = $state(false);
  let personalBusy = $state(false);
  let selectedUserContexts = $state<UserApplicationContext[]>([]);
  let loading = $state(true);
  let guildsLoading = $state(false);
  let guildsError = $state('');
  let loadedApplicationRef = $state('');
  let loadedTemplateSlug = $state('');
  let controller = new AbortController();
  let requestGeneration = 0;
  const routeIsLoaded = $derived(
    loadedApplicationRef === data.applicationRef && loadedTemplateSlug === data.templateSlug
  );
  const userContextLabels: Record<UserApplicationContext, string> = {
    guild: 'Guild channels',
    private_channel: 'Private conversations and group DMs',
    bot_dm: 'Direct messages with the app bot'
  };

  function toggleUserContext(context: UserApplicationContext) {
    selectedUserContexts = selectedUserContexts.includes(context)
      ? selectedUserContexts.filter((item) => item !== context)
      : [...selectedUserContexts, context];
  }

  function requestIsCurrent(
    signal: AbortSignal,
    generation: number,
    applicationRef: string,
    templateSlug: string
  ): boolean {
    return (
      !signal.aborted &&
      requestGeneration === generation &&
      loadedApplicationRef === applicationRef &&
      loadedTemplateSlug === templateSlug &&
      data.applicationRef === applicationRef &&
      data.templateSlug === templateSlug
    );
  }

  function applicationRefFor(invitePayload: Invite): string {
    return (
      invitePayload.application.ref ??
      `${invitePayload.application.id}@${invitePayload.application.origin_domain}`
    );
  }

  function loadedInviteIsCurrent(
    applicationRef: string,
    templateSlug: string,
    loadedInvite: Invite
  ): boolean {
    return (
      loadedApplicationRef === applicationRef &&
      loadedTemplateSlug === templateSlug &&
      data.applicationRef === applicationRef &&
      data.templateSlug === templateSlug &&
      invite === loadedInvite
    );
  }

  async function load(
    applicationRef: string,
    templateSlug: string,
    signal: AbortSignal,
    generation: number
  ) {
    try {
      const resolvedInvite = await api<Invite>(
        `/bot-invites/${encodeURIComponent(applicationRef)}/${encodeURIComponent(templateSlug)}`,
        { signal }
      );
      if (!requestIsCurrent(signal, generation, applicationRef, templateSlug)) return;
      if (applicationRefFor(resolvedInvite) !== applicationRef) {
        throw new Error($t('ui_the_resolved_invitation_did_not_match_the_req_40abcd8b'));
      }
      invite = resolvedInvite;
      selectedUserContexts = [...resolvedInvite.application.user_install_contexts];
      loading = false;

      if (!resolvedInvite.application.supported_install_types.includes('guild_install')) return;
      guildsLoading = true;
      try {
        const memberships = await api<Guild[]>('/users/@me/guilds', { signal });
        if (!requestIsCurrent(signal, generation, applicationRef, templateSlug)) return;
        guilds = memberships.filter((guild) => {
          try {
            const permissions = BigInt(guild.permissions ?? '0');
            return Boolean(permissions & (Permission.MANAGE_GUILD | Permission.ADMINISTRATOR));
          } catch {
            return false;
          }
        });
        if (guilds.length) selected = `${guilds[0].id}@${guilds[0].origin_domain}`;
      } catch (caught) {
        if (requestIsCurrent(signal, generation, applicationRef, templateSlug)) {
          guildsError = userErrorMessage(
            caught,
            resolvedInvite.application.supported_install_types.includes('user_install')
              ? $t('ui_your_guilds_could_not_be_loaded_account_insta_cb9f43f0')
              : $t('ui_your_guilds_could_not_be_loaded_reload_this_p_d7992ba4')
          );
        }
      } finally {
        if (requestIsCurrent(signal, generation, applicationRef, templateSlug)) {
          guildsLoading = false;
        }
      }
    } catch (caught) {
      if (requestIsCurrent(signal, generation, applicationRef, templateSlug)) {
        error = userErrorMessage(caught, $t('ui_this_bot_invitation_is_unavailable_b2641d14'));
        loading = false;
      }
    }
  }
  async function install() {
    const applicationRef = loadedApplicationRef;
    const templateSlug = loadedTemplateSlug;
    const loadedInvite = invite;
    const selectedGuild = selected;
    if (
      !selectedGuild ||
      busy ||
      !loadedInvite ||
      !applicationRef ||
      !templateSlug ||
      !routeIsLoaded ||
      applicationRefFor(loadedInvite) !== applicationRef
    )
      return;
    busy = true;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(selectedGuild)}/integrations/bots?application_ref=${encodeURIComponent(applicationRef)}&template_slug=${encodeURIComponent(templateSlug)}`,
        { method: 'POST' }
      );
      if (loadedInviteIsCurrent(applicationRef, templateSlug, loadedInvite)) {
        installed = true;
      }
    } catch (caught) {
      if (loadedInviteIsCurrent(applicationRef, templateSlug, loadedInvite)) {
        error = userErrorMessage(
          caught,
          $t('ui_the_bot_could_not_be_added_to_that_guild_fb9971c2')
        );
      }
    } finally {
      if (loadedInviteIsCurrent(applicationRef, templateSlug, loadedInvite)) {
        busy = false;
      }
    }
  }
  async function installForUser() {
    const applicationRef = loadedApplicationRef;
    const templateSlug = loadedTemplateSlug;
    const loadedInvite = invite;
    if (
      personalBusy ||
      !loadedInvite ||
      !applicationRef ||
      !templateSlug ||
      !routeIsLoaded ||
      applicationRefFor(loadedInvite) !== applicationRef
    )
      return;
    personalBusy = true;
    error = '';
    try {
      await installUserApplication(applicationRef, {
        ...userApplicationGrantFromPolicy(loadedInvite.application),
        contexts: [...selectedUserContexts]
      });
      if (loadedInviteIsCurrent(applicationRef, templateSlug, loadedInvite)) {
        userInstalled = true;
      }
    } catch (caught) {
      if (loadedInviteIsCurrent(applicationRef, templateSlug, loadedInvite)) {
        error = userErrorMessage(
          caught,
          $t('ui_this_app_does_not_currently_offer_commands_th_06ac1b41')
        );
      }
    } finally {
      if (loadedInviteIsCurrent(applicationRef, templateSlug, loadedInvite)) {
        personalBusy = false;
      }
    }
  }

  $effect(() => {
    const applicationRef = data.applicationRef;
    const templateSlug = data.templateSlug;
    if (applicationRef === loadedApplicationRef && templateSlug === loadedTemplateSlug) return;

    loadedApplicationRef = applicationRef;
    loadedTemplateSlug = templateSlug;
    controller.abort();
    controller = new AbortController();
    const generation = ++requestGeneration;
    invite = null;
    guilds = [];
    selected = '';
    selectedUserContexts = [];
    installed = false;
    userInstalled = false;
    busy = false;
    personalBusy = false;
    loading = true;
    guildsLoading = false;
    guildsError = '';
    error = '';
    void load(applicationRef, templateSlug, controller.signal, generation);
  });

  onDestroy(() => {
    requestGeneration += 1;
    controller.abort();
  });
</script>

<svelte:head
  ><title
    >{$t('ui_value0_kaede_chat_4bf52868', {
      value0: String(invite?.application.name ?? 'Add App')
    })}</title
  ></svelte:head
>
<!-- eslint-disable svelte/no-navigation-without-resolve -- privacy and support destinations are external URLs supplied by the application -->
<main>
  <a class="back" href={resolve(data.returnTo as '/home')}>{$t('ui_back_to_kaede_bfbafc02')}</a
  >{#if error}<div class="notice error" role="alert">
      {error}
    </div>{/if}{#if loading || !routeIsLoaded}<div class="notice" role="status">
      {$t('ui_loading_app_authorization_547b7dfd')}
    </div>{:else if invite}<article class="invite">
      <header>
        <span class="avatar">{invite.application.name.slice(0, 1).toUpperCase()}</span>
        <div>
          <small>{$t('ui_app_authorization_08b9aae0')}</small>
          <h1>{invite.application.name}</h1>
          <p>
            {invite.application.bot_user.handle ??
              `${invite.application.bot_user.username}@${invite.application.origin_domain}`}
          </p>
        </div>
      </header>
      <p class="description">
        {invite.application.description ??
          invite.template.description ??
          $t('ui_this_bot_has_not_provided_a_description_529a7f45')}
      </p>
      {#if invite.application.supported_install_types.includes('guild_install')}<section>
          <h2>{$t('ui_add_to_a_guild_5b54c3b9')}</h2>
          {#if guildsError}<p class="notice error" role="alert">{guildsError}</p>{/if}
          {#if guildsLoading}<p class="muted" role="status">
              {$t('ui_loading_your_guilds_678f2416')}
            </p>{/if}
          {#if installed}<div class="success">
              <strong>{$t('ui_bot_added_0e121074')}</strong>
              <p>{$t('ui_the_bot_is_now_a_visible_member_of_the_select_b25e909a')}</p>
            </div>{:else}<label
              >{$t('ui_guild_298ffc49')}<select bind:value={selected} disabled={busy}
                >{#each guilds as guild (`${guild.id}@${guild.origin_domain}`)}<option
                    value={`${guild.id}@${guild.origin_domain}`}
                    >{guild.name} · {guild.origin_domain}</option
                  >{/each}</select
              ></label
            >{#if !guildsLoading && !guildsError && guilds.length === 0}<p class="muted">
                {$t('ui_you_do_not_have_any_guilds_available_for_inst_7290a07f')}
              </p>{/if}{/if}
        </section>{/if}
      {#if invite.application.supported_install_types.includes('user_install')}<section>
          <h2>{$t('ui_install_for_your_account_15133076')}</h2>
          {#if userInstalled}
            <div class="success">
              <strong>{$t('ui_installed_for_your_account_75042a0e')}</strong>
              <p>{$t('ui_this_app_s_user_installable_commands_can_appe_f9c95fb1')}</p>
            </div>
          {:else}
            <p class="muted">{$t('ui_authorize_this_app_for_your_account_without_a_8275e81f')}</p>
            <fieldset class="context-options">
              <legend>{$t('ui_use_commands_in_a6990ab8')}</legend>
              {#each invite.application.user_install_contexts as context (context)}
                <label>
                  <input
                    type="checkbox"
                    checked={selectedUserContexts.includes(context)}
                    onchange={() => toggleUserContext(context)}
                  />
                  {userContextLabels[context]}
                </label>
              {/each}
            </fieldset>
            <button
              class="personal-install"
              type="button"
              disabled={personalBusy || selectedUserContexts.length === 0}
              onclick={() => void installForUser()}
            >
              {personalBusy
                ? $t('ui_authorizing_a4dfff4b')
                : $t('ui_authorize_for_my_account_bfe5140e')}
            </button>
          {/if}
        </section>{/if}
      {#if invite.application.supported_install_types.includes('guild_install')}
        <section>
          <h2>{$t('ui_guild_installation_access_a1118c3d')}</h2>
          <div class="pills">
            {#each invite.template.scopes as scope (scope)}<span>{scope}</span>{/each}
          </div>
          <details>
            <summary>{$t('ui_live_event_intents_016f4f45')}</summary>
            <div class="pills">
              {#each invite.template.intents as intent (intent)}<span>{intent}</span>{/each}
            </div>
          </details>
          <details>
            <summary>{$t('ui_server_permissions_d2673811')}</summary>
            {#if selectedPermissionMetadata(invite.template.permissions).length}
              <div class="pills">
                {#each selectedPermissionMetadata(invite.template.permissions) as permission (permission.permission)}
                  <span title={permission.description}>{permission.label}</span>
                {/each}
              </div>
            {:else}
              <p class="muted">{$t('ui_no_server_permissions_requested_b58f7e3b')}</p>
            {/if}
          </details>
        </section>
      {/if}
      {#if invite.application.supported_install_types.includes('user_install')}
        <section>
          <h2>{$t('ui_account_installation_access_5f48d7fe')}</h2>
          <p class="muted">{$t('ui_commands_and_responses_only_this_does_not_add_3b9a8417')}</p>
          <div class="pills">
            {#each invite.application.user_install_scopes as scope (scope)}<span>{scope}</span
              >{/each}
            <span>interactions</span>
          </div>
          <details>
            <summary>{$t('ui_supported_command_locations_49519617')}</summary>
            <div class="pills">
              {#each invite.application.user_install_contexts as context (context)}
                <span>{userContextLabels[context]}</span>
              {/each}
            </div>
          </details>
        </section>
      {/if}
      <section class="privacy">
        <h2>{$t('ui_encryption_and_privacy_63e025d3')}</h2>
        {#if invite.application.supported_install_types.includes('guild_install')}
          {#if invite.template.e2ee_mode === 'participant'}<p>
              <strong>{$t('ui_guild_install_97f108da')}</strong>
              {$t('ui_this_bot_may_become_an_e2ee_participant_in_e2_cb40d44b')}
            </p>{:else}<p>{$t('ui_the_guild_install_has_no_access_to_e2ee_chann_827450bb')}</p>{/if}
        {/if}
        {#if invite.application.supported_install_types.includes('user_install')}
          <p>
            <strong>{$t('ui_account_install_126dc88c')}</strong>
            {$t('ui_the_app_receives_only_interactions_you_explic_3385a600')}
          </p>
        {/if}
        <p>{$t('ui_for_plaintext_channels_the_bot_can_access_onl_f23ea870')}</p>
      </section>
      <footer>
        <div>
          <a
            href={invite.application.privacy_url ?? '#'}
            aria-disabled={!invite.application.privacy_url}>{$t('ui_privacy_54a57c31')}</a
          ><a
            href={invite.application.support_url ?? '#'}
            aria-disabled={!invite.application.support_url}>{$t('ui_support_be91940b')}</a
          ><small
            >{$t('ui_application_home_value0_644d593c', {
              value0: String(invite.application.origin_domain)
            })}</small
          >
        </div>
        {#if installed}
          <a class="return-link" href={resolve(data.returnTo as '/home')}
            >{$t('ui_return_to_kaede_df76c5df')}</a
          >
        {:else if invite.application.supported_install_types.includes('guild_install')}<button
            onclick={install}
            disabled={busy || !selected}
            >{busy ? $t('ui_adding_bot_8d3bf9db') : $t('ui_authorize_and_add_bot_d0cae0d9')}</button
          >{/if}
      </footer>
    </article>{/if}
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  main {
    min-height: 100dvh;
    box-sizing: border-box;
    padding: clamp(1rem, 5vw, 4rem);
    color: var(--text);
    background: radial-gradient(
      circle at top,
      color-mix(in srgb, var(--accent) 12%, var(--bg)),
      var(--bg) 45%
    );
  }
  .back {
    display: block;
    width: min(720px, 100%);
    margin: 0 auto 1rem;
    color: var(--text-muted);
    text-decoration: none;
  }
  .invite,
  .notice {
    width: min(720px, 100%);
    box-sizing: border-box;
    margin: auto;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: var(--surface);
    box-shadow: 0 25px 80px #0005;
  }
  .invite > header {
    display: flex;
    gap: 1rem;
    align-items: center;
    padding: 1.4rem;
  }
  .avatar {
    display: grid;
    width: 68px;
    height: 68px;
    place-items: center;
    border-radius: 18px;
    color: white;
    background: var(--accent);
    font-size: 1.8rem;
    font-weight: 850;
  }
  .invite header h1,
  .invite header p {
    margin: 0;
  }
  .invite header small {
    color: var(--accent);
    font-weight: 800;
  }
  .description {
    margin: 0;
    padding: 0 1.4rem 1.4rem;
    color: var(--text-soft);
  }
  section {
    border-top: 1px solid var(--line);
    padding: 1.3rem 1.4rem;
  }
  section h2 {
    margin: 0 0 0.8rem;
    font-size: 1rem;
  }
  label {
    display: grid;
    gap: 0.45rem;
    font-size: 0.8rem;
    font-weight: 750;
  }
  select {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.8rem;
    color: var(--text);
    background: var(--bg);
    font: inherit;
  }
  .pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .pills span {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.3rem 0.55rem;
    color: var(--text-soft);
    font-size: 0.75rem;
    background: var(--surface-hover);
  }
  .context-options {
    display: grid;
    gap: 0.55rem;
    border: 0;
    margin: 1rem 0;
    padding: 0;
  }
  .context-options legend {
    margin-bottom: 0.35rem;
    font-size: 0.8rem;
    font-weight: 800;
  }
  .context-options label {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    font-weight: 650;
  }
  .context-options input {
    width: 1rem;
    height: 1rem;
    accent-color: var(--accent);
  }
  details {
    margin-top: 1rem;
  }
  .muted,
  .privacy p {
    color: var(--text-muted);
  }
  footer {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: center;
    border-top: 1px solid var(--line);
    padding: 1.2rem 1.4rem;
  }
  footer > div {
    display: flex;
    gap: 0.8rem;
    flex-wrap: wrap;
  }
  footer a {
    color: var(--accent);
  }
  footer small {
    width: 100%;
    color: var(--text-muted);
  }
  footer button,
  .personal-install,
  .return-link {
    border: 0;
    border-radius: 9px;
    padding: 0.8rem 1rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 800;
    text-decoration: none;
    cursor: pointer;
  }
  .return-link {
    display: inline-block;
    text-align: center;
  }
  footer button:disabled,
  .personal-install:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .success {
    border: 1px solid var(--success);
    border-radius: 10px;
    padding: 1rem;
  }
  .success p {
    color: var(--text-muted);
  }
  .notice {
    padding: 1rem;
  }
  .notice.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  a[aria-disabled='true'] {
    pointer-events: none;
    opacity: 0.4;
  }
  @media (max-width: 560px) {
    main {
      padding: 1rem;
    }
    .invite {
      border-radius: 12px;
    }
    footer {
      align-items: stretch;
      flex-direction: column;
    }
    footer button {
      width: 100%;
    }
  }
</style>
