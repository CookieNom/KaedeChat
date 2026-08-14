<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { resolve } from '$app/paths';
  import { onMount } from 'svelte';

  let { data } = $props<{ data: { applicationRef: string } }>();
  const ref = $derived(data.applicationRef);
  interface Application {
    ref: string;
    name: string;
    description: string | null;
    status: string;
    target_policy: string;
    default_scopes: string[];
    default_intents: string[];
    default_permissions: string;
    e2ee_modes: string[];
    bot_user: { handle: string };
  }
  interface Credential {
    id: string;
    label: string;
    token_hint: string;
    scopes: string[];
    created_at: string;
    last_used_at: string | null;
    revoked_at: string | null;
  }
  interface Worker {
    id: string;
    name: string;
    scopes: string[];
    intents: string[];
    target_domains: string[];
    revoked_at: string | null;
  }
  interface Template {
    id: string;
    slug: string;
    name: string;
    description: string | null;
    scopes: string[];
    intents: string[];
    permissions: string;
    e2ee_mode: string;
    active: boolean;
    invite_url: string;
  }
  interface Installation {
    id: string;
    guild_ref: string;
    status: string;
    scopes: string[];
    intents: string[];
    permissions: string;
    e2ee_mode: string;
  }
  interface Rule {
    target_domain: string;
    effect: 'allow' | 'deny';
  }

  const scopes = [
    'applications.commands',
    'interactions.respond',
    'guilds.read',
    'channels.read',
    'members.read',
    'roles.read',
    'messages.metadata',
    'messages.content',
    'messages.history',
    'messages.send',
    'messages.edit.own',
    'messages.delete.own',
    'messages.manage',
    'attachments.read',
    'attachments.write',
    'reactions.read',
    'reactions.write',
    'moderation.members',
    'moderation.messages',
    'voice.states.read',
    'dm.send'
  ];
  const intents = [
    'guilds',
    'guild_members',
    'guild_presences',
    'guild_messages',
    'message_content',
    'message_reactions',
    'voice_states',
    'interactions'
  ];
  let application = $state<Application | null>(null);
  let credentials = $state<Credential[]>([]);
  let workers = $state<Worker[]>([]);
  let templates = $state<Template[]>([]);
  let installations = $state<Installation[]>([]);
  let rules = $state<Rule[]>([]);
  let commandsText = $state('[]');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let credentialLabel = $state('Deployment');
  let credentialToken = $state('');
  let workerName = $state('Production worker');
  let workerKey = $state('');
  let workerTargets = $state('');
  let templateSlug = $state('install');
  let templateName = $state('Install bot');
  let templateDescription = $state('');
  let ruleDomain = $state('');
  let ruleEffect = $state<'allow' | 'deny'>('deny');

  async function load() {
    error = '';
    try {
      const [
        app,
        commandList,
        credentialList,
        workerList,
        templateList,
        installationList,
        ruleList
      ] = await Promise.all([
        api<Application>(`/applications/${encodeURIComponent(ref)}`),
        api<Record<string, unknown>[]>(`/applications/${encodeURIComponent(ref)}/commands`),
        api<Credential[]>(`/applications/${encodeURIComponent(ref)}/credentials`),
        api<Worker[]>(`/applications/${encodeURIComponent(ref)}/workers`),
        api<Template[]>(`/applications/${encodeURIComponent(ref)}/install-templates`),
        api<Installation[]>(`/applications/${encodeURIComponent(ref)}/installations`),
        api<Rule[]>(`/applications/${encodeURIComponent(ref)}/instance-rules`)
      ]);
      application = app;
      commandsText = JSON.stringify(commandList, null, 2);
      credentials = credentialList;
      workers = workerList;
      templates = templateList;
      installations = installationList;
      rules = ruleList;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load this application.');
    }
  }

  function toggle(list: string[], value: string) {
    return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
  }
  async function saveApplication() {
    if (!application) return;
    busy = true;
    error = '';
    notice = '';
    try {
      application = await api<Application>(`/applications/${encodeURIComponent(ref)}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: application.name,
          description: application.description,
          target_policy: application.target_policy,
          default_scopes: application.default_scopes,
          default_intents: application.default_intents,
          default_permissions: Number(application.default_permissions),
          e2ee_modes: application.e2ee_modes
        })
      });
      notice = 'Application settings saved.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save the application.');
    } finally {
      busy = false;
    }
  }
  async function saveCommands() {
    busy = true;
    error = '';
    try {
      const parsed = JSON.parse(commandsText);
      if (!Array.isArray(parsed)) throw new Error('Commands must be a JSON array.');
      await api(`/applications/${encodeURIComponent(ref)}/commands`, {
        method: 'PUT',
        body: JSON.stringify({ commands: parsed })
      });
      notice = 'Commands published.';
      await load();
    } catch (caught) {
      error =
        caught instanceof SyntaxError ||
        (caught instanceof Error && caught.message.startsWith('Commands'))
          ? caught.message
          : userErrorMessage(caught, 'Could not publish commands.');
    } finally {
      busy = false;
    }
  }
  async function createCredential() {
    busy = true;
    error = '';
    credentialToken = '';
    try {
      const created = await api<{ token: string }>(
        `/applications/${encodeURIComponent(ref)}/credentials`,
        {
          method: 'POST',
          body: JSON.stringify({
            label: credentialLabel,
            scopes: ['workers.manage', 'commands.manage']
          })
        }
      );
      credentialToken = created.token;
      notice = 'Control credential created. Copy it now; it will not be shown again.';
      await load();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the control credential.');
    } finally {
      busy = false;
    }
  }
  async function revokeCredential(id: string) {
    if (!confirm('Revoke this control credential?')) return;
    await api(`/applications/${encodeURIComponent(ref)}/credentials/${id}`, {
      method: 'DELETE'
    });
    await load();
  }
  async function createWorker() {
    if (!application) return;
    busy = true;
    error = '';
    try {
      await api(`/applications/${encodeURIComponent(ref)}/workers`, {
        method: 'POST',
        body: JSON.stringify({
          name: workerName,
          public_key: workerKey.trim(),
          scopes: application.default_scopes,
          intents: application.default_intents,
          target_domains: workerTargets
            .split(',')
            .map((x) => x.trim())
            .filter(Boolean),
          session_limit: 1
        })
      });
      workerKey = '';
      notice = 'Worker enrolled.';
      await load();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not enroll the worker.');
    } finally {
      busy = false;
    }
  }
  async function revokeWorker(id: string) {
    if (!confirm('Revoke this worker? Existing tokens and gateway sessions will stop working.'))
      return;
    await api(`/applications/${encodeURIComponent(ref)}/workers/${id}`, { method: 'DELETE' });
    await load();
  }
  async function createTemplate() {
    if (!application) return;
    busy = true;
    error = '';
    try {
      await api(`/applications/${encodeURIComponent(ref)}/install-templates`, {
        method: 'POST',
        body: JSON.stringify({
          slug: templateSlug,
          name: templateName,
          description: templateDescription || null,
          scopes: application.default_scopes,
          intents: application.default_intents,
          permissions: Number(application.default_permissions),
          contexts: ['guild'],
          e2ee_mode: application.e2ee_modes.includes('interaction_only')
            ? 'interaction_only'
            : 'participant'
        })
      });
      notice = 'Invite link created.';
      await load();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the invite link.');
    } finally {
      busy = false;
    }
  }
  async function addRule() {
    if (!ruleDomain.trim()) return;
    try {
      await api(
        `/applications/${encodeURIComponent(ref)}/instance-rules/${encodeURIComponent(ruleDomain.trim())}`,
        { method: 'PUT', body: JSON.stringify({ effect: ruleEffect }) }
      );
      ruleDomain = '';
      await load();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save the instance rule.');
    }
  }
  async function deleteRule(domain: string) {
    await api(
      `/applications/${encodeURIComponent(ref)}/instance-rules/${encodeURIComponent(domain)}`,
      { method: 'DELETE' }
    );
    await load();
  }
  async function copy(value: string, label = 'Invite link') {
    await navigator.clipboard.writeText(value);
    notice = `${label} copied.`;
  }
  onMount(() => void load());
</script>

<svelte:head><title>{application?.name ?? 'Application'} · Developer Portal</title></svelte:head>
<main class="page">
  <header class="top">
    <a href="/developers">← Applications</a>
    <div>
      <small>Developer Portal</small>
      <h1>{application?.name ?? 'Loading…'}</h1>
      <p>{application?.bot_user.handle ?? ref}</p>
    </div>
    <button onclick={saveApplication} disabled={busy || !application}>Save changes</button>
  </header>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
      class="notice success"
    >
      {notice}<button aria-label="Dismiss" onclick={() => (notice = '')}>×</button>
    </div>{/if}
  {#if application}
    <div class="layout">
      <nav>
        <a href="#general">General</a><a href="#access">Access</a><a href="#credentials"
          >Credentials</a
        ><a href="#commands">Commands</a><a href="#workers">Workers</a><a href="#invites"
          >Invite links</a
        ><a href="#federation">Federation</a><a href="#installations">Installations</a>
      </nav>
      <div class="sections">
        <section id="general">
          <h2>General information</h2>
          <div class="grid">
            <label>Name<input bind:value={application.name} maxlength="100" /></label><label
              >Status<input value={application.status} disabled /></label
            >
          </div>
          <label
            >Description<textarea bind:value={application.description} rows="3" maxlength="1000"
            ></textarea></label
          >
        </section>
        <section id="access">
          <h2>API access</h2>
          <p>
            Scopes control what the bot can request. Intents control which live events are
            delivered. A guild may approve less.
          </p>
          <h3>Scopes</h3>
          <div class="chips">
            {#each scopes as scope}<label class:active={application.default_scopes.includes(scope)}
                ><input
                  type="checkbox"
                  checked={application.default_scopes.includes(scope)}
                  onchange={() =>
                    application &&
                    (application.default_scopes = toggle(application.default_scopes, scope))}
                />{scope}</label
              >{/each}
          </div>
          <h3>Gateway intents</h3>
          <div class="chips">
            {#each intents as intent}<label
                class:active={application.default_intents.includes(intent)}
                ><input
                  type="checkbox"
                  checked={application.default_intents.includes(intent)}
                  onchange={() =>
                    application &&
                    (application.default_intents = toggle(application.default_intents, intent))}
                />{intent}</label
              >{/each}
          </div>
          <div class="grid">
            <label
              >Default permission bits<input
                type="number"
                min="0"
                bind:value={application.default_permissions}
              /></label
            ><label
              >Target policy<select bind:value={application.target_policy}
                ><option value="open">Open federation</option><option value="allowlist"
                  >Allowlist only</option
                ><option value="blocklist">Open except blocked instances</option><option
                  value="local_only">Local instance only</option
                ></select
              ></label
            >
          </div>
          <p class="warning">
            Message content and history remain unavailable in E2EE channels unless the bot is
            installed as a visible cryptographic participant. Interaction-only bots receive only
            command payloads users explicitly submit.
          </p>
        </section>
        <section id="credentials">
          <h2>Control credentials</h2>
          <p>
            Deployment tools use these scoped secrets only to enroll workers and publish command
            definitions. They cannot connect as the bot or sign in as a user.
          </p>
          <div class="inline">
            <input bind:value={credentialLabel} maxlength="100" placeholder="Deployment" /><button
              onclick={createCredential}
              disabled={busy || !credentialLabel.trim()}>Create credential</button
            >
          </div>
          {#if credentialToken}
            <div class="secret" role="status">
              <strong>Copy this token now</strong><code>{credentialToken}</code><button
                onclick={() => copy(credentialToken, 'Credential')}>Copy</button
              >
            </div>
          {/if}
          <div class="rows">
            {#each credentials as credential}<article>
                <div>
                  <strong>{credential.label}</strong><small
                    >{credential.token_hint} · {credential.scopes.join(', ')}</small
                  >
                </div>
                <span class:revoked={credential.revoked_at}
                  >{credential.revoked_at ? 'Revoked' : 'Active'}</span
                >{#if !credential.revoked_at}<button
                    class="danger"
                    onclick={() => revokeCredential(credential.id)}>Revoke</button
                  >{/if}
              </article>{/each}
          </div>
        </section>
        <section id="commands">
          <h2>Slash and context commands</h2>
          <p>
            Publish up to 100 definitions. Names use lowercase letters, numbers, dashes, and
            underscores.
          </p>
          <textarea class="code" bind:value={commandsText} rows="14" spellcheck="false"
          ></textarea><button onclick={saveCommands} disabled={busy}>Publish commands</button>
        </section>
        <section id="workers">
          <h2>Worker keys</h2>
          <p>
            A worker signs short-lived token assertions and connects directly to every target
            instance. Private keys never leave the worker.
          </p>
          <div class="grid">
            <label>Worker name<input bind:value={workerName} /></label><label
              >Ed25519 public key (base64url)<input
                bind:value={workerKey}
                placeholder="43-character public key"
              /></label
            >
          </div>
          <label
            >Target domains (comma separated, empty means any approved target)<input
              bind:value={workerTargets}
              placeholder="chat.example, community.example"
            /></label
          ><button onclick={createWorker} disabled={busy || workerKey.length < 43}
            >Enroll worker</button
          >
          <div class="rows">
            {#each workers as worker}<article>
                <div>
                  <strong>{worker.name}</strong><small
                    >#{worker.id} · {worker.target_domains.join(', ') ||
                      'all approved targets'}</small
                  >
                </div>
                <span class:revoked={worker.revoked_at}
                  >{worker.revoked_at ? 'Revoked' : 'Active'}</span
                >{#if !worker.revoked_at}<button
                    class="danger"
                    onclick={() => revokeWorker(worker.id)}>Revoke</button
                  >{/if}
              </article>{/each}
          </div>
        </section>
        <section id="invites">
          <h2>Bot invite links</h2>
          <p>
            Invite pages show the app origin, requested permissions, data access, and E2EE behavior
            before an administrator approves.
          </p>
          <div class="grid">
            <label>Slug<input bind:value={templateSlug} /></label><label
              >Invite name<input bind:value={templateName} /></label
            >
          </div>
          <label>Description<input bind:value={templateDescription} /></label><button
            onclick={createTemplate}
            disabled={busy}>Create invite link</button
          >
          <div class="rows">
            {#each templates as template}<article>
                <div>
                  <strong>{template.name}</strong><small
                    >{template.e2ee_mode} · {template.active ? 'active' : 'disabled'}</small
                  >
                </div>
                <code>{template.invite_url}</code><button onclick={() => copy(template.invite_url)}
                  >Copy</button
                >
              </article>{/each}
          </div>
        </section>
        <section id="federation">
          <h2>Federated instance policy</h2>
          <p>
            Rules match exact verified instance domains. Deny always wins. Wildcards are not
            supported.
          </p>
          <div class="inline">
            <input bind:value={ruleDomain} placeholder="instance.example" /><select
              bind:value={ruleEffect}
              ><option value="deny">Deny</option><option value="allow">Allow</option></select
            ><button onclick={addRule}>Add rule</button>
          </div>
          <div class="rows">
            {#each rules as rule}<article>
                <code>{rule.target_domain}</code><span class:revoked={rule.effect === 'deny'}
                  >{rule.effect}</span
                ><button onclick={() => deleteRule(rule.target_domain)}>Remove</button>
              </article>{/each}
          </div>
        </section>
        <section id="installations">
          <h2>Installations</h2>
          <div class="rows">
            {#each installations as installation}<article>
                <div>
                  <strong>{installation.guild_ref}</strong><small
                    >{installation.e2ee_mode} · revision {installation.id}</small
                  >
                </div>
                <span class:revoked={installation.status !== 'active'}>{installation.status}</span>
              </article>{/each}{#if installations.length === 0}<p>
                No guilds have installed this application.
              </p>{/if}
          </div>
        </section>
      </div>
    </div>
  {/if}
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  .page {
    min-height: 100dvh;
    padding: 1.5rem clamp(1rem, 4vw, 4rem) 5rem;
    background: var(--bg);
    color: var(--text);
  }
  .top {
    display: grid;
    grid-template-columns: 180px 1fr auto;
    gap: 1rem;
    align-items: center;
    max-width: 1280px;
    margin: auto;
  }
  .top a {
    color: var(--text-muted);
    text-decoration: none;
  }
  .top h1,
  .top p {
    margin: 0;
  }
  .top small {
    color: var(--accent);
    font-weight: 800;
    text-transform: uppercase;
  }
  .top button,
  section > button,
  .inline button,
  .rows button {
    border: 0;
    border-radius: 8px;
    padding: 0.7rem 1rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 800;
    cursor: pointer;
  }
  .secret {
    display: grid;
    gap: 0.65rem;
    margin-top: 1rem;
    padding: 1rem;
    border: 1px solid var(--warning, #d7a447);
    border-radius: 10px;
    background: color-mix(in srgb, var(--warning, #d7a447) 10%, transparent);
  }
  .secret code {
    overflow-wrap: anywhere;
    user-select: all;
  }
  .layout {
    display: grid;
    grid-template-columns: 180px minmax(0, 900px);
    gap: 2rem;
    max-width: 1280px;
    margin: 2rem auto;
  }
  .layout > nav {
    position: sticky;
    top: 1rem;
    display: grid;
    align-content: start;
    gap: 0.2rem;
    height: max-content;
  }
  .layout > nav a {
    border-radius: 7px;
    padding: 0.55rem 0.7rem;
    color: var(--text-muted);
    text-decoration: none;
  }
  .layout > nav a:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
  .sections {
    display: grid;
    gap: 1rem;
  }
  section {
    scroll-margin-top: 1rem;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1.3rem;
    background: var(--surface);
  }
  section h2 {
    margin: 0 0 0.3rem;
  }
  section h3 {
    margin: 1.3rem 0 0.6rem;
  }
  section p {
    color: var(--text-muted);
  }
  label {
    display: grid;
    gap: 0.4rem;
    margin: 0.7rem 0;
    font-size: 0.8rem;
    font-weight: 700;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }
  input,
  textarea,
  select {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.7rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
  }
  .code {
    font-family: ui-monospace, monospace;
    font-size: 0.82rem;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }
  .chips label {
    display: block;
    margin: 0;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.35rem 0.65rem;
    color: var(--text-muted);
    cursor: pointer;
  }
  .chips label.active {
    border-color: var(--accent);
    color: var(--text);
    background: color-mix(in srgb, var(--accent) 15%, transparent);
  }
  .chips input {
    position: absolute;
    opacity: 0;
    width: 1px;
  }
  .warning {
    border-left: 3px solid var(--warning, #d79b36);
    padding: 0.7rem 1rem;
    background: var(--surface-hover);
  }
  .rows {
    display: grid;
    gap: 0.5rem;
    margin-top: 1rem;
  }
  .rows article {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    border-top: 1px solid var(--line);
    padding: 0.8rem 0;
  }
  .rows article > div {
    display: grid;
    flex: 1;
  }
  .rows small {
    color: var(--text-muted);
  }
  .rows code {
    overflow: hidden;
    flex: 1;
    text-overflow: ellipsis;
  }
  .rows span {
    border-radius: 999px;
    padding: 0.2rem 0.5rem;
    background: var(--success);
    color: white;
    font-size: 0.72rem;
  }
  .rows span.revoked,
  .danger {
    background: var(--danger) !important;
  }
  .inline {
    display: grid;
    grid-template-columns: 1fr 130px auto;
    gap: 0.5rem;
  }
  .notice {
    max-width: 1100px;
    margin: 1rem auto;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.8rem 1rem;
  }
  .notice.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  .notice.success {
    border-color: var(--success);
  }
  .notice button {
    float: right;
    border: 0;
    color: inherit;
    background: none;
    font-size: 1.2rem;
  }
  @media (max-width: 760px) {
    .top {
      grid-template-columns: 1fr auto;
    }
    .top > a {
      grid-column: 1/-1;
    }
    .layout {
      display: block;
    }
    .layout > nav {
      position: static;
      display: flex;
      overflow-x: auto;
      margin-bottom: 1rem;
    }
    .grid,
    .inline {
      grid-template-columns: 1fr;
    }
    .rows article {
      align-items: flex-start;
      flex-wrap: wrap;
    }
    .page {
      padding: 1rem 1rem 4rem;
    }
  }
</style>
