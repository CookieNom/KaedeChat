import { parseCanonicalEntityRef, entityRef } from './refs';
import type { Message, UserSummary } from './types';
import { tombstoneMessage } from './message-deletions';
import { revokeMedia, releaseMedia, resetMediaRestrictions } from '$lib/media/content-revocation';
import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';
import { SvelteMap, SvelteSet } from 'svelte/reactivity';

export interface ContentSafetyUpdate {
  sequence?: string;
  message_refs: string[];
  attachment_refs: string[];
  state: 'pending_review' | 'removed' | 'hidden' | 'released' | 'unavailable';
  reason: string;
}

type Restriction = Pick<ContentSafetyUpdate, 'state' | 'reason'>;
let messages = $state<Record<string, Restriction>>({});
let attachments = $state<Record<string, Restriction>>({});
let account = '';
let cursor = '0';
let sync: Promise<void> | null = null;
let resyncRequested = false;
const freshMessages = new SvelteMap<string, string>();
const freshAttachments = new SvelteSet<string>();
const unavailableMessages = new SvelteSet<string>();
const releasedMessages = new SvelteSet<string>();
export const contentSafety = $state({ ready: false, error: '', revision: 0 });

function storageKey(): string {
  return `kaede.content-safety:${account}`;
}

function validUpdate(value: unknown): value is ContentSafetyUpdate {
  if (!value || typeof value !== 'object') return false;
  const update = value as ContentSafetyUpdate;
  return (
    ['pending_review', 'removed', 'hidden', 'released', 'unavailable'].includes(update.state) &&
    typeof update.reason === 'string' &&
    Array.isArray(update.message_refs) &&
    update.message_refs.every((ref) => parseCanonicalEntityRef(ref)) &&
    Array.isArray(update.attachment_refs) &&
    update.attachment_refs.every((ref) => parseCanonicalEntityRef(ref))
  );
}

export function safetyMessageRestriction(ref: string): Restriction | undefined {
  return messages[ref];
}

export function safetyAttachmentRestricted(ref: string): boolean {
  return Boolean(attachments[ref]);
}

type AuthorizationMessage = Pick<Message, 'id' | 'origin_domain'> & Partial<Message>;

function authorizationKey(message: AuthorizationMessage): string {
  return JSON.stringify([
    entityRef(message),
    message.channel_id,
    message.channel_domain,
    message.author_id,
    message.author_domain,
    message.created_at,
    message.edited_at ?? null,
    message.content ?? null,
    message.e2ee ?? null,
    message.attachments ?? [],
    message.message_type,
    message.flags,
    message.tts,
    message.embeds,
    message.components,
    message.sticker_items,
    message.poll,
    message.message_reference,
    message.referenced_message_id,
    message.referenced_message_domain,
    message.referenced_message,
    message.forwarded_message_ref,
    message.forwarded_message,
    message.message_snapshots,
    message.application_id,
    message.application_domain,
    message.interaction_metadata,
    message.mention_user_refs,
    message.mention_role_refs,
    message.mention_everyone
  ]);
}

export function messageAvailabilityRestricted(message: AuthorizationMessage): boolean {
  const ref = entityRef(message);
  return unavailableMessages.has(ref) && freshMessages.get(ref) !== authorizationKey(message);
}

export function permitsSafetyRestoration(message: Message): boolean {
  const ref = entityRef(message);
  return (
    !messages[ref] &&
    !message.safety_state &&
    !message.safety_locked &&
    (releasedMessages.has(ref) || freshMessages.get(ref) === authorizationKey(message))
  );
}

/** Only a complete authenticated network response can restore transient access. */
export function acceptAuthorizedMessage(message: Message, requestRevision: number): boolean {
  if (
    requestRevision !== contentSafety.revision ||
    message.deleted_at ||
    message.safety_state ||
    message.safety_locked ||
    typeof message.created_at !== 'string' ||
    !parseCanonicalEntityRef(`${message.channel_id}@${message.channel_domain}`) ||
    !parseCanonicalEntityRef(`${message.author_id}@${message.author_domain}`) ||
    !Array.isArray(message.attachments) ||
    !Object.hasOwn(message, 'content') ||
    !Object.hasOwn(message, 'e2ee')
  )
    return false;
  const ref = entityRef(message);
  const refs = message.attachments.map(entityRef);
  if (
    (messages[ref] && messages[ref].state !== 'unavailable') ||
    refs.some((item) => attachments[item] && attachments[item].state !== 'unavailable')
  )
    return false;
  freshMessages.set(ref, authorizationKey(message));
  refs.forEach((item) => freshAttachments.add(item));
  if (messages[ref]?.state === 'unavailable') {
    const next = { ...messages };
    delete next[ref];
    messages = next;
  }
  const next = { ...attachments };
  const released = refs.filter((item) => next[item]?.state === 'unavailable');
  released.forEach((item) => delete next[item]);
  attachments = next;
  releaseMedia(released);
  if (isNativeDesktop() && released.length) {
    void nativeInvoke('native_revoke_media', {
      attachmentRefs: [],
      releasedAttachmentRefs: released
    }).catch(() => {
      contentSafety.ready = false;
    });
  }
  persist();
  return unavailableMessages.has(ref);
}

export function cachedContentReferences(
  cache: Record<string, { plaintext: string; messageRef: string | null }>
): {
  messageRefs: string[];
  attachmentRefs: string[];
} {
  const messageRefs = new SvelteSet<string>();
  const attachmentRefs = new SvelteSet<string>();
  const visit = (value: unknown): void => {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    const record = value as Record<string, unknown>;
    if (typeof record.attachment_id === 'string' && typeof record.attachment_domain === 'string') {
      const ref = `${record.attachment_id}@${record.attachment_domain}`;
      if (parseCanonicalEntityRef(ref)) attachmentRefs.add(ref);
    }
    for (const [key, item] of Object.entries(record)) {
      if (
        ['message_ref', 'source_message_ref', 'target_message'].includes(key) &&
        parseCanonicalEntityRef(item)
      ) {
        messageRefs.add(item as string);
      }
      visit(item);
    }
  };
  for (const entry of Object.values(cache)) {
    if (entry.messageRef) messageRefs.add(entry.messageRef);
    try {
      visit(JSON.parse(entry.plaintext));
    } catch {
      /* Existing cache validation rejects malformed plaintext. */
    }
  }
  return { messageRefs: [...messageRefs], attachmentRefs: [...attachmentRefs] };
}

export function cachedContentRestricted(entry: {
  plaintext: string;
  messageRef: string | null;
}): boolean {
  const refs = cachedContentReferences({ entry });
  return (
    refs.messageRefs.some((ref) => messages[ref]) ||
    refs.attachmentRefs.some((ref) => attachments[ref])
  );
}

/** Reversible restrictions must retain sealed MLS history for a later dismissal/access restore. */
export function cachedContentMustPurge(entry: {
  plaintext: string;
  messageRef: string | null;
}): boolean {
  const refs = cachedContentReferences({ entry });
  const terminal = (restriction?: Restriction) =>
    restriction?.state === 'removed' || restriction?.state === 'hidden';
  return (
    refs.messageRefs.some((ref) => terminal(messages[ref])) ||
    refs.attachmentRefs.some((ref) => terminal(attachments[ref]))
  );
}

export function applyContentSafetyUpdate(value: unknown): void {
  if (!validUpdate(value)) throw new Error('The content safety update is invalid.');
  const update =
    value.state === 'unavailable'
      ? {
          ...value,
          message_refs: value.message_refs.filter((ref) => !freshMessages.has(ref)),
          attachment_refs: value.attachment_refs.filter((ref) => !freshAttachments.has(ref))
        }
      : value;
  if (!update.message_refs.length && !update.attachment_refs.length) return;
  for (const ref of update.message_refs) {
    if (update.state === 'released' && !['removed', 'hidden'].includes(messages[ref]?.state))
      releasedMessages.add(ref);
    else releasedMessages.delete(ref);
  }
  if (update.state === 'unavailable')
    update.message_refs.forEach((ref) => unavailableMessages.add(ref));
  else {
    update.message_refs.forEach((ref) => freshMessages.delete(ref));
    update.attachment_refs.forEach((ref) => freshAttachments.delete(ref));
  }
  const apply = (current: Record<string, Restriction>, refs: string[]) => {
    const next = { ...current };
    for (const ref of refs) {
      if (update.state === 'released') {
        if (next[ref]?.state === 'pending_review') delete next[ref];
      } else if (update.state === 'unavailable' && next[ref]) {
        continue;
      } else if (next[ref]?.state !== 'removed' && next[ref]?.state !== 'hidden') {
        next[ref] = { state: update.state, reason: update.reason };
      } else if (update.state === 'removed') {
        next[ref] = { state: 'removed', reason: update.reason };
      }
    }
    return next;
  };
  messages = apply(messages, update.message_refs);
  attachments = apply(attachments, update.attachment_refs);
  if (update.state !== 'released') revokeMedia(update.attachment_refs);
  else releaseMedia(update.attachment_refs.filter((ref) => !attachments[ref]));
  if (isNativeDesktop()) {
    void nativeInvoke('native_revoke_media', {
      attachmentRefs: update.attachment_refs.filter((ref) => attachments[ref]),
      releasedAttachmentRefs: update.attachment_refs.filter((ref) => !attachments[ref])
    }).catch(() => {
      contentSafety.ready = false;
      contentSafety.error = 'Could not remove unavailable media from this device. Try again.';
    });
  }
  contentSafety.revision += 1;
  persist();
}

function persist(): void {
  if (!account) return;
  // A cursor is saved only after cache cleanup succeeds. Replaying is idempotent.
  globalThis.localStorage?.setItem(storageKey(), JSON.stringify({ messages, attachments, cursor }));
}

export function protectMessage(message: Message): Message {
  const attachmentRestriction = (message.attachments ?? [])
    .map((attachment) => attachments[entityRef(attachment)])
    .find(Boolean);
  const ref = entityRef(message);
  const stale = messageAvailabilityRestricted(message);
  const restriction =
    messages[ref] ??
    attachmentRestriction ??
    (stale
      ? {
          state: 'unavailable' as const,
          reason: 'This message is currently unavailable.'
        }
      : undefined);
  const state = restriction?.state ?? message.safety_state;
  if (!state || state === 'released') return message;
  return {
    ...tombstoneMessage(message),
    deleted_at: message.deleted_at,
    safety_state: state === 'hidden' || state === 'unavailable' ? null : state,
    safety_hidden: state === 'hidden',
    safety_unavailable: state === 'unavailable',
    safety_locked: state === 'pending_review' || message.safety_locked === true,
    safety_reason: restriction?.reason ?? message.safety_reason,
    referenced_message: null,
    content: null
  };
}

export function hideReportedMessage(message: Message): void {
  applyContentSafetyUpdate({
    message_refs: [entityRef(message)],
    attachment_refs: [
      ...(message.attachments ?? []).map(entityRef),
      ...(message.decrypted_attachments ?? []).flatMap((manifest) =>
        [manifest, manifest.preview].flatMap((item) =>
          item?.attachment_id && item.attachment_domain
            ? [`${item.attachment_id}@${item.attachment_domain}`]
            : []
        )
      )
    ],
    state: 'hidden',
    reason: 'Hidden after your child safety report.'
  });
}

export async function reconcileSafetyReferences(
  messageRefs: string[],
  attachmentRefs: string[]
): Promise<void> {
  const { api, ApiError } = await import('$lib/api/client');
  const count = Math.max(messageRefs.length, attachmentRefs.length);
  for (let offset = 0; offset < count; offset += 200) {
    try {
      const result = await api<{ items: ContentSafetyUpdate[] }>('/safety/content-status', {
        method: 'POST',
        body: JSON.stringify({
          message_refs: messageRefs.slice(offset, offset + 200),
          attachment_refs: attachmentRefs.slice(offset, offset + 200)
        })
      });
      if (!Array.isArray(result.items)) throw new Error('The content safety response is invalid.');
      for (const update of result.items) applyContentSafetyUpdate(update);
    } catch (error) {
      if (error instanceof ApiError && [404, 405].includes(error.status)) return;
      throw error;
    }
  }
}

export function syncContentSafety(user: UserSummary): Promise<void> {
  if (sync) {
    resyncRequested = true;
    return sync;
  }
  if (!contentSafety.ready || account !== entityRef(user)) {
    freshMessages.clear();
    freshAttachments.clear();
    unavailableMessages.clear();
    releasedMessages.clear();
    contentSafety.revision += 1;
  }
  contentSafety.ready = false;
  contentSafety.error = '';
  sync = (async () => {
    const nextAccount = entityRef(user);
    if (account !== nextAccount) {
      account = nextAccount;
      resetMediaRestrictions();
      messages = {};
      attachments = {};
      cursor = '0';
      const raw = globalThis.localStorage?.getItem(storageKey());
      if (raw) {
        const stored = JSON.parse(raw);
        for (const [ref, item] of Object.entries(stored.messages ?? {})) {
          applyContentSafetyUpdate({
            ...(item as Restriction),
            message_refs: [ref],
            attachment_refs: []
          });
        }
        for (const [ref, item] of Object.entries(stored.attachments ?? {})) {
          applyContentSafetyUpdate({
            ...(item as Restriction),
            message_refs: [],
            attachment_refs: [ref]
          });
        }
        if (!/^\d+$/.test(stored.cursor))
          throw new Error('The saved content safety cursor is invalid.');
        cursor = stored.cursor;
      }
    }
    const { api, ApiError } = await import('$lib/api/client');
    let next = cursor;
    try {
      for (;;) {
        const page = await api<{
          items: ContentSafetyUpdate[];
          next_cursor: string;
          has_more: boolean;
        }>(`/safety/client-state?after=${encodeURIComponent(next)}&limit=200`);
        if (
          !Array.isArray(page.items) ||
          !/^\d+$/.test(page.next_cursor) ||
          BigInt(page.next_cursor) < BigInt(next) ||
          (page.has_more && page.next_cursor === next)
        ) {
          throw new Error('The content safety response is invalid.');
        }
        for (const item of page.items) applyContentSafetyUpdate(item);
        next = page.next_cursor;
        if (!page.has_more) break;
      }
    } catch (error) {
      if (!(error instanceof ApiError && [404, 405].includes(error.status))) throw error;
    }
    const { chatEntities } = await import('$lib/stores/entities.svelte');
    await reconcileSafetyReferences(
      chatEntities.messages.values.map(entityRef),
      chatEntities.messages.values.flatMap((message) => (message.attachments ?? []).map(entityRef))
    );
    const { purgeSafetyE2EEState } = await import('$lib/e2ee/client');
    do {
      resyncRequested = false;
      await purgeSafetyE2EEState(user);
      if (isNativeDesktop()) {
        await nativeInvoke('native_revoke_media', {
          attachmentRefs: Object.keys(attachments),
          releasedAttachmentRefs: []
        });
      }
    } while (resyncRequested);
    chatEntities.messages.replace(chatEntities.messages.values.map(protectMessage));
    cursor = next;
    persist();
    contentSafety.ready = true;
  })()
    .catch((error) => {
      contentSafety.error =
        'Could not check removed content. Reconnect and try again to open your messages.';
      throw error;
    })
    .finally(() => {
      sync = null;
    });
  return sync;
}
