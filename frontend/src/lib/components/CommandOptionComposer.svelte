<script lang="ts">
  import type { ApplicationCommandOption } from '$lib/chat/application-commands';

  let {
    commandName,
    applicationName,
    options,
    values,
    disabled = false,
    onValueChange,
    onCancel
  }: {
    commandName: string;
    applicationName?: string;
    options: ApplicationCommandOption[];
    values: Record<string, string>;
    disabled?: boolean;
    onValueChange: (name: string, value: string) => void;
    onCancel: () => void;
  } = $props();
</script>

<div class="command-fields" aria-label={`Options for /${commandName}`}>
  <span class="command-name">/{commandName}</span>
  {#each options.filter((option) => option.type === 'string') as option (option.name)}
    <label title={option.description ?? option.name}>
      <span>{option.name}{option.required ? '' : ' (optional)'}</span>
      {#if option.choices?.length}
        <select
          value={values[option.name] ?? ''}
          {disabled}
          required={option.required}
          onchange={(event) => onValueChange(option.name, event.currentTarget.value)}
        >
          <option value="">Select</option>
          {#each option.choices as choice (`${option.name}:${choice.value}`)}
            <option value={String(choice.value)}>{choice.name}</option>
          {/each}
        </select>
      {:else}
        <input
          value={values[option.name] ?? ''}
          {disabled}
          required={option.required}
          minlength={option.min_length}
          maxlength={option.max_length ?? 4000}
          placeholder={option.description ?? option.name}
          oninput={(event) => onValueChange(option.name, event.currentTarget.value)}
        />
      {/if}
    </label>
  {/each}
  {#if applicationName}<small>{applicationName}</small>{/if}
  <button type="button" {disabled} aria-label="Cancel command" onclick={onCancel}>×</button>
</div>

<style>
  .command-fields {
    display: flex;
    min-width: 0;
    flex: 1;
    align-items: center;
    gap: 0.45rem;
    overflow-x: auto;
    scrollbar-width: thin;
  }

  .command-name {
    flex: 0 0 auto;
    font-weight: 800;
  }

  label {
    display: flex;
    min-width: 9rem;
    align-items: center;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--surface-subtle);
  }

  label > span {
    flex: 0 0 auto;
    padding: 0.42rem 0.5rem;
    color: var(--text-muted);
    background: var(--surface-raised);
    font-size: 0.7rem;
    font-weight: 750;
  }

  input,
  select {
    min-width: 5.5rem;
    height: 34px;
    border: 0;
    border-radius: 0;
    padding: 0 0.5rem;
    background: transparent;
    color: var(--text);
    outline: 0;
  }

  input:focus,
  select:focus {
    box-shadow: inset 0 0 0 2px var(--accent);
  }

  small {
    flex: 0 0 auto;
    color: var(--text-muted);
    font-size: 0.68rem;
  }

  button {
    width: 30px;
    height: 30px;
    flex: 0 0 auto;
    border: 0;
    border-radius: 6px;
    color: var(--text-muted);
    background: transparent;
    cursor: pointer;
  }

  button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
</style>
