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

  removeItem(key: string): void {
    this.#values.delete(key);
  }
}

describe('native session startup', () => {
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

  it('restores credentials even when the WebView remembered its instance', async () => {
    storage.setItem('kaede.native.instance', 'kaede.chat');
    const invoke = vi.fn(async (command: string) => {
      if (command === 'native_set_instance') return 'kaede.chat';
      if (command === 'native_restore_session') {
        return { instance: 'kaede.chat', authenticated: true };
      }
      throw new Error(`Unexpected command: ${command}`);
    });
    vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
    const { initializeNativeInstance } = await import('./native');

    await expect(initializeNativeInstance()).resolves.toEqual({
      instance: 'kaede.chat',
      authenticated: true
    });
    expect(invoke.mock.calls.map(([command]) => command)).toEqual([
      'native_set_instance',
      'native_restore_session'
    ]);
  });

  it('shares one restore across concurrent startup requests', async () => {
    let releaseRestore: (() => void) | undefined;
    const restorePending = new Promise<void>((resolve) => {
      releaseRestore = resolve;
    });
    const invoke = vi.fn(async (command: string) => {
      if (command !== 'native_restore_session') throw new Error('Unexpected command');
      await restorePending;
      return { instance: 'kaede.chat', authenticated: true };
    });
    vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
    const { initializeNativeInstance } = await import('./native');

    const first = initializeNativeInstance();
    const second = initializeNativeInstance();
    releaseRestore?.();

    await expect(Promise.all([first, second])).resolves.toEqual([
      { instance: 'kaede.chat', authenticated: true },
      { instance: 'kaede.chat', authenticated: true }
    ]);
    expect(invoke).toHaveBeenCalledOnce();
  });

  it('allows restoration to retry after a temporary vault failure', async () => {
    const invoke = vi
      .fn()
      .mockRejectedValueOnce(new Error('vault unavailable'))
      .mockResolvedValueOnce({ instance: 'kaede.chat', authenticated: true });
    vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
    const { initializeNativeInstance } = await import('./native');

    await expect(initializeNativeInstance()).rejects.toThrow('vault unavailable');
    await expect(initializeNativeInstance()).resolves.toMatchObject({ authenticated: true });
    expect(invoke).toHaveBeenCalledTimes(2);
  });

  it('retains only safe authenticated routes across a native process restart', async () => {
    vi.stubGlobal('window', {
      __TAURI__: { core: { invoke: vi.fn() } }
    });
    const { rememberNativeRoute, storedNativeRoute } = await import('./native');

    rememberNativeRoute('/g/123%40chat.example/456%40chat.example?around=789');
    expect(storedNativeRoute()).toBe('/g/123%40chat.example/456%40chat.example?around=789');

    rememberNativeRoute('/login');
    expect(storedNativeRoute()).toBe('/g/123%40chat.example/456%40chat.example?around=789');

    storage.setItem('kaede.native.last-route', 'https://attacker.example/steal');
    expect(storedNativeRoute()).toBeNull();
  });
});
