<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { resolve } from '$app/paths';
  import { onMount } from 'svelte';

  interface Team {
    ref: string;
    name: string;
    personal: boolean;
    role: string;
  }

  interface Application {
    ref: string;
    name: string;
    description: string | null;
    status: string;
    target_policy: string;
    bot_user: { display_name: string | null; username: string; handle: string };
  }

  let applications = $state<Application[]>([]);
  let teams = $state<Team[]>([]);
  let teamRef = $state('');
  let name = $state('');
  let description = $state('');
  let busy = $state(false);
  let loaded = $state(false);
  let error = $state('');

  async function refresh() {
    try {
      [applications, teams] = await Promise.all([
        api<Application[]>('/applications'),
        api<Team[]>('/developer-teams')
      ]);
      if (!teamRef && teams.length) teamRef = teams[0].ref;
      loaded = true;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load your applications.');
    }
  }

  async function createApplication(event: SubmitEvent) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    busy = true;
    error = '';
    try {
      const application = await api<Application>('/applications', {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim() || null,
          team_ref: teamRef || null
        })
      });
      location.href = `/developers/${encodeURIComponent(application.ref)}`;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the application.');
    } finally {
      busy = false;
    }
  }

  onMount(() => void refresh());
</script>

<svelte:head><title>Developer Portal · Kaede Chat</title></svelte:head>

<main class="portal-shell">
  <aside>
    <a class="back" href={resolve('/settings')}>← User settings</a>
    <p>Kaede developers</p>
    <h1>Developer Portal</h1>
    <nav>
      <a class="active" href="/developers">Applications</a>
      <a href="/developers/teams">Developer teams</a>
      <a href="/docs/bots-and-automations">Bot API guide</a>
    </nav>
  </aside>
  <section class="portal-content">
    <header>
      <div>
        <span class="eyebrow">Applications and automations</span>
        <h2>Your applications</h2>
        <p>Create bots, publish slash commands, configure federation, and inspect installations.</p>
      </div>
    </header>

    {#if error}<div class="notice error" role="alert">{error}</div>{/if}

    <form class="create-card" onsubmit={createApplication}>
      <div>
        <h3>Create an application</h3>
        <p>
          Every local account can create applications. You can add workers and invite links next.
        </p>
      </div>
      <label
        >Name <input bind:value={name} maxlength="100" required placeholder="Weather bot" /></label
      >
      <label
        >Team<select bind:value={teamRef}>
          {#each teams as team}<option value={team.ref}>{team.name} · {team.role}</option>{/each}
        </select></label
      >
      <label
        >Description <textarea
          bind:value={description}
          maxlength="1000"
          rows="2"
          placeholder="What does this bot do?"
        ></textarea></label
      >
      <button type="submit" disabled={busy || !name.trim()}
        >{busy ? 'Creating…' : 'Create application'}</button
      >
    </form>

    <div class="section-heading">
      <h3>Applications</h3>
      <span>{applications.length}</span>
    </div>
    {#if !loaded}
      <p class="muted">Loading applications…</p>
    {:else if applications.length === 0}
      <div class="empty">
        <strong>No applications yet</strong>
        <p>Create one above to get started.</p>
      </div>
    {:else}
      <div class="app-grid">
        {#each applications as application (application.ref)}
          <a class="app-card" href={`/developers/${encodeURIComponent(application.ref)}`}>
            <span class="avatar">{application.name.slice(0, 1).toUpperCase()}</span>
            <span class="app-copy">
              <span><strong>{application.name}</strong><small>{application.status}</small></span>
              <p>{application.description || 'No description yet.'}</p>
              <small
                >{application.bot_user.handle} · {application.target_policy.replace(
                  '_',
                  ' '
                )}</small
              >
            </span>
            <b aria-hidden="true">›</b>
          </a>
        {/each}
      </div>
    {/if}
  </section>
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  .portal-shell {
    min-height: 100dvh;
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
    background: var(--bg);
    color: var(--text);
  }
  aside {
    position: sticky;
    top: 0;
    height: 100dvh;
    padding: 2rem 1.4rem;
    border-right: 1px solid var(--line);
    background: var(--surface);
  }
  aside > p,
  .eyebrow {
    margin: 2rem 0 0.25rem;
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  h1 {
    margin: 0 0 2rem;
    font-size: 1.45rem;
  }
  nav {
    display: grid;
    gap: 0.35rem;
  }
  nav a,
  .back {
    border-radius: 8px;
    padding: 0.7rem 0.8rem;
    color: var(--text-muted);
    text-decoration: none;
  }
  nav a:hover,
  nav a.active {
    background: var(--surface-hover);
    color: var(--text);
  }
  .portal-content {
    width: min(1120px, 100%);
    padding: 3rem clamp(1rem, 4vw, 4rem) 5rem;
  }
  header h2 {
    margin: 0.2rem 0;
    font-size: clamp(1.8rem, 4vw, 2.6rem);
  }
  header p,
  .create-card p,
  .empty p,
  .muted {
    color: var(--text-muted);
  }
  .create-card {
    display: grid;
    grid-template-columns: 1.15fr 1fr 1fr 1.25fr auto;
    gap: 1rem;
    align-items: end;
    margin: 2rem 0;
    padding: 1.2rem;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--surface);
  }
  .create-card h3,
  .create-card p {
    margin: 0;
  }
  label {
    display: grid;
    gap: 0.4rem;
    font-size: 0.8rem;
    font-weight: 700;
  }
  input,
  textarea,
  select {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.7rem 0.8rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
    resize: vertical;
  }
  button {
    border: 0;
    border-radius: 8px;
    padding: 0.75rem 1rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 800;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.55;
    cursor: wait;
  }
  .section-heading {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin: 2rem 0 0.8rem;
  }
  .section-heading h3 {
    margin: 0;
  }
  .section-heading span {
    border-radius: 999px;
    padding: 0.15rem 0.5rem;
    background: var(--surface-hover);
    color: var(--text-muted);
  }
  .app-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(310px, 1fr));
    gap: 0.8rem;
  }
  .app-card {
    display: flex;
    gap: 0.9rem;
    align-items: center;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem;
    color: inherit;
    background: var(--surface);
    text-decoration: none;
  }
  .app-card:hover {
    border-color: var(--accent);
    transform: translateY(-1px);
  }
  .avatar {
    display: grid;
    flex: 0 0 48px;
    height: 48px;
    place-items: center;
    border-radius: 14px;
    color: white;
    background: var(--accent);
    font-size: 1.2rem;
    font-weight: 800;
  }
  .app-copy {
    min-width: 0;
    flex: 1;
  }
  .app-copy > span {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .app-copy p {
    overflow: hidden;
    margin: 0.25rem 0;
    color: var(--text-soft);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .app-copy small {
    color: var(--text-muted);
  }
  .notice,
  .empty {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 1rem;
    background: var(--surface);
  }
  .notice.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  @media (max-width: 820px) {
    .portal-shell {
      display: block;
    }
    aside {
      position: static;
      height: auto;
      padding: 1rem;
    }
    aside > p,
    aside h1 {
      display: none;
    }
    aside nav {
      display: flex;
      overflow-x: auto;
    }
    .portal-content {
      padding: 1.5rem 1rem 4rem;
    }
    .create-card {
      grid-template-columns: 1fr;
    }
  }
</style>
