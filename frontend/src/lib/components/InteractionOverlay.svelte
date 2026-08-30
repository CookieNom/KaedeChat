<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import { interactionResponses } from '$lib/chat/interaction-responses.svelte';
  import { createInteraction } from '$lib/chat/interactions';
  import {
    entitySelectOptions,
    fileUploadAccept,
    modalSubmitBody,
    type CheckboxGroupComponent,
    type CheckboxV2Component,
    type EntitySelectComponent,
    type FileUploadComponent,
    type InteractionModal,
    type ModalLayoutComponent,
    type ModalInputComponent,
    type RadioGroupComponent,
    type MessageComponent,
    type StringSelectComponent,
    type TextInputComponent
  } from '$lib/chat/rich-content';
  import { uploadChannelFile } from '$lib/media/uploads';
  import { uploadEncryptedChannelFile, type EncryptedFileManifest } from '$lib/e2ee/media';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import Markdown from './Markdown.svelte';

  let values = $state<Record<string, string | boolean | string[] | null>>({});
  let submitting = $state(false);
  let error = $state('');
  let uploading = $state<Record<string, number>>({});
  let uploadNames = $state<Record<string, string[]>>({});
  let uploadManifests = $state<Record<string, EncryptedFileManifest>>({});
  const active = $derived(interactionResponses.activeModal);
  const requestContext = $derived(interactionResponses.context(active?.interactionRef ?? null));
  const sourceChannel = $derived(
    requestContext ? chatEntities.channels.get(requestContext.channelRef) : undefined
  );
  const sourceGuild = $derived(
    sourceChannel?.guild_id && sourceChannel.guild_domain
      ? chatEntities.guilds.get(`${sourceChannel.guild_id}@${sourceChannel.guild_domain}`)
      : undefined
  );
  const componentContext = $derived({
    users: sourceGuild
      ? chatEntities.members.values
          .filter(
            (member) =>
              member.guild_id === sourceGuild.id &&
              member.guild_domain === sourceGuild.origin_domain
          )
          .map((member) => member.user)
      : (sourceChannel?.recipients ?? []),
    roles: sourceGuild?.roles ?? [],
    channels: sourceGuild?.channels ?? []
  });

  type ModalField = {
    input: ModalInputComponent | MessageComponent;
    label: string;
    description: string | null;
  };

  function fieldsForTop(topLevel: ModalLayoutComponent): ModalField[] {
    if (topLevel.type === 10) return [];
    if (topLevel.type === 18) {
      return [
        {
          input: topLevel.component,
          label: topLevel.label,
          description: topLevel.description ?? null
        }
      ];
    }
    return topLevel.components.map((input) => ({
      input,
      label: input.type === 4 ? (input.label ?? 'Text') : 'Field',
      description: null
    }));
  }

  function modalFields(modal: InteractionModal): ModalField[] {
    return modal.components.flatMap(fieldsForTop);
  }

  function initialValue(component: ModalInputComponent | MessageComponent) {
    if (component.type === 23) return Boolean(component.default);
    if (component.type === 3)
      return component.options.filter((option) => option.default).map((option) => option.value);
    if ([5, 6, 7, 8].includes(component.type as number))
      return (component as EntitySelectComponent).default_values?.map((item) => item.id) ?? [];
    if (component.type === 21)
      return (
        (component as RadioGroupComponent).options.find((option) => option.default)?.value ?? null
      );
    if (component.type === 22)
      return (component as CheckboxGroupComponent).options
        .filter((option) => option.default)
        .map((option) => option.value);
    if (component.type === 19) return [];
    return component.type === 4 ? (component.value ?? '') : '';
  }

  $effect(() => {
    const modal = active?.modal;
    if (!modal) return;
    values = Object.fromEntries(
      modalFields(modal).map(({ input }) => [
        'custom_id' in input ? input.custom_id : '',
        initialValue(input)
      ])
    );
    uploading = {};
    uploadNames = {};
    uploadManifests = {};
    error = '';
  });

  function dismiss() {
    if (submitting) return;
    interactionResponses.dismissModal();
  }

  function selectedValues(event: Event): string[] {
    return [...(event.currentTarget as HTMLSelectElement).selectedOptions]
      .map((option) => option.value)
      .filter(Boolean);
  }

  function isSelectComponent(
    component: ModalInputComponent | MessageComponent
  ): component is StringSelectComponent | EntitySelectComponent {
    return [3, 5, 6, 7, 8].includes(component.type as number);
  }

  async function uploadFiles(component: FileUploadComponent, files: FileList | null) {
    if (!files || !requestContext) return;
    const maximum = component.max_values ?? 1;
    const selected = [...files].slice(0, maximum);
    uploading = { ...uploading, [component.custom_id]: 1 };
    error = '';
    try {
      const tickets = [];
      const nextManifests: Record<string, EncryptedFileManifest> = {};
      for (const file of selected) {
        const ticket = requestContext.e2ee
          ? await uploadEncryptedChannelFile(requestContext.channelRef, file, (progress) => {
              uploading = { ...uploading, [component.custom_id]: progress };
            })
          : await uploadChannelFile(requestContext.channelRef, file, (progress) => {
              uploading = { ...uploading, [component.custom_id]: progress };
            });
        // Modal submissions use Discord-style attachment IDs. The ticket is
        // issued by the channel authority, including for federated channels.
        const id = 'ticket' in ticket ? ticket.ticket.id : ticket.id;
        tickets.push(String(id));
        if ('manifest' in ticket) nextManifests[String(id)] = ticket.manifest;
      }
      values = { ...values, [component.custom_id]: tickets };
      uploadNames = { ...uploadNames, [component.custom_id]: selected.map((file) => file.name) };
      uploadManifests = { ...uploadManifests, ...nextManifests };
    } catch (caught) {
      error = userErrorMessage(caught, 'One or more files could not be uploaded. Try again.');
    } finally {
      const next = { ...uploading };
      delete next[component.custom_id];
      uploading = next;
    }
  }

  function isSelected(customId: string, option: string): boolean {
    const selected = values[customId];
    return Array.isArray(selected) && selected.includes(option);
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!active || !requestContext) {
      error = 'This form is no longer connected to its bot interaction. Run the command again.';
      return;
    }
    for (const { input: component } of modalFields(active.modal)) {
      if (component.type === 21) {
        if (component.required !== false && !values[component.custom_id]) {
          error = 'Choose one option for every required field.';
          return;
        }
        continue;
      }
      if (component.type === 19 || component.type === 22) {
        const selected = values[component.custom_id];
        const count = Array.isArray(selected) ? selected.length : 0;
        const minimum = component.min_values ?? 1;
        const maximum =
          component.max_values ?? (component.type === 22 ? component.options.length : 1);
        if (count < minimum || count > maximum) {
          error = `Choose between ${minimum} and ${maximum} values for this field.`;
          return;
        }
        continue;
      }
      if (!isSelectComponent(component)) continue;
      const selected = values[component.custom_id];
      const count = Array.isArray(selected) ? selected.length : 0;
      const minimum = component.min_values ?? 1;
      const maximum = component.max_values ?? 1;
      if (count < minimum || count > maximum) {
        error = `Choose between ${minimum} and ${maximum} items for ${component.placeholder ?? 'this field'}.`;
        return;
      }
    }
    const message = {
      application_id: requestContext.applicationRef.split('@')[0],
      application_domain: requestContext.applicationRef.slice(
        requestContext.applicationRef.indexOf('@') + 1
      )
    } as never;
    const body = active.responseId
      ? modalSubmitBody(message, active.responseId, active.modal, values)
      : null;
    if (!body) {
      error = 'This form is missing its app. Run the command again.';
      return;
    }
    submitting = true;
    error = '';
    try {
      const attachmentIds = modalFields(active.modal).flatMap(({ input }) => {
        if (input.type !== 19) return [];
        const selected = values[input.custom_id];
        return Array.isArray(selected) ? selected : [];
      });
      await createInteraction(
        requestContext,
        body,
        requestContext.e2ee
          ? {
              attachmentIds,
              attachments: Object.fromEntries(
                attachmentIds.map((id) => {
                  const manifest = uploadManifests[id];
                  if (!manifest)
                    throw new Error(
                      'Reattach every file before submitting this encrypted bot form.'
                    );
                  return [id, manifest];
                })
              )
            }
          : {}
      );
      interactionResponses.dismissModal();
    } catch (caught) {
      error = userErrorMessage(
        caught,
        'The bot did not receive this form. Check it and try again.'
      );
    } finally {
      submitting = false;
    }
  }
</script>

{#if active}
  <div
    class="modal-backdrop"
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && dismiss()}
  >
    <div
      class="interaction-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="interaction-modal-title"
    >
      <header>
        <h2 id="interaction-modal-title">{active.modal.title}</h2>
        <button type="button" aria-label="Close form" disabled={submitting} onclick={dismiss}
          >×</button
        >
      </header>
      <form onsubmit={submit}>
        {#each active.modal.components as topLevel, topIndex (topLevel.id ?? topIndex)}
          {#if topLevel.type === 10}
            <div class="modal-text-display"><Markdown content={topLevel.content} /></div>
          {:else}
            {#each fieldsForTop(topLevel) as field ('custom_id' in field.input ? field.input.custom_id : `${field.input.type}`)}
              {@const component = field.input}
              {#if component.type === 4}
                {@const input = component as TextInputComponent}
                <label>
                  <span>{field.label}{input.required === false ? ' (optional)' : ''}</span>
                  {#if field.description}<small>{field.description}</small>{/if}
                  {#if input.style === 2}
                    <textarea
                      value={String(values[input.custom_id] ?? '')}
                      required={input.required !== false}
                      minlength={input.min_length ?? undefined}
                      maxlength={input.max_length ?? 4000}
                      placeholder={input.placeholder ?? undefined}
                      disabled={submitting}
                      oninput={(event) => (values[input.custom_id] = event.currentTarget.value)}
                    ></textarea>
                  {:else}
                    <input
                      value={String(values[input.custom_id] ?? '')}
                      required={input.required !== false}
                      minlength={input.min_length ?? undefined}
                      maxlength={input.max_length ?? 4000}
                      placeholder={input.placeholder ?? undefined}
                      disabled={submitting}
                      oninput={(event) => (values[input.custom_id] = event.currentTarget.value)}
                    />
                  {/if}
                </label>
              {:else if component.type === 23}
                {@const checkbox = component as CheckboxV2Component}
                <label class="modal-checkbox">
                  <input
                    type="checkbox"
                    checked={Boolean(values[checkbox.custom_id])}
                    disabled={submitting}
                    onchange={(event) => (values[checkbox.custom_id] = event.currentTarget.checked)}
                  />
                  <span>{field.label}</span>
                </label>
                {#if field.description}<small>{field.description}</small>{/if}
              {:else if component.type === 3}
                {@const select = component as StringSelectComponent}
                <label>
                  <span>{field.label}{select.required === false ? ' (optional)' : ''}</span>
                  {#if field.description}<small>{field.description}</small>{/if}
                  <select
                    multiple={(select.max_values ?? 1) > 1}
                    size={(select.max_values ?? 1) > 1 ? Math.min(select.options.length, 5) : 1}
                    required={(select.min_values ?? 1) > 0}
                    disabled={submitting || select.disabled}
                    onchange={(event) => (values[select.custom_id] = selectedValues(event))}
                  >
                    {#if (select.min_values ?? 1) === 0 && (select.max_values ?? 1) === 1}
                      <option value="">None</option>
                    {:else if (select.max_values ?? 1) === 1}
                      <option value="" disabled>Choose an option</option>
                    {/if}
                    {#each select.options as option (option.value)}
                      <option
                        value={option.value}
                        selected={isSelected(select.custom_id, option.value)}
                        >{option.label}{option.description
                          ? ` — ${option.description}`
                          : ''}</option
                      >
                    {/each}
                  </select>
                </label>
              {:else if [5, 6, 7, 8].includes(component.type as number)}
                {@const select = component as EntitySelectComponent}
                {@const options = entitySelectOptions(select, componentContext)}
                <label>
                  <span>{field.label}{select.required === false ? ' (optional)' : ''}</span>
                  {#if field.description}<small>{field.description}</small>{/if}
                  <select
                    multiple={(select.max_values ?? 1) > 1}
                    size={(select.max_values ?? 1) > 1
                      ? Math.min(Math.max(options.length, 2), 5)
                      : 1}
                    required={(select.min_values ?? 1) > 0}
                    disabled={submitting || options.length === 0}
                    onchange={(event) => (values[select.custom_id] = selectedValues(event))}
                  >
                    {#if (select.min_values ?? 1) === 0 && (select.max_values ?? 1) === 1}
                      <option value="">None</option>
                    {:else if (select.max_values ?? 1) === 1}
                      <option value="" disabled>Choose an item</option>
                    {/if}
                    {#each options as option (option.value)}
                      <option
                        value={option.value}
                        selected={isSelected(select.custom_id, option.value)}>{option.label}</option
                      >
                    {/each}
                  </select>
                  {#if options.length === 0}
                    <small>No matching items are available in this channel.</small>
                  {/if}
                </label>
              {:else if component.type === 21}
                {@const radio = component as RadioGroupComponent}
                <fieldset>
                  <legend>{field.label}{radio.required === false ? ' (optional)' : ''}</legend>
                  {#if field.description}<small>{field.description}</small>{/if}
                  {#each radio.options as option (option.value)}
                    <label class="modal-choice">
                      <input
                        type="radio"
                        name={radio.custom_id}
                        value={option.value}
                        checked={values[radio.custom_id] === option.value}
                        required={radio.required !== false}
                        disabled={submitting}
                        onchange={() => (values[radio.custom_id] = option.value)}
                      />
                      <span
                        ><b>{option.label}</b>{#if option.description}<small
                            >{option.description}</small
                          >{/if}</span
                      >
                    </label>
                  {/each}
                  {#if radio.required === false && values[radio.custom_id] !== null}
                    <button
                      type="button"
                      class="clear-radio"
                      disabled={submitting}
                      onclick={() => (values[radio.custom_id] = null)}>Clear selection</button
                    >
                  {/if}
                </fieldset>
              {:else if component.type === 22}
                {@const group = component as CheckboxGroupComponent}
                <fieldset>
                  <legend>{field.label}{group.required === false ? ' (optional)' : ''}</legend>
                  {#if field.description}<small>{field.description}</small>{/if}
                  {#each group.options as option (option.value)}
                    <label class="modal-choice">
                      <input
                        type="checkbox"
                        checked={isSelected(group.custom_id, option.value)}
                        disabled={submitting}
                        onchange={(event) => {
                          const current = Array.isArray(values[group.custom_id])
                            ? (values[group.custom_id] as string[])
                            : [];
                          values[group.custom_id] = event.currentTarget.checked
                            ? [...current, option.value]
                            : current.filter((value) => value !== option.value);
                        }}
                      />
                      <span
                        ><b>{option.label}</b>{#if option.description}<small
                            >{option.description}</small
                          >{/if}</span
                      >
                    </label>
                  {/each}
                </fieldset>
              {:else if component.type === 19}
                {@const upload = component as FileUploadComponent}
                <label>
                  <span>{field.label}{upload.required === false ? ' (optional)' : ''}</span>
                  {#if field.description}<small>{field.description}</small>{/if}
                  <input
                    type="file"
                    multiple={(upload.max_values ?? 1) > 1}
                    accept={fileUploadAccept(upload.file_types)}
                    required={(upload.required ?? true) &&
                      (!Array.isArray(values[upload.custom_id]) ||
                        (values[upload.custom_id] as string[]).length === 0)}
                    disabled={submitting || Boolean(uploading[upload.custom_id])}
                    onchange={(event) => void uploadFiles(upload, event.currentTarget.files)}
                  />
                  {#if uploading[upload.custom_id]}
                    <small>Uploading… {uploading[upload.custom_id]}%</small>
                  {:else if uploadNames[upload.custom_id]?.length}
                    <small>{uploadNames[upload.custom_id].join(', ')}</small>
                  {/if}
                </label>
              {/if}
            {/each}
          {/if}
        {/each}
        {#if error}<p role="alert">{error}</p>{/if}
        <footer>
          <button type="button" disabled={submitting} onclick={dismiss}>Cancel</button>
          <button class="submit" type="submit" disabled={submitting}
            >{submitting ? 'Sending…' : 'Submit'}</button
          >
        </footer>
      </form>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    z-index: 2000;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 20px;
    background: rgb(0 0 0 / 66%);
  }
  .interaction-modal {
    width: min(480px, 100%);
    max-height: min(760px, calc(100vh - 40px));
    overflow-y: auto;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface);
    box-shadow: 0 24px 80px rgb(0 0 0 / 45%);
  }
  header,
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 14px 16px;
  }
  header {
    border-bottom: 1px solid var(--line);
  }
  h2 {
    margin: 0;
    font-size: 1.05rem;
  }
  header button {
    border: 0;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.4rem;
    cursor: pointer;
  }
  form {
    display: grid;
    gap: 14px;
    padding: 16px;
  }
  label:not(.modal-checkbox) {
    display: grid;
    gap: 6px;
  }
  label > span {
    font-size: 0.8rem;
    font-weight: 750;
  }
  input:not([type='checkbox']),
  textarea,
  select {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 9px 10px;
    color: var(--text);
    background: var(--surface-subtle);
    font: inherit;
  }
  textarea {
    min-height: 100px;
    resize: vertical;
  }
  select {
    min-height: 40px;
  }
  small {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  .modal-checkbox {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .modal-checkbox input {
    width: 18px;
    height: 18px;
    accent-color: var(--accent);
  }
  p {
    margin: 0;
    color: var(--danger);
    font-size: 0.8rem;
  }
  footer {
    margin: 2px -16px -16px;
    border-top: 1px solid var(--line);
    background: var(--surface-raised);
  }
  footer button {
    min-height: 36px;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 7px 13px;
    color: var(--text);
    background: var(--surface-subtle);
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }
  footer .submit {
    margin-left: auto;
    border-color: transparent;
    color: white;
    background: var(--accent);
  }
</style>
