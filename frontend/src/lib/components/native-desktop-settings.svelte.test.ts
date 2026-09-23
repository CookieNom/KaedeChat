// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import NativeDesktopSettings from './NativeDesktopSettings.svelte';
const { lifecycle } = vi.hoisted(() => ({
  lifecycle: {
    taskbar: { supported: false, allowed: false, pinned: false },
    update: { supported: true, available: false, current_version: '1.0' },
    refreshTaskbarStatus: vi.fn(),
    refreshAutostartStatus: vi.fn(),
    checkForUpdates: vi.fn()
  }
}));
vi.mock('$lib/platform/desktop-lifecycle.svelte', () => ({ desktopLifecycle: lifecycle }));

it('only shows taskbar settings when pinning is available or already completed', async () => {
  for (const [supported, allowed, pinned, visible, actionable] of [
    [false, false, false, false, false],
    [true, false, false, false, false],
    [true, true, false, true, true],
    [true, false, true, true, false]
  ]) {
    lifecycle.taskbar = { supported, allowed, pinned };
    const target = document.createElement('div');
    const component = mount(NativeDesktopSettings, { target });
    flushSync();
    expect(Boolean(target.querySelector('.desktop-taskbar'))).toBe(visible);
    expect(Boolean(target.querySelector('.desktop-taskbar button'))).toBe(actionable);
    await unmount(component);
  }
});
