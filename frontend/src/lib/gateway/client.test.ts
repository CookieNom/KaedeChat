import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GatewayCloseCode, GatewayOp, PROTOCOL_VERSION } from '$lib/generated/ops';

const authMocks = vi.hoisted(() => ({
  expireBrowserSession: vi.fn(),
  refreshSession: vi.fn<() => Promise<'ok' | 'invalid' | 'unavailable'>>()
}));

vi.mock('$lib/api/client', () => authMocks);

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

  clear(): void {
    this.#values.clear();
  }
}

class FakeWebSocket extends EventTarget {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readonly sent: string[] = [];
  readyState = FakeWebSocket.OPEN;
  closeCode: number | null = null;

  constructor(readonly url: string) {
    super();
    FakeWebSocket.instances.push(this);
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(code = 1000): void {
    if (this.readyState !== FakeWebSocket.OPEN) return;
    this.readyState = 3;
    this.closeCode = code;
    const event = new Event('close');
    Object.defineProperty(event, 'code', { value: code });
    this.dispatchEvent(event);
  }

  message(payload: unknown): void {
    this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(payload) }));
  }
}

describe('GatewayClient lifecycle', () => {
  const storage = new MemoryStorage();
  const localStorageMemory = new MemoryStorage();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0);
    storage.clear();
    localStorageMemory.clear();
    FakeWebSocket.instances = [];
    authMocks.expireBrowserSession.mockReset();
    authMocks.refreshSession.mockReset().mockResolvedValue('ok');
    vi.stubGlobal('sessionStorage', storage);
    vi.stubGlobal('localStorage', localStorageMemory);
    vi.stubGlobal('WebSocket', FakeWebSocket);
    vi.stubGlobal('window', {
      location: { protocol: 'https:', host: 'chat.example' }
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('uses the generated protocol version and identifies after HELLO', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const socket = FakeWebSocket.instances[0];

    expect(socket.url).toContain(`v=${PROTOCOL_VERSION}`);
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    expect(JSON.parse(socket.sent[0])).toEqual({ op: GatewayOp.IDENTIFY, d: {} });
    client.close();
  });

  it('publishes the preferred presence after READY', async () => {
    localStorageMemory.setItem('kaede.presence', 'dnd');
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    socket.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'presence-session' }
    });

    expect(JSON.parse(socket.sent[1])).toEqual({
      op: GatewayOp.PRESENCE_UPDATE,
      d: { status: 'dnd' }
    });
    client.close();
  });

  it('re-announces presence after RESUMED so an expired projection is recreated', async () => {
    localStorageMemory.setItem('kaede.presence', 'idle');
    storage.setItem('kaede.gateway.session', 'resumed-session');
    storage.setItem('kaede.gateway.sequence', '12');
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    socket.message({
      op: GatewayOp.DISPATCH,
      t: 'RESUMED',
      s: 13,
      d: {}
    });

    expect(JSON.parse(socket.sent[1])).toEqual({
      op: GatewayOp.PRESENCE_UPDATE,
      d: { status: 'idle' }
    });
    client.close();
  });

  it('clears an invalid resume sequence before reconnecting', async () => {
    storage.setItem('kaede.gateway.session', 'old-session');
    storage.setItem('kaede.gateway.sequence', '9');
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const first = FakeWebSocket.instances[0];
    first.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    expect(JSON.parse(first.sent[0]).op).toBe(GatewayOp.RESUME);

    first.close(GatewayCloseCode.INVALID_SEQUENCE);
    await vi.advanceTimersByTimeAsync(751);
    const second = FakeWebSocket.instances[1];
    second.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    expect(JSON.parse(second.sent[0])).toEqual({ op: GatewayOp.IDENTIFY, d: {} });
    client.close();
  });

  it('requests one route reconciliation after an invalid session reaches a fresh READY', async () => {
    storage.setItem('kaede.gateway.session', 'old-session');
    storage.setItem('kaede.gateway.sequence', '9');
    const { GATEWAY_SESSION_RESET_EVENT, GatewayClient } = await import('./client');
    const client = new GatewayClient();
    const reset = vi.fn();
    client.addEventListener(GATEWAY_SESSION_RESET_EVENT, reset);
    client.connect();

    const first = FakeWebSocket.instances[0];
    first.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    first.message({ op: GatewayOp.INVALID_SESSION, d: false });
    await vi.advanceTimersByTimeAsync(751);

    const second = FakeWebSocket.instances[1];
    second.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    expect(JSON.parse(second.sent[0])).toEqual({ op: GatewayOp.IDENTIFY, d: {} });
    second.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'new-session' }
    });

    expect(reset).toHaveBeenCalledOnce();
    client.close();
  });

  it('does not request route reconciliation for the initial READY', async () => {
    const { GATEWAY_SESSION_RESET_EVENT, GatewayClient } = await import('./client');
    const client = new GatewayClient();
    const reset = vi.fn();
    client.addEventListener(GATEWAY_SESSION_RESET_EVENT, reset);
    client.connect();

    const socket = FakeWebSocket.instances[0];
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    socket.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'initial-session' }
    });

    expect(reset).not.toHaveBeenCalled();
    client.close();
  });

  it('closes a connection that misses a heartbeat acknowledgement', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 1_000 } });

    await vi.advanceTimersByTimeAsync(2_000);
    expect(socket.closeCode).toBe(1012);
    client.close();
  });

  it('closes on an unknown server opcode instead of leaving a stuck connection', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const socket = FakeWebSocket.instances[0];

    socket.message({ op: 999, d: null });

    expect(socket.closeCode).toBe(1002);
    client.close();
  });

  it('refreshes after authentication failure but not a normal token-rotation close', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    FakeWebSocket.instances[0].close(GatewayCloseCode.SESSION_TIMED_OUT);
    expect(authMocks.refreshSession).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(751);
    FakeWebSocket.instances[1].close(GatewayCloseCode.AUTHENTICATION_FAILED);
    await vi.runAllTimersAsync();
    expect(authMocks.refreshSession).toHaveBeenCalledOnce();
    client.close();
  });

  it('does not reconnect when authentication recovery finishes after close', async () => {
    let finishRefresh!: (result: 'ok') => void;
    authMocks.refreshSession.mockReturnValue(
      new Promise((resolve) => {
        finishRefresh = resolve;
      })
    );
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();

    FakeWebSocket.instances[0].close(GatewayCloseCode.AUTHENTICATION_FAILED);
    client.close();
    finishRefresh('ok');
    await Promise.resolve();
    await vi.runAllTimersAsync();

    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('can reconnect after an explicit close cancels a pending retry', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    FakeWebSocket.instances[0].close(GatewayCloseCode.SESSION_TIMED_OUT);

    client.close();
    client.connect();
    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.instances[1].close(GatewayCloseCode.SESSION_TIMED_OUT);
    await vi.advanceTimersByTimeAsync(751);

    expect(FakeWebSocket.instances).toHaveLength(3);
    client.close();
  });
});
