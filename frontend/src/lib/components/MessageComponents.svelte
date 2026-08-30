<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import type { Attachment, Channel, Message, Role, UserSummary } from '$lib/chat/types';
  import type {
    ActionRow,
    ButtonComponent,
    EntitySelectComponent,
    MessageComponent,
    MessageLayoutComponent,
    StringSelectComponent
  } from '$lib/chat/rich-content';
  import {
    componentInteractionBody,
    ephemeralComponentInteractionBody,
    entitySelectOptions,
    partialEmojiText,
    selectDefaultValues,
    selectSubmissionState
  } from '$lib/chat/rich-content';
  import { applicationRef as messageApplicationRef } from '$lib/chat/rich-content';
  import { createInteraction } from '$lib/chat/interactions';
  import type { InteractionRequestContext } from '$lib/chat/interaction-responses.svelte';
  import GuildMemberPicker from './GuildMemberPicker.svelte';
  import PartialEmoji from './PartialEmoji.svelte';
  import V2MessageLayout from './V2MessageLayout.svelte';

  let {
    message = null,
    components = [],
    application = null,
    channel = null,
    ephemeralResponseId = null,
    viewVersion = null,
    users = [],
    roles = [],
    channels = [],
    guildRef = null,
    disabled = false,
    attachments = [],
    allowExternalMedia = true,
    interactionRequest = null,
    resolveInteractionRequest
  }: {
    message?: Message | null;
    components?: MessageLayoutComponent[];
    application?: string | null;
    channel?: string | null;
    ephemeralResponseId?: string | null;
    viewVersion?: number | null;
    users?: UserSummary[];
    roles?: Role[];
    channels?: Channel[];
    guildRef?: string | null;
    disabled?: boolean;
    attachments?: Attachment[];
    /** False for decrypted E2EE layouts so authored URLs stay device-private until clicked. */
    allowExternalMedia?: boolean;
    interactionRequest?: InteractionRequestContext | null;
    resolveInteractionRequest?: (applicationRef: string) => Promise<InteractionRequestContext>;
  } = $props();

  let busyId = $state<string | null>(null);
  let notice = $state('');
  let error = $state('');
  let selectionDrafts = $state<Record<string, string[]>>({});
  const rows = $derived(message?.components ?? components);
  const appRef = $derived(application ?? (message ? messageApplicationRef(message) : null));
  const channelRef = $derived(
    channel ?? (message ? `${message.channel_id}@${message.channel_domain}` : null)
  );
  const messageRef = $derived(message ? `${message.id}@${message.origin_domain}` : null);

  function customId(component: MessageComponent): string | null {
    return 'custom_id' in component ? (component.custom_id ?? null) : null;
  }

  async function invoke(component: Exclude<MessageComponent, { type: 4 }>, values: string[] = []) {
    const body = message
      ? componentInteractionBody(message, component, values)
      : appRef && ephemeralResponseId && viewVersion
        ? ephemeralComponentInteractionBody(
            appRef,
            ephemeralResponseId,
            viewVersion,
            component,
            values
          )
        : null;
    const id = customId(component);
    if (!body || !id || !appRef || !channelRef) {
      error = 'This control is no longer connected to its app.';
      return;
    }
    busyId = id;
    notice = '';
    error = '';
    try {
      const request =
        interactionRequest ??
        (resolveInteractionRequest
          ? await resolveInteractionRequest(appRef)
          : {
              channelRef,
              applicationRef: appRef
            });
      await createInteraction(
        {
          ...request,
          ...(messageRef ? { messageRef } : {})
        },
        body,
        { componentType: component.type }
      );
      notice = 'Sent to the bot.';
    } catch (caught) {
      error = userErrorMessage(caught, 'The bot did not receive that interaction. Try again.');
    } finally {
      busyId = null;
    }
  }

  function selectedValues(event: Event): string[] {
    return [...(event.currentTarget as HTMLSelectElement).selectedOptions]
      .map((option) => option.value)
      .filter(Boolean);
  }

  function selectionKey(
    rowIndex: number | string,
    componentIndex: number,
    component: StringSelectComponent | EntitySelectComponent
  ): string {
    return `${viewVersion ?? message?.view_version ?? 0}:${rowIndex}:${componentIndex}:${component.custom_id}`;
  }

  function stagedValues(
    key: string,
    component: StringSelectComponent | EntitySelectComponent
  ): string[] {
    return selectionDrafts[key] ?? selectDefaultValues(component);
  }

  function stageSelection(key: string, values: string[]) {
    selectionDrafts = { ...selectionDrafts, [key]: values };
    notice = '';
    error = '';
  }

  function buttonClass(component: ButtonComponent): string {
    return `component-button style-${component.style}`;
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- link buttons are bot-authored external URLs -->

{#snippet actionRow(row: ActionRow, rowKey: string)}
  <div class="action-row">
    {#each row.components as component, componentIndex (`${viewVersion ?? message?.view_version ?? 0}:${rowKey}:${componentIndex}:${customId(component) ?? component.type}`)}
      {#if component.type === 2}
        {@const button = component as ButtonComponent}
        {#if button.style === 5 && button.url && !disabled}
          <a
            class={buttonClass(button)}
            href={button.url}
            target="_blank"
            rel="noopener noreferrer nofollow"
          >
            {#if button.emoji}<PartialEmoji emoji={button.emoji} decorative />{/if}
            <span>{button.label ?? 'Open link'}</span>
            <span aria-hidden="true">↗</span>
          </a>
        {:else if button.style === 6}
          <button
            class={buttonClass(button)}
            type="button"
            disabled
            title="Premium purchase buttons do not invoke the bot"
          >
            {#if button.emoji}<PartialEmoji emoji={button.emoji} decorative />{/if}
            <span>{button.label ?? 'Premium item'}</span>
          </button>
        {:else}
          <button
            class={buttonClass(button)}
            type="button"
            disabled={disabled || button.disabled || busyId !== null || !appRef}
            onclick={() => void invoke(button)}
          >
            {#if button.emoji}<PartialEmoji emoji={button.emoji} decorative />{/if}
            <span>{button.label ?? 'Button'}</span>
          </button>
        {/if}
      {:else if component.type === 3}
        {@const select = component as StringSelectComponent}
        {@const key = selectionKey(rowKey, componentIndex, select)}
        {@const values = stagedValues(key, select)}
        {@const submission = selectSubmissionState(select, values)}
        <label class="select-wrap">
          <span class="visually-hidden">{select.placeholder ?? 'Choose an option'}</span>
          <select
            multiple={submission.staged}
            size={submission.staged ? Math.min(select.options.length, 5) : 1}
            disabled={disabled || select.disabled || busyId !== null || !appRef}
            aria-label={select.placeholder ?? 'Choose an option'}
            onchange={(event) => {
              const next = selectedValues(event);
              if (submission.staged) stageSelection(key, next);
              else void invoke(select, next);
            }}
          >
            {#if (select.min_values ?? 1) === 0 && (select.max_values ?? 1) === 1}
              <option value="">{select.placeholder ?? 'None'}</option>
            {:else if (select.max_values ?? 1) === 1}
              <option value="" disabled selected={values.length === 0}
                >{select.placeholder ?? 'Choose an option'}</option
              >
            {/if}
            {#each select.options as option (option.value)}
              <option value={option.value} selected={values.includes(option.value)}>
                {partialEmojiText(option.emoji)}{option.emoji
                  ? ' '
                  : ''}{option.label}{option.description ? ` — ${option.description}` : ''}
              </option>
            {/each}
          </select>
          {#if submission.staged}
            <button
              class="selection-submit"
              type="button"
              disabled={disabled ||
                select.disabled ||
                busyId !== null ||
                !appRef ||
                !submission.valid}
              onclick={() => void invoke(select, values)}
              >Submit selection · {values.length}/{submission.maximum}</button
            >
            {#if !submission.valid}<small
                >Choose between {submission.minimum} and {submission.maximum} options.</small
              >{/if}
          {/if}
        </label>
      {:else if [5, 6, 7, 8].includes(component.type as number)}
        {@const select = component as EntitySelectComponent}
        {@const key = selectionKey(rowKey, componentIndex, select)}
        {@const values = stagedValues(key, select)}
        {@const submission = selectSubmissionState(select, values)}
        {@const searchesGuildMembers = select.type === 5 || select.type === 7}
        {@const options = entitySelectOptions(select, {
          users: searchesGuildMembers ? [] : users,
          roles,
          channels
        })}
        <label class="select-wrap">
          <span class="visually-hidden">{select.placeholder ?? 'Choose an item'}</span>
          {#if searchesGuildMembers}
            <GuildMemberPicker
              {guildRef}
              fallbackUsers={guildRef ? [] : users}
              staticOptions={options}
              value={values}
              multiple={submission.staged}
              maxValues={submission.maximum}
              optional={submission.minimum === 0}
              placeholder={select.placeholder ?? 'Choose an item'}
              disabled={disabled || select.disabled || busyId !== null || !appRef}
              onChange={(next) => {
                if (submission.staged) stageSelection(key, next);
                else void invoke(select, next);
              }}
            />
          {:else}
            <select
              multiple={submission.staged}
              size={submission.staged ? Math.min(Math.max(options.length, 2), 5) : 1}
              disabled={disabled ||
                select.disabled ||
                busyId !== null ||
                !appRef ||
                options.length === 0}
              aria-label={select.placeholder ?? 'Choose an item'}
              onchange={(event) => {
                const next = selectedValues(event);
                if (submission.staged) stageSelection(key, next);
                else void invoke(select, next);
              }}
            >
              {#if submission.minimum === 0 && !submission.staged}
                <option value="">{select.placeholder ?? 'None'}</option>
              {:else if !submission.staged}
                <option value="" disabled selected={values.length === 0}
                  >{select.placeholder ?? 'Choose an item'}</option
                >
              {/if}
              {#each options as option (option.value)}
                <option value={option.value} selected={values.includes(option.value)}
                  >{option.label}</option
                >
              {/each}
            </select>
            {#if options.length === 0}<small>No matching items are available here.</small>{/if}
          {/if}
          {#if submission.staged}
            <button
              class="selection-submit"
              type="button"
              disabled={disabled ||
                select.disabled ||
                busyId !== null ||
                !appRef ||
                !submission.valid}
              onclick={() => void invoke(select, values)}
              >Submit selection · {values.length}/{submission.maximum}</button
            >
            {#if !submission.valid}<small
                >Choose between {submission.minimum} and {submission.maximum} items.</small
              >{/if}
          {/if}
        </label>
      {/if}
    {/each}
  </div>
{/snippet}

<div class="message-components" aria-label="Message controls and layout">
  {#each rows as layout, rowIndex (`${viewVersion ?? message?.view_version ?? 0}:${layout.id ?? rowIndex}`)}
    {@const rowKey = `${viewVersion ?? message?.view_version ?? 0}:${layout.id ?? rowIndex}`}
    {#if layout.type === 1}
      {@render actionRow(layout, rowKey)}
    {:else}
      <V2MessageLayout
        {layout}
        layoutKey={rowKey}
        attachments={message?.attachments ?? attachments}
        mentionUsers={users}
        mentionRoles={roles}
        {allowExternalMedia}
        {actionRow}
      />
    {/if}
  {/each}
  {#if notice}<small class="component-notice" role="status">{notice}</small>{/if}
  {#if error}<small class="component-error" role="alert">{error}</small>{/if}
</div>

<style>
  .message-components {
    display: grid;
    width: min(540px, 100%);
    gap: 6px;
    margin-top: 8px;
  }
  .action-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .component-button {
    display: inline-flex;
    min-height: 34px;
    align-items: center;
    justify-content: center;
    gap: 6px;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 12px;
    color: white;
    background: #5865f2;
    font: inherit;
    font-size: 0.82rem;
    font-weight: 700;
    text-decoration: none;
    cursor: pointer;
  }
  .component-button.style-2,
  .component-button.style-5 {
    border-color: var(--line);
    color: var(--text);
    background: var(--surface-raised);
  }
  .component-button.style-3 {
    background: #248046;
  }
  .component-button.style-4 {
    background: var(--danger);
  }
  .component-button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  .select-wrap {
    display: grid;
    min-width: min(280px, 100%);
    flex: 1;
    gap: 4px;
  }
  select {
    min-height: 36px;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 6px 9px;
    color: var(--text);
    background: var(--surface-raised);
  }
  .select-wrap small,
  .component-notice {
    color: var(--text-muted);
  }
  .selection-submit {
    min-height: 32px;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 5px 10px;
    color: var(--text);
    background: var(--surface-raised);
    font: inherit;
    font-size: 0.76rem;
    font-weight: 750;
    cursor: pointer;
  }
  .selection-submit:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  .component-notice,
  .component-error {
    font-size: 0.72rem;
  }
  .component-error {
    color: var(--danger);
  }
</style>
