import { expireBrowserSession, refreshSession } from '$lib/api/client';
import {
  EVENT_NAMES,
  GatewayCloseCode,
  GatewayOp,
  PROTOCOL_VERSION,
  type EventName
} from '$lib/generated/ops';

export interface Dispatch<T = unknown> {
  op: GatewayOp.DISPATCH;
  t: EventName;
  s: number;
  d: T;
}

export const GATEWAY_SESSION_RESET_EVENT = 'gateway-session-reset';

interface GatewayEnvelope {
  op: GatewayOp;
  t?: string;
  s?: number;
  d: unknown;
}

export class GatewayClient extends EventTarget {
  #socket: WebSocket | null = null;
  #heartbeat: ReturnType<typeof setInterval> | null = null;
  #sequence: number | null = null;
  #manualClose = false;
  #reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  #retry = 0;
  #heartbeatAcknowledged = true;
  #lifecycle = 0;
  #reconcileOnReady = false;

  connect(): void {
    this.#manualClose = false;
    this.#lifecycle += 1;
    this.#open();
  }

  #open(): void {
    if (this.#socket) return;
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(
      `${scheme}//${window.location.host}/gateway?v=${PROTOCOL_VERSION}&encoding=json`
    );
    this.#socket = socket;
    socket.addEventListener('message', (event) => {
      try {
        this.#receive(String(event.data));
      } catch {
        this.#socket?.close(1002, 'Invalid gateway payload');
      }
    });
    socket.addEventListener('error', () => socket.close());
    socket.addEventListener('close', (event) => {
      if (this.#socket !== socket) return;
      this.#cleanup();
      if (this.#manualClose) return;
      if (event.code === GatewayCloseCode.INVALID_SEQUENCE) {
        sessionStorage.removeItem('kaede.gateway.session');
        sessionStorage.removeItem('kaede.gateway.sequence');
        this.#sequence = null;
        this.#reconcileOnReady = true;
        this.#scheduleReconnect();
        return;
      }
      if (
        event.code === GatewayCloseCode.NOT_AUTHENTICATED ||
        event.code === GatewayCloseCode.AUTHENTICATION_FAILED
      ) {
        void this.#recoverAuthentication(this.#lifecycle);
      } else {
        this.#scheduleReconnect();
      }
    });
  }

  close(): void {
    this.#manualClose = true;
    this.#lifecycle += 1;
    this.#reconcileOnReady = false;
    if (this.#reconnectTimer) clearTimeout(this.#reconnectTimer);
    this.#reconnectTimer = null;
    this.#retry = 0;
    this.#socket?.close(1000);
    this.#cleanup();
  }

  requestMembers(guildRef: string, query = '', limit = 100): void {
    this.#send({ op: GatewayOp.REQUEST_MEMBERS, d: { guild_id: guildRef, query, limit } });
  }

  subscribeMembers(guildRef: string, ranges: [number, number][] = [[0, 99]]): void {
    this.#send({ op: GatewayOp.SUBSCRIBE_MEMBER_LIST, d: { guild_id: guildRef, ranges } });
  }

  setPresence(status: 'online' | 'idle' | 'dnd' | 'invisible'): void {
    this.#send({ op: GatewayOp.PRESENCE_UPDATE, d: { status } });
  }

  #receive(raw: string): void {
    const envelope = JSON.parse(raw) as GatewayEnvelope;
    if (typeof envelope !== 'object' || envelope === null || !Number.isInteger(envelope.op)) {
      throw new TypeError('Invalid gateway envelope');
    }
    if (envelope.op === GatewayOp.HELLO) {
      const hello = envelope.d as { heartbeat_interval: number };
      if (
        !Number.isFinite(hello.heartbeat_interval) ||
        hello.heartbeat_interval < 1_000 ||
        hello.heartbeat_interval > 120_000
      ) {
        this.#socket?.close(1002, 'Invalid heartbeat interval');
        return;
      }
      const sessionId = sessionStorage.getItem('kaede.gateway.session');
      const sequence = sessionStorage.getItem('kaede.gateway.sequence');
      if (sessionId && sequence !== null) {
        this.#send({
          op: GatewayOp.RESUME,
          d: { session_id: sessionId, seq: Number(sequence) }
        });
      } else {
        this.#send({ op: GatewayOp.IDENTIFY, d: {} });
      }
      if (this.#heartbeat) clearInterval(this.#heartbeat);
      this.#heartbeatAcknowledged = true;
      this.#heartbeat = setInterval(() => {
        if (!this.#heartbeatAcknowledged) {
          this.#socket?.close(1012, 'Heartbeat not acknowledged');
          return;
        }
        this.#heartbeatAcknowledged = false;
        this.#send({ op: GatewayOp.HEARTBEAT, d: this.#sequence });
      }, hello.heartbeat_interval);
      return;
    }
    if (envelope.op === GatewayOp.HEARTBEAT_ACK) {
      this.#heartbeatAcknowledged = true;
      return;
    }
    if (envelope.op === GatewayOp.DISPATCH) {
      if (
        typeof envelope.t !== 'string' ||
        !(EVENT_NAMES as readonly string[]).includes(envelope.t) ||
        !Number.isSafeInteger(envelope.s) ||
        (envelope.s ?? -1) < 0
      ) {
        throw new TypeError('Invalid gateway dispatch');
      }
      this.#sequence = envelope.s ?? this.#sequence;
      if (this.#sequence !== null) {
        sessionStorage.setItem('kaede.gateway.sequence', String(this.#sequence));
      }
      if (envelope.t === 'READY') {
        const ready = envelope.d as { session_id: string };
        if (typeof ready.session_id !== 'string' || !ready.session_id) {
          throw new TypeError('Invalid gateway session');
        }
        sessionStorage.setItem('kaede.gateway.session', ready.session_id);
        this.#retry = 0;
      } else if (envelope.t === 'RESUMED') {
        this.#retry = 0;
      }
      if (envelope.t === 'READY' || envelope.t === 'RESUMED') {
        let preferred: string | null = null;
        try {
          preferred = globalThis.localStorage?.getItem('kaede.presence') ?? null;
        } catch {
          // Browsers can deny storage access in hardened or ephemeral contexts.
        }
        this.setPresence(
          preferred === 'idle' || preferred === 'dnd' || preferred === 'invisible'
            ? preferred
            : 'online'
        );
      }
      this.dispatchEvent(
        new CustomEvent('dispatch', {
          detail: envelope as Dispatch
        })
      );
      if (envelope.t === 'READY' && this.#reconcileOnReady) {
        this.#reconcileOnReady = false;
        this.dispatchEvent(new Event(GATEWAY_SESSION_RESET_EVENT));
      }
      return;
    }
    if (envelope.op === GatewayOp.RECONNECT) {
      this.#socket?.close(1012);
      return;
    }
    if (envelope.op === GatewayOp.INVALID_SESSION) {
      sessionStorage.removeItem('kaede.gateway.session');
      sessionStorage.removeItem('kaede.gateway.sequence');
      this.#sequence = null;
      this.#reconcileOnReady = true;
      this.#socket?.close(1000);
      return;
    }
    throw new TypeError('Unknown gateway opcode');
  }

  #send(payload: object): void {
    if (this.#socket?.readyState === WebSocket.OPEN) this.#socket.send(JSON.stringify(payload));
  }

  #cleanup(): void {
    if (this.#heartbeat) clearInterval(this.#heartbeat);
    this.#heartbeat = null;
    this.#heartbeatAcknowledged = true;
    this.#socket = null;
  }

  #scheduleReconnect(): void {
    if (this.#manualClose || this.#reconnectTimer) return;
    const ceiling = Math.min(1000 * 2 ** this.#retry, 15_000);
    const delay = Math.round(ceiling * (0.75 + Math.random() * 0.5));
    this.#retry += 1;
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      this.#open();
    }, delay);
  }

  async #recoverAuthentication(lifecycle: number): Promise<void> {
    const refresh = await refreshSession();
    if (this.#manualClose || lifecycle !== this.#lifecycle) return;
    if (refresh === 'ok' || refresh === 'unavailable') {
      this.#scheduleReconnect();
    } else {
      expireBrowserSession();
    }
  }
}
