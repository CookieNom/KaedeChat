<script lang="ts">
  import { trackerFieldTypes, type TrackerField } from '$lib/task-tracker/types';
  let {
    fields,
    busy = false,
    onSave
  }: {
    fields: TrackerField[];
    busy?: boolean;
    onSave?: (fields: TrackerField[]) => Promise<void> | void;
  } = $props();
  let editing = $state<string | null>(null);
  let name = $state('');
  let type = $state<TrackerField['type']>('text');
  let choices = $state('');
  let removing = $state('');
  let localError = $state('');
  function edit(field?: TrackerField) {
    editing = field?.id ?? '';
    name = field?.name ?? '';
    type = field?.type ?? 'text';
    choices = field?.options.join('\n') ?? '';
    localError = '';
  }
  async function save() {
    const options = ['select', 'multiselect'].includes(type)
      ? choices
          .split('\n')
          .map((v) => v.trim())
          .filter(Boolean)
      : [];
    if (
      fields.some((f) => f.id !== editing && f.name.toLowerCase() === name.trim().toLowerCase())
    ) {
      localError = 'Choose a unique field name.';
      return;
    }
    if (
      ['select', 'multiselect'].includes(type) &&
      (!options.length ||
        options.length > 100 ||
        options.some((v) => v.length > 100) ||
        new Set(options).size !== options.length)
    ) {
      localError = 'Enter 1–100 unique choices, up to 100 characters each.';
      return;
    }
    const field: TrackerField = {
      id: editing || crypto.randomUUID(),
      name: name.trim(),
      type,
      options
    };
    await onSave?.(
      editing ? fields.map((f) => (f.id === editing ? field : f)) : [...fields, field]
    );
    // Keep the draft open when the parent reports a failed save.
    if (
      fields.some(
        (f) =>
          f.id === field.id &&
          f.name === field.name &&
          JSON.stringify(f.options) === JSON.stringify(field.options)
      )
    )
      editing = null;
  }
  function move(index: number, offset: number) {
    const next = [...fields];
    [next[index], next[index + offset]] = [next[index + offset], next[index]];
    void onSave?.(next);
  }
</script>

<section aria-labelledby="custom-fields-heading">
  <div class="heading">
    <div>
      <h3 id="custom-fields-heading">Custom task fields</h3>
      <p>Add details such as reviewers, estimates, or related channels to every task.</p>
    </div>
    <span>{fields.length}/50</span>
  </div>
  {#each fields as field, i (field.id)}
    <div class="field-row">
      <div class="field-name">
        <strong>{field.name}</strong><small>{trackerFieldTypes[field.type]}</small>
      </div>
      <div class="actions">
        <button
          type="button"
          disabled={busy || i === 0}
          onclick={() => move(i, -1)}
          aria-label={`Move ${field.name} up`}>↑</button
        >
        <button
          type="button"
          disabled={busy || i === fields.length - 1}
          onclick={() => move(i, 1)}
          aria-label={`Move ${field.name} down`}>↓</button
        >
        <button type="button" disabled={busy} onclick={() => edit(field)}>Edit</button>
        <button type="button" disabled={busy} onclick={() => (removing = field.id)}>Remove</button>
      </div>
      {#if removing === field.id}
        <div class="confirmation">
          <p>Remove “{field.name}” and its values from every task?</p>
          <button
            type="button"
            disabled={busy}
            onclick={() => void onSave?.(fields.filter((f) => f.id !== field.id))}
            >Remove field and values</button
          ><button type="button" disabled={busy} onclick={() => (removing = '')}>Keep field</button>
        </div>
      {/if}
    </div>
  {/each}
  {#if editing !== null}
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <label>Field name<input bind:value={name} maxlength="100" required disabled={busy} /></label>
      <label
        >Field type<select
          value={type}
          aria-label="Field type"
          onchange={(event) => (type = event.currentTarget.value as TrackerField['type'])}
          disabled={busy || Boolean(editing)}
          >{#each Object.entries(trackerFieldTypes) as [value, label] (value)}<option {value}
              >{label}</option
            >{/each}</select
        ></label
      >
      {#if ['select', 'multiselect'].includes(type)}<label class="wide"
          >Choices, one per line<textarea bind:value={choices} rows="4" required disabled={busy}
          ></textarea></label
        >{/if}
      {#if localError}<p class="wide" role="alert">{localError}</p>{/if}
      <div class="actions wide">
        <button class="primary" disabled={busy || !name.trim()}
          >{editing ? 'Save field' : 'Add field'}</button
        ><button type="button" disabled={busy} onclick={() => (editing = null)}>Cancel</button>
      </div>
    </form>
  {:else}
    <button
      class="primary"
      type="button"
      disabled={busy || fields.length >= 50}
      onclick={() => edit()}>Add custom field</button
    >
  {/if}
</section>

<style>
  section {
    display: grid;
    gap: 0.75rem;
  }
  .heading,
  .field-row,
  .actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .heading {
    justify-content: space-between;
  }
  h3,
  p {
    margin: 0;
  }
  p,
  small,
  .heading > span {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  p {
    margin-top: 0.3rem;
  }
  .field-row,
  form {
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    padding: 0.75rem;
    background: var(--surface-subtle);
  }
  .field-row {
    flex-wrap: wrap;
  }
  .field-name {
    flex: 1;
    min-width: 120px;
    overflow-wrap: anywhere;
    display: grid;
    gap: 0.2rem;
  }
  .actions {
    flex-wrap: wrap;
  }
  .confirmation {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .confirmation p {
    width: 100%;
  }
  button {
    border: 1px solid var(--line);
    border-radius: 8px;
    min-height: 36px;
    padding: 0.4rem 0.7rem;
    color: var(--text);
    background: var(--surface-raised);
    cursor: pointer;
    font: inherit;
    font-size: 0.75rem;
  }
  button.primary {
    background: var(--accent);
    color: var(--on-accent);
    justify-self: start;
  }
  form {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.75rem;
  }
  label {
    display: grid;
    gap: 0.4rem;
    font-size: 0.75rem;
    min-width: 0;
  }
  input,
  select,
  textarea {
    width: 100%;
    min-height: 40px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.6rem;
    color: var(--text);
    background: var(--surface-raised);
    font: inherit;
  }
  textarea {
    resize: vertical;
  }
  .wide {
    grid-column: 1 / -1;
  }
  :disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  @media (max-width: 620px) {
    form {
      grid-template-columns: minmax(0, 1fr);
    }
    .field-row > .actions {
      width: 100%;
    }
  }
</style>
