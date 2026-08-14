<script lang="ts">
  import { page } from '$app/state';
  import { api, userErrorMessage } from '$lib/api/client';
  import type { Guild } from '$lib/chat/types';
  import { onMount } from 'svelte';

  interface Installation {
    id: string;
    status: string;
    scopes: string[];
    intents: string[];
    permissions: string;
    e2ee_mode: string;
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

  async function load() {
    error = '';
    try {
      [guild, installations] = await Promise.all([
        api<Guild>(`/guilds/${encodeURIComponent(guildRef)}`),
        api<Installation[]>(`/guilds/${encodeURIComponent(guildRef)}/integrations/bots`)
      ]);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load bot integrations for this guild.');
    }
  }
  async function remove(installation: Installation) {
    if (!confirm(`Remove ${installation.application.name} from ${guild?.name ?? 'this guild'}?`))
      return;
    busyRef = installation.application.ref;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(guildRef)}/integrations/bots/${encodeURIComponent(installation.application.ref)}`,
        { method: 'DELETE' }
      );
      installations = installations.filter((item) => item.id !== installation.id);
      notice = `${installation.application.name} was removed. Its future access is revoked.`;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not remove the bot.');
    } finally {
      busyRef = '';
    }
  }
  onMount(() => void load());
</script>

<svelte:head><title>Bot integrations · {guild?.name ?? 'Guild'} · Kaede Chat</title></svelte:head>
<main>
  <header>
    <div>
      <a href={`/g/${encodeURIComponent(guildRef)}/settings`}>← Guild settings</a><span
        >Guild integrations</span
      >
      <h1>Bots and automations</h1>
      <p>{guild?.name ?? guildRef}</p>
    </div>
  </header>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
      class="notice"
    >
      {notice}
    </div>{/if}
  <section class="intro">
    <h2>Installed bots</h2>
    <p>
      Each bot keeps only the scopes, event intents, role permissions, and encryption mode approved
      for this guild. Removing a bot revokes future API and Gateway access.
    </p>
  </section>
  {#if !error && !guild}<p>Loading integrations…</p>
  {:else if installations.length === 0}<section class="empty">
      <strong>No bots installed</strong>
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
              {#each installation.scopes as scope}<span>{scope}</span>{/each}
            </div>
            <h3>Live events</h3>
            <div class="pills">
              {#each installation.intents as intent}<span>{intent}</span>{/each}
            </div>
            <p>Guild permission bits: {installation.permissions}</p>
          </details>
          <footer>
            <small>Installed {new Date(installation.installed_at).toLocaleString()}</small><button
              disabled={busyRef === installation.application.ref}
              onclick={() => remove(installation)}
              >{busyRef === installation.application.ref ? 'Removing…' : 'Remove bot'}</button
            >
          </footer>
        </article>{/each}
    </div>{/if}
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
    border: 1px solid var(--danger, #d84a4a);
    border-radius: 8px;
    padding: 0.65rem 0.8rem;
    color: var(--danger, #ef6767);
    background: transparent;
    font: inherit;
    font-weight: 800;
  }
  .notice {
    margin-bottom: 1rem;
  }
  .error {
    color: var(--danger, #ef6767);
  }
</style>
