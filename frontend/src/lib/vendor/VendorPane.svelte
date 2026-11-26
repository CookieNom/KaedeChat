<script lang="ts">
  import { untrack } from 'svelte';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import type { Pane, Entry } from './panes';
  let { pane }: { pane: Pane } = $props();
  let rows = $state<Entry[]>(untrack(() => pane.records.map((row) => ({ ...row }))));
  let busy = $state(false);
  let message = $state('');
  let failed = $state(false);
  const dirty = $derived(JSON.stringify(rows) !== JSON.stringify(pane.records));
  const valid = $derived(
    rows.every((row) =>
      pane.fields.every((f) => f.type === 'checkbox' || String(row[f.key] ?? '').trim())
    )
  );
  const path = $derived('/administration/extensions/' + encodeURIComponent(pane.id));
  async function run(action?: string) {
    if (busy) return;
    busy = true;
    failed = false;
    message = '';
    try {
      if (action) {
        await api(path + '/' + encodeURIComponent(action), { method: 'POST' });
        message = 'Task completed.';
      } else {
        const response = await api<{ version: number; records: Entry[] }>(path, {
          method: 'PUT',
          body: JSON.stringify({ version: pane.version, records: rows })
        });
        pane.version = response.version;
        pane.records = response.records;
        rows = response.records.map((row) => ({ ...row }));
        message = 'Changes saved.';
      }
    } catch (error) {
      failed = true;
      if (
        error instanceof ApiError &&
        error.code === 'VENDOR_CONFIGURATION_INVALID' &&
        typeof error.detail.reason === 'string'
      )
        message = error.detail.reason;
      else if (error instanceof ApiError && error.code === 'VENDOR_CONFIGURATION_STALE')
        message = 'Another administrator saved changes. Refresh the page before editing.';
      else message = userErrorMessage(error, 'Could not complete this request.');
    } finally {
      busy = false;
    }
  }
</script>

<form
  onsubmit={(e) => {
    e.preventDefault();
    if (dirty && valid) void run();
  }}
>
  {#if !rows.length && pane.fields.length}<p>No entries configured.</p>{/if}
  {#each rows as row, i (row)}
    <fieldset disabled={busy}>
      <legend>{String(row.name || 'Entry ' + (i + 1))}</legend>
      <div class="fields">
        {#each pane.fields.filter((f) => f.type !== 'checkbox') as field (field.key)}
          <label
            >{field.label}<input
              required
              maxlength="253"
              placeholder={field.placeholder}
              value={String(row[field.key] ?? '')}
              oninput={(e) => (row[field.key] = e.currentTarget.value)}
            /></label
          >
        {/each}
      </div>
      <div class="checks">
        {#each pane.fields.filter((f) => f.type === 'checkbox') as field (field.key)}
          <label
            ><input
              type="checkbox"
              checked={row[field.key] === true}
              onchange={(e) => (row[field.key] = e.currentTarget.checked)}
            />
            {field.label}</label
          >
        {/each}
      </div>
      <button class="secondary-button" type="button" onclick={() => rows.splice(i, 1)}
        >Remove entry</button
      >
    </fieldset>
  {/each}
  <div class="buttons">
    {#if pane.fields.length}
      <button
        class="secondary-button"
        type="button"
        disabled={busy || rows.length >= 100}
        onclick={() => rows.push({})}>Add entry</button
      >
      <button class="primary-button" disabled={busy || !dirty || !valid}
        >{busy ? 'Working…' : 'Save changes'}</button
      >
    {/if}
    {#each pane.actions as [action, label] (action)}
      <button
        class="secondary-button"
        type="button"
        disabled={busy || dirty}
        onclick={() => run(action)}>{label}</button
      >
    {/each}
  </div>
  {#if message}<p role={failed ? 'alert' : 'status'}>{message}</p>{/if}
</form>

<style>
  form {
    display: grid;
    gap: 1rem;
  }
  fieldset {
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem;
    background: var(--surface);
  }
  legend {
    padding: 0 0.4rem;
    font-weight: 700;
    overflow-wrap: anywhere;
  }
  .fields {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 15rem), 1fr));
    gap: 0.85rem;
  }
  .fields label {
    display: grid;
    gap: 0.35rem;
    font-weight: 600;
  }
  .fields input {
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
    padding: 0.6rem;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--surface);
    color: inherit;
  }
  .checks,
  .buttons {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
  }
  .checks {
    margin: 0.75rem 0;
  }
  .checks label {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    min-height: 44px;
  }
</style>
