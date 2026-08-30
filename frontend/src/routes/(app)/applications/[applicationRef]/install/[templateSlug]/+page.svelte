<script lang="ts">
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
        throw new Error('The resolved invitation did not match the requested application.');
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
              ? 'Your guilds could not be loaded. Account installation is still available.'
              : 'Your guilds could not be loaded. Reload this page to try again.'
          );
        }
      } finally {
        if (requestIsCurrent(signal, generation, applicationRef, templateSlug)) {
          guildsLoading = false;
        }
      }
    } catch (caught) {
      if (requestIsCurrent(signal, generation, applicationRef, templateSlug)) {
        error = userErrorMessage(caught, 'This bot invitation is unavailable.');
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
        error = userErrorMessage(caught, 'The bot could not be added to that guild.');
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
          'This app does not currently offer commands that can be installed for your account.'
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

<svelte:head><title>{invite?.application.name ?? 'Add App'} · Kaede Chat</title></svelte:head>
<!-- eslint-disable svelte/no-navigation-without-resolve -- privacy and support destinations are external URLs supplied by the application -->
<main>
  <a class="back" href={resolve(data.returnTo as '/home')}>← Back to Kaede</a>{#if error}<div
      class="notice error"
      role="alert"
    >
      {error}
    </div>{/if}{#if loading || !routeIsLoaded}<div class="notice" role="status">
      Loading app authorization…
    </div>{:else if invite}<article class="invite">
      <header>
        <span class="avatar">{invite.application.name.slice(0, 1).toUpperCase()}</span>
        <div>
          <small>APP AUTHORIZATION</small>
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
          'This bot has not provided a description.'}
      </p>
      {#if invite.application.supported_install_types.includes('guild_install')}<section>
          <h2>Add to a guild</h2>
          {#if guildsError}<p class="notice error" role="alert">{guildsError}</p>{/if}
          {#if guildsLoading}<p class="muted" role="status">Loading your guilds…</p>{/if}
          {#if installed}<div class="success">
              <strong>Bot added</strong>
              <p>
                The bot is now a visible member of the selected guild. Its permissions can be
                changed through guild roles.
              </p>
              <a href={resolve(data.returnTo as '/home')}>Return to Kaede</a>
            </div>{:else}<label
              >Guild<select bind:value={selected} disabled={busy}
                >{#each guilds as guild (`${guild.id}@${guild.origin_domain}`)}<option
                    value={`${guild.id}@${guild.origin_domain}`}
                    >{guild.name} · {guild.origin_domain}</option
                  >{/each}</select
              ></label
            >{#if !guildsLoading && !guildsError && guilds.length === 0}<p class="muted">
                You do not have any guilds available for installation.
              </p>{/if}{/if}
        </section>{/if}
      {#if invite.application.supported_install_types.includes('user_install')}<section>
          <h2>Install for your account</h2>
          {#if userInstalled}
            <div class="success">
              <strong>Installed for your account</strong>
              <p>
                This app’s user-installable commands can appear in the locations you authorized. You
                can change or revoke this in Authorized apps under Settings.
              </p>
            </div>
          {:else}
            <p class="muted">
              Authorize this app for your account without adding it as a guild member. It receives
              only command interactions you explicitly start.
            </p>
            <fieldset class="context-options">
              <legend>Use commands in</legend>
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
              {personalBusy ? 'Authorizing…' : 'Authorize for my account'}
            </button>
          {/if}
        </section>{/if}
      {#if invite.application.supported_install_types.includes('guild_install')}
        <section>
          <h2>Guild installation access</h2>
          <div class="pills">
            {#each invite.template.scopes as scope (scope)}<span>{scope}</span>{/each}
          </div>
          <details>
            <summary>Live event intents</summary>
            <div class="pills">
              {#each invite.template.intents as intent (intent)}<span>{intent}</span>{/each}
            </div>
          </details>
          <details>
            <summary>Server permissions</summary>
            {#if selectedPermissionMetadata(invite.template.permissions).length}
              <div class="pills">
                {#each selectedPermissionMetadata(invite.template.permissions) as permission (permission.permission)}
                  <span title={permission.description}>{permission.label}</span>
                {/each}
              </div>
            {:else}
              <p class="muted">No server permissions requested.</p>
            {/if}
          </details>
        </section>
      {/if}
      {#if invite.application.supported_install_types.includes('user_install')}
        <section>
          <h2>Account installation access</h2>
          <p class="muted">Commands and responses only; this does not add a guild member.</p>
          <div class="pills">
            {#each invite.application.user_install_scopes as scope (scope)}<span>{scope}</span
              >{/each}
            <span>interactions</span>
          </div>
          <details>
            <summary>Supported command locations</summary>
            <div class="pills">
              {#each invite.application.user_install_contexts as context (context)}
                <span>{userContextLabels[context]}</span>
              {/each}
            </div>
          </details>
        </section>
      {/if}
      <section class="privacy">
        <h2>Encryption and privacy</h2>
        {#if invite.application.supported_install_types.includes('guild_install')}
          {#if invite.template.e2ee_mode === 'participant'}<p>
              <strong>Guild install:</strong> this bot may become an E2EE participant. In E2EE channels
              where it is explicitly added, the bot operator can decrypt future messages and keep anything
              the bot receives. Installing or removing it rotates room keys. It receives no pre-install
              history by default.
            </p>{:else}<p>The guild install has no access to E2EE channel contents.</p>{/if}
        {/if}
        {#if invite.application.supported_install_types.includes('user_install')}
          <p>
            <strong>Account install:</strong> the app receives only interactions you explicitly start
            in an authorized location. Encrypted interactions require a registered app device and current
            room consent; they do not grant ordinary message or DM access.
          </p>
        {/if}
        <p>
          For plaintext channels, the bot can access only the scopes and channel permissions shown
          above. Revocation stops future access but cannot erase information already delivered to
          the bot operator.
        </p>
      </section>
      <footer>
        <div>
          <a
            href={invite.application.privacy_url ?? '#'}
            aria-disabled={!invite.application.privacy_url}>Privacy</a
          ><a
            href={invite.application.support_url ?? '#'}
            aria-disabled={!invite.application.support_url}>Support</a
          ><small>Application home: {invite.application.origin_domain}</small>
        </div>
        {#if invite.application.supported_install_types.includes('guild_install') && !installed}<button
            onclick={install}
            disabled={busy || !selected}>{busy ? 'Adding bot…' : 'Authorize and add bot'}</button
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
  .success a {
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
