<script lang="ts">
  import { PERMISSION_METADATA } from '$lib/generated/permissions';
  import {
    permissionMask,
    permissionSelected,
    setPermissionSelected
  } from '$lib/chat/permission-selection';

  let {
    value,
    onChange,
    disabled = false,
    label = 'Default permissions'
  }: {
    value: string;
    onChange: (value: string) => void;
    disabled?: boolean;
    label?: string;
  } = $props();

  let search = $state('');
  const installPermissions = PERMISSION_METADATA.filter(
    (item) =>
      (item.resourceScopes as readonly string[]).includes('guild') ||
      (item.resourceScopes as readonly string[]).includes('channel')
  );
  const permissionLabels = new Map<string, string>(
    PERMISSION_METADATA.map((item) => [item.permission, item.label])
  );

  function dependencyLabels(dependencies: readonly string[]): string {
    return dependencies.map((name) => permissionLabels.get(name) ?? name).join(', ');
  }
  const visible = $derived.by(() => {
    const query = search.trim().toLocaleLowerCase();
    return query
      ? installPermissions.filter(
          (item) =>
            item.label.toLocaleLowerCase().includes(query) ||
            item.description.toLocaleLowerCase().includes(query) ||
            item.group.toLocaleLowerCase().includes(query)
        )
      : installPermissions;
  });
  const groups = $derived([...new Set(visible.map((item) => item.group))]);
  const validValue = $derived.by(() => {
    try {
      return permissionMask(value).toString();
    } catch {
      return '0';
    }
  });
  const selectedCount = $derived(
    installPermissions.filter((item) => permissionSelected(validValue, item.bit)).length
  );
</script>

<fieldset class="permission-checklist" {disabled}>
  <legend>{label}</legend>
  <p>
    Choose permissions by name. Kaede preserves the exact permission mask on the wire, including
    future bits this client does not recognize.
  </p>
  <div class="permission-checklist-tools">
    <label>
      <span class="visually-hidden">Search permissions</span>
      <input bind:value={search} type="search" placeholder="Search permissions" />
    </label>
    <span>{selectedCount} selected</span>
  </div>
  <div class="permission-checklist-groups">
    {#each groups as group (group)}
      <section>
        <h4>{group}</h4>
        {#each visible.filter((item) => item.group === group) as item (item.permission)}
          <label class:danger={item.danger === 'dangerous' || item.danger === 'critical'}>
            <input
              type="checkbox"
              checked={permissionSelected(validValue, item.bit)}
              onchange={(event) =>
                onChange(setPermissionSelected(validValue, item.bit, event.currentTarget.checked))}
            />
            <span>
              <strong>{item.label}</strong>
              <small>{item.description}</small>
              {#if item.dependencies.length}
                <small>Also requires: {dependencyLabels(item.dependencies)}</small>
              {/if}
            </span>
          </label>
        {/each}
      </section>
    {/each}
  </div>
</fieldset>

<style>
  .permission-checklist {
    display: grid;
    gap: 0.75rem;
    margin: 1rem 0;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 0.9rem;
  }

  legend {
    padding: 0 0.35rem;
    font-weight: 760;
  }

  p {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.76rem;
  }

  .permission-checklist-tools {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .permission-checklist-tools label {
    flex: 1;
  }

  .permission-checklist-tools input {
    width: 100%;
  }

  .permission-checklist-tools > span {
    color: var(--text-muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .permission-checklist-groups {
    display: grid;
    max-height: 420px;
    gap: 0.9rem;
    overflow-y: auto;
    padding-right: 0.25rem;
  }

  section {
    display: grid;
    gap: 0.3rem;
  }

  h4 {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.68rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  section > label {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: 0.6rem;
    border-radius: 8px;
    padding: 0.45rem 0.5rem;
  }

  section > label:hover {
    background: var(--surface-hover);
  }

  section > label input {
    margin-top: 0.2rem;
  }

  section > label span {
    display: grid;
    gap: 0.15rem;
  }

  section > label strong {
    font-size: 0.76rem;
  }

  section > label small {
    color: var(--text-muted);
    font-size: 0.67rem;
    line-height: 1.35;
  }

  section > label.danger strong {
    color: var(--danger);
  }

  @media (max-width: 620px) {
    .permission-checklist-tools {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
