import { api } from '$lib/api/client';
import {
  parseApplicationCommandAutocompleteChoices,
  type ApplicationCommand,
  type ApplicationCommandAutocompleteChoice
} from './application-commands';
import type { Channel, Message, UserSummary } from './types';
import { initializeE2EE, type KaedeE2EEClient } from '$lib/e2ee/client';
import { entityRef } from './refs';
import type { EncryptedFileManifest } from '$lib/e2ee/media';
import type { PendingUpload } from '$lib/media/uploads';
import {
  interactionResponses,
  type InteractionRequestContext
} from './interaction-responses.svelte';

export interface InteractionAcknowledgement {
  id: string;
  interaction_ref: string;
  status: string;
  ack_deadline: string;
}

export interface InteractionEncryptionIntent {
  componentType?: number | string | null;
  attachmentIds?: readonly string[];
  attachments?: Record<string, EncryptedFileManifest>;
}

export function interactionFileEncryptionIntent(
  attachmentIds: readonly string[],
  uploads: readonly PendingUpload[]
): InteractionEncryptionIntent {
  if (!attachmentIds.length) return {};
  const attachments = Object.fromEntries(
    attachmentIds.map((attachmentId) => {
      const upload = uploads.find(
        (candidate) => candidate.attachmentId === attachmentId && candidate.status === 'ready'
      );
      if (!upload?.encryptedManifest) {
        throw new Error('Reattach the file before sending this encrypted bot interaction.');
      }
      return [attachmentId, upload.encryptedManifest];
    })
  );
  return { attachmentIds, attachments };
}

export async function commandInteractionRequestContext(
  channel: Channel,
  command: ApplicationCommand,
  user: UserSummary | null,
  activeClient: KaedeE2EEClient | null
): Promise<InteractionRequestContext> {
  const context: InteractionRequestContext = {
    channelRef: entityRef(channel),
    applicationRef: command.application_ref
  };
  if (channel.encryption_mode !== 'e2ee') return context;
  if (!user) throw new Error('Sign in again before using an encrypted bot command.');
  const client = activeClient ?? (await initializeE2EE(user));
  return {
    ...context,
    e2ee: {
      client,
      channel,
      integrationType: command.integration_type,
      interactionContext: command.interaction_context
    }
  };
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function optionalInteger(value: unknown): string | number | null {
  return typeof value === 'string' || typeof value === 'number' ? value : null;
}

async function encryptedInteractionBody(
  context: InteractionRequestContext,
  body: Record<string, unknown>,
  intent: InteractionEncryptionIntent
): Promise<Record<string, unknown>> {
  const encryption = context.e2ee;
  if (!encryption) return body;
  const interactionType =
    body.interaction_type === 'autocomplete' ||
    body.interaction_type === 'component' ||
    body.interaction_type === 'modal_submit'
      ? body.interaction_type
      : 'command';
  const commandType =
    interactionType === 'command' || interactionType === 'autocomplete'
      ? body.command_type === 'user' || body.command_type === 'message'
        ? body.command_type
        : 'chat_input'
      : null;
  const prepared = await encryption.client.encryptInteraction(encryption.channel, {
    applicationRef: context.applicationRef,
    integrationType: encryption.integrationType,
    interactionContext: encryption.interactionContext,
    interactionType,
    commandId:
      interactionType === 'command' || interactionType === 'autocomplete'
        ? optionalString(body.command_id)
        : null,
    commandName:
      interactionType === 'command' || interactionType === 'autocomplete'
        ? optionalString(body.command_name)
        : null,
    commandType,
    componentType: interactionType === 'component' ? (intent.componentType ?? null) : null,
    customId:
      interactionType === 'component' || interactionType === 'modal_submit'
        ? optionalString(body.custom_id)
        : null,
    messageRef: interactionType === 'component' ? optionalString(body.message_ref) : null,
    responseId:
      interactionType === 'component' || interactionType === 'modal_submit'
        ? optionalInteger(body.response_id)
        : null,
    targetRef: interactionType === 'command' ? optionalString(body.target_ref) : null,
    viewVersion: interactionType === 'component' ? optionalInteger(body.view_version) : null,
    autocompleteGeneration:
      interactionType === 'autocomplete' ? optionalInteger(body.autocomplete_generation) : null,
    focusedOption: interactionType === 'autocomplete' ? optionalString(body.focused_option) : null,
    attachmentIds: intent.attachmentIds,
    attachments: intent.attachments,
    options:
      interactionType === 'command' || interactionType === 'autocomplete'
        ? ((body.options as Record<string, unknown> | undefined) ?? {})
        : {},
    values:
      interactionType === 'component' && Array.isArray(body.values)
        ? body.values.filter((item): item is string => typeof item === 'string')
        : [],
    components:
      interactionType === 'modal_submit' && Array.isArray(body.components)
        ? body.components.filter(
            (item): item is Record<string, unknown> =>
              Boolean(item) && typeof item === 'object' && !Array.isArray(item)
          )
        : []
  });
  return {
    ...body,
    options: {},
    values: [],
    components: [],
    encrypted_payload: prepared.envelope,
    attachment_ids: prepared.attachmentIds
  };
}

export async function createInteraction(
  context: InteractionRequestContext,
  body: Record<string, unknown>,
  encryptionIntent: InteractionEncryptionIntent = {}
): Promise<InteractionAcknowledgement> {
  const wireBody = await encryptedInteractionBody(context, body, encryptionIntent);
  const acknowledged = await api<InteractionAcknowledgement>(
    `/channels/${encodeURIComponent(context.channelRef)}/interactions`,
    { method: 'POST', body: JSON.stringify(wireBody) }
  );
  interactionResponses.register(acknowledged.interaction_ref, context);
  return acknowledged;
}

export async function requestCommandAutocomplete(
  context: InteractionRequestContext,
  body: Record<string, unknown>,
  encryptionIntent: InteractionEncryptionIntent = {}
): Promise<ApplicationCommandAutocompleteChoice[]> {
  const acknowledged = await createInteraction(context, body, encryptionIntent);
  const event = await interactionResponses.wait(acknowledged.interaction_ref);
  if (!event || (event.callback_type ?? event.response_type ?? event.type) !== 8) return [];
  const data = event.data ?? event.payload;
  if (!data || !Array.isArray(data.choices)) return [];
  try {
    return parseApplicationCommandAutocompleteChoices(data.choices);
  } catch {
    return [];
  }
}

export function pollVotePath(channelRef: string, messageRef: string, answerId: number): string {
  return `/channels/${encodeURIComponent(channelRef)}/messages/${encodeURIComponent(messageRef)}/polls/answers/${answerId}/@me`;
}

export interface PollVotersPage {
  users: UserSummary[];
  next_after: string | null;
}

export function pollVotersPath(
  channelRef: string,
  messageRef: string,
  answerId: number,
  after?: string | null
): string {
  const base = pollVotePath(channelRef, messageRef, answerId).replace(/\/@me$/, '');
  return after ? `${base}?after=${encodeURIComponent(after)}` : base;
}

export async function listPollVoters(
  channelRef: string,
  messageRef: string,
  answerId: number,
  after?: string | null
): Promise<PollVotersPage> {
  return api<PollVotersPage>(pollVotersPath(channelRef, messageRef, answerId, after));
}

export function finalizePollPath(channelRef: string, messageRef: string): string {
  return `/channels/${encodeURIComponent(channelRef)}/messages/${encodeURIComponent(messageRef)}/polls/expire`;
}

export async function finalizePoll(channelRef: string, messageRef: string): Promise<Message> {
  return api<Message>(finalizePollPath(channelRef, messageRef), { method: 'POST' });
}

export async function setPollVote(
  channelRef: string,
  messageRef: string,
  answerId: number,
  selected: boolean
): Promise<void> {
  await api<void>(pollVotePath(channelRef, messageRef, answerId), {
    method: selected ? 'PUT' : 'DELETE'
  });
}

export function interactionPollVotePath(
  interactionId: string,
  responseId: string,
  answerId: number
): string {
  return `/interactions/${encodeURIComponent(interactionId)}/responses/${encodeURIComponent(responseId)}/polls/answers/${answerId}/@me`;
}

export function interactionPollVotersPath(
  interactionId: string,
  responseId: string,
  answerId: number,
  after?: string | null
): string {
  const base = interactionPollVotePath(interactionId, responseId, answerId).replace(/\/@me$/, '');
  return after ? `${base}?after=${encodeURIComponent(after)}` : base;
}

export async function setInteractionPollVote(
  interactionId: string,
  responseId: string,
  answerId: number,
  selected: boolean
): Promise<void> {
  await api<void>(interactionPollVotePath(interactionId, responseId, answerId), {
    method: selected ? 'PUT' : 'DELETE'
  });
}

export async function listInteractionPollVoters(
  interactionId: string,
  responseId: string,
  answerId: number,
  after?: string | null
): Promise<PollVotersPage> {
  return api<PollVotersPage>(interactionPollVotersPath(interactionId, responseId, answerId, after));
}

export function forwardedMessagePath(channelRef: string, messageRef: string): string {
  return `/channels/${encodeURIComponent(channelRef)}/messages/${encodeURIComponent(messageRef)}/forwarded`;
}

export interface ForwardMessageResult {
  forwards: Array<{ destination_channel_ref: string; message: Message }>;
  failures: Array<{
    destination_channel_ref: string;
    status: number;
    error: { code?: string; message?: string } | string;
  }>;
}

export interface PreparedForwardSource {
  message_ref: string;
  channel_ref: string;
  encryption_mode: 'plaintext' | 'e2ee';
  projection_version: 2;
  projection_digest: string;
  created_at: string;
  edited_at: string | null;
  flags: number;
  message_type: number;
  nsfw: boolean;
  attachment_refs: string[];
  snapshot: Record<string, unknown> | null;
}

export interface PreparedForwardDestination {
  channel_id: string;
  client_nonce: string;
  encryption_mode: 'plaintext' | 'e2ee';
  requires_plaintext_disclosure: boolean;
  authorization: Record<string, unknown>;
}

export interface PreparedForwardResponse {
  source: PreparedForwardSource;
  destinations: PreparedForwardDestination[];
}

export interface PreparedForwardMessage {
  destination_channel_id: string;
  message: Record<string, unknown>;
}

export async function prepareMessageForward(
  sourceChannelRef: string,
  sourceMessageRef: string,
  destinations: Array<{ channel_id: string; client_nonce: string }>
): Promise<PreparedForwardResponse> {
  return api<PreparedForwardResponse>(
    `/channels/${encodeURIComponent(sourceChannelRef)}/messages/${encodeURIComponent(sourceMessageRef)}/forward/prepare`,
    { method: 'POST', body: JSON.stringify({ destinations }) }
  );
}

export async function submitPreparedMessageForward(
  sourceChannelRef: string,
  sourceMessageRef: string,
  destinations: PreparedForwardMessage[]
): Promise<ForwardMessageResult> {
  return api<ForwardMessageResult>(
    `/channels/${encodeURIComponent(sourceChannelRef)}/messages/${encodeURIComponent(sourceMessageRef)}/forward`,
    { method: 'POST', body: JSON.stringify({ destinations }) }
  );
}

export async function forwardMessage(
  sourceChannelRef: string,
  sourceMessageRef: string,
  destinationChannelRefs: string[],
  content?: string
): Promise<ForwardMessageResult> {
  return api<ForwardMessageResult>(
    `/channels/${encodeURIComponent(sourceChannelRef)}/messages/${encodeURIComponent(sourceMessageRef)}/forward`,
    {
      method: 'POST',
      body: JSON.stringify({
        destination_channel_ids: destinationChannelRefs,
        ...(content?.trim() ? { content: content.trim() } : {})
      })
    }
  );
}
