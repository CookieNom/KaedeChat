import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class MemoryStorage {
  #values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }

  clear(): void {
    this.#values.clear();
  }
}

describe('desktop lifecycle', () => {
  const storage = new MemoryStorage();

  beforeEach(() => {
    vi.resetModules();
    storage.clear();
    vi.stubGlobal('localStorage', storage);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('polls signed updates and offers taskbar pinning only when Windows allows it', async () => {
    const invoke = vi.fn(async (command: string) => {
      if (command === 'native_update_check') {
        return {
          current_version: '0.1.10',
          supported: true,
          support_message: null,
          available: true,
          version: '0.1.11',
          notes: 'A new release',
          published_at: null
        };
      }
      if (command === 'native_platform_info') return { os: 'windows' };
      if (command === 'native_taskbar_pin_status') {
        return { supported: true, allowed: true, pinned: false };
      }
      throw new Error(`Unexpected command: ${command}`);
    });
    vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
    const { desktopLifecycle } = await import('./desktop-lifecycle.svelte');

    await desktopLifecycle.initialize();

    expect(desktopLifecycle.update).toMatchObject({ available: true, version: '0.1.11' });
    expect(desktopLifecycle.showTaskbarPrompt).toBe(true);
    desktopLifecycle.dismissUpdate();
    expect(storage.getItem('kaede.native.update-dismissed-version')).toBe('0.1.11');
  });

  it('keeps background polling failures quiet but reports a manual failure', async () => {
    const invoke = vi.fn().mockRejectedValue(new Error('offline'));
    vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
    const { desktopLifecycle } = await import('./desktop-lifecycle.svelte');

    await desktopLifecycle.checkForUpdates(false);
    expect(desktopLifecycle.updateError).toBe('');

    await desktopLifecycle.checkForUpdates(true);
    expect(desktopLifecycle.updateError).toBe('offline');
  });
});
