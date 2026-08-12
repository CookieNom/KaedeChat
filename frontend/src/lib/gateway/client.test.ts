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

  close(code = 1000, reason = ''): void {
    if (this.readyState !== FakeWebSocket.OPEN) return;
    this.readyState = 3;
    this.closeCode = code;
    const event = new Event('close');
    Object.defineProperty(event, 'code', { value: code });
    Object.defineProperty(event, 'reason', { value: reason });
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

  it('reports an actionable reconnect state instead of silently going stale', async () => {
    const { GATEWAY_STATUS_EVENT, GatewayClient } = await import('./client');
    const client = new GatewayClient();
    const statuses: Array<{ state: string; message: string; retryInMs?: number }> = [];
    client.addEventListener(GATEWAY_STATUS_EVENT, (event) => {
      statuses.push(
        (event as CustomEvent<{ state: string; message: string; retryInMs?: number }>).detail
      );
    });
    client.connect();

    FakeWebSocket.instances[0].close(1012);

    expect(statuses.at(-1)).toEqual({
      state: 'reconnecting',
      message: 'Live updates were interrupted. Reconnecting automatically…',
      retryInMs: 750
    });
    client.close();
  });

  it('clears the reconnect warning once live state is ready', async () => {
    const { GATEWAY_STATUS_EVENT, GatewayClient } = await import('./client');
    const client = new GatewayClient();
    const statuses: Array<{ state: string; message: string }> = [];
    client.addEventListener(GATEWAY_STATUS_EVENT, (event) => {
      statuses.push((event as CustomEvent<{ state: string; message: string }>).detail);
    });
    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    socket.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'status-session' }
    });

    expect(statuses.at(-1)).toEqual({ state: 'connected', message: '' });
    client.close();
  });

  it('does not overwrite the account presence preference after READY', async () => {
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

    client.setPresence('dnd');
    expect(JSON.parse(socket.sent[1])).toEqual({
      op: GatewayOp.PRESENCE_UPDATE,
      d: { status: 'dnd' }
    });
    client.close();
  });

  it('applies a server-backed preference that loads before READY', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });

    client.setPresence('dnd');
    expect(socket.sent).toHaveLength(1);
    socket.message({
      op: GatewayOp.DISPATCH,
      s: 1,
      t: 'READY',
      d: { session_id: 'presence-session' }
    });

    expect(JSON.parse(socket.sent.at(-1) ?? '')).toEqual({
      op: GatewayOp.PRESENCE_UPDATE,
      d: { status: 'dnd' }
    });
    client.close();
  });

  it('queues member commands while disconnected and sends them after READY', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const socket = FakeWebSocket.instances[0];

    client.requestMembers('10@remote.test', 'ali', 25);
    client.subscribeMembers('10@remote.test', [[0, 49]]);
    expect(socket.sent).toHaveLength(0);

    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    socket.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'member-session' }
    });

    expect(socket.sent.map((payload) => JSON.parse(payload).op)).toEqual([
      GatewayOp.IDENTIFY,
      GatewayOp.REQUEST_MEMBERS,
      GatewayOp.SUBSCRIBE_MEMBER_LIST
    ]);
    client.close();
  });

  it('keeps only the active guild subscription and replays it once after a fresh session', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const first = FakeWebSocket.instances[0];
    client.subscribeMembers('10@remote.test', [[0, 9]]);
    client.subscribeMembers('11@remote.test', [[0, 49]]);
    first.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    first.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'first-member-session' }
    });

    const firstSubscriptions = first.sent
      .map((payload) => JSON.parse(payload))
      .filter((payload) => payload.op === GatewayOp.SUBSCRIBE_MEMBER_LIST);
    expect(firstSubscriptions).toEqual([
      {
        op: GatewayOp.SUBSCRIBE_MEMBER_LIST,
        d: { guild_id: '11@remote.test', ranges: [[0, 49]] }
      }
    ]);

    first.close(GatewayCloseCode.INVALID_SEQUENCE);
    await vi.advanceTimersByTimeAsync(751);
    const second = FakeWebSocket.instances[1];
    second.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    second.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'replacement-member-session' }
    });
    const replayedSubscriptions = second.sent
      .map((payload) => JSON.parse(payload))
      .filter((payload) => payload.op === GatewayOp.SUBSCRIBE_MEMBER_LIST);
    expect(replayedSubscriptions).toHaveLength(1);
    expect(replayedSubscriptions[0].d.guild_id).toBe('11@remote.test');
    client.close();
  });

  it('does not replay a released guild subscription', async () => {
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.connect();
    const first = FakeWebSocket.instances[0];
    client.subscribeMembers('10@remote.test');
    client.releaseMembers();
    first.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    first.message({
      op: GatewayOp.DISPATCH,
      t: 'READY',
      s: 0,
      d: { session_id: 'released-member-session' }
    });

    expect(
      first.sent
        .map((payload) => JSON.parse(payload).op)
        .filter((op) => op === GatewayOp.SUBSCRIBE_MEMBER_LIST)
    ).toHaveLength(0);
    client.close();
  });

  it('honors the admission rate-limit close payload without displaying raw close text', async () => {
    const { GATEWAY_STATUS_EVENT, GatewayClient } = await import('./client');
    const client = new GatewayClient();
    const statuses: Array<{ message: string; retryInMs?: number }> = [];
    client.addEventListener(GATEWAY_STATUS_EVENT, (event) => {
      statuses.push((event as CustomEvent<{ message: string; retryInMs?: number }>).detail);
    });
    client.connect();

    FakeWebSocket.instances[0].close(
      GatewayCloseCode.RATE_LIMITED,
      JSON.stringify({ retry_after_ms: 4_000 })
    );

    expect(statuses.at(-1)).toMatchObject({
      message: 'Live updates are being requested too quickly. Kaede will retry automatically.',
      retryInMs: 4_000
    });
    await vi.advanceTimersByTimeAsync(3_999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    client.close();
  });

  it('backs off session-limit reconnects for the real close payload with no retry field', async () => {
    const { GATEWAY_STATUS_EVENT, GatewayClient } = await import('./client');
    const client = new GatewayClient();
    const statuses: Array<{ message: string; retryInMs?: number }> = [];
    client.addEventListener(GATEWAY_STATUS_EVENT, (event) => {
      statuses.push((event as CustomEvent<{ message: string; retryInMs?: number }>).detail);
    });
    client.connect();

    FakeWebSocket.instances[0].close(
      GatewayCloseCode.RATE_LIMITED,
      JSON.stringify({ code: 'SESSION_LIMIT', limit: 5 })
    );

    expect(statuses.at(-1)).toMatchObject({
      message:
        'This account has too many active live-update sessions. Close Kaede on another device; this session will keep retrying.',
      retryInMs: 30_000
    });
    await vi.advanceTimersByTimeAsync(29_999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    client.close();
  });

  it('does not let a resumed device restore its stale local presence', async () => {
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

    expect(socket.sent).toHaveLength(1);
    client.close();
  });

  it('uses the authoritative account preference after resume', async () => {
    storage.setItem('kaede.gateway.session', 'resumed-session');
    storage.setItem('kaede.gateway.sequence', '12');
    const { GatewayClient } = await import('./client');
    const client = new GatewayClient();
    client.rememberPresence('online');
    client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.message({ op: GatewayOp.HELLO, d: { heartbeat_interval: 41_250 } });
    socket.message({
      op: GatewayOp.DISPATCH,
      t: 'RESUMED',
      s: 13,
      d: { presence_preference: 'dnd' }
    });

    expect(JSON.parse(socket.sent.at(-1) ?? '')).toEqual({
      op: GatewayOp.PRESENCE_UPDATE,
      d: { status: 'dnd' }
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
