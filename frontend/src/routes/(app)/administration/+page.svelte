<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { resolve } from '$app/paths';
  import { onMount } from 'svelte';
  type View =
    'overview' | 'users' | 'applications' | 'reports' | 'instances' | 'operators' | 'audit';
  interface AdminIdentity {
    roles: string[];
    capabilities: string[];
    user: { username: string; display_name: string | null };
  }
  interface User {
    id: string;
    origin_domain: string;
    username: string;
    display_name: string | null;
    account_type?: string;
    disabled_at: string | null;
  }
  interface App {
    ref: string;
    name: string;
    status: string;
    team_ref: string;
    updated_at: string;
  }
  interface Report {
    id: string;
    target_type: string;
    target_ref: string;
    category: string;
    description: string | null;
    evidence: {
      content?: string | null;
      author_ref?: string;
      channel_ref?: string;
      created_at?: string;
    };
    status: string;
    created_at: string;
    resolution: string | null;
  }
  interface Block {
    domain: string;
    level: 'silence' | 'suspend';
    include_subdomains: boolean;
    reason: string | null;
  }
  interface Operator {
    id: string;
    role: string;
    user: { id: string; origin_domain: string; username: string; display_name: string | null };
    created_at: string;
  }
  interface Audit {
    id: string;
    actor_ref: string | null;
    actor_kind: string;
    action: string;
    target_type: string;
    target_ref: string;
    metadata: Record<string, unknown>;
    created_at: string;
  }
  let me = $state<AdminIdentity | null>(null);
  let overview = $state<Record<string, number>>({});
  let users = $state<User[]>([]);
  let apps = $state<App[]>([]);
  let reports = $state<Report[]>([]);
  let blocks = $state<Block[]>([]);
  let operators = $state<Operator[]>([]);
  let audits = $state<Audit[]>([]);
  let view = $state<View>('overview');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let userQuery = $state('');
  let blockDomain = $state('');
  let blockLevel = $state<'silence' | 'suspend'>('suspend');
  let blockReason = $state('');
  let operatorRef = $state('');
  let operatorRole = $state('administrator');
  const reportStatuses = [
    'triaged',
    'in_review',
    'awaiting_remote',
    'needs_information',
    'action_taken',
    'closed_no_action',
    'duplicate',
    'reopened'
  ];
  const roleOptions = ['administrator', 'trust_safety', 'bot_reviewer', 'operations', 'auditor'];
  function can(capability: string) {
    return me?.capabilities.includes(capability) ?? false;
  }
  async function loadAll() {
    try {
      me = await api<AdminIdentity>('/administration/@me');
      const requests: Promise<unknown>[] = [
        api('/administration/overview').then((x) => (overview = x as Record<string, number>))
      ];
      if (can('admin.read'))
        requests.push(
          api<User[]>('/administration/users').then((x) => (users = x)),
          api<App[]>('/administration/applications').then((x) => (apps = x)),
          api<Block[]>('/administration/instances/blocks').then((x) => (blocks = x)),
          api<Operator[]>('/administration/operators').then((x) => (operators = x))
        );
      if (can('reports.read'))
        requests.push(api<Report[]>('/administration/reports').then((x) => (reports = x)));
      if (can('audit.read'))
        requests.push(api<Audit[]>('/administration/audit').then((x) => (audits = x)));
      await Promise.all(requests);
    } catch (caught) {
      error = userErrorMessage(caught, 'Administration is unavailable for this account.');
    }
  }
  async function searchUsers() {
    try {
      users = await api<User[]>(`/administration/users?query=${encodeURIComponent(userQuery)}`);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not search users.');
    }
  }
  async function patchUser(user: User) {
    const disabled = !user.disabled_at;
    if (!confirm(`${disabled ? 'Disable' : 'Enable'} ${user.username}?`)) return;
    try {
      const updated = await api<User>(`/administration/users/${user.id}@${user.origin_domain}`, {
        method: 'PATCH',
        body: JSON.stringify({ disabled, reason: null })
      });
      users = users.map((x) =>
        x.id === user.id && x.origin_domain === user.origin_domain ? updated : x
      );
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the account.');
    }
  }
  async function patchApp(app: App) {
    const status = app.status === 'suspended' ? 'active' : 'suspended';
    if (!confirm(`${status === 'suspended' ? 'Suspend' : 'Activate'} ${app.name}?`)) return;
    try {
      const updated = await api<{ status: string }>(
        `/administration/applications/${encodeURIComponent(app.ref)}`,
        { method: 'PATCH', body: JSON.stringify({ status, reason: null }) }
      );
      app.status = updated.status;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the application.');
    }
  }
  async function patchReport(report: Report, status: string) {
    try {
      const updated = await api<Report>(`/administration/reports/${report.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status, resolution: report.resolution })
      });
      reports = reports.map((x) => (x.id === report.id ? updated : x));
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the report.');
    }
  }
  async function addBlock() {
    if (!blockDomain.trim()) return;
    busy = true;
    try {
      await api('/administration/instances/blocks', {
        method: 'PUT',
        body: JSON.stringify({
          domain: blockDomain.trim(),
          level: blockLevel,
          include_subdomains: false,
          reason: blockReason || null
        })
      });
      blockDomain = '';
      blockReason = '';
      blocks = await api('/administration/instances/blocks');
      notice = 'Federation policy updated.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update federation policy.');
    } finally {
      busy = false;
    }
  }
  async function removeBlock(domain: string) {
    if (!confirm(`Remove the block for ${domain}?`)) return;
    await api(`/administration/instances/blocks/${encodeURIComponent(domain)}`, {
      method: 'DELETE'
    });
    blocks = blocks.filter((x) => x.domain !== domain);
  }
  async function addOperator() {
    if (!operatorRef.trim()) return;
    try {
      await api('/administration/operators', {
        method: 'POST',
        body: JSON.stringify({ user_ref: operatorRef.trim(), role: operatorRole })
      });
      operatorRef = '';
      operators = await api('/administration/operators');
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not grant the role.');
    }
  }
  async function removeOperator(operator: Operator) {
    if (
      operator.role === 'owner' ||
      !confirm(`Revoke ${operator.role} from ${operator.user.username}?`)
    )
      return;
    await api(`/administration/operators/${operator.id}`, { method: 'DELETE' });
    operators = operators.filter((x) => x.id !== operator.id);
  }
  onMount(() => void loadAll());
</script>

<svelte:head><title>Instance Administration · Kaede Chat</title></svelte:head>
<main class="admin-shell">
  <aside>
    <a class="back" href={resolve('/settings')}>← User settings</a>
    <p>Kaede instance</p>
    <h1>Administration</h1>
    {#if me}<small>{me.user.display_name ?? me.user.username}<br />{me.roles.join(' · ')}</small
      >{/if}
    <nav>
      {#each ['overview', 'users', 'applications', 'reports', 'instances', 'operators', 'audit'] as item}<button
          class:active={view === item}
          onclick={() => (view = item as View)}>{item}</button
        >{/each}
    </nav>
  </aside>
  <section class="content">
    <header>
      <div>
        <span>Instance control center</span>
        <h2>{view[0].toUpperCase() + view.slice(1)}</h2>
      </div>
      <a href={resolve('/home')} aria-label="Close">×</a>
    </header>
    {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
        class="notice"
      >
        {notice}
      </div>{/if}
    {#if !me && !error}<p>Loading administration…</p>
    {:else if view === 'overview'}<div class="metrics">
        {#each Object.entries(overview) as [label, value]}<article>
            <strong>{value.toLocaleString()}</strong><span>{label.replaceAll('_', ' ')}</span>
          </article>{/each}
      </div>
      <section class="panel">
        <h3>Administrative access</h3>
        <p>
          Owner grants are managed only from the server CLI. Delegated roles are auditable and can
          be revoked here. Browser sessions use normal user authentication; the static server admin
          token is never sent to this panel.
        </p>
      </section>
    {:else if view === 'users'}<section class="panel">
        <div class="toolbar">
          <input
            bind:value={userQuery}
            placeholder="Search local usernames"
            onkeydown={(e) => e.key === 'Enter' && searchUsers()}
          /><button onclick={searchUsers}>Search</button>
        </div>
        <div class="table">
          {#each users as user}<article>
              <div>
                <strong>{user.display_name ?? user.username}</strong><small
                  >{user.username}@{user.origin_domain} · {user.account_type ?? 'human'}</small
                >
              </div>
              <span class:bad={user.disabled_at}>{user.disabled_at ? 'Disabled' : 'Active'}</span
              >{#if can('users.manage') && user.account_type !== 'bot'}<button
                  class:danger={user.disabled_at === null}
                  onclick={() => patchUser(user)}>{user.disabled_at ? 'Enable' : 'Disable'}</button
                >{/if}
            </article>{/each}
        </div>
      </section>
    {:else if view === 'applications'}<section class="panel">
        <p>
          Suspending an app immediately suspends its active installations and advances its
          revocation generation.
        </p>
        <div class="table">
          {#each apps as app}<article>
              <div><strong>{app.name}</strong><small>{app.ref} · team {app.team_ref}</small></div>
              <span class:bad={app.status === 'suspended'}>{app.status}</span
              >{#if can('bots.manage')}<button
                  class:danger={app.status !== 'suspended'}
                  onclick={() => patchApp(app)}
                  >{app.status === 'suspended' ? 'Activate' : 'Suspend'}</button
                >{/if}
            </article>{/each}
        </div>
      </section>
    {:else if view === 'reports'}<section class="panel">
        <p>
          Reports contain only plaintext message evidence the reporter can access. E2EE content is
          not submitted to the instance.
        </p>
        <div class="table reports">
          {#each reports as report}<article>
              <div>
                <strong>#{report.id} · {report.category}</strong><small
                  >{report.target_type}
                  {report.target_ref} · {new Date(report.created_at).toLocaleString()}</small
                >
                <p>{report.description ?? 'No reporter note.'}</p>
                {#if report.evidence.content}
                  <blockquote>
                    <small>
                      Message by {report.evidence.author_ref ?? 'unknown'} in
                      {report.evidence.channel_ref ?? 'unknown'}
                    </small>
                    <p>{report.evidence.content}</p>
                  </blockquote>
                {/if}
                <textarea bind:value={report.resolution} placeholder="Internal resolution note"
                ></textarea>
              </div>
              {#if can('reports.manage')}<select
                  value={report.status}
                  onchange={(e) => patchReport(report, e.currentTarget.value)}
                  >{#each reportStatuses as status}<option value={status}
                      >{status.replaceAll('_', ' ')}</option
                    >{/each}</select
                >{:else}<span>{report.status}</span>{/if}
            </article>{/each}
        </div>
      </section>
    {:else if view === 'instances'}<section class="panel">
        <p>
          <b>Silence</b> stops inbound delivery and reports. <b>Suspend</b> also disables shared replicas
          and outbound contact. Exact domains are the safe default.
        </p>
        {#if can('instances.manage')}<div class="toolbar">
            <input bind:value={blockDomain} placeholder="instance.example" /><select
              bind:value={blockLevel}
              ><option value="silence">Silence</option><option value="suspend">Suspend</option
              ></select
            ><input bind:value={blockReason} placeholder="Reason (optional)" /><button
              disabled={busy}
              onclick={addBlock}>Apply</button
            >
          </div>{/if}
        <div class="table">
          {#each blocks as block}<article>
              <div>
                <strong>{block.domain}</strong><small
                  >{block.reason ?? 'No public reason'}{block.include_subdomains
                    ? ' · includes subdomains'
                    : ''}</small
                >
              </div>
              <span class="bad">{block.level}</span>{#if can('instances.manage')}<button
                  onclick={() => removeBlock(block.domain)}>Remove</button
                >{/if}
            </article>{/each}
        </div>
      </section>
    {:else if view === 'operators'}<section class="panel">
        <p>Owners are CLI-only. An Owner may delegate the fixed operational roles below.</p>
        {#if me?.roles.includes('owner')}<div class="toolbar">
            <input bind:value={operatorRef} placeholder="User ID (snowflake@instance)" /><select
              bind:value={operatorRole}
              >{#each roleOptions as role}<option value={role}>{role.replaceAll('_', ' ')}</option
                >{/each}</select
            ><button onclick={addOperator}>Grant role</button>
          </div>{/if}
        <div class="table">
          {#each operators as operator}<article>
              <div>
                <strong>{operator.user.display_name ?? operator.user.username}</strong><small
                  >{operator.user.id}@{operator.user.origin_domain}</small
                >
              </div>
              <span>{operator.role.replaceAll('_', ' ')}</span
              >{#if me?.roles.includes('owner') && operator.role !== 'owner'}<button
                  onclick={() => removeOperator(operator)}>Revoke</button
                >{/if}
            </article>{/each}
        </div>
      </section>
    {:else if view === 'audit'}<section class="panel">
        <div class="table">
          {#each audits as event}<article>
              <div>
                <strong>{event.action}</strong><small
                  >{event.actor_ref ?? event.actor_kind} · {new Date(
                    event.created_at
                  ).toLocaleString()}</small
                >
                <p>{event.target_type}: {event.target_ref}</p>
              </div>
              <code>{JSON.stringify(event.metadata)}</code>
            </article>{/each}
        </div>
      </section>{/if}
  </section>
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  .admin-shell {
    min-height: 100dvh;
    display: grid;
    grid-template-columns: 250px minmax(0, 1fr);
    color: var(--text);
    background: var(--bg);
  }
  aside {
    position: sticky;
    top: 0;
    height: 100dvh;
    box-sizing: border-box;
    border-right: 1px solid var(--line);
    padding: 1.7rem 1.2rem;
    background: var(--surface);
  }
  aside > p {
    margin: 2rem 0 0.2rem;
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 800;
    text-transform: uppercase;
  }
  aside h1 {
    margin: 0 0 0.3rem;
  }
  aside small,
  .back {
    color: var(--text-muted);
  }
  .back {
    text-decoration: none;
  }
  nav {
    display: grid;
    gap: 0.2rem;
    margin-top: 2rem;
  }
  nav button {
    border: 0;
    border-radius: 8px;
    padding: 0.65rem 0.75rem;
    color: var(--text-muted);
    background: transparent;
    text-align: left;
    text-transform: capitalize;
    cursor: pointer;
  }
  nav button.active,
  nav button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
  .content {
    padding: 2.5rem clamp(1rem, 4vw, 4rem) 5rem;
    min-width: 0;
  }
  .content > header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .content > header span {
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 800;
    text-transform: uppercase;
  }
  .content > header h2 {
    margin: 0.2rem 0;
    font-size: 2.2rem;
  }
  .content > header a {
    color: var(--text-muted);
    font-size: 2rem;
    text-decoration: none;
  }
  .metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.7rem;
    margin: 1.5rem 0;
  }
  .metrics article,
  .panel {
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface);
  }
  .metrics article {
    display: grid;
    padding: 1rem;
  }
  .metrics strong {
    font-size: 1.8rem;
  }
  .metrics span,
  .panel > p,
  .table small {
    color: var(--text-muted);
  }
  .panel {
    margin-top: 1rem;
    padding: 1rem;
  }
  .panel h3 {
    margin-top: 0;
  }
  .toolbar {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }
  .toolbar input {
    min-width: 0;
    flex: 1;
  }
  input,
  textarea,
  select {
    box-sizing: border-box;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0.65rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
  }
  button {
    border: 0;
    border-radius: 7px;
    padding: 0.6rem 0.8rem;
    font: inherit;
    font-weight: 750;
    cursor: pointer;
  }
  .toolbar button {
    color: var(--on-accent, white);
    background: var(--accent);
  }
  .table {
    display: grid;
  }
  .table article {
    display: flex;
    gap: 0.8rem;
    align-items: center;
    border-top: 1px solid var(--line);
    padding: 0.8rem 0;
  }
  .table article > div {
    display: grid;
    min-width: 0;
    flex: 1;
  }
  .table span {
    border-radius: 999px;
    padding: 0.2rem 0.5rem;
    color: white;
    background: var(--success);
    font-size: 0.72rem;
  }
  .table span.bad,
  .danger {
    color: white;
    background: var(--danger);
  }
  .table code {
    max-width: 35%;
    overflow: hidden;
    color: var(--text-muted);
    font-size: 0.72rem;
    text-overflow: ellipsis;
  }
  .reports article {
    align-items: flex-start;
  }
  .reports textarea {
    width: 100%;
    margin-top: 0.5rem;
  }
  .reports p,
  .table p {
    margin: 0.3rem 0;
  }
  .notice {
    margin: 1rem 0;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.8rem;
  }
  .notice.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  @media (max-width: 760px) {
    .admin-shell {
      display: block;
    }
    aside {
      position: static;
      height: auto;
      padding: 1rem;
    }
    aside > p,
    aside h1,
    aside small {
      display: none;
    }
    nav {
      display: flex;
      overflow-x: auto;
      margin-top: 1rem;
    }
    nav button {
      flex: 0 0 auto;
    }
    .content {
      padding: 1.2rem 1rem 4rem;
    }
    .toolbar {
      display: grid;
    }
    .table article {
      align-items: flex-start;
      flex-wrap: wrap;
    }
    .metrics {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
