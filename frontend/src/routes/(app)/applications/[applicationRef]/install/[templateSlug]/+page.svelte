<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { resolve } from '$app/paths';
  import { Permission } from '$lib/generated/permissions';
  import { onMount } from 'svelte';
  let { data } = $props<{ data: { applicationRef: string; templateSlug: string } }>();
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
  const appRef = $derived(
    invite?.application.ref ?? `${invite?.application.id}@${invite?.application.origin_domain}`
  );
  async function load() {
    try {
      const [resolvedInvite, memberships] = await Promise.all([
        api<Invite>(
          `/bot-invites/${encodeURIComponent(data.applicationRef)}/${encodeURIComponent(data.templateSlug)}`
        ),
        api<Guild[]>('/users/@me/guilds')
      ]);
      invite = resolvedInvite;
      guilds = memberships.filter((guild) => {
        const permissions = BigInt(guild.permissions ?? '0');
        return Boolean(permissions & (Permission.MANAGE_GUILD | Permission.ADMINISTRATOR));
      });
      if (guilds.length) selected = `${guilds[0].id}@${guilds[0].origin_domain}`;
    } catch (caught) {
      error = userErrorMessage(caught, 'This bot invitation is unavailable.');
    }
  }
  async function install() {
    if (!selected || busy) return;
    busy = true;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(selected)}/integrations/bots?application_ref=${encodeURIComponent(data.applicationRef)}&template_slug=${encodeURIComponent(data.templateSlug)}`,
        { method: 'POST' }
      );
      installed = true;
    } catch (caught) {
      error = userErrorMessage(caught, 'The bot could not be added to that guild.');
    } finally {
      busy = false;
    }
  }
  onMount(() => void load());
</script>

<svelte:head><title>{invite?.application.name ?? 'Bot invitation'} · Kaede Chat</title></svelte:head
>
<main>
  <a class="back" href={resolve('/home')}>← Back to Kaede</a>{#if error}<div
      class="notice error"
      role="alert"
    >
      {error}
    </div>{/if}{#if invite}<article class="invite">
      <header>
        <span class="avatar">{invite.application.name.slice(0, 1).toUpperCase()}</span>
        <div>
          <small>BOT INVITATION</small>
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
      <section>
        <h2>Add to a guild</h2>
        {#if installed}<div class="success">
            <strong>Bot added</strong>
            <p>
              The bot is now a visible member of the selected guild. Its permissions can be changed
              through guild roles.
            </p>
            <a href={resolve('/home')}>Return to Kaede</a>
          </div>{:else}<label
            >Guild<select bind:value={selected}
              >{#each guilds as guild}<option value={`${guild.id}@${guild.origin_domain}`}
                  >{guild.name} · {guild.origin_domain}</option
                >{/each}</select
            ></label
          >{#if guilds.length === 0}<p class="muted">
              You do not have any guilds available for installation.
            </p>{/if}{/if}
      </section>
      <section>
        <h2>Requested access</h2>
        <div class="pills">
          {#each invite.template.scopes as scope}<span>{scope}</span>{/each}
        </div>
        <details>
          <summary>Live event intents</summary>
          <div class="pills">
            {#each invite.template.intents as intent}<span>{intent}</span>{/each}
          </div>
        </details>
        <p class="muted">Guild permission bits: {invite.template.permissions}</p>
      </section>
      <section class="privacy">
        <h2>Encryption and privacy</h2>
        {#if invite.template.e2ee_mode === 'participant'}<p>
            <strong>This bot may become an E2EE participant.</strong> In E2EE channels where it is explicitly
            added, the bot operator can decrypt future messages and keep anything the bot receives. Installing
            or removing it rotates room keys. It receives no pre-install history by default.
          </p>{:else if invite.template.e2ee_mode === 'interaction_only'}<p>
            This bot receives only encrypted command payloads users explicitly submit in E2EE
            channels. It cannot passively read the channel or request plaintext history.
          </p>{:else}<p>This bot has no access to E2EE channel contents or interactions.</p>{/if}
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
        {#if !installed}<button onclick={install} disabled={busy || !selected}
            >{busy ? 'Adding bot…' : 'Authorize and add bot'}</button
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
  footer button:disabled {
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
