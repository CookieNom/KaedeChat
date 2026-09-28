<script lang="ts">
  import { measureRegionalLatency, type RegionalProbe } from '$lib/voice/rtc-probes';
  import { onMount } from 'svelte';
  import { api, userErrorMessage } from '$lib/api/client';

  interface Region {
    id: string;
    name: string;
    url: string;
    enabled: boolean;
  }
  interface Configuration {
    provider: 'builtin' | 'cinnamon';
    api_key: string;
    secret_configured: boolean;
    automatic_url: string;
    regions: Region[];
    default_region: string | null;
    allow_region_selection: boolean;
    webhook_url: string;
    webhooks: { accepted: number; pending: number; last_processed_at: string | null };
  }
  interface TestResult {
    region: string;
    ok: boolean;
    message: string;
    room: string;
    cleaned_up: boolean;
  }
  let config = $state<Configuration | null>(null);
  let secret = $state('');
  let savedKey = $state('');
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let dirty = $state(false);
  let results = $state<TestResult[]>([]);
  const hasSavedSecret = $derived(
    Boolean(config?.secret_configured && config.api_key === savedKey)
  );
  const canEnable = $derived(
    Boolean(config?.api_key && config.automatic_url && (secret.length >= 32 || hasSavedSecret))
  );
  const canTest = $derived(
    Boolean(config?.api_key && config.secret_configured && config.automatic_url) && !busy && !dirty
  );

  async function load() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      config = await api<Configuration>('/administration/rtc');
      savedKey = config.api_key;
      secret = '';
      dirty = false;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load RTC settings.');
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    void load();
  });

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!config || busy || (config.provider === 'cinnamon' && !canEnable)) return;
    busy = true;
    error = '';
    notice = '';
    try {
      config = await api<Configuration>('/administration/rtc', {
        method: 'PUT',
        body: JSON.stringify({
          provider: config.provider,
          api_key: config.api_key,
          api_secret: secret || null,
          automatic_url: config.automatic_url,
          regions: config.regions,
          default_region: config.default_region,
          allow_region_selection: config.allow_region_selection
        })
      });
      savedKey = config.api_key;
      secret = '';
      dirty = false;
      results = [];
      notice = 'RTC settings saved. Active calls keep their current provider and placement.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save RTC settings.');
    } finally {
      busy = false;
    }
  }
  async function test(region: string, fallback = false) {
    if (!canTest) return;
    busy = true;
    error = '';
    try {
      let latency = {};
      let probe_ticket = null;
      if (region === 'automatic' && !fallback) {
        const discovery = await api<{ probes: RegionalProbe[]; probe_ticket: string | null }>(
          '/administration/rtc/probes'
        );
        latency = await measureRegionalLatency({ regions: discovery.probes, max_age_seconds: 60 });
        probe_ticket = discovery.probe_ticket;
      }
      const result = await api<TestResult>('/administration/rtc/test', {
        method: 'POST',
        body: JSON.stringify({ region, latency, probe_ticket })
      });
      results = [...results.filter((r) => r.region !== region), result];
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not run the connection test.');
    } finally {
      busy = false;
    }
  }
</script>

<div class="rtc-settings">
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if !config}
    <p>{busy ? 'Loading RTC settings…' : 'RTC settings are unavailable.'}</p>
    <button type="button" disabled={busy} onclick={load}>Reload settings</button>
  {:else}
    <form
      onsubmit={save}
      oninput={() => {
        dirty = true;
        notice = '';
      }}
      onchange={() => {
        dirty = true;
      }}
    >
      <fieldset disabled={busy}>
        <legend>Voice and video provider</legend>
        <label
          >Provider<select bind:value={config.provider}
            ><option value="builtin">Built-in LiveKit</option><option value="cinnamon"
              >Cinnamon RTC</option
            ></select
          ></label
        >
        <p>Built-in LiveKit is the default. Configure Cinnamon below before enabling it.</p>
        <div class="columns">
          <label
            >Project API key<input
              autocomplete="off"
              bind:value={config.api_key}
              maxlength="128"
            /></label
          >
          <label
            >Project API secret<input
              type="password"
              autocomplete="new-password"
              bind:value={secret}
              placeholder={hasSavedSecret ? '•••••••• — saved' : 'Enter project secret'}
            />
            <small
              >{hasSavedSecret
                ? 'Leave blank to keep the saved secret for this key.'
                : 'Stored encrypted on the server.'}</small
            ></label
          >
        </div>
        <p>Use Cinnamon project credentials. Do not enter a Cinnamon administration token.</p>
        <label
          >Automatic-routing endpoint<input
            type="url"
            bind:value={config.automatic_url}
            placeholder="wss://"
          /></label
        >
        <h3>Regional endpoints</h3>
        {#if config.regions.length === 0}<p>
            No regions configured. Add the regions permitted for your project.
          </p>{/if}
        {#each config.regions as region, index (region)}
          <div class="region">
            <label>Region ID<input bind:value={region.id} required maxlength="63" /></label>
            <label>Display name<input bind:value={region.name} required maxlength="100" /></label>
            <label
              >Endpoint<input
                type="url"
                bind:value={region.url}
                required
                placeholder="wss://"
              /></label
            >
            <label class="check"
              ><input type="checkbox" bind:checked={region.enabled} /> Enabled</label
            >
            <button
              type="button"
              onclick={() => {
                config!.regions.splice(index, 1);
                dirty = true;
              }}>Remove region</button
            >
          </div>
        {/each}
        <button
          type="button"
          disabled={config.regions.length >= 64}
          onclick={() => {
            config!.regions.push({ id: '', name: '', url: '', enabled: true });
            dirty = true;
          }}>Add region</button
        >
        <div class="columns routing">
          <label
            >Default routing<select bind:value={config.default_region}
              ><option value={null}>Automatic</option
              >{#each config.regions.filter((r) => r.enabled && r.id) as region (region)}<option
                  value={region.id}>{region.name || region.id}</option
                >{/each}</select
            ></label
          >
          <label class="check"
            ><input type="checkbox" bind:checked={config.allow_region_selection} /> Allow users to select
            a region</label
          >
        </div>
        <p>
          Automatic uses successful client latency measurements. If every probe fails, Cinnamon
          chooses an eligible node using its load and capacity policy. Location is never collected
          or sent.
        </p>
        {#if config.provider === 'cinnamon' && !canEnable}
          <p>Enter the project key, secret, and automatic endpoint before enabling Cinnamon.</p>
        {/if}
        <button
          class="primary"
          type="submit"
          disabled={!dirty || busy || (config.provider === 'cinnamon' && !canEnable)}
          >{busy ? 'Working…' : 'Save RTC settings'}</button
        >
      </fieldset>
    </form>
    <section aria-label="RTC connection tests">
      <h3>Connection tests</h3>
      <p>
        Tests create and remove uniquely named disposable rooms. Save credentials and endpoints
        before testing.
      </p>
      {#if dirty}<p>Save your changes to test them.</p>{/if}
      <div class="actions">
        <button disabled={!canTest} onclick={() => test('automatic')}>Test Automatic</button>
        <button disabled={!canTest} onclick={() => test('automatic', true)}
          >Test without latency hints</button
        >
        {#each config.regions.filter((r) => r.enabled) as region (region)}<button
            disabled={!canTest}
            onclick={() => test(region.id)}>Test {region.name}</button
          >{/each}
      </div>
      {#each results as result (result.region)}<p class:error={!result.ok} role="status">
          <strong>{result.region}: {result.ok ? 'Passed' : 'Failed'}</strong> — {result.message}
        </p>{/each}
    </section>
    <section aria-label="Cinnamon webhook configuration">
      <h3>Cinnamon webhook</h3>
      <label
        >Public receiver URL<input
          readonly
          value={config.webhook_url}
          onclick={(event) => event.currentTarget.select()}
        /></label
      >
      <p>Signing API-key identifier: <code>{config.api_key || 'Not configured'}</code></p>
      <p>
        Enabled region IDs: {config.regions
          .filter((r) => r.enabled)
          .map((r) => r.id)
          .join(', ') || 'None configured'}
      </p>
      <p>
        On Cinnamon, permit automatic routing and manual routing for the enabled regions. Allow
        automatic routing without latency hints. Use the project key shown above to sign webhook
        deliveries.
      </p>
      <p>
        Webhook processing: {config.webhooks.accepted} accepted, {config.webhooks.pending} pending.
        {config.webhooks.last_processed_at
          ? `Last processed: ${new Date(config.webhooks.last_processed_at).toLocaleString()}`
          : 'No webhook processed yet.'}
      </p>
      <button disabled={busy || dirty} onclick={load}>Refresh webhook status</button>
    </section>
  {/if}
</div>

<style>
  .rtc-settings {
    display: grid;
    gap: 1.5rem;
    min-width: 0;
  }
  fieldset {
    display: grid;
    gap: 1rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    padding: 1.25rem;
    min-width: 0;
  }
  legend,
  h3 {
    font-weight: 600;
  }
  h3,
  p {
    margin: 0;
  }
  p,
  small {
    line-height: 1.5;
  }
  label {
    display: grid;
    gap: 0.4rem;
    min-width: 0;
  }
  input,
  select,
  button {
    font: inherit;
    min-height: 2.75rem;
    border: 1px solid var(--line);
    border-radius: 0.4rem;
    padding: 0.6rem 0.75rem;
    color: inherit;
    background: var(--surface-raised);
  }
  input,
  select {
    width: 100%;
    font-weight: 400;
    box-sizing: border-box;
  }
  button {
    cursor: pointer;
    width: fit-content;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .primary {
    background: var(--accent);
    color: var(--on-accent);
  }
  .columns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    align-items: start;
    gap: 1rem;
  }
  .region {
    display: grid;
    grid-template-columns: 1fr 1fr 2fr;
    gap: 1rem;
    padding: 1rem;
    border: 1px solid var(--line);
    border-radius: 0.5rem;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .check input {
    width: 1.2rem;
    min-height: 1.2rem;
  }
  .routing {
    margin-top: 0.5rem;
  }
  section {
    display: grid;
    gap: 0.85rem;
    min-width: 0;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  .error {
    color: var(--danger, #f98b93);
  }
  code {
    overflow-wrap: anywhere;
  }
  @media (max-width: 700px) {
    .columns,
    .region {
      grid-template-columns: 1fr;
    }
    fieldset {
      padding: 0.85rem;
    }
  }
</style>
