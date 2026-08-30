<script lang="ts">
  import { interactionResponses } from '$lib/chat/interaction-responses.svelte';
  import {
    interactionResponseAttachments,
    interactionResponseEncryptedManifests,
    interactionResponseHasMessageContent,
    interactionResponsePoll,
    type MessageLayoutComponent,
    type MessageEmbed
  } from '$lib/chat/rich-content';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { entityKey, entityRef } from '$lib/chat/refs';
  import { listInteractionPollVoters, setInteractionPollVote } from '$lib/chat/interactions';
  import { onMount } from 'svelte';
  import EphemeralAttachments from './EphemeralAttachments.svelte';
  import Markdown from './Markdown.svelte';
  import MessageComponents from './MessageComponents.svelte';
  import MessagePoll from './MessagePoll.svelte';
  import RichEmbed from './RichEmbed.svelte';

  let { channelRef }: { channelRef: string } = $props();

  const visible = $derived(
    Object.values(interactionResponses.byResponse)
      .filter(
        (event) =>
          event.ephemeral &&
          interactionResponses.context(event.interaction_ref ?? null)?.channelRef === channelRef &&
          [4, 5].includes(event.callback_type ?? event.response_type ?? event.type ?? 0) &&
          !event.deleted_at
      )
      .sort((left, right) => {
        const interaction = (left.interaction_ref ?? '').localeCompare(right.interaction_ref ?? '');
        return interaction || (left.sequence ?? 0) - (right.sequence ?? 0);
      })
  );
  let now = $state(Date.now());

  onMount(() => {
    const timer = window.setInterval(() => (now = Date.now()), 1_000);
    return () => window.clearInterval(timer);
  });

  function data(event: (typeof visible)[number]): Record<string, unknown> {
    return event.data ?? event.payload ?? {};
  }

  function viewExpired(payload: Record<string, unknown>): boolean {
    if (typeof payload.view_expires_at !== 'string') return false;
    const expiry = Date.parse(payload.view_expires_at);
    return !Number.isFinite(expiry) || expiry <= now;
  }

  function guildForChannel(channelRef: string) {
    const channel = chatEntities.channels.values.find((item) => entityKey(item) === channelRef);
    if (!channel?.guild_id || !channel.guild_domain) return null;
    return (
      chatEntities.guilds.values.find(
        (guild) => guild.id === channel.guild_id && guild.origin_domain === channel.guild_domain
      ) ?? null
    );
  }
</script>

{#if visible.length}
  <section class="ephemeral-tray" aria-label="Private bot responses" aria-live="polite">
    {#each visible as event (event.response_ref ?? event.interaction_ref)}
      {@const payload = data(event)}
      {@const responseType = event.callback_type ?? event.response_type ?? event.type}
      {@const attachments = interactionResponseAttachments(payload)}
      {@const encryptedManifests = interactionResponseEncryptedManifests(payload)}
      {@const poll = interactionResponsePoll(payload)}
      {@const hasMessage = interactionResponseHasMessageContent(payload)}
      {@const request = interactionResponses.context(event.interaction_ref ?? null)}
      <article>
        <header>
          <span><strong>Only you can see this</strong> · Bot response</span>
          <button
            type="button"
            aria-label="Dismiss private bot response"
            onclick={() =>
              interactionResponses.clear(event.interaction_ref ?? '', event.response_ref)}>×</button
          >
        </header>
        {#if responseType === 5 && !hasMessage}
          <p class="thinking" role="status"><span aria-hidden="true"></span> Bot is thinking…</p>
        {/if}
        {#if typeof payload.content === 'string' && payload.content}
          <Markdown content={payload.content} />
        {/if}
        {#if Array.isArray(payload.embeds)}
          {#each payload.embeds as embed, index (index)}
            <RichEmbed
              embed={embed as MessageEmbed}
              {attachments}
              allowExternalMedia={!request?.e2ee}
            />
          {/each}
        {/if}
        {#if attachments.length}
          <EphemeralAttachments {attachments} {encryptedManifests} />
        {/if}
        {#if poll}
          <MessagePoll
            {poll}
            channelRef={request?.channelRef ?? ''}
            messageRef={event.response_ref ?? ''}
            disabled={!event.response_ref}
            onVote={(answerId, selected) =>
              setInteractionPollVote(
                event.interaction_ref ?? '',
                event.response_ref ?? '',
                answerId,
                selected
              )}
            onLoadVoters={(answerId, after) =>
              listInteractionPollVoters(
                event.interaction_ref ?? '',
                event.response_ref ?? '',
                answerId,
                after
              )}
          />
        {/if}
        {#if Array.isArray(payload.components) && payload.components.length}
          {@const viewVersion = Number(payload.view_version)}
          {@const expired = viewExpired(payload)}
          {@const guild = request ? guildForChannel(request.channelRef) : null}
          {#if request && event.response_id && Number.isInteger(viewVersion) && viewVersion > 0}
            <MessageComponents
              components={payload.components as MessageLayoutComponent[]}
              application={request.applicationRef}
              channel={request.channelRef}
              ephemeralResponseId={event.response_id}
              {viewVersion}
              guildRef={guild ? entityRef(guild) : null}
              users={guild ? [] : chatEntities.users.values}
              roles={guild?.roles ?? []}
              channels={guild?.channels ?? chatEntities.channels.values}
              disabled={expired}
              allowExternalMedia={!request?.e2ee}
              interactionRequest={request}
              {attachments}
            />
            {#if expired}
              <p class="component-note">
                These private bot controls expired. Run the command again.
              </p>
            {/if}
          {:else}
            <p class="component-note">
              These bot controls expired or are missing their private interaction context. Run the
              command again.
            </p>
          {/if}
        {/if}
      </article>
    {/each}
  </section>
{/if}

<style>
  .ephemeral-tray {
    display: grid;
    width: min(720px, calc(100% - 32px));
    margin: 8px 16px 16px;
    gap: 9px;
  }
  article {
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--line));
    border-radius: 10px;
    padding: 12px;
    background: var(--surface);
    box-shadow: var(--shadow-lg);
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
    color: var(--text-muted);
    font-size: 0.72rem;
  }
  header button {
    border: 0;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.2rem;
    cursor: pointer;
  }
  .component-note {
    margin: 8px 0 0;
    color: var(--text-muted);
    font-size: 0.74rem;
  }
  .thinking {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    color: var(--text-muted);
    font-size: 0.84rem;
  }
  .thinking span {
    width: 13px;
    height: 13px;
    border: 2px solid var(--line);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
