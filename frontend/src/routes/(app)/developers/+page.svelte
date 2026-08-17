<script lang="ts">
  import { resolve } from '$app/paths';
  import DeveloperPortalNav from '$lib/components/DeveloperPortalNav.svelte';
  import { api, userErrorMessage } from '$lib/api/client';
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
  let showCreate = $state(false);
  let busy = $state(false);
  let loaded = $state(false);
  let error = $state('');

  async function refresh() {
    try {
      [applications, teams] = await Promise.all([
        api<Application[]>('/applications'),
        api<Team[]>('/developer-teams')
      ]);
      if (!teams.some((team) => team.ref === teamRef)) {
        teamRef = teams.find((team) => team.personal)?.ref ?? teams[0]?.ref ?? '';
      }
      loaded = true;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load your applications.');
    }
  }

  function openCreate() {
    teamRef = teams.find((team) => team.personal)?.ref ?? teams[0]?.ref ?? '';
    showCreate = true;
  }

  function closeCreate() {
    if (busy) return;
    showCreate = false;
    name = '';
    description = '';
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
  <DeveloperPortalNav active="applications" />
  <section class="portal-content">
    <header class="page-header">
      <div>
        <span class="eyebrow">Applications</span>
        <h1>Build on Kaede</h1>
        <p>Manage bots, commands, federation access, and installations.</p>
      </div>
      <button class="primary" type="button" onclick={openCreate}>＋ New application</button>
    </header>

    {#if error}<div class="notice error" role="alert">{error}</div>{/if}

    {#if showCreate}
      <form class="create-panel" onsubmit={createApplication}>
        <header>
          <div>
            <span class="eyebrow">New application</span>
            <h2>Create an application</h2>
            <p>You can configure its bot identity, commands, and invite link after creation.</p>
          </div>
          <button class="icon-button" type="button" aria-label="Close" onclick={closeCreate}
            >×</button
          >
        </header>
        <div class="form-grid">
          <label>
            Name
            <input bind:value={name} maxlength="100" required placeholder="Weather bot" />
          </label>
          <label>
            Owner
            <select bind:value={teamRef}>
              {#each teams as team (team.ref)}
                <option value={team.ref}>
                  {team.personal ? 'Personal' : team.name} · {team.role}
                </option>
              {/each}
            </select>
          </label>
          <label class="description">
            Description <span>Optional</span>
            <textarea
              bind:value={description}
              maxlength="1000"
              rows="3"
              placeholder="What does this application do?"
            ></textarea>
          </label>
        </div>
        <footer>
          <button class="secondary" type="button" onclick={closeCreate}>Cancel</button>
          <button class="primary" type="submit" disabled={busy || !name.trim() || !teamRef}>
            {busy ? 'Creating…' : 'Create application'}
          </button>
        </footer>
      </form>
    {/if}

    <section class="applications" aria-labelledby="applications-heading">
      <div class="section-heading">
        <div>
          <h2 id="applications-heading">Your applications</h2>
          <p>Applications you own personally or through a team.</p>
        </div>
        <span>{applications.length}</span>
      </div>

      {#if !loaded}
        <div class="empty"><strong>Loading applications…</strong></div>
      {:else if applications.length === 0}
        <div class="empty">
          <span class="empty-icon" aria-hidden="true">◇</span>
          <strong>No applications yet</strong>
          <p>Your Personal team is ready. Create an application whenever you are.</p>
          <button class="secondary" type="button" onclick={openCreate}
            >Create your first application</button
          >
        </div>
      {:else}
        <div class="app-grid">
          {#each applications as application (application.ref)}
            <a
              class="app-card"
              href={resolve(`/developers/${encodeURIComponent(application.ref)}`)}
            >
              <span class="avatar">{application.name.slice(0, 1).toUpperCase()}</span>
              <span class="app-copy">
                <span class="app-title">
                  <strong>{application.name}</strong>
                  <small>{application.status.replace('_', ' ')}</small>
                </span>
                <p>{application.description || 'No description yet.'}</p>
                <small>{application.bot_user.handle}</small>
              </span>
              <span class="arrow" aria-hidden="true">›</span>
            </a>
          {/each}
        </div>
      {/if}
    </section>
  </section>
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  .portal-shell {
    min-height: 100dvh;
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    color: var(--text);
    background: var(--bg);
  }
  .portal-content {
    box-sizing: border-box;
    width: min(1040px, 100%);
    padding: 3rem clamp(1.25rem, 5vw, 4.5rem) 5rem;
  }
  .page-header,
  .create-panel > header,
  .create-panel footer,
  .section-heading,
  .app-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  .page-header {
    margin-bottom: 2.25rem;
  }
  h1,
  h2,
  p {
    margin: 0;
  }
  h1 {
    margin-top: 0.2rem;
    font-size: clamp(2rem, 5vw, 3rem);
    letter-spacing: -0.04em;
  }
  h2 {
    font-size: 1.2rem;
  }
  .page-header p,
  .create-panel p,
  .section-heading p,
  .empty p {
    margin-top: 0.35rem;
    color: var(--text-muted);
  }
  .eyebrow {
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 850;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }
  button {
    border: 0;
    border-radius: 9px;
    padding: 0.72rem 1rem;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
    font-weight: 800;
    cursor: pointer;
  }
  button.primary {
    color: var(--on-accent, #fff);
    background: var(--accent);
  }
  button.secondary {
    border: 1px solid var(--line);
    background: var(--surface);
  }
  button.icon-button {
    padding: 0.3rem 0.55rem;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.5rem;
  }
  button:disabled {
    opacity: 0.5;
    cursor: wait;
  }
  .create-panel {
    margin: -0.5rem 0 2.25rem;
    padding: 1.25rem;
    border: 1px solid color-mix(in srgb, var(--accent) 50%, var(--line));
    border-radius: 14px;
    background: var(--surface);
    box-shadow: 0 14px 34px color-mix(in srgb, #000 16%, transparent);
  }
  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin: 1.25rem 0;
  }
  label {
    display: grid;
    gap: 0.45rem;
    font-size: 0.82rem;
    font-weight: 750;
  }
  label > span {
    margin-left: 0.25rem;
    color: var(--text-muted);
    font-weight: 500;
  }
  label.description {
    grid-column: 1 / -1;
  }
  input,
  textarea,
  select {
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.75rem 0.8rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
  }
  input:focus,
  textarea:focus,
  select:focus {
    border-color: var(--accent);
    outline: 2px solid color-mix(in srgb, var(--accent) 24%, transparent);
  }
  textarea {
    resize: vertical;
  }
  .create-panel footer {
    justify-content: flex-end;
  }
  .section-heading {
    align-items: end;
    margin-bottom: 1rem;
  }
  .section-heading > span {
    min-width: 1.8rem;
    border-radius: 999px;
    padding: 0.25rem 0.55rem;
    color: var(--text-muted);
    background: var(--surface-hover);
    text-align: center;
  }
  .app-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
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
    transition:
      border-color 120ms ease,
      transform 120ms ease,
      background 120ms ease;
  }
  .app-card:hover {
    border-color: color-mix(in srgb, var(--accent) 60%, var(--line));
    background: var(--surface-raised);
    transform: translateY(-1px);
  }
  .avatar {
    display: grid;
    flex: 0 0 48px;
    height: 48px;
    place-items: center;
    border-radius: 14px;
    color: var(--on-accent, #fff);
    background: var(--accent);
    font-size: 1.2rem;
    font-weight: 850;
  }
  .app-copy {
    min-width: 0;
    flex: 1;
  }
  .app-title small {
    border-radius: 999px;
    padding: 0.18rem 0.45rem;
    color: var(--text-muted);
    background: var(--surface-hover);
    text-transform: capitalize;
  }
  .app-copy p {
    overflow: hidden;
    margin: 0.25rem 0;
    color: var(--text-muted);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .app-copy > small {
    color: var(--text-soft, var(--text-muted));
  }
  .arrow {
    color: var(--text-muted);
    font-size: 1.5rem;
  }
  .notice,
  .empty {
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem;
    background: var(--surface);
  }
  .notice {
    margin-bottom: 1rem;
  }
  .notice.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  .empty {
    display: grid;
    justify-items: start;
    gap: 0.35rem;
    padding: 2rem;
  }
  .empty-icon {
    color: var(--accent);
    font-size: 2rem;
  }
  .empty button {
    margin-top: 0.65rem;
  }
  @media (max-width: 820px) {
    .portal-shell {
      display: block;
    }
    .portal-content {
      padding: 1.5rem 1rem 4rem;
    }
  }
  @media (max-width: 600px) {
    .page-header {
      align-items: flex-start;
      flex-direction: column;
    }
    .form-grid {
      grid-template-columns: 1fr;
    }
    label.description {
      grid-column: auto;
    }
    .app-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
