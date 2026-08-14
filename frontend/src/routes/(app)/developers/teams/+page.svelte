<script lang="ts">
  import DeveloperPortalNav from '$lib/components/DeveloperPortalNav.svelte';
  import { api, userErrorMessage } from '$lib/api/client';
  import { onMount } from 'svelte';

  type TeamRole = 'owner' | 'administrator' | 'developer' | 'security' | 'analyst' | 'support';
  interface Team {
    ref: string;
    name: string;
    personal: boolean;
    role: TeamRole;
  }
  interface TeamMember {
    role: TeamRole;
    user: {
      id: string;
      origin_domain: string;
      username: string;
      display_name: string | null;
      handle?: string;
    };
  }
  interface UserLookup {
    id: string;
    origin_domain: string;
  }

  const roles: TeamRole[] = [
    'owner',
    'administrator',
    'developer',
    'security',
    'analyst',
    'support'
  ];
  let teams = $state<Team[]>([]);
  let selected = $state<Team | null>(null);
  let members = $state<TeamMember[]>([]);
  let showCreate = $state(false);
  let showAddMember = $state(false);
  let name = $state('');
  let memberIdentity = $state('');
  let memberRole = $state<TeamRole>('developer');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let loaded = $state(false);

  async function loadTeams(preferredRef?: string) {
    teams = await api<Team[]>('/developer-teams');
    const wanted = preferredRef ?? selected?.ref;
    selected = teams.find((team) => team.ref === wanted) ?? teams[0] ?? null;
    loaded = true;
    if (selected) await loadMembers();
  }

  async function loadMembers() {
    members = selected
      ? await api<TeamMember[]>(`/developer-teams/${encodeURIComponent(selected.ref)}/members`)
      : [];
  }

  async function createTeam(event: SubmitEvent) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    busy = true;
    error = '';
    notice = '';
    try {
      const created = await api<Team>('/developer-teams', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim() })
      });
      name = '';
      showCreate = false;
      await loadTeams(created.ref);
      notice = 'Team created.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the team.');
    } finally {
      busy = false;
    }
  }

  async function selectTeam(team: Team) {
    selected = team;
    showAddMember = false;
    error = '';
    notice = '';
    await loadMembers();
  }

  async function resolveMemberRef(value: string): Promise<string> {
    if (/^\d+@[^@\s]+$/.test(value)) return value;
    const user = await api<UserLookup>(`/users/lookup?handle=${encodeURIComponent(value)}`);
    return `${user.id}@${user.origin_domain}`;
  }

  async function addMember(event: SubmitEvent) {
    event.preventDefault();
    if (!selected || !memberIdentity.trim() || busy) return;
    busy = true;
    error = '';
    notice = '';
    try {
      const userRef = await resolveMemberRef(memberIdentity.trim());
      await api(`/developer-teams/${encodeURIComponent(selected.ref)}/members`, {
        method: 'POST',
        body: JSON.stringify({ user_ref: userRef, role: memberRole })
      });
      memberIdentity = '';
      showAddMember = false;
      await loadMembers();
      notice = 'Team member added.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not add the team member.');
    } finally {
      busy = false;
    }
  }

  async function changeRole(member: TeamMember, role: TeamRole) {
    if (!selected) return;
    error = '';
    try {
      await api(
        `/developer-teams/${encodeURIComponent(selected.ref)}/members/${encodeURIComponent(`${member.user.id}@${member.user.origin_domain}`)}`,
        { method: 'PATCH', body: JSON.stringify({ role }) }
      );
      await loadMembers();
      notice = 'Role updated.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the team member.');
    }
  }

  async function removeMember(member: TeamMember) {
    if (!selected || !confirm(`Remove ${member.user.username} from ${selected.name}?`)) return;
    error = '';
    try {
      await api(
        `/developer-teams/${encodeURIComponent(selected.ref)}/members/${encodeURIComponent(`${member.user.id}@${member.user.origin_domain}`)}`,
        { method: 'DELETE' }
      );
      await loadMembers();
      notice = 'Team member removed.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not remove the team member.');
    }
  }

  onMount(
    () =>
      void loadTeams().catch((caught) => {
        loaded = true;
        error = userErrorMessage(caught, 'Could not load developer teams.');
      })
  );
</script>

<svelte:head><title>Developer teams · Kaede Chat</title></svelte:head>

<main class="portal-shell">
  <DeveloperPortalNav active="teams" />
  <section class="portal-content">
    <header class="page-header">
      <div>
        <span class="eyebrow">Access</span>
        <h1>Teams</h1>
        <p>Keep personal projects separate or collaborate with other local Kaede accounts.</p>
      </div>
      <button class="primary" type="button" onclick={() => (showCreate = true)}>＋ New team</button>
    </header>

    {#if error}<div class="notice error" role="alert">{error}</div>{/if}
    {#if notice}<div class="notice" role="status">{notice}</div>{/if}

    {#if showCreate}
      <form class="compact-form" onsubmit={createTeam}>
        <div>
          <span class="eyebrow">Shared workspace</span>
          <h2>Create a team</h2>
          <p>Use teams when multiple people need to manage the same applications.</p>
        </div>
        <label>
          Team name
          <input bind:value={name} maxlength="100" required placeholder="Platform engineering" />
        </label>
        <div class="form-actions">
          <button class="secondary" type="button" onclick={() => (showCreate = false)}
            >Cancel</button
          >
          <button class="primary" disabled={busy || !name.trim()}>
            {busy ? 'Creating…' : 'Create team'}
          </button>
        </div>
      </form>
    {/if}

    <section aria-labelledby="team-list-heading">
      <div class="section-heading">
        <div>
          <h2 id="team-list-heading">Your workspaces</h2>
          <p>Personal is always available and private to your account.</p>
        </div>
        <span>{teams.length}</span>
      </div>

      {#if !loaded}
        <div class="loading-card">Loading teams…</div>
      {:else}
        <div class="team-grid">
          {#each teams as team (team.ref)}
            <button
              class="team-card"
              class:active={selected?.ref === team.ref}
              type="button"
              onclick={() => selectTeam(team)}
            >
              <span class:personal-mark={team.personal} class="team-mark" aria-hidden="true">
                {team.personal ? 'P' : team.name.slice(0, 1).toUpperCase()}
              </span>
              <span>
                <strong>{team.personal ? 'Personal' : team.name}</strong>
                <small>{team.personal ? 'Only you' : team.role}</small>
              </span>
              <span class="arrow" aria-hidden="true">›</span>
            </button>
          {/each}
        </div>
      {/if}
    </section>

    {#if selected}
      <section class="team-detail" aria-labelledby="selected-team-heading">
        <header>
          <div>
            <span class="eyebrow"
              >{selected.personal ? 'Your default workspace' : 'Shared workspace'}</span
            >
            <h2 id="selected-team-heading">{selected.personal ? 'Personal' : selected.name}</h2>
            <p>
              {selected.personal
                ? 'New applications belong here by default. This workspace cannot be shared or removed.'
                : 'Members can manage applications according to their assigned role.'}
            </p>
          </div>
          {#if !selected.personal && ['owner', 'administrator'].includes(selected.role)}
            <button
              class="secondary"
              type="button"
              onclick={() => (showAddMember = !showAddMember)}
            >
              ＋ Add member
            </button>
          {/if}
        </header>

        {#if showAddMember && !selected.personal}
          <form class="add-member" onsubmit={addMember}>
            <label>
              Local username
              <input
                bind:value={memberIdentity}
                placeholder="username@this-instance"
                autocomplete="off"
                required
              />
              <small>You can also paste a complete account ID.</small>
            </label>
            <label>
              Role
              <select bind:value={memberRole}>
                {#each roles as role}<option value={role}>{role}</option>{/each}
              </select>
            </label>
            <button class="primary" disabled={busy}>{busy ? 'Adding…' : 'Add member'}</button>
          </form>
        {/if}

        <div class="member-heading">
          <h3>Members</h3>
          <span>{members.length}</span>
        </div>
        <div class="members">
          {#each members as member (`${member.user.id}@${member.user.origin_domain}`)}
            <article>
              <span class="member-avatar" aria-hidden="true">
                {(member.user.display_name ?? member.user.username).slice(0, 1).toUpperCase()}
              </span>
              <div>
                <strong>{member.user.display_name ?? member.user.username}</strong>
                <small>@{member.user.username}@{member.user.origin_domain}</small>
              </div>
              {#if !selected.personal && ['owner', 'administrator'].includes(selected.role)}
                <select
                  aria-label={`Role for ${member.user.username}`}
                  value={member.role}
                  onchange={(event) => changeRole(member, event.currentTarget.value as TeamRole)}
                >
                  {#each roles as role}<option value={role}>{role}</option>{/each}
                </select>
                <button class="remove" type="button" onclick={() => removeMember(member)}
                  >Remove</button
                >
              {:else}
                <span class="role">{member.role}</span>
              {/if}
            </article>
          {/each}
        </div>
        <footer>
          <span>Team ID</span>
          <code>{selected.ref}</code>
        </footer>
      </section>
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
  .section-heading,
  .team-detail > header,
  .member-heading,
  article,
  .team-detail footer {
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
  h3,
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
  .section-heading p,
  .compact-form p,
  .team-detail header p {
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
  button:disabled {
    opacity: 0.5;
    cursor: wait;
  }
  .notice,
  .loading-card {
    margin-bottom: 1rem;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0.85rem 1rem;
    background: var(--surface);
  }
  .notice.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  .compact-form {
    display: grid;
    grid-template-columns: minmax(180px, 1fr) minmax(220px, 1.2fr) auto;
    gap: 1rem;
    align-items: end;
    margin: -0.5rem 0 2rem;
    padding: 1.2rem;
    border: 1px solid color-mix(in srgb, var(--accent) 50%, var(--line));
    border-radius: 14px;
    background: var(--surface);
  }
  .form-actions {
    display: flex;
    gap: 0.5rem;
  }
  label {
    display: grid;
    gap: 0.4rem;
    font-size: 0.82rem;
    font-weight: 750;
  }
  input,
  select {
    box-sizing: border-box;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.72rem 0.8rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
  }
  input:focus,
  select:focus {
    border-color: var(--accent);
    outline: 2px solid color-mix(in srgb, var(--accent) 24%, transparent);
  }
  .section-heading {
    align-items: end;
    margin-bottom: 1rem;
  }
  .section-heading > span,
  .member-heading span {
    min-width: 1.8rem;
    border-radius: 999px;
    padding: 0.2rem 0.5rem;
    color: var(--text-muted);
    background: var(--surface-hover);
    text-align: center;
  }
  .team-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: 0.7rem;
    margin-bottom: 1.5rem;
  }
  button.team-card {
    display: grid;
    grid-template-columns: 42px minmax(0, 1fr) auto;
    gap: 0.75rem;
    align-items: center;
    border: 1px solid var(--line);
    padding: 0.9rem;
    background: var(--surface);
    text-align: left;
  }
  button.team-card:hover,
  button.team-card.active {
    border-color: color-mix(in srgb, var(--accent) 60%, var(--line));
    background: var(--surface-raised);
  }
  button.team-card.active {
    box-shadow: inset 3px 0 var(--accent);
  }
  .team-card > span:nth-child(2) {
    display: grid;
    min-width: 0;
  }
  .team-card small {
    color: var(--text-muted);
    font-weight: 500;
    text-transform: capitalize;
  }
  .team-mark,
  .member-avatar {
    display: grid;
    place-items: center;
    border-radius: 12px;
    color: var(--text);
    background: var(--surface-hover);
    font-weight: 850;
  }
  .team-mark {
    width: 42px;
    height: 42px;
  }
  .team-mark.personal-mark {
    color: var(--on-accent, #fff);
    background: var(--accent);
  }
  .arrow {
    color: var(--text-muted);
    font-size: 1.35rem;
  }
  .team-detail {
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--surface);
  }
  .team-detail > header {
    align-items: flex-start;
    padding: 1.25rem;
    border-bottom: 1px solid var(--line);
  }
  .add-member {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) 180px auto;
    gap: 0.75rem;
    align-items: end;
    margin: 1rem 1.25rem;
    padding: 1rem;
    border-radius: 10px;
    background: var(--surface-subtle);
  }
  .add-member label small {
    color: var(--text-muted);
    font-weight: 500;
  }
  .member-heading {
    padding: 1.1rem 1.25rem 0.55rem;
  }
  .members {
    display: grid;
    gap: 0.45rem;
    padding: 0 1.25rem 1.25rem;
  }
  article {
    justify-content: flex-start;
    padding: 0.75rem;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--bg);
  }
  .member-avatar {
    flex: 0 0 38px;
    width: 38px;
    height: 38px;
  }
  article > div {
    display: grid;
    min-width: 0;
    margin-right: auto;
  }
  article small {
    overflow: hidden;
    color: var(--text-muted);
    text-overflow: ellipsis;
  }
  article select {
    width: 150px;
    text-transform: capitalize;
  }
  button.remove {
    padding: 0.55rem 0.7rem;
    color: var(--danger);
    background: transparent;
  }
  .role {
    border-radius: 999px;
    padding: 0.25rem 0.55rem;
    color: var(--text-muted);
    background: var(--surface-hover);
    font-size: 0.78rem;
    text-transform: capitalize;
  }
  .team-detail footer {
    justify-content: flex-start;
    padding: 0.8rem 1.25rem;
    border-top: 1px solid var(--line);
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  code {
    overflow: hidden;
    text-overflow: ellipsis;
  }
  @media (max-width: 820px) {
    .portal-shell {
      display: block;
    }
    .portal-content {
      padding: 1.5rem 1rem 4rem;
    }
  }
  @media (max-width: 680px) {
    .page-header,
    .team-detail > header {
      align-items: flex-start;
      flex-direction: column;
    }
    .compact-form,
    .add-member {
      grid-template-columns: 1fr;
    }
    .form-actions {
      justify-content: flex-end;
    }
    article {
      align-items: stretch;
      flex-wrap: wrap;
    }
    article > div {
      width: calc(100% - 54px);
    }
    article select {
      width: auto;
      flex: 1;
    }
  }
</style>
