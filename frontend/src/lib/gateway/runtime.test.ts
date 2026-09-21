import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from '$lib/api/client';
import type { Message, Relationship, UserSummary } from '$lib/chat/types';
import { reconcileMessage } from '$lib/chat/reconcile';
import { chatEntities } from '$lib/stores/entities.svelte';

vi.mock('$lib/api/client', () => ({ api: vi.fn() }));

vi.mock('$lib/notifications/browser.svelte', () => ({
  browserNotifications: { notifyMessage: vi.fn() }
}));
vi.mock('./client', () => ({
  GATEWAY_STATUS_EVENT: 'status',
  GatewayClient: class extends EventTarget {
    connect() {}
    close() {}
    rememberPresence() {}
  }
}));
import { authenticatedGateway } from './runtime.svelte';

const message: Message = {
  id: '12',
  origin_domain: 'chat.example',
  channel_id: '1',
  channel_domain: 'chat.example',
  author_id: '2',
  author_domain: 'chat.example',
  author: null,
  content: 'Hello',
  message_type: 0,
  flags: 0,
  client_nonce: 'sent',
  referenced_message_id: null,
  referenced_message_domain: null,
  mention_user_refs: [],
  edited_at: null,
  deleted_at: null,
  created_at: '2026-07-20T00:00:00Z'
};

function dispatch(incoming: Message) {
  authenticatedGateway.client.dispatchEvent(
    new CustomEvent('dispatch', {
      detail: { t: 'MESSAGE_CREATE', d: incoming, s: 1 }
    })
  );
}

beforeEach(() => {
  vi.mocked(api).mockReset();
  vi.mocked(api).mockResolvedValue([]);
  vi.stubGlobal('BroadcastChannel', undefined);
  authenticatedGateway.start();
});
afterEach(() => {
  authenticatedGateway.stop();
  vi.unstubAllGlobals();
});

it('keeps the local draft visible until an encrypted gateway echo is verified', async () => {
  const optimistic = {
    ...message,
    id: 'pending-sent',
    origin_domain: '',
    pending: true,
    e2ee_verified: true,
    decrypted_content: 'Hello'
  };
  const echo = { ...message, content: null, e2ee: { ciphertext: 'sealed' } };
  chatEntities.messages.upsert(optimistic);
  let finishVerification!: (message: Message) => void;
  const verification = new Promise<Message>((resolve) => {
    finishVerification = resolve;
  });
  const publishVerified = verification.then((verified) => {
    chatEntities.messages.replace(reconcileMessage(chatEntities.messages.values, verified));
  });

  dispatch(echo);
  await Promise.resolve();
  expect(chatEntities.messages.values).toEqual([optimistic]);
  expect(chatEntities.messages.get('12@chat.example')).toBeUndefined();

  const verified = { ...echo, e2ee_verified: true, decrypted_content: 'Hello' };
  finishVerification(verified);
  await publishVerified;
  expect(chatEntities.messages.values).toEqual([verified]);
  dispatch(echo);
  expect(chatEntities.messages.values).toEqual([verified]);
});

it('defers incoming ciphertext to channel decryption but still stores plaintext immediately', () => {
  const encrypted = { ...message, content: null, e2ee: { ciphertext: 'sealed' } };
  dispatch(encrypted);
  expect(chatEntities.messages.values).toEqual([]);
  // The channel can still publish a real decryption failure after trying.
  const failed = { ...encrypted, e2ee_verified: false };
  chatEntities.messages.replace(reconcileMessage(chatEntities.messages.values, failed));
  expect(chatEntities.messages.values).toEqual([failed]);
  const plaintext = { ...message, id: '13', client_nonce: null };
  dispatch(plaintext);
  expect(chatEntities.messages.get('13@chat.example')).toEqual(plaintext);
});

it('keeps friend request badges current across snapshot races and clears resolved requests', async () => {
  const user = { id: '2', origin_domain: 'chat.example', username: 'Maple' } as UserSummary;
  const relationship: Relationship = {
    type: 'pending_in',
    user,
    created_at: '',
    updated_at: ''
  };
  let finish!: (relationships: Relationship[]) => void;
  vi.mocked(api).mockReturnValueOnce(
    new Promise<Relationship[]>((resolve) => {
      finish = resolve;
    })
  );
  const emit = (t: string, d: unknown) =>
    authenticatedGateway.client.dispatchEvent(
      new CustomEvent('dispatch', { detail: { t, d, s: 1 } })
    );
  emit('READY', { user, guilds: [], dm_channels: [], read_states: [] });
  emit('USER_UPDATE', { relationship });
  emit('USER_UPDATE', { relationship });
  expect(chatEntities.relationships.values).toHaveLength(1);
  emit('USER_UPDATE', { relationship: { ...relationship, type: 'none' } });
  finish([relationship]);
  await vi.waitFor(() => expect(chatEntities.relationships.values).toEqual([]));
  await Promise.resolve();
  expect(chatEntities.relationships.values).toEqual([]);
  emit('USER_UPDATE', { relationship });
  emit('USER_UPDATE', { relationship: { ...relationship, type: 'friend' } });
  expect(chatEntities.relationships.values.filter((item) => item.type === 'pending_in')).toEqual(
    []
  );
});

it('loads pending requests on sign-in and ignores a snapshot after sign-out', async () => {
  const user = { id: '2', origin_domain: 'chat.example', username: 'Maple' } as UserSummary;
  const relationship: Relationship = { type: 'pending_in', user, created_at: '', updated_at: '' };
  vi.mocked(api).mockResolvedValueOnce([relationship]);
  const ready = () =>
    authenticatedGateway.client.dispatchEvent(
      new CustomEvent('dispatch', {
        detail: { t: 'READY', d: { user, guilds: [], dm_channels: [], read_states: [] }, s: 1 }
      })
    );
  ready();
  await vi.waitFor(() => expect(chatEntities.relationships.values).toEqual([relationship]));
  let finish!: (relationships: Relationship[]) => void;
  vi.mocked(api).mockReturnValueOnce(
    new Promise<Relationship[]>((resolve) => {
      finish = resolve;
    })
  );
  ready();
  authenticatedGateway.stop();
  finish([relationship]);
  await Promise.resolve();
  expect(chatEntities.relationships.values).toEqual([]);
});
