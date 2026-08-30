import type { InteractionModal, InteractionResponseEvent } from './rich-content';
import { modalFromInteractionEvent } from './rich-content';
import { SvelteSet } from 'svelte/reactivity';
import type {
  InteractionChannelContext,
  InteractionIntegrationType,
  KaedeE2EEClient
} from '$lib/e2ee/client';
import type { Channel } from './types';
import { isCanonicalFederationDomain, parseCanonicalEntityRef } from './refs';
import { isCanonicalBase64url32 } from '$lib/e2ee/encoding';

export type InteractionResponseEventName =
  'INTERACTION_RESPONSE_CREATE' | 'INTERACTION_RESPONSE_UPDATE' | 'INTERACTION_RESPONSE_DELETE';

interface ActiveModal {
  interactionRef: string;
  interactionId: string;
  responseRef: string;
  responseId: string | null;
  modal: InteractionModal;
}

export interface InteractionRequestContext {
  channelRef: string;
  applicationRef: string;
  authorityDomain?: string;
  messageRef?: string;
  e2ee?: {
    client: KaedeE2EEClient;
    channel: Channel;
    integrationType: InteractionIntegrationType;
    interactionContext: InteractionChannelContext;
  };
}

function encryptedResponseViewProjection(
  data: Record<string, unknown>,
  callbackType: number,
  responseExpiresAt: string
): Record<string, unknown> | null {
  const fields = Object.keys(data).sort().join(',');
  if (fields === 'attachments,e2ee') return {};
  if (
    fields !== 'attachments,e2ee,view_expires_at,view_persistent,view_version' ||
    ![4, 7].includes(callbackType) ||
    !Number.isSafeInteger(data.view_version) ||
    Number(data.view_version) < 1 ||
    data.view_persistent !== false ||
    typeof data.view_expires_at !== 'string' ||
    !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(data.view_expires_at) ||
    !Number.isFinite(Date.parse(data.view_expires_at)) ||
    Date.parse(data.view_expires_at) > Date.parse(responseExpiresAt)
  ) {
    return null;
  }
  return {
    view_expires_at: data.view_expires_at,
    view_persistent: false,
    view_version: data.view_version
  };
}

class InteractionResponseState {
  /** Latest callback for autocomplete/modal correlation by interaction. */
  byInteraction = $state<Record<string, InteractionResponseEvent>>({});
  /** Every independently editable message response, including follow-ups. */
  byResponse = $state<Record<string, InteractionResponseEvent>>({});
  contexts = $state<Record<string, InteractionRequestContext>>({});
  activeModal = $state<ActiveModal | null>(null);
  private waiters = new Map<string, Set<(event: InteractionResponseEvent | null) => void>>();
  private revisions = new Map<string, bigint>();
  private tombstones = new Set<string>();
  private responseIdentity = new Map<
    string,
    {
      interactionRef: string;
      invokerRef: string;
      channelRef: string;
      applicationRef: string;
      responseGrantId: string;
      sequence: number;
      callbackType: number;
      ephemeral: boolean;
      messageRef: string | null;
      autocompleteGeneration: string | null;
      expiresAt: string;
    }
  >();
  private pending = new Map<
    string,
    {
      name: Exclude<InteractionResponseEventName, 'INTERACTION_RESPONSE_DELETE'>;
      event: InteractionResponseEvent;
    }
  >();
  private decrypting = new Map<string, symbol>();
  private generation = 0;

  private resolveWaiters(interactionRef: string, event: InteractionResponseEvent | null): void {
    const listeners = this.waiters.get(interactionRef);
    if (!listeners) return;
    this.waiters.delete(interactionRef);
    for (const listener of listeners) listener(event);
  }

  apply(name: InteractionResponseEventName, raw: unknown): void {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
    const event = this.validEvent(name, raw as InteractionResponseEvent);
    if (!event) return;
    const interactionRef = event.interaction_ref!;
    const responseRef = event.response_ref!;
    const revision = BigInt(event.revision!);
    const identity = this.responseIdentity.get(responseRef);
    if (
      identity &&
      (identity.interactionRef !== interactionRef ||
        identity.invokerRef !== event.invoker_ref ||
        identity.channelRef !== event.channel_ref ||
        identity.applicationRef !== event.application_ref ||
        identity.responseGrantId !== event.response_grant_id ||
        identity.sequence !== event.sequence ||
        identity.callbackType !== event.callback_type ||
        identity.ephemeral !== event.ephemeral ||
        identity.messageRef !== event.message_ref ||
        identity.autocompleteGeneration !== event.autocomplete_generation ||
        identity.expiresAt !== event.expires_at)
    ) {
      return;
    }
    const previousRevision = this.revisions.get(responseRef);
    if (previousRevision !== undefined && revision <= previousRevision) return;
    this.responseIdentity.set(responseRef, {
      interactionRef,
      invokerRef: event.invoker_ref!,
      channelRef: event.channel_ref!,
      applicationRef: event.application_ref!,
      responseGrantId: event.response_grant_id!,
      sequence: event.sequence!,
      callbackType: event.callback_type!,
      ephemeral: event.ephemeral!,
      messageRef: event.message_ref!,
      autocompleteGeneration: event.autocomplete_generation as string | null,
      expiresAt: event.expires_at!
    });
    this.revisions.set(responseRef, revision);
    if (name === 'INTERACTION_RESPONSE_DELETE') {
      this.tombstones.add(responseRef);
      this.pending.delete(responseRef);
      const remaining = { ...this.byResponse };
      delete remaining[responseRef];
      this.byResponse = remaining;
      this.refreshLatest(interactionRef);
      if (
        this.activeModal?.interactionRef === interactionRef &&
        this.activeModal.responseRef === responseRef
      ) {
        this.activeModal = null;
      }
      this.resolveWaiters(interactionRef, null);
      return;
    }
    if (this.tombstones.has(responseRef)) return;
    const isolated =
      event.ephemeral === true || event.callback_type === 8 || event.callback_type === 9;
    if (isolated) {
      this.pending.set(responseRef, { name, event });
      while (this.pending.size > 256) this.pending.delete(this.pending.keys().next().value!);
      void this.consumePending(responseRef);
      return;
    }
    this.commit(event);
  }

  private commit(event: InteractionResponseEvent): void {
    const interactionRef = event.interaction_ref!;
    const responseRef = event.response_ref!;
    if (this.tombstones.has(responseRef)) return;
    const merged = { ...(this.byResponse[responseRef] ?? {}), ...event, data: event.data ?? {} };
    this.byResponse = { ...this.byResponse, [responseRef]: merged };
    this.refreshLatest(interactionRef);
    this.resolveWaiters(interactionRef, merged);
    const modal = modalFromInteractionEvent(merged);
    if (modal) {
      this.activeModal = {
        interactionRef,
        interactionId: event.interaction_id,
        responseRef,
        responseId: event.response_id ?? null,
        modal
      };
    }
  }

  private unavailable(event: InteractionResponseEvent): InteractionResponseEvent {
    return {
      ...event,
      data: {
        content: 'This encrypted bot response is unavailable on this device.'
      },
      decryption_unavailable: true
    };
  }

  private async consumePending(responseRef: string): Promise<void> {
    if (this.decrypting.has(responseRef)) return;
    const pending = this.pending.get(responseRef);
    if (!pending) return;
    const interactionRef = pending.event.interaction_ref!;
    const request = this.contexts[interactionRef];
    if (!request) return;
    const generation = this.generation;
    const decryptToken = Symbol(responseRef);
    this.decrypting.set(responseRef, decryptToken);
    this.pending.delete(responseRef);
    const revision = pending.event.revision!;
    let ready: InteractionResponseEvent;
    try {
      const rawData = pending.event.data ?? {};
      const encrypted = rawData.e2ee;
      if (
        pending.event.channel_ref !== request.channelRef ||
        pending.event.application_ref !== request.applicationRef
      ) {
        ready = this.unavailable(pending.event);
      } else if (request.e2ee) {
        const viewProjection = encryptedResponseViewProjection(
          rawData,
          pending.event.callback_type!,
          pending.event.expires_at!
        );
        if (
          viewProjection === null ||
          !encrypted ||
          typeof encrypted !== 'object' ||
          Array.isArray(encrypted) ||
          !Array.isArray(rawData.attachments)
        ) {
          ready = this.unavailable(pending.event);
        } else {
          const decrypted = await request.e2ee.client.decryptInteractionResponse(
            request.e2ee.channel,
            {
              authorityDomain: pending.event.authority_domain!,
              interactionRef,
              responseRef,
              invokerRef: pending.event.invoker_ref!,
              channelRef: pending.event.channel_ref!,
              applicationRef: pending.event.application_ref!,
              sequence: pending.event.sequence!,
              revision,
              callbackType: pending.event.callback_type!,
              operation: pending.event.operation as 'CREATE' | 'UPDATE',
              envelope: encrypted as Record<string, unknown>,
              attachments: rawData.attachments
            }
          );
          const interactive =
            [4, 7].includes(decrypted.context.callback_type) &&
            decrypted.context.interaction_contract_digest !== null;
          if (interactive !== Object.hasOwn(viewProjection, 'view_version')) {
            ready = this.unavailable(pending.event);
          } else {
            ready = { ...pending.event, data: { ...decrypted.data, ...viewProjection } };
          }
        }
      } else if ('e2ee' in rawData) {
        ready = this.unavailable(pending.event);
      } else {
        ready = pending.event;
      }
    } catch {
      ready = this.unavailable(pending.event);
    } finally {
      if (this.decrypting.get(responseRef) === decryptToken) {
        this.decrypting.delete(responseRef);
      }
    }
    if (
      this.generation !== generation ||
      this.revisions.get(responseRef) !== BigInt(revision) ||
      this.tombstones.has(responseRef) ||
      this.contexts[interactionRef] !== request
    ) {
      void this.consumePending(responseRef);
      return;
    }
    this.commit(ready);
    void this.consumePending(responseRef);
  }

  private validEvent(
    name: InteractionResponseEventName,
    event: InteractionResponseEvent
  ): InteractionResponseEvent | null {
    const fields = [
      'application_ref',
      'authority_domain',
      'autocomplete_generation',
      'callback_type',
      'channel_ref',
      'data',
      'deleted_at',
      'ephemeral',
      'expires_at',
      'interaction_id',
      'interaction_ref',
      'invoker_ref',
      'message_ref',
      'operation',
      'response_grant_id',
      'response_id',
      'response_ref',
      'revision',
      'sequence',
      'user_ref'
    ];
    if (Object.keys(event).sort().join(',') !== fields.join(',')) return null;
    const authority = event.authority_domain;
    const interactionRef = this.validRef(event.interaction_ref, authority);
    const responseRef = this.validRef(event.response_ref, authority);
    const channelRef = this.validRef(event.channel_ref, authority);
    const applicationRef = this.validRef(event.application_ref);
    const invokerRef = this.validRef(event.invoker_ref);
    const userRef = this.validRef(event.user_ref);
    const messageRef =
      event.message_ref == null ? null : this.validRef(event.message_ref, authority);
    const autocompleteGeneration = event.autocomplete_generation;
    const expectedOperation = name.slice('INTERACTION_RESPONSE_'.length);
    const create = expectedOperation === 'CREATE';
    const remove = expectedOperation === 'DELETE';
    if (
      !authority ||
      authority !== authority.toLowerCase() ||
      !interactionRef ||
      !responseRef ||
      !channelRef ||
      !applicationRef ||
      !invokerRef ||
      !userRef ||
      invokerRef.ref !== userRef.ref ||
      interactionRef.id !== event.interaction_id ||
      responseRef.id !== event.response_id ||
      event.operation !== expectedOperation ||
      !isCanonicalBase64url32(event.response_grant_id) ||
      typeof event.revision !== 'string' ||
      !/^[1-9]\d{0,18}$/u.test(event.revision) ||
      BigInt(event.revision) > 9_223_372_036_854_775_807n ||
      (create ? event.revision !== '1' : BigInt(event.revision) <= 1n) ||
      !Number.isInteger(event.sequence) ||
      event.sequence! < 0 ||
      event.sequence! > Number.MAX_SAFE_INTEGER ||
      !Number.isInteger(event.callback_type) ||
      ![4, 7, 8, 9].includes(event.callback_type!) ||
      typeof event.ephemeral !== 'boolean' ||
      typeof event.expires_at !== 'string' ||
      !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(event.expires_at) ||
      !Number.isFinite(Date.parse(event.expires_at)) ||
      Date.parse(event.expires_at) <= Date.now() - 30_000 ||
      !event.data ||
      typeof event.data !== 'object' ||
      Array.isArray(event.data) ||
      (remove
        ? typeof event.deleted_at !== 'string' ||
          !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(event.deleted_at) ||
          !Number.isFinite(Date.parse(event.deleted_at)) ||
          Object.keys(event.data).length !== 0
        : event.deleted_at !== null) ||
      (event.message_ref !== null && !messageRef) ||
      (event.ephemeral === true && messageRef !== null) ||
      ([8, 9].includes(event.callback_type!) && messageRef !== null) ||
      (event.callback_type === 8
        ? typeof autocompleteGeneration !== 'string' ||
          !/^[1-9][0-9]{0,18}$/u.test(autocompleteGeneration) ||
          BigInt(autocompleteGeneration) > 9_223_372_036_854_775_807n
        : autocompleteGeneration !== null)
    ) {
      return null;
    }
    return event;
  }

  private validRef(
    value: string | undefined,
    authority?: string
  ): { id: string; ref: string } | null {
    if (!value || (authority !== undefined && !isCanonicalFederationDomain(authority))) return null;
    const parsed = parseCanonicalEntityRef(value, authority);
    if (!parsed) return null;
    const id = parsed.id;
    return { id, ref: value };
  }

  private refreshLatest(interactionRef: string): void {
    const remaining = Object.values(this.byResponse)
      .filter((event) => event.interaction_ref === interactionRef)
      .sort((left, right) => {
        const sequence = (left.sequence ?? 0) - (right.sequence ?? 0);
        if (sequence) return sequence;
        const leftRevision = BigInt(left.revision ?? '0');
        const rightRevision = BigInt(right.revision ?? '0');
        return leftRevision < rightRevision ? -1 : leftRevision > rightRevision ? 1 : 0;
      });
    const next = { ...this.byInteraction };
    const latest = remaining.at(-1);
    if (latest) next[interactionRef] = latest;
    else delete next[interactionRef];
    this.byInteraction = next;
  }

  response(interactionRef: string | null): InteractionResponseEvent | null {
    return interactionRef ? (this.byInteraction[interactionRef] ?? null) : null;
  }

  register(interactionRef: string, context: InteractionRequestContext): void {
    const separator = interactionRef.lastIndexOf('@');
    const authorityDomain = separator > 0 ? interactionRef.slice(separator + 1) : '';
    if (!this.validRef(interactionRef, authorityDomain)) return;
    this.contexts = {
      ...this.contexts,
      [interactionRef]: { ...context, authorityDomain }
    };
    for (const [responseRef, pending] of this.pending) {
      if (pending.event.interaction_ref === interactionRef) void this.consumePending(responseRef);
    }
  }

  wait(interactionRef: string, timeoutMs = 10_000): Promise<InteractionResponseEvent | null> {
    const existing = this.byInteraction[interactionRef];
    if (existing) return Promise.resolve(existing);
    return new Promise((resolve) => {
      const listeners = this.waiters.get(interactionRef) ?? new SvelteSet();
      listeners.add(resolve);
      this.waiters.set(interactionRef, listeners);
      globalThis.setTimeout(() => {
        const current = this.waiters.get(interactionRef);
        if (!current?.delete(resolve)) return;
        if (!current.size) this.waiters.delete(interactionRef);
        resolve(null);
      }, timeoutMs);
    });
  }

  context(interactionRef: string | null): InteractionRequestContext | null {
    return interactionRef ? (this.contexts[interactionRef] ?? null) : null;
  }

  dismissModal(): void {
    this.activeModal = null;
  }

  clear(interactionRef: string, responseRef?: string): void {
    const next = { ...this.byResponse };
    for (const [key, current] of Object.entries(next)) {
      if (
        current.interaction_ref === interactionRef &&
        (!responseRef || current.response_ref === responseRef)
      )
        delete next[key];
    }
    this.byResponse = next;
    for (const [key, pending] of this.pending) {
      if (
        pending.event.interaction_ref === interactionRef &&
        (!responseRef || pending.event.response_ref === responseRef)
      ) {
        this.pending.delete(key);
      }
    }
    this.refreshLatest(interactionRef);
    if (!Object.values(next).some((event) => event.interaction_ref === interactionRef)) {
      const nextContexts = { ...this.contexts };
      delete nextContexts[interactionRef];
      this.contexts = nextContexts;
    }
  }

  reset(): void {
    this.generation += 1;
    for (const listeners of this.waiters.values()) {
      for (const listener of listeners) listener(null);
    }
    this.waiters.clear();
    this.revisions.clear();
    this.tombstones.clear();
    this.responseIdentity.clear();
    this.pending.clear();
    this.decrypting.clear();
    this.byInteraction = {};
    this.byResponse = {};
    this.contexts = {};
    this.activeModal = null;
  }
}

export const interactionResponses = new InteractionResponseState();
