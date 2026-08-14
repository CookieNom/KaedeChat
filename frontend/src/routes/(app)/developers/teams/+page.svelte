<script lang="ts">
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
  let name = $state('');
  let memberRef = $state('');
  let memberRole = $state<TeamRole>('developer');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);

  async function loadTeams() {
    teams = await api<Team[]>('/developer-teams');
    const next = selected ? teams.find((team) => team.ref === selected?.ref) : teams[0];
    selected = next ?? null;
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
      selected = await api<Team>('/developer-teams', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim() })
      });
      name = '';
      await loadTeams();
      notice = 'Team created.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the team.');
    } finally {
      busy = false;
    }
  }
  async function selectTeam(team: Team) {
    selected = team;
    error = '';
    await loadMembers();
  }
  async function addMember(event: SubmitEvent) {
    event.preventDefault();
    if (!selected || !memberRef.trim()) return;
    busy = true;
    error = '';
    try {
      await api(`/developer-teams/${encodeURIComponent(selected.ref)}/members`, {
        method: 'POST',
        body: JSON.stringify({ user_ref: memberRef.trim(), role: memberRole })
      });
      memberRef = '';
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
    try {
      await api(
        `/developer-teams/${encodeURIComponent(selected.ref)}/members/${encodeURIComponent(`${member.user.id}@${member.user.origin_domain}`)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({ role })
        }
      );
      await loadMembers();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the team member.');
    }
  }
  async function removeMember(member: TeamMember) {
    if (!selected || !confirm(`Remove ${member.user.username} from ${selected.name}?`)) return;
    try {
      await api(
        `/developer-teams/${encodeURIComponent(selected.ref)}/members/${encodeURIComponent(`${member.user.id}@${member.user.origin_domain}`)}`,
        { method: 'DELETE' }
      );
      await loadMembers();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not remove the team member.');
    }
  }
  onMount(
    () =>
      void loadTeams().catch(
        (caught) => (error = userErrorMessage(caught, 'Could not load developer teams.'))
      )
  );
</script>

<svelte:head><title>Developer teams · Kaede Chat</title></svelte:head>
<main>
  <header>
    <div>
      <a href="/developers">← Developer Portal</a>
      <h1>Developer teams</h1>
      <p>Share application management with other local accounts.</p>
    </div>
  </header>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
      class="notice"
    >
      {notice}
    </div>{/if}
  <div class="layout">
    <aside>
      <form onsubmit={createTeam}>
        <label>New team<input bind:value={name} maxlength="100" placeholder="My team" /></label
        ><button disabled={busy || !name.trim()}>Create</button>
      </form>
      <nav>
        {#each teams as team (team.ref)}<button
            class:active={selected?.ref === team.ref}
            onclick={() => selectTeam(team)}
            ><strong>{team.name}</strong><small>{team.personal ? 'Personal' : team.role}</small
            ></button
          >{/each}
      </nav>
    </aside>
    <section>
      {#if selected}<header>
          <div>
            <span>{selected.personal ? 'Personal team' : 'Shared team'}</span>
            <h2>{selected.name}</h2>
          </div>
          <code>{selected.ref}</code>
        </header>
        {#if !selected.personal && ['owner', 'administrator'].includes(selected.role)}<form
            class="add"
            onsubmit={addMember}
          >
            <input
              bind:value={memberRef}
              placeholder="Local user ID: snowflake@instance"
              required
            /><select bind:value={memberRole}
              >{#each roles as role}<option value={role}>{role}</option>{/each}</select
            ><button disabled={busy}>Add member</button>
          </form>{/if}
        <div class="members">
          {#each members as member (`${member.user.id}@${member.user.origin_domain}`)}<article>
              <div>
                <strong>{member.user.display_name ?? member.user.username}</strong><small
                  >@{member.user.username}@{member.user.origin_domain}</small
                >
              </div>
              {#if !selected.personal && ['owner', 'administrator'].includes(selected.role)}<select
                  value={member.role}
                  onchange={(event) => changeRole(member, event.currentTarget.value as TeamRole)}
                  >{#each roles as role}<option value={role}>{role}</option>{/each}</select
                ><button class="remove" onclick={() => removeMember(member)}>Remove</button
                >{:else}<span>{member.role}</span>{/if}
            </article>{/each}
        </div>
      {:else}<p>No developer teams are available.</p>{/if}
    </section>
  </div>
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  main {
    box-sizing: border-box;
    min-height: 100dvh;
    padding: clamp(1rem, 4vw, 3rem);
    color: var(--text);
    background: var(--bg);
  }
  main > header {
    width: min(1100px, 100%);
    margin: auto auto 1.5rem;
  }
  h1,
  h2,
  p {
    margin: 0.25rem 0;
  }
  p,
  small {
    color: var(--text-muted);
  }
  a {
    color: var(--accent);
  }
  .layout {
    display: grid;
    grid-template-columns: 290px minmax(0, 1fr);
    width: min(1100px, 100%);
    margin: auto;
    border: 1px solid var(--line);
    border-radius: 14px;
    overflow: hidden;
    background: var(--surface);
  }
  aside,
  section {
    padding: 1.2rem;
  }
  aside {
    border-right: 1px solid var(--line);
  }
  form,
  label,
  nav {
    display: grid;
    gap: 0.6rem;
  }
  nav {
    margin-top: 1.2rem;
  }
  input,
  select {
    box-sizing: border-box;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.7rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
  }
  button {
    border: 0;
    border-radius: 8px;
    padding: 0.7rem;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
    font-weight: 750;
    cursor: pointer;
  }
  nav button {
    display: flex;
    justify-content: space-between;
    text-align: left;
  }
  nav button.active {
    outline: 2px solid var(--accent);
  }
  section > header,
  article,
  .add {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
  }
  .add {
    margin: 1rem 0;
  }
  .add input {
    flex: 1;
  }
  .members {
    display: grid;
    gap: 0.5rem;
    margin-top: 1rem;
  }
  article {
    padding: 0.8rem;
    border: 1px solid var(--line);
    border-radius: 9px;
  }
  article div {
    display: grid;
    margin-right: auto;
  }
  .remove,
  .error {
    color: var(--danger, #ef6767);
  }
  .notice {
    width: min(1100px, 100%);
    box-sizing: border-box;
    margin: 0 auto 1rem;
    padding: 0.8rem;
    border: 1px solid var(--line);
    border-radius: 9px;
    background: var(--surface);
  }
  @media (max-width: 760px) {
    .layout {
      grid-template-columns: 1fr;
    }
    aside {
      border-right: 0;
      border-bottom: 1px solid var(--line);
    }
    .add,
    article {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
