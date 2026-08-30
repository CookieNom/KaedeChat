<script lang="ts">
  import type {
    ApplicationCommandAutocompleteChoice,
    ApplicationCommandOption,
    CommandComposerValue,
    CommandComposerValues
  } from '$lib/chat/application-commands';
  import {
    commandComposerModel,
    commandOptionAllowsChannelType,
    localizedCommandText
  } from '$lib/chat/application-commands';
  import { entityRef } from '$lib/chat/refs';
  import { fileUploadAccept, fileUploadMatches } from '$lib/chat/rich-content';
  import type { Channel, Role, UserSummary } from '$lib/chat/types';
  import { onDestroy } from 'svelte';
  import { preferredLocale } from '$lib/ui/locale';
  import GuildMemberPicker from './GuildMemberPicker.svelte';

  let {
    commandName,
    commandDisplayName = commandName,
    applicationName,
    options,
    values,
    users = [],
    guildRef = null,
    roles = [],
    channels = [],
    attachments = [],
    disabled = false,
    locale = preferredLocale(),
    onValueChange,
    onAttachmentFiles,
    onAutocomplete,
    onCancel
  }: {
    commandName: string;
    commandDisplayName?: string;
    applicationName?: string;
    options: ApplicationCommandOption[];
    values: CommandComposerValues;
    users?: UserSummary[];
    guildRef?: string | null;
    roles?: Role[];
    channels?: Channel[];
    attachments?: Array<{ id: string; label: string; filename: string; contentType: string }>;
    disabled?: boolean;
    locale?: string;
    onValueChange: (path: string, value: CommandComposerValue) => void;
    onAttachmentFiles?: (option: ApplicationCommandOption, path: string, files: FileList) => void;
    onAutocomplete?: (
      option: ApplicationCommandOption,
      value: string,
      generation: number,
      path: string
    ) => Promise<ApplicationCommandAutocompleteChoice[]>;
    onCancel: () => void;
  } = $props();

  let suggestions = $state<Record<string, ApplicationCommandAutocompleteChoice[]>>({});
  let loadingOption = $state<string | null>(null);
  let autocompleteTimer: number | null = null;
  let autocompleteGeneration = 0;
  const model = $derived(commandComposerModel(options, values));

  function optionValueChanged(option: ApplicationCommandOption, path: string, value: string) {
    onValueChange(path, value);
    if (!option.autocomplete || !onAutocomplete) return;
    if (autocompleteTimer !== null) window.clearTimeout(autocompleteTimer);
    const generation = ++autocompleteGeneration;
    loadingOption = path;
    autocompleteTimer = window.setTimeout(() => {
      autocompleteTimer = null;
      void onAutocomplete(option, value, generation, path)
        .then((choices) => {
          if (generation !== autocompleteGeneration) return;
          suggestions = { ...suggestions, [path]: choices };
        })
        .finally(() => {
          if (generation === autocompleteGeneration) loadingOption = null;
        });
    }, 175);
  }

  onDestroy(() => {
    if (autocompleteTimer !== null) window.clearTimeout(autocompleteTimer);
  });

  function fieldId(path: string): string {
    return `command-${commandName}-${path.replaceAll('.', '-')}`;
  }

  function optionName(option: ApplicationCommandOption): string {
    return localizedCommandText(option.name, option.name_localizations, locale);
  }

  function optionDescription(option: ApplicationCommandOption): string {
    return localizedCommandText(
      option.description ?? option.name,
      option.description_localizations,
      locale
    );
  }
</script>

<div class="command-fields" aria-label={`Options for /${commandName}`}>
  <span class="command-name">/{commandDisplayName}</span>
  {#each model.selectors as selector (selector.path)}
    <label title={`Choose ${selector.label}`}>
      <span>{selector.label}</span>
      <select
        value={selector.selected}
        {disabled}
        required
        aria-label={`Choose ${selector.label} for /${commandName}`}
        onchange={(event) => onValueChange(selector.path, event.currentTarget.value)}
      >
        <option value="">Select</option>
        {#each selector.options as option (option.name)}
          <option value={option.name}>{optionName(option)}</option>
        {/each}
      </select>
    </label>
  {/each}
  {#each model.fields as field (field.path)}
    {@const option = field.option}
    <label title={optionDescription(option)}>
      <span>{optionName(option)}{option.required ? '' : ' (optional)'}</span>
      {#if option.choices?.length && !option.autocomplete}
        <select
          value={String(values[field.path] ?? '')}
          {disabled}
          required={option.required}
          onchange={(event) => optionValueChanged(option, field.path, event.currentTarget.value)}
        >
          <option value="">Select</option>
          {#each option.choices as choice (`${field.path}:${choice.value}`)}
            <option value={String(choice.value)}
              >{localizedCommandText(choice.name, choice.name_localizations, locale)}</option
            >
          {/each}
        </select>
      {:else if option.type === 'boolean'}
        <select
          value={values[field.path] === true ? 'true' : values[field.path] === false ? 'false' : ''}
          {disabled}
          required={option.required}
          onchange={(event) => {
            const value = event.currentTarget.value;
            onValueChange(field.path, value === '' ? '' : value === 'true');
          }}
        >
          <option value="">Select</option>
          <option value="true">True</option>
          <option value="false">False</option>
        </select>
      {:else if option.type === 'user'}
        <GuildMemberPicker
          {guildRef}
          fallbackUsers={guildRef ? [] : users}
          value={String(values[field.path] ?? '') ? [String(values[field.path])] : []}
          optional={!option.required}
          placeholder="Select a user"
          {disabled}
          onChange={(selected) => onValueChange(field.path, selected[0] ?? '')}
        />
      {:else if option.type === 'channel'}
        <select
          value={String(values[field.path] ?? '')}
          {disabled}
          required={option.required}
          onchange={(event) => onValueChange(field.path, event.currentTarget.value)}
        >
          <option value="">Select a channel</option>
          {#each channels.filter( (channel) => commandOptionAllowsChannelType(option, channel.type) ) as channel (entityRef(channel))}
            <option value={entityRef(channel)}>#{channel.name ?? 'channel'}</option>
          {/each}
        </select>
      {:else if option.type === 'role'}
        <select
          value={String(values[field.path] ?? '')}
          {disabled}
          required={option.required}
          onchange={(event) => onValueChange(field.path, event.currentTarget.value)}
        >
          <option value="">Select a role</option>
          {#each roles as role (entityRef(role))}
            <option value={entityRef(role)}>@{role.name}</option>
          {/each}
        </select>
      {:else if option.type === 'mentionable'}
        <GuildMemberPicker
          {guildRef}
          fallbackUsers={guildRef ? [] : users}
          staticOptions={roles.map((role) => ({
            value: entityRef(role),
            label: `@${role.name}`,
            group: 'Roles'
          }))}
          value={String(values[field.path] ?? '') ? [String(values[field.path])] : []}
          optional={!option.required}
          placeholder="Select a user or role"
          {disabled}
          onChange={(selected) => onValueChange(field.path, selected[0] ?? '')}
        />
      {:else if option.type === 'attachment'}
        {@const matchingAttachments = attachments.filter((attachment) =>
          fileUploadMatches(option.file_types, attachment.filename, attachment.contentType)
        )}
        <select
          value={String(values[field.path] ?? '')}
          {disabled}
          required={option.required}
          onchange={(event) => onValueChange(field.path, event.currentTarget.value)}
        >
          <option value="">Select an uploaded file</option>
          {#each matchingAttachments as attachment (attachment.id)}
            <option value={attachment.id}>{attachment.label}</option>
          {/each}
        </select>
        <input
          class="attachment-input"
          type="file"
          accept={fileUploadAccept(option.file_types)}
          {disabled}
          aria-label={`Add a file for ${optionName(option)}`}
          onchange={(event) => {
            const target = event.currentTarget;
            if (target.files?.length) onAttachmentFiles?.(option, field.path, target.files);
            target.value = '';
          }}
        />
      {:else}
        <input
          type={option.type === 'string' ? 'text' : 'number'}
          value={String(values[field.path] ?? '')}
          {disabled}
          required={option.required}
          minlength={option.min_length}
          maxlength={option.max_length ?? 4000}
          min={option.min_value}
          max={option.max_value}
          step={option.type === 'integer' ? 1 : 'any'}
          list={option.autocomplete ? `${fieldId(field.path)}-choices` : undefined}
          aria-busy={loadingOption === field.path}
          placeholder={optionDescription(option)}
          oninput={(event) => optionValueChanged(option, field.path, event.currentTarget.value)}
        />
        {#if option.autocomplete}
          <datalist id={`${fieldId(field.path)}-choices`}>
            {#each suggestions[field.path] ?? [] as choice (`${choice.name}:${choice.value}`)}
              <option value={String(choice.value)}>{choice.name}</option>
            {/each}
          </datalist>
        {/if}
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

  .attachment-input {
    min-width: 9rem;
    max-width: 12rem;
    padding: 0.3rem;
    font-size: 0.72rem;
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
