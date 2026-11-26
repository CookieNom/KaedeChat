import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from '$lib/api/client';
import type { Message, Relationship, UserSummary } from '$lib/chat/types';
import { reconcileMessage } from '$lib/chat/reconcile';
import { contentSafety } from '$lib/chat/content-safety.svelte';
import { purgeSafetyE2EEState } from '$lib/e2ee/client';
import { chatEntities } from '$lib/stores/entities.svelte';

vi.mock('$lib/api/client', async () => ({ api: vi.fn(), ...(await import('$lib/api/errors')) }));
vi.mock('$lib/e2ee/client', () => ({ purgeSafetyE2EEState: vi.fn() }));

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

function dispatch(incoming: Message, type = 'MESSAGE_CREATE') {
  authenticatedGateway.client.dispatchEvent(
    new CustomEvent('dispatch', {
      detail: { t: type, d: incoming, s: 1 }
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

it('keeps encryption and deletion authoritative across gateway updates and duplicate creates', () => {
  const encrypted = { ...message, content: null, e2ee: { ciphertext: 'sealed' } };
  chatEntities.messages.upsert({
    ...encrypted,
    e2ee_verified: true,
    decrypted_content: 'verified'
  });
  dispatch({ ...message, e2ee: null }, 'MESSAGE_UPDATE');
  expect(chatEntities.messages.get('12@chat.example')).toMatchObject({
    e2ee: encrypted.e2ee,
    e2ee_verified: false,
    decrypted_content: null
  });
  const deleted = { ...message, content: null, deleted_at: '2026-07-20T00:00:01Z' };
  chatEntities.messages.upsert(deleted);
  dispatch(message, 'MESSAGE_UPDATE');
  dispatch(message);
  expect(chatEntities.messages.get('12@chat.example')).toMatchObject(deleted);
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

const resumedUser = { id: '1', origin_domain: 'chat.example' } as UserSummary;
const blocked: Relationship = {
  type: 'blocked',
  user: { id: '2', origin_domain: 'chat.example' } as UserSummary,
  created_at: '',
  updated_at: ''
};
const safetyPage = { items: [], next_cursor: '0', has_more: false };

function gatewayEvent(t: string, d: unknown = {}) {
  authenticatedGateway.client.dispatchEvent(
    new CustomEvent('dispatch', { detail: { t, d, s: 1 } })
  );
}

it('restores a refreshed tab on RESUMED before opening checked messages', async () => {
  let finish!: (items: Relationship[]) => void;
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === '/users/@me') return resumedUser;
    if (path === '/users/@me/relationships')
      return new Promise<Relationship[]>((resolve) => (finish = resolve));
    return safetyPage;
  });
  gatewayEvent('RESUMED');
  const pending = authenticatedGateway.retryContentSafety();
  expect(authenticatedGateway.retryContentSafety()).toBe(pending);
  await vi.waitFor(() => expect(finish).toBeDefined());
  expect(contentSafety.ready).toBe(false);
  expect(chatEntities.currentUser).toBeNull();
  gatewayEvent('USER_UPDATE', { relationship: blocked });
  finish([]);
  await pending;
  expect(chatEntities.currentUser).toEqual(resumedUser);
  expect(chatEntities.isBlocked(blocked.user)).toBe(true);
  expect(contentSafety.ready).toBe(true);
  expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/users/@me')).toHaveLength(1);
});

it.each(['READY', 'RESUMED'])(
  'keeps messages closed if relationships fail on %s',
  async (event) => {
    let fail = true;
    vi.mocked(api).mockImplementation(async (path) => {
      if (path === '/users/@me') return resumedUser;
      if (path === '/users/@me/relationships') {
        if (fail) throw new Error('network failure');
        return [blocked];
      }
      return safetyPage;
    });
    gatewayEvent(event, { user: resumedUser, guilds: [], dm_channels: [], read_states: [] });
    await authenticatedGateway.retryContentSafety();
    expect(chatEntities.currentUser).toEqual(event === 'READY' ? resumedUser : null);
    expect(contentSafety.ready).toBe(false);
    expect(contentSafety.error).toBeTruthy();
    fail = false;
    await authenticatedGateway.retryContentSafety();
    expect(chatEntities.relationships.values).toEqual([blocked]);
    expect(contentSafety.ready).toBe(true);
  }
);

it.each(['READY', 'RESUMED'])(
  'uses the gateway relationship snapshot when HTTP is unavailable on %s',
  async (event) => {
    vi.mocked(api).mockImplementation(async (path) => {
      if (path === '/users/@me/relationships') throw new Error('offline');
      if (path === '/users/@me') return resumedUser;
      return safetyPage;
    });
    gatewayEvent(event, {
      user: resumedUser,
      guilds: [],
      read_states: [],
      dm_channels: [],
      relationships: [blocked]
    });
    await vi.waitFor(() => expect(contentSafety.ready).toBe(true));
    expect(chatEntities.isBlocked(blocked.user)).toBe(true);
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/users/@me/relationships')).toBe(
      false
    );
  }
);

it('purges again when a restriction arrives during the bootstrap purge', async () => {
  const purge = vi.mocked(purgeSafetyE2EEState);
  purge.mockClear();
  const cleanup = Promise.withResolvers<void>();
  purge.mockReturnValueOnce(cleanup.promise);
  vi.mocked(api).mockImplementation(async (path) =>
    path === '/users/@me' ? resumedUser : path === '/users/@me/relationships' ? [] : safetyPage
  );
  gatewayEvent('RESUMED');
  await vi.waitFor(() => expect(purge).toHaveBeenCalledOnce());
  gatewayEvent('CONTENT_SAFETY_UPDATE', {
    state: 'hidden',
    reason: 'Unavailable',
    message_refs: ['12@chat.example'],
    attachment_refs: []
  });
  cleanup.resolve();
  await vi.waitFor(() => expect(contentSafety.ready).toBe(true));
  expect(purge).toHaveBeenCalledTimes(2);
});

it.each(['stop', 'READY'])('discards a late cold-resume snapshot after %s', async (action) => {
  let finish!: (items: Relationship[]) => void;
  vi.mocked(api)
    .mockResolvedValueOnce(resumedUser)
    .mockImplementationOnce(() => new Promise<Relationship[]>((resolve) => (finish = resolve)))
    .mockImplementation(async (path) => (path === '/users/@me/relationships' ? [] : safetyPage));
  gatewayEvent('RESUMED');
  const pending = authenticatedGateway.retryContentSafety();
  await vi.waitFor(() => expect(finish).toBeDefined());
  const newUser = { ...resumedUser, id: '100' };
  if (action === 'stop') authenticatedGateway.stop();
  else gatewayEvent('READY', { user: newUser, guilds: [], dm_channels: [], read_states: [] });
  finish([blocked]);
  await pending;
  expect(chatEntities.currentUser).toEqual(action === 'stop' ? null : newUser);
  expect(chatEntities.relationships.values).toEqual([]);
});

it('merges a read cursor from a sibling tab only for the active account and version', () => {
  authenticatedGateway.stop();
  const bus = Object.assign(new EventTarget(), { close: vi.fn(), postMessage: vi.fn() });
  vi.stubGlobal(
    'BroadcastChannel',
    vi.fn().mockImplementation(function () {
      return bus;
    })
  );
  authenticatedGateway.start();
  const domain = resumedUser.origin_domain;
  const state = {
    channel_id: '7',
    channel_domain: domain,
    guild_id: null,
    guild_domain: null,
    last_message_id: '20',
    last_message_domain: domain,
    read_message_id: '2',
    read_message_domain: domain,
    read_version: 3,
    mention_count: 0,
    unread: true
  };
  gatewayEvent('READY', {
    user: resumedUser,
    guilds: [],
    dm_channels: [],
    read_states: [state],
    relationships: []
  });
  const account = resumedUser.id + '@' + domain;
  const receive = (owner: string, version: number) =>
    bus.dispatchEvent(
      new MessageEvent('message', {
        data: {
          account: owner,
          update: {
            channel_id: '7',
            channel_domain: domain,
            last_message_id: '10',
            last_message_domain: domain,
            mention_count: 0,
            read_version: version
          }
        }
      })
    );
  receive('other@' + domain, 4);
  receive(account, 2);
  expect(chatEntities.readStates.values[0]).toEqual(state);
  receive(account, 3);
  expect(chatEntities.readStates.values[0]).toMatchObject({
    last_message_id: '20',
    read_message_id: '10',
    unread: true
  });
});
