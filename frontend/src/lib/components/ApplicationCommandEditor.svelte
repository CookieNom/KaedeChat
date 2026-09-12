<script lang="ts">
  import { t } from '$lib/ui/locale';

  import type { ApplicationCommandOption } from '$lib/chat/application-commands';
  import {
    COMMAND_KINDS,
    INPUT_KINDS,
    readCommandDraft,
    commandDraftError,
    hasAdvancedInputSettings,
    type CommandDraft
  } from '$lib/chat/command-editor';

  let {
    value,
    onChange,
    disabled = false,
    installTypes = ['guild_install']
  }: {
    value: string;
    onChange: (value: string) => void;
    disabled?: boolean;
    installTypes?: Array<'guild_install' | 'user_install'>;
  } = $props();
  let advanced = $state(false);
  let removedDraft = $state<string | null>(null);
  const parsed = $derived.by(() => {
    try {
      return { commands: readCommandDraft(value), error: '' };
    } catch {
      return {
        commands: [] as CommandDraft[],
        error: 'This JSON cannot be shown in the form yet. Correct its structure in Advanced JSON.'
      };
    }
  });
  const validation = $derived(commandDraftError(value));

  function update(commands: CommandDraft[]) {
    removedDraft = null;
    onChange(JSON.stringify(commands, null, 2));
  }
  function add(type: 'chat_input' | 'user' | 'message') {
    update([
      ...parsed.commands,
      {
        type,
        name: '',
        ...(type === 'chat_input' ? { description: '', options: [] } : {}),
        integration_types: [...installTypes],
        contexts: installTypes.includes('user_install')
          ? ['guild', 'bot_dm', 'private_channel']
          : ['guild']
      }
    ]);
  }
  function change(index: number, patch: Partial<CommandDraft>) {
    update(parsed.commands.map((command, i) => (i === index ? { ...command, ...patch } : command)));
  }
  function changeInput(
    index: number,
    inputIndex: number,
    patch: Partial<ApplicationCommandOption>
  ) {
    let options = (parsed.commands[index].options ?? []).map((option, i) =>
      i === inputIndex ? { ...option, ...patch } : option
    );
    if ('required' in patch)
      options = options.toSorted((a, b) => Number(!!b.required) - Number(!!a.required));
    change(index, { options });
  }
  function remove(index: number) {
    const previous = value;
    update(parsed.commands.filter((_, i) => i !== index));
    removedDraft = previous;
  }
  function toggle(values: string[], value: string) {
    return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
  }
</script>

<div class="editor-heading">
  <div>
    <span class="eyebrow">{$t('ui_command_builder_f5da9a4d')}</span>
    <h3>
      {parsed.error
        ? $t('ui_command_definitions_42ac2d07')
        : `${parsed.commands.length} command${parsed.commands.length === 1 ? '' : 's'}`}
    </h3>
  </div>
  <button
    class="secondary"
    onclick={() => (advanced = !advanced)}
    disabled={advanced && !!parsed.error}
  >
    {advanced ? $t('ui_use_form_editor_1a5cff1a') : $t('ui_advanced_json_493a4596')}
  </button>
</div>
<div class="explanation">
  <strong>{$t('ui_define_what_people_can_ask_your_app_to_do_c4121d34')}</strong>
  <p>{$t('ui_publishing_makes_these_commands_available_whe_0dbb8ad5')}</p>
</div>

{#if removedDraft !== null}
  <div class="feedback" role="status">
    {$t('ui_command_removed_from_the_draft_ddf6c3d4')}
    <button
      {disabled}
      onclick={() => {
        if (removedDraft !== null) onChange(removedDraft);
        removedDraft = null;
      }}>{$t('ui_undo_a8283ade')}</button
    >
  </div>
{/if}

{#if advanced || parsed.error}
  <label class="json-label"
    >{$t('ui_command_definitions_json_f0a28201')}
    <textarea
      {value}
      oninput={(event) => {
        removedDraft = null;
        onChange(event.currentTarget.value);
      }}
      rows="18"
      spellcheck="false"
      {disabled}
    ></textarea>
  </label>
  <p class="hint">{$t('ui_use_json_for_subcommands_choices_autocomplete_79a1a7cd')}</p>
{:else}
  {#if parsed.commands.length === 0}
    <div class="empty">
      <span class="empty-icon" aria-hidden="true">/</span>
      <h3>{$t('ui_create_your_first_command_ceca2d78')}</h3>
      <p>{$t('ui_choose_how_people_will_interact_with_your_app_60b8bc00')}</p>
    </div>
  {/if}
  <fieldset {disabled} class="command-fields">
    {#each parsed.commands as command, index (index)}
      {@const kind = COMMAND_KINDS.find((item) => item.type === (command.type ?? 'chat_input'))!}
      {@const nested = command.options?.some((option) =>
        ['subcommand', 'subcommand_group'].includes(option.type)
      )}
      <details class="command-card" open>
        <summary
          ><span class="command-symbol" aria-hidden="true"
            >{kind.type === 'chat_input' ? '/' : kind.type === 'user' ? '@' : '↳'}</span
          ><span
            ><strong>{command.name || $t('ui_untitled_command_f9b8dd25')}</strong><small
              >{kind.label}</small
            ></span
          ></summary
        >
        <div class="command-body">
          <div class="field-grid">
            <label
              >{$t('ui_command_name_5af23acc')}<input
                value={command.name}
                maxlength="32"
                placeholder={kind.type === 'chat_input' ? 'help' : kind.example}
                oninput={(event) => change(index, { name: event.currentTarget.value })}
              /><small
                >{kind.type === 'chat_input'
                  ? $t('ui_lowercase_without_spaces_or_the_leading_e4668a21')
                  : $t('ui_the_label_people_see_in_the_apps_menu_3b35874f')}</small
              ></label
            >
            {#if kind.type === 'chat_input'}<label
                >{$t('ui_command_description_64eaeba7')}<input
                  value={command.description ?? ''}
                  maxlength="100"
                  placeholder={$t('ui_show_what_this_app_can_do_acd09d91')}
                  oninput={(event) => change(index, { description: event.currentTarget.value })}
                /><small>{$t('ui_a_short_explanation_shown_beside_the_command_2638a5f0')}</small
                ></label
              >{/if}
          </div>
          <div class="command-preview" aria-label={$t('ui_command_preview_3fb27fc1')}>
            <span class="eyebrow">{$t('ui_what_people_see_ddebf5ba')}</span><strong
              >{kind.type === 'chat_input' ? '/' : $t('ui_apps_e6f98b9c')}{command.name ||
                (kind.type === 'chat_input' ? 'command-name' : kind.example)}</strong
            ><span
              >{kind.type === 'chat_input'
                ? command.description || $t('ui_your_description_appears_here_1f6d0dda')
                : kind.description}</span
            >
          </div>
          {#if kind.type === 'chat_input'}
            <div class="input-heading">
              <div>
                <h4>{$t('ui_inputs_7abc49df')} <span>{command.options?.length ?? 0}/25</span></h4>
                <p>{$t('ui_ask_for_extra_information_such_as_a_person_or_760a0bd2')}</p>
              </div>
            </div>
            {#if nested}
              <div class="feedback">
                {$t('ui_this_command_uses_subcommands_2866a601')}
                <button onclick={() => (advanced = true)}
                  >{$t('ui_edit_subcommands_in_json_fcf8780a')}</button
                >
              </div>
            {:else}
              {#each command.options ?? [] as option, optionIndex (optionIndex)}
                <div class="input-card">
                  <div class="input-fields">
                    <label
                      >{$t('ui_input_name_71ce094a')}<input
                        value={option.name}
                        maxlength="32"
                        placeholder="query"
                        oninput={(event) =>
                          changeInput(index, optionIndex, { name: event.currentTarget.value })}
                      /></label
                    >
                    <label
                      >{$t('ui_input_type_0f9015c3')}<select
                        value={option.type}
                        disabled={hasAdvancedInputSettings(option)}
                        onchange={(event) =>
                          changeInput(index, optionIndex, {
                            type: event.currentTarget.value as ApplicationCommandOption['type']
                          })}
                        >{#each INPUT_KINDS as type (type[0])}<option value={type[0]}
                            >{type[1]}</option
                          >{/each}</select
                      ></label
                    >
                    <label class="input-description"
                      >{$t('ui_input_description_196bc6fb')}<input
                        value={option.description ?? ''}
                        maxlength="100"
                        placeholder={$t('ui_what_would_you_like_to_search_for_364bbb9d')}
                        oninput={(event) =>
                          changeInput(index, optionIndex, {
                            description: event.currentTarget.value
                          })}
                      /></label
                    >
                  </div>
                  <div class="input-actions">
                    <label class="check"
                      ><input
                        type="checkbox"
                        checked={option.required ?? false}
                        onchange={(event) =>
                          changeInput(index, optionIndex, {
                            required: event.currentTarget.checked
                          })}
                      />{$t('ui_required_4850b174')}</label
                    ><button
                      class="text-danger"
                      onclick={() =>
                        change(index, {
                          options: command.options?.filter((_, i) => i !== optionIndex)
                        })}
                      aria-label={`Remove input ${option.name || optionIndex + 1}`}
                      >{$t('ui_remove_input_95e788ac')}</button
                    >
                  </div>
                  {#if hasAdvancedInputSettings(option)}<p class="hint">
                      {$t('ui_additional_rules_are_preserved_use_advanced_j_705a5074')}
                    </p>{/if}
                </div>
              {/each}
              <button
                class="secondary"
                disabled={(command.options?.length ?? 0) >= 25}
                onclick={() =>
                  change(index, {
                    options: [
                      ...(command.options ?? []),
                      { type: 'string', name: '', description: '', required: false }
                    ]
                  })}>{$t('ui_add_input_ddb07141')}</button
              >
            {/if}
          {/if}
          <details class="availability">
            <summary>{$t('ui_where_can_people_use_this_command_ac6f090e')}</summary>
            <p class="hint">{$t('ui_install_types_must_also_be_enabled_and_saved__716f1af7')}</p>
            <div class="availability-grid">
              <div>
                <strong>{$t('ui_installed_for_31b5a78b')}</strong
                >{#each [['guild_install', $t('ui_a_server_ff064f86')], ['user_install', $t('ui_a_person_94d7f303')]] as type (type[0])}<label
                    class="check"
                    ><input
                      type="checkbox"
                      checked={(command.integration_types ?? ['guild_install']).includes(
                        type[0] as 'guild_install' | 'user_install'
                      )}
                      onchange={() =>
                        change(index, {
                          integration_types: toggle(
                            command.integration_types ?? ['guild_install'],
                            type[0]
                          ) as CommandDraft['integration_types']
                        })}
                    />{type[1]}</label
                  >{/each}
              </div>
              <div>
                <strong>{$t('ui_available_in_59e5d85f')}</strong
                >{#each [['guild', $t('ui_servers_68d7beb6')], ['bot_dm', $t('ui_direct_messages_with_the_app_81185886')], ['private_channel', $t('ui_group_and_personal_direct_messages_60ed94f5')]] as context (context[0])}<label
                    class="check"
                    ><input
                      type="checkbox"
                      checked={(
                        command.contexts ?? ['guild', 'bot_dm', 'private_channel']
                      ).includes(context[0] as 'guild' | 'bot_dm' | 'private_channel')}
                      onchange={() =>
                        change(index, {
                          contexts: toggle(
                            command.contexts ?? ['guild', 'bot_dm', 'private_channel'],
                            context[0]
                          ) as CommandDraft['contexts']
                        })}
                    />{context[1]}</label
                  >{/each}
              </div>
            </div>
          </details>
          <div class="command-footer">
            <span>{$t('ui_changes_take_effect_when_you_publish_8c2d1ec9')}</span><button
              class="text-danger"
              onclick={() => remove(index)}
              aria-label={`Remove command ${command.name || index + 1}`}
              >{$t('ui_remove_command_068d8c54')}</button
            >
          </div>
        </div>
      </details>
    {/each}
    <div class="add-kinds">
      {#each COMMAND_KINDS as kind (kind.type)}
        <button
          class="add-kind"
          disabled={parsed.commands.filter(
            (command) => (command.type ?? 'chat_input') === kind.type
          ).length >= (kind.type === 'chat_input' ? 100 : 15)}
          onclick={() => add(kind.type)}
          ><strong>+ {kind.label}</strong><span>{kind.description}</span><code>{kind.example}</code
          ></button
        >
      {/each}
    </div>
  </fieldset>
{/if}
{#if validation}<p class="validation" role="status">{validation}</p>{/if}
<p class="publish-help">{$t('ui_publishing_updates_the_complete_command_list__c965eac6')}</p>

<style>
  .editor-heading,
  .input-heading,
  .input-actions,
  .command-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  h3,
  h4,
  p {
    margin: 0;
  }
  .eyebrow {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    font-weight: 750;
  }
  .editor-heading h3 {
    margin-top: 0.3rem;
  }
  button,
  input,
  select,
  textarea {
    font: inherit;
  }
  button {
    cursor: pointer;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.6rem 0.85rem;
    color: var(--text);
    background: var(--surface-hover);
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .explanation {
    padding: 1rem;
    border-left: 3px solid var(--accent);
    background: color-mix(in srgb, var(--accent) 6%, var(--surface));
    border-radius: 0 10px 10px 0;
    margin: 1.25rem 0;
  }
  .explanation p {
    margin-top: 0.4rem;
    color: var(--text-muted);
    font-size: 0.85rem;
    line-height: 1.6;
  }
  .command-fields {
    padding: 0;
    border: 0;
    min-width: 0;
    margin: 0;
  }
  .command-card {
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--bg);
    margin-bottom: 1rem;
  }
  .command-card > summary {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    padding: 1rem;
    cursor: pointer;
  }
  .command-card > summary::after {
    content: '+';
    margin-left: auto;
    color: var(--text-muted);
  }
  .command-card[open] > summary::after {
    content: '−';
  }
  summary small {
    display: block;
    color: var(--text-muted);
    font-size: 0.75rem;
    margin-top: 0.2rem;
  }
  .command-symbol {
    display: grid;
    place-items: center;
    width: 38px;
    height: 38px;
    border-radius: 10px;
    font-size: 1.25rem;
    background: color-mix(in srgb, var(--accent) 12%, var(--surface));
    color: var(--accent);
  }
  .command-body {
    padding: 0 1rem 1rem;
    border-top: 1px solid var(--line);
  }
  .field-grid,
  .availability-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
  }
  label {
    display: grid;
    gap: 0.4rem;
    font-size: 0.8rem;
    font-weight: 650;
    margin: 1rem 0;
  }
  label small,
  .hint,
  .command-footer,
  .publish-help {
    font-size: 0.75rem;
    color: var(--text-muted);
    font-weight: 400;
    line-height: 1.5;
  }
  input,
  textarea,
  select {
    box-sizing: border-box;
    min-width: 0;
    width: 100%;
    padding: 0.7rem;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  textarea {
    resize: vertical;
    font-family: var(--font-mono, monospace);
    font-size: 0.8rem;
  }
  .command-preview {
    display: grid;
    gap: 0.35rem;
    padding: 1rem;
    border: 1px dashed var(--line);
    border-radius: 8px;
    margin-bottom: 1.25rem;
    overflow-wrap: anywhere;
  }
  .command-preview strong {
    color: var(--accent);
  }
  .command-preview > span:last-child {
    color: var(--text-muted);
    font-size: 0.8rem;
  }
  .input-heading h4 span {
    font-size: 0.75rem;
    color: var(--text-muted);
    font-weight: 400;
    margin-left: 0.4rem;
  }
  .input-heading p {
    color: var(--text-muted);
    font-size: 0.8rem;
    margin: 0.35rem 0 0.8rem;
  }
  .input-card {
    border: 1px solid var(--line);
    padding: 0 0.8rem 0.6rem;
    border-radius: 8px;
    margin-bottom: 0.65rem;
  }
  .input-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0 0.75rem;
  }
  .input-description {
    grid-column: 1/-1;
    margin-top: 0;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.6rem 0;
  }
  .check input {
    width: auto;
    accent-color: var(--accent);
  }
  .text-danger {
    border: 0;
    background: transparent;
    color: var(--danger);
    font-size: 0.75rem;
  }
  .availability {
    border-top: 1px solid var(--line);
    margin-top: 1rem;
    padding-top: 1rem;
  }
  .availability > summary {
    font-size: 0.8rem;
    cursor: pointer;
  }
  .availability .hint {
    margin: 0.7rem 0;
  }
  .availability-grid > div > strong {
    font-size: 0.75rem;
  }
  .command-footer {
    border-top: 1px solid var(--line);
    padding-top: 0.6rem;
    margin-top: 1rem;
  }
  .add-kinds {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.65rem;
  }
  .add-kind {
    display: grid;
    gap: 0.5rem;
    text-align: left;
    padding: 1rem;
    background: var(--surface);
  }
  .add-kind:hover {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 5%, var(--surface));
  }
  .add-kind strong {
    font-size: 0.8rem;
  }
  .add-kind span {
    font-size: 0.75rem;
    line-height: 1.5;
    color: var(--text-muted);
  }
  .add-kind code {
    font-size: 0.75rem;
    color: var(--accent);
  }
  .empty {
    text-align: center;
    padding: 1.5rem 1rem 2rem;
  }
  .empty-icon {
    display: grid;
    place-items: center;
    margin: 0 auto 0.8rem;
    width: 48px;
    height: 48px;
    border-radius: 14px;
    font-size: 1.5rem;
    color: var(--accent);
    background: var(--surface-hover);
  }
  .empty p {
    color: var(--text-muted);
    font-size: 0.85rem;
    margin: 0.5rem auto 0;
    max-width: 360px;
    line-height: 1.5;
  }
  .feedback {
    padding: 0.75rem;
    font-size: 0.8rem;
    margin-bottom: 0.8rem;
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  .feedback button {
    padding: 0.2rem 0.4rem;
  }
  .validation {
    color: var(--warning, var(--danger));
    padding-top: 1rem;
    font-size: 0.85rem;
  }
  .publish-help {
    margin-top: 1.25rem;
  }
  @media (max-width: 640px) {
    .field-grid,
    .availability-grid,
    .add-kinds {
      grid-template-columns: 1fr;
    }
    .editor-heading,
    .command-footer {
      flex-wrap: wrap;
    }
  }
</style>
