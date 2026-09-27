// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import NativeDesktopLifecycle from './NativeDesktopLifecycle.svelte';
import { chatEntities } from '$lib/stores/entities.svelte';
import { setTaskbarUnreadCount } from '$lib/platform/taskbar';
import type { ReadStateStatus } from '$lib/chat/types';

vi.mock('$lib/platform/native', () => ({ isNativeDesktop: () => true }));
vi.mock('$lib/platform/taskbar', () => ({ setTaskbarUnreadCount: vi.fn(async () => {}) }));
vi.mock('$lib/platform/desktop-lifecycle.svelte', () => ({
  desktopLifecycle: { initialize: vi.fn(), checkForUpdates: vi.fn() },
  NATIVE_UPDATE_POLL_INTERVAL_MS: 60_000
}));

it('updates from read state, recovers after a failed update, and clears on session reset', async () => {
  const target = document.createElement('div');
  const component = mount(NativeDesktopLifecycle, { target });
  const update = vi.mocked(setTaskbarUnreadCount);
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  try {
    flushSync();
    await vi.waitFor(() => expect(update).toHaveBeenLastCalledWith(0));
    update.mockRejectedValueOnce(new Error('Temporary native failure'));
    flushSync(() => {
      chatEntities.readStates.replace([
        {
          channel_id: '1',
          channel_domain: 'home.test',
          guild_id: '10',
          unread: true,
          mention_count: 2
        },
        {
          channel_id: '2',
          channel_domain: 'home.test',
          guild_id: null,
          unread: true,
          mention_count: 0
        }
      ] as ReadStateStatus[]);
    });
    await vi.waitFor(() => expect(warning).toHaveBeenCalledOnce());
    expect(update).toHaveBeenLastCalledWith(3);
    flushSync(() =>
      chatEntities.readStates.update('1@home.test', (state) => ({ ...state, unread: false }))
    );
    await vi.waitFor(() => expect(update).toHaveBeenLastCalledWith(1));
    flushSync(() => chatEntities.clearSession());
    await vi.waitFor(() => expect(update).toHaveBeenLastCalledWith(0));
  } finally {
    await unmount(component);
    chatEntities.clearSession();
    warning.mockRestore();
  }
});
