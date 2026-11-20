// @vitest-environment happy-dom
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import Route from '../../routes/(app)/home/[dmId]/+page.svelte';
import { acknowledgeEncryptedRoom } from '$lib/e2ee/disclosures';
import { chatEntities } from '$lib/stores/entities.svelte';
import { draftKey, writeDraft } from './local-drafts';
import type { Message } from './types';

const { api, initializeE2EE } = vi.hoisted(() => ({
  api: vi.fn(),
  initializeE2EE: vi.fn()
}));
vi.mock('$app/state', () => ({
  page: { params: { dmId: '2@chat.example' }, url: new URL('https://chat.example/home/2') }
}));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api
}));
vi.mock('$lib/auth/config', () => ({
  loadAuthConfiguration: async () => ({ gif_picker_enabled: false, e2ee_activation_enabled: true })
}));
vi.mock('$lib/e2ee/client', async (original) => ({
  ...(await original<typeof import('$lib/e2ee/client')>()),
  initializeE2EE
}));

const viewer = {
  id: '7',
  origin_domain: 'chat.example',
  username: 'Viewer',
  handle: 'Viewer@chat.example',
  display_name: null,
  avatar_hash: null
};
const peer = { ...viewer, id: '8', username: 'Peer', handle: 'Peer@chat.example' };
const room = '/channels/2%40chat.example';
const key = draftKey('7@chat.example', '2@chat.example');
let component: ReturnType<typeof mount> | undefined;

afterEach(async () => {
  if (component) await unmount(component);
  chatEntities.clearSession();
  writeDraft(key, '');
  document.body.replaceChildren();
  localStorage.clear();
});

it('keeps authorized pre-encryption history readable and gates sending through initialization failure and retry', async () => {
  const earlier: Message = {
    id: '10',
    origin_domain: 'chat.example',
    channel_id: '2',
    channel_domain: 'chat.example',
    author: peer,
    author_id: peer.id,
    author_domain: peer.origin_domain,
    content: 'Before encryption was enabled',
    encryption_policy_generation: '0',
    message_type: 0,
    flags: 0,
    client_nonce: null,
    referenced_message_id: null,
    referenced_message_domain: null,
    mention_user_refs: [],
    created_at: '2026-01-01T00:00:00Z',
    edited_at: null,
    deleted_at: null
  };
  const encrypted: Message = {
    ...earlier,
    id: '11',
    encryption_policy_generation: '1',
    e2ee: { operation: 'application' },
    content: 'Untrusted outer body'
  };
  api.mockImplementation(async (path: string) => {
    if (path === '/users/@me') return viewer;
    if (path === '/users/@me/channels')
      return [
        {
          id: '2',
          origin_domain: 'chat.example',
          guild_id: null,
          type: 1,
          recipients: [peer],
          encryption_mode: 'e2ee',
          encryption_state: 'active',
          encryption_policy_generation: '1'
        }
      ];
    if (path === room + '/messages') return [encrypted, earlier];
    if (path === room + '/calls/active') return { call: null, joined: false };
    if (
      [
        '/users/@me/guilds',
        '/users/@me/read-states',
        '/users/@me/emojis',
        '/users/@me/stickers',
        room + '/application-commands',
        room + '/pins'
      ].includes(path)
    )
      return [];
    throw new Error(`Unexpected route request: ${path}`);
  });
  let failInitialization!: (error: Error) => void;
  const pending = new Promise<never>((_, reject) => {
    failInitialization = reject;
  });
  let finishDecryption!: () => void;
  const decrypting = new Promise<void>((resolve) => {
    finishDecryption = resolve;
  });
  const decryptMessages = vi.fn(async (_channel: unknown, messages: Message[]) => {
    await decrypting;
    return messages.map((message) =>
      message.e2ee ? { content: 'Verified encrypted reply', attachments: [] } : null
    );
  });
  initializeE2EE.mockReturnValueOnce(pending).mockResolvedValueOnce({ decryptMessages });
  acknowledgeEncryptedRoom('7@chat.example', '2@chat.example');
  chatEntities.ingestCurrentUser(viewer);
  writeDraft(key, 'Ready to send');
  component = mount(Route, { target: document.body });
  flushSync();

  const timeline = document.querySelector<HTMLElement>('[role="log"]')!;
  const composer = document.querySelector<HTMLTextAreaElement>('textarea')!;
  const send = document.querySelector<HTMLButtonElement>('.send-button')!;
  function expectReadableHistoryWhileLocked() {
    expect(timeline.textContent).toContain(earlier.content);
    expect(timeline.textContent).not.toContain(encrypted.content);
    expect(timeline.textContent).not.toContain('Verified encrypted reply');
    expect(composer.value).toBe('Ready to send');
    expect(composer.disabled).toBe(true);
    expect(send.disabled).toBe(true);
  }
  await vi.waitFor(() => expect(initializeE2EE).toHaveBeenCalledTimes(1));
  expectReadableHistoryWhileLocked();

  failInitialization(new Error('Could not open device storage.'));
  await vi.waitFor(() => expect(timeline.querySelector('[role="alert"] button')).not.toBeNull());
  expectReadableHistoryWhileLocked();
  timeline.querySelector<HTMLButtonElement>('[role="alert"] button')!.click();
  await vi.waitFor(() => expect(decryptMessages).toHaveBeenCalledOnce());
  expectReadableHistoryWhileLocked();

  finishDecryption();
  await vi.waitFor(() => expect(timeline.textContent).toContain('Verified encrypted reply'));
  expect(timeline.textContent).toContain(earlier.content);
  expect(timeline.textContent).not.toContain(encrypted.content);
  expect(timeline.querySelector('[role="alert"]')).toBeNull();
  expect(composer.disabled).toBe(false);
  expect(send.disabled).toBe(false);

  document.querySelector<HTMLButtonElement>('[aria-label="Search on this device"]')!.click();
  const search = document.querySelector<HTMLInputElement>('[aria-label="Search loaded messages"]')!;
  for (const [query, matches] of [
    ['verified encrypted', 1],
    ['untrusted outer', 0],
    ['before encryption', 1]
  ] as const) {
    search.value = query;
    search.dispatchEvent(new Event('input'));
    flushSync();
    expect(document.querySelectorAll('dialog .result')).toHaveLength(matches);
  }
  expect(api.mock.calls.some(([path]) => path === '/search/messages')).toBe(false);
});

it('follows an archived link through two continuations to the listed conversation', async () => {
  const next = {
    id: '4',
    origin_domain: 'chat.example',
    type: 1,
    guild_id: null,
    recipients: [peer]
  };
  const redirect = vi.spyOn(window.location, 'replace').mockImplementation(() => {});
  api.mockImplementation(async (path: string) => {
    if (path === '/users/@me') return viewer;
    if (path === '/users/@me/channels') return [next];
    if (path.includes('plaintext-continuation/consent')) {
      return { channel_ref: path.includes('/2%40') ? '3@chat.example' : '4@chat.example' };
    }
    if (path.endsWith('/calls/active')) return { call: null, joined: false };
    return [];
  });
  try {
    component = mount(Route, { target: document.body });
    await vi.waitFor(() => expect(redirect).toHaveBeenCalledOnce());
    expect(decodeURIComponent(String(redirect.mock.calls[0][0]))).toContain('/home/4@chat.example');
  } finally {
    redirect.mockRestore();
  }
});
