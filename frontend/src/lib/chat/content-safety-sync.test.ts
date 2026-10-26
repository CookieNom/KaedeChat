import { afterEach, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api/errors';
import type { Message, UserSummary } from './types';
const network = vi.hoisted(() => ({ api: vi.fn(), purge: vi.fn().mockResolvedValue(undefined) }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
vi.mock('$lib/e2ee/client', () => ({ purgeSafetyE2EEState: network.purge }));
vi.mock('$lib/stores/entities.svelte', () => ({
  chatEntities: { messages: { values: [], replace: vi.fn() } }
}));
import {
  applyContentSafetyUpdate,
  contentSafety,
  syncContentSafety
} from './content-safety.svelte';
import { stripNetworkClientState } from '$lib/api/network-boundary';
afterEach(() => {
  vi.clearAllMocks();
});
const user = { id: '1', origin_domain: 'home.example' } as UserSummary;

it.each([404, 405])(
  'allows legacy servers only when the safety endpoint is unsupported (%s)',
  async (status) => {
    network.api.mockRejectedValue(new ApiError('UNSUPPORTED', 'Unsupported', status));
    await syncContentSafety(user);
    expect(contentSafety.ready).toBe(true);
    expect(network.purge).toHaveBeenCalledOnce();
  }
);

it.each([401, 403, 429, 500, 503])(
  'keeps content closed when safety synchronization fails (%s)',
  async (status) => {
    network.api.mockRejectedValue(new ApiError('FAILED', 'Failed', status));
    await expect(syncContentSafety(user)).rejects.toBeInstanceOf(ApiError);
    expect(contentSafety.ready).toBe(false);
    expect(contentSafety.error).toContain('Could not check');
    expect(network.purge).not.toHaveBeenCalled();
  }
);

it('does not finish synchronization before durable encrypted cache cleanup completes', async () => {
  network.api.mockResolvedValue({ items: [], next_cursor: '0', has_more: false });
  let release!: () => void;
  network.purge.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        release = resolve;
      })
  );
  const pending = syncContentSafety(user);
  await vi.waitFor(() => expect(network.purge).toHaveBeenCalledOnce());
  expect(contentSafety.ready).toBe(false);
  release();
  await pending;
  expect(contentSafety.ready).toBe(true);
});

it('invalidates request authorization revisions when reconnecting', async () => {
  network.api.mockResolvedValue({ items: [], next_cursor: '0', has_more: false });
  const before = contentSafety.revision;
  contentSafety.ready = false;
  const pending = syncContentSafety(user);
  expect(contentSafety.revision).toBeGreaterThan(before);
  const message = {
    id: '9201',
    origin_domain: 'home.example',
    channel_id: '1',
    channel_domain: 'home.example',
    author_id: '2',
    author_domain: 'home.example',
    created_at: '2026-10-26T00:00:00Z',
    content: 'old response',
    e2ee: null,
    attachments: []
  } as unknown as Message;
  applyContentSafetyUpdate({
    state: 'unavailable',
    reason: 'Unavailable',
    message_refs: ['9201@home.example'],
    attachment_refs: []
  });
  expect(stripNetworkClientState(message, before).safety_unavailable).toBe(true);
  await pending;
});
