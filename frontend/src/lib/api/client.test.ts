import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class MemoryStorage {
  #values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }

  removeItem(key: string): void {
    this.#values.delete(key);
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' }
  });
}

describe('API session recovery', () => {
  const browserWindow = new EventTarget();
  const storage = new MemoryStorage();

  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal('window', browserWindow);
    vi.stubGlobal('sessionStorage', storage);
    vi.stubGlobal('navigator', {
      locks: {
        request: (_name: string, callback: () => Promise<unknown>) => callback()
      }
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('rotates once and retries a protected request after a 401', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ detail: { code: 'AUTHENTICATION_REQUIRED' } }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: { code: 'AUTHENTICATION_REQUIRED' } }, 401))
      .mockResolvedValueOnce(jsonResponse({ expires_in: 900 }))
      .mockResolvedValueOnce(jsonResponse({ id: '1' }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await import('./client');

    await expect(api<{ id: string }>('/guilds/1')).resolves.toEqual({ id: '1' });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/guilds/1',
      '/api/v1/users/@me',
      '/api/v1/auth/refresh',
      '/api/v1/guilds/1'
    ]);
  });

  it('forces same-origin cookie credentials and the web CSRF header', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await import('./client');

    await api('/users/@me/settings', {
      method: 'PATCH',
      credentials: 'omit',
      headers: { 'X-Kaede-Client': 'mobile' },
      body: JSON.stringify({ theme: 'dark' })
    });

    const [, request] = fetchMock.mock.calls[0];
    const headers = new Headers(request?.headers);
    expect(request?.credentials).toBe('include');
    expect(headers.get('X-Kaede-Client')).toBe('web');
    expect(headers.get('Content-Type')).toBe('application/json');
  });

  it('preserves caller cancellation while applying the request timeout', async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn<typeof fetch>((_input, init) => {
      const signal = init?.signal ?? null;
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(signal.reason), {
          once: true
        });
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await import('./client');

    const request = api('/users/@me', { signal: controller.signal });
    controller.abort();

    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    const combinedSignal = fetchMock.mock.calls[0][1]?.signal;
    expect(combinedSignal?.aborted).toBe(true);
    expect(combinedSignal).not.toBe(controller.signal);
  });

  it('shares one refresh across concurrent requests in a tab', async () => {
    let protectedCalls = 0;
    let refreshCalls = 0;
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url === '/api/v1/users/@me') return jsonResponse({}, 401);
      if (url === '/api/v1/auth/refresh') {
        refreshCalls += 1;
        await Promise.resolve();
        return jsonResponse({ expires_in: 900 });
      }
      protectedCalls += 1;
      return protectedCalls <= 2 ? jsonResponse({}, 401) : jsonResponse({ ok: true });
    });
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await import('./client');

    await Promise.all([api('/guilds/1'), api('/guilds/2')]);
    expect(refreshCalls).toBe(1);
  });

  it('clears browser resume state only when refresh credentials are invalid', async () => {
    storage.setItem('kaede.gateway.session', 'session');
    storage.setItem('kaede.gateway.sequence', '7');
    const expired = vi.fn();
    browserWindow.addEventListener('kaede:session-expired', expired);
    vi.stubGlobal(
      'fetch',
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse({}, 401))
        .mockResolvedValueOnce(jsonResponse({}, 401))
        .mockResolvedValueOnce(jsonResponse({}, 401))
    );
    const { api, ApiError } = await import('./client');

    await expect(api('/guilds/1')).rejects.toBeInstanceOf(ApiError);
    expect(storage.getItem('kaede.gateway.session')).toBeNull();
    expect(expired).toHaveBeenCalledOnce();
    browserWindow.removeEventListener('kaede:session-expired', expired);
  });

  it('preserves the browser session during a transient refresh outage', async () => {
    storage.setItem('kaede.gateway.session', 'session');
    const expired = vi.fn();
    browserWindow.addEventListener('kaede:session-expired', expired);
    vi.stubGlobal(
      'fetch',
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse({}, 401))
        .mockResolvedValueOnce(jsonResponse({}, 503))
    );
    const { api } = await import('./client');

    await expect(api('/guilds/1')).rejects.toMatchObject({
      code: 'SESSION_REFRESH_UNAVAILABLE',
      status: 503
    });
    expect(storage.getItem('kaede.gateway.session')).toBe('session');
    expect(expired).not.toHaveBeenCalled();
    browserWindow.removeEventListener('kaede:session-expired', expired);
  });

  it('turns permission responses into useful messages', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse({ detail: { code: 'MISSING_PERMISSIONS' } }, 403))
    );
    const { api } = await import('./client');

    await expect(api('/channels/1/messages')).rejects.toMatchObject({
      code: 'MISSING_PERMISSIONS',
      message: "You don't have permission to do that.",
      status: 403
    });
  });

  it('does not expose a bare Forbidden response to the interface', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ detail: 'Forbidden' }, 403))
    );
    const { api } = await import('./client');

    await expect(api('/channels/1/messages')).rejects.toMatchObject({
      message: "You don't have permission to do that."
    });
  });

  it('does not discard credentials when refresh is rate limited', async () => {
    storage.setItem('kaede.gateway.session', 'session');
    const expired = vi.fn();
    browserWindow.addEventListener('kaede:session-expired', expired);
    vi.stubGlobal(
      'fetch',
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse({}, 401))
        .mockResolvedValueOnce(jsonResponse({}, 401))
        .mockResolvedValueOnce(jsonResponse({}, 429))
    );
    const { api } = await import('./client');

    await expect(api('/guilds/1')).rejects.toMatchObject({
      code: 'SESSION_REFRESH_UNAVAILABLE',
      status: 503
    });
    expect(storage.getItem('kaede.gateway.session')).toBe('session');
    expect(expired).not.toHaveBeenCalled();
    browserWindow.removeEventListener('kaede:session-expired', expired);
  });
});
