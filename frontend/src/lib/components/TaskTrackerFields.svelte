<script lang="ts">
  import { api } from '$lib/api/client';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';
  import { scrubImageMetadata } from '$lib/media/privacy-metadata';
  import { completeScannedMediaResource } from '$lib/media/scanned';
  import { onDestroy } from 'svelte';
  import TaskTrackerAttachment from './TaskTrackerAttachment.svelte';
  import type { TrackerField, TrackerValues, TrackerAttachment } from '$lib/task-tracker/types';
  import type { GuildMemberSummary, UserSummary } from '$lib/chat/types';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { entityKey } from '$lib/chat/refs';
  import GuildMemberPicker from './GuildMemberPicker.svelte';
  let {
    fields,
    channelRef,
    uploading = $bindable(false),
    values = $bindable({}),
    guildRef = null,
    members = [],
    currentUser = null,
    canAssign = false,
    disabled = false
  }: {
    fields: TrackerField[];
    channelRef: string;
    uploading?: boolean;
    values?: TrackerValues;
    guildRef?: string | null;
    members?: GuildMemberSummary[];
    currentUser?: UserSummary | null;
    canAssign?: boolean;
    disabled?: boolean;
  } = $props();
  const channels = $derived(
    chatEntities.channels.values.filter(
      (c) => `${c.guild_id}@${c.guild_domain}` === guildRef && c.type !== 4
    )
  );
  function selected(field: TrackerField): string[] {
    return (values[field.id] as string[]) ?? [];
  }
  function files(field: TrackerField): TrackerAttachment[] {
    return (values[field.id] as TrackerAttachment[]) ?? [];
  }
  let urls = $state<Record<string, string>>({});
  let names = $state<Record<string, string>>({});
  let kinds = $state<Record<string, TrackerAttachment['type']>>({});
  const controller = new AbortController();
  onDestroy(() => controller.abort());
  async function upload(field: TrackerField, input: HTMLInputElement) {
    const selectedFiles = Array.from(input.files ?? []);
    if (!selectedFiles.length) return;
    if (files(field).length + selectedFiles.length > 10) {
      error = 'Choose up to 10 attachments per field.';
      input.value = '';
      return;
    }
    uploading = true;
    error = '';
    const path = `/channels/${encodeURIComponent(channelRef)}/tracker/attachments`;
    try {
      for (const original of selectedFiles) {
        const file = await scrubImageMetadata(original);
        const ticket = await api<UploadTicket>(`${path}/ticket`, {
          method: 'POST',
          signal: controller.signal,
          body: JSON.stringify({
            filename: file.name,
            content_type: file.type || 'application/octet-stream',
            size: file.size
          })
        });
        await uploadObject(ticket, file, () => {}, controller.signal);
        const attachment = await completeScannedMediaResource<
          Record<string, unknown>,
          TrackerAttachment & Record<string, unknown>
        >(
          () =>
            api(`${path}/commit`, {
              method: 'POST',
              signal: controller.signal,
              body: JSON.stringify({ attachment_id: `${ticket.id}@${ticket.origin_domain}` })
            }),
          (value): value is TrackerAttachment & Record<string, unknown> =>
            typeof value.name === 'string' && typeof value.type === 'string',
          { signal: controller.signal }
        );
        values[field.id] = [...files(field), attachment];
      }
    } catch (caught) {
      if (!controller.signal.aborted)
        error = caught instanceof Error ? caught.message : 'Could not upload the file.';
    } finally {
      uploading = false;
      input.value = '';
    }
  }
  let error = $state('');
  function addFile(field: TrackerField) {
    try {
      const url = new URL(urls[field.id] ?? '');
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
        throw new Error();
      values[field.id] = [
        ...files(field),
        {
          name: names[field.id]?.trim() || url.pathname.split('/').pop() || 'Attachment',
          url: url.href,
          type: kinds[field.id] ?? 'file'
        }
      ];
      urls[field.id] = '';
      names[field.id] = '';
      error = '';
    } catch {
      error = 'Enter a valid http or https attachment link.';
    }
  }
</script>

{#if fields.length}
  <section aria-label="Custom task fields">
    {#each fields as field (field.id)}
      <div class:wide={['textarea', 'attachments'].includes(field.type)} class="field">
        {#if field.type === 'users'}
          <span class="field-label" id={`field-${field.id}`}>{field.name}</span>
          <div role="group" aria-labelledby={`field-${field.id}`}>
            <GuildMemberPicker
              {guildRef}
              fallbackUsers={members.map((m) => m.user)}
              value={selected(field)}
              multiple
              maxValues={100}
              optional
              {disabled}
              placeholder="Choose people"
              filterUser={(user) =>
                canAssign || entityKey(user) === (currentUser ? entityKey(currentUser) : '')}
              onChange={(next) => {
                const old = selected(field);
                const own = currentUser ? entityKey(currentUser) : '';
                if (canAssign || old.filter((v) => v !== own).every((v) => next.includes(v)))
                  values[field.id] = next;
              }}
            />
          </div>
          {#if !canAssign && !disabled}<small
              >You can add or remove yourself. Assigning others requires permission.</small
            >{/if}
        {:else if field.type === 'channels'}
          <span class="field-label" id={`field-${field.id}`}>{field.name}</span>
          <div role="group" aria-labelledby={`field-${field.id}`}>
            <GuildMemberPicker
              staticOptions={channels.map((channel) => ({
                value: entityKey(channel),
                label: `#${channel.name}`,
                group: 'Channels'
              }))}
              value={selected(field)}
              multiple
              maxValues={100}
              optional
              {disabled}
              entityName="channels"
              placeholder="Choose channels"
              searchPlaceholder="Search channels"
              onChange={(next) => (values[field.id] = next)}
            />
          </div>
        {:else if field.type === 'multiselect'}
          <fieldset {disabled}>
            <legend>{field.name}</legend>
            {#each field.options as option (option)}<label class="checkbox"
                ><input
                  type="checkbox"
                  checked={selected(field).includes(option)}
                  onchange={(event) =>
                    (values[field.id] = event.currentTarget.checked
                      ? [...selected(field), option]
                      : selected(field).filter((v) => v !== option))}
                />{option}</label
              >{/each}
          </fieldset>
        {:else if field.type === 'attachments'}
          <span class="field-label">{field.name}</span>
          {#each files(field) as attachment, i (attachment)}
            <TaskTrackerAttachment
              {attachment}
              {channelRef}
              disabled={disabled || uploading}
              onRemove={() => (values[field.id] = files(field).filter((_, index) => index !== i))}
            />
          {/each}
          {#if !disabled && files(field).length < 10}
            <label class="upload"
              >Upload files<input
                type="file"
                multiple
                disabled={uploading}
                onchange={(event) => void upload(field, event.currentTarget)}
              /></label
            >
            {#if uploading}<small role="status">Uploading and checking files…</small>{/if}
            <div class="attachment-inputs">
              <label
                >File link<input
                  type="url"
                  bind:value={urls[field.id]}
                  placeholder="https://…"
                  maxlength="2048"
                /></label
              >
              <label
                >Name<input
                  bind:value={names[field.id]}
                  maxlength="255"
                  placeholder="Attachment name"
                /></label
              >
              <label
                >Preview<select bind:value={kinds[field.id]}
                  ><option value="file">File</option><option value="image">Image</option><option
                    value="video">Video</option
                  ></select
                ></label
              >
              <button
                type="button"
                onclick={() => addFile(field)}
                disabled={!urls[field.id]?.trim()}>Attach link</button
              >
            </div>
          {/if}
        {:else if field.type === 'checkbox'}
          <label class="checkbox"
            ><input
              type="checkbox"
              checked={values[field.id] === true}
              {disabled}
              onchange={(event) => (values[field.id] = event.currentTarget.checked)}
            />{field.name}</label
          >
        {:else}
          <label for={`field-${field.id}`}>{field.name}</label>
          {#if field.type === 'textarea'}
            <textarea
              id={`field-${field.id}`}
              rows="4"
              maxlength="10000"
              value={String(values[field.id] ?? '')}
              {disabled}
              oninput={(event) => (values[field.id] = event.currentTarget.value)}
            ></textarea>
          {:else if field.type === 'select'}
            <select
              id={`field-${field.id}`}
              value={String(values[field.id] ?? '')}
              {disabled}
              onchange={(event) => (values[field.id] = event.currentTarget.value)}
              ><option value="">None</option>{#each field.options as option (option)}<option
                  value={option}>{option}</option
                >{/each}</select
            >
          {:else}
            <input
              id={`field-${field.id}`}
              type={field.type === 'number'
                ? 'number'
                : field.type === 'date'
                  ? 'date'
                  : field.type === 'url'
                    ? 'url'
                    : 'text'}
              value={String(values[field.id] ?? '')}
              step={field.type === 'number' ? 'any' : undefined}
              maxlength={field.type === 'url' ? 2048 : 500}
              {disabled}
              oninput={(event) =>
                (values[field.id] =
                  field.type === 'number'
                    ? event.currentTarget.value === ''
                      ? null
                      : event.currentTarget.valueAsNumber
                    : event.currentTarget.value)}
            />
          {/if}
        {/if}
      </div>
    {/each}
    {#if error}<p role="alert">{error}</p>{/if}
  </section>
{/if}

<style>
  section {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
  }
  .field {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: 0.45rem;
  }
  .wide {
    grid-column: 1 / -1;
  }
  label,
  .field-label {
    font-size: 0.75rem;
    font-weight: 700;
    color: var(--text-soft);
    overflow-wrap: anywhere;
  }
  input,
  textarea,
  select {
    width: 100%;
    min-height: 42px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.6rem;
    background: var(--surface-subtle);
    color: var(--text);
    font: inherit;
    font-size: 0.8rem;
  }
  fieldset {
    min-width: 0;
    max-height: 180px;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.5rem;
  }
  legend {
    font-size: 0.75rem;
    font-weight: 700;
  }
  textarea {
    resize: vertical;
  }
  small {
    color: var(--text-muted);
    font-size: 0.7rem;
  }
  .checkbox {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-height: 42px;
  }
  .checkbox input {
    width: 18px;
    min-height: 18px;
  }
  .attachment-inputs {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 0.5rem;
    align-items: end;
  }
  .attachment-inputs label {
    display: grid;
    gap: 0.3rem;
  }
  button {
    min-height: 36px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.4rem 0.65rem;
    background: var(--surface-raised);
    color: var(--text);
    cursor: pointer;
    font: inherit;
    font-size: 0.75rem;
  }
  :disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  @media (max-width: 620px) {
    section,
    .attachment-inputs {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
