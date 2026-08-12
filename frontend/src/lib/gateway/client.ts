import { expireBrowserSession, refreshSession, userErrorMessage } from '$lib/api/client';
import {
  EVENT_NAMES,
  GatewayCloseCode,
  GatewayOp,
  PROTOCOL_VERSION,
  type EventName
} from '$lib/generated/ops';
import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';

export interface Dispatch<T = unknown> {
  op: GatewayOp.DISPATCH;
  t: EventName;
  s: number;
  d: T;
}

export const GATEWAY_SESSION_RESET_EVENT = 'gateway-session-reset';
export const GATEWAY_STATUS_EVENT = 'gateway-status';

export interface GatewayStatus {
  state: 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'degraded';
  message: string;
  retryInMs?: number;
}

interface GatewayEnvelope {
  op: GatewayOp;
  t?: string;
  s?: number;
  d: unknown;
}

interface QueuedGatewayCommand {
  key: string;
  browserPayload: object;
  nativeCommand: 'presence' | 'request_members' | 'subscribe_members';
  nativePayload: Record<string, unknown>;
  failureMessage: string;
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
  #nativeGeneration = 0;
  #preferredPresence: 'online' | 'idle' | 'dnd' | 'invisible' | null = null;
  #gatewayReady = false;
  #presencePending = false;
  #nativeFailures = 0;
  #pendingCommands = new Map<string, QueuedGatewayCommand>();
  #memberSubscription: QueuedGatewayCommand | null = null;
  #nativeCommandsInFlight = new Set<string>();
  #failedCommandKeys = new Set<string>();
  #commandRetryTimer: ReturnType<typeof setTimeout> | null = null;
  #commandRetryAttempt = 0;

  connect(): void {
    this.#manualClose = false;
    this.#lifecycle += 1;
    this.#reportStatus({ state: 'connecting', message: 'Connecting live updates…' });
    if (isNativeDesktop()) {
      const generation = ++this.#nativeGeneration;
      void this.#pollNative(generation);
      return;
    }
    this.#open();
  }

  async #pollNative(generation: number): Promise<void> {
    while (!this.#manualClose && generation === this.#nativeGeneration) {
      try {
        const envelope = await nativeInvoke<GatewayEnvelope | null>('native_gateway_next');
        if (envelope && !this.#manualClose && generation === this.#nativeGeneration) {
          this.#nativeFailures = 0;
          this.#receive(JSON.stringify(envelope));
        }
      } catch {
        if (!this.#manualClose && generation === this.#nativeGeneration) {
          this.#nativeFailures += 1;
          this.#reportStatus({
            state: this.#nativeFailures >= 5 ? 'offline' : 'reconnecting',
            message:
              this.#nativeFailures >= 5
                ? 'Live updates are unavailable. Messages may be out of date; Kaede is still retrying.'
                : 'Live updates were interrupted. Reconnecting automatically…',
            retryInMs: 1_000
          });
          await new Promise((resolve) => setTimeout(resolve, 1_000));
        }
      }
    }
  }

  #open(): void {
    if (this.#socket) return;
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket: WebSocket;
    try {
      socket = new WebSocket(
        `${scheme}//${window.location.host}/gateway?v=${PROTOCOL_VERSION}&encoding=json`
      );
    } catch {
      this.#scheduleReconnect(
        'Kaede could not open the live-update connection. Check your connection; Kaede will retry automatically.'
      );
      return;
    }
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
        this.#scheduleReconnect('Live updates need to resync. Reconnecting automatically…');
        return;
      }
      if (
        event.code === GatewayCloseCode.NOT_AUTHENTICATED ||
        event.code === GatewayCloseCode.AUTHENTICATION_FAILED
      ) {
        void this.#recoverAuthentication(this.#lifecycle);
      } else {
        const close = gatewayCloseStatus(event.code, event.reason);
        this.#scheduleReconnect(close.message, close.retryAfterMs);
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
    this.#nativeGeneration += 1;
    this.#nativeFailures = 0;
    if (this.#commandRetryTimer) clearTimeout(this.#commandRetryTimer);
    this.#commandRetryTimer = null;
    this.#pendingCommands.clear();
    this.#memberSubscription = null;
    this.#nativeCommandsInFlight.clear();
    this.#failedCommandKeys.clear();
    this.#commandRetryAttempt = 0;
    this.#socket?.close(1000);
    this.#cleanup();
  }

  requestMembers(guildRef: string, query = '', limit = 100): void {
    const [guild_id, guild_domain = ''] = guildRef.split('@', 2);
    this.#queueCommand({
      // Only the latest search for a guild matters after a reconnect; replaying
      // stale queries can make an older chunk win the race in the member list.
      key: 'members-request',
      browserPayload: {
        op: GatewayOp.REQUEST_MEMBERS,
        d: { guild_id: guildRef, query, limit }
      },
      nativeCommand: 'request_members',
      nativePayload: { guild_id, guild_domain, query, limit },
      failureMessage:
        'Could not refresh the guild member list. Kaede will retry; reopen the guild if it stays out of date.'
    });
  }

  subscribeMembers(guildRef: string, ranges: [number, number][] = [[0, 99]]): void {
    const [guild_id, guild_domain = ''] = guildRef.split('@', 2);
    const command: QueuedGatewayCommand = {
      key: 'member-subscription',
      browserPayload: {
        op: GatewayOp.SUBSCRIBE_MEMBER_LIST,
        d: { guild_id: guildRef, ranges }
      },
      nativeCommand: 'subscribe_members',
      nativePayload: { guild_id, guild_domain, ranges },
      failureMessage:
        'Could not subscribe to guild member updates. Kaede will retry; reopen the guild if it stays out of date.'
    };
    this.#memberSubscription = command;
    this.#queueCommand(command);
  }

  releaseMembers(): void {
    this.#memberSubscription = null;
    this.#pendingCommands.delete('member-subscription');
    this.#pendingCommands.delete('members-request');
    this.#failedCommandKeys.delete('member-subscription');
    this.#failedCommandKeys.delete('members-request');
    if (!this.#pendingCommands.size) {
      if (this.#commandRetryTimer) clearTimeout(this.#commandRetryTimer);
      this.#commandRetryTimer = null;
      this.#commandRetryAttempt = 0;
    }
    if (this.#gatewayReady && !this.#failedCommandKeys.size) {
      this.#reportStatus({ state: 'connected', message: '' });
    }
  }

  setPresence(status: 'online' | 'idle' | 'dnd' | 'invisible'): void {
    this.#preferredPresence = status;
    this.#presencePending = true;
    this.#queueCommand({
      key: 'presence',
      browserPayload: { op: GatewayOp.PRESENCE_UPDATE, d: { status } },
      nativeCommand: 'presence',
      nativePayload: { status },
      failureMessage:
        'Could not update your online status. Kaede will retry when live updates reconnect.'
    });
  }

  rememberPresence(status: 'online' | 'idle' | 'dnd' | 'invisible'): void {
    this.#preferredPresence = status;
    this.#presencePending = false;
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
        const ready = envelope.d as { session_id: string; presence_preference?: string };
        if (typeof ready.session_id !== 'string' || !ready.session_id) {
          throw new TypeError('Invalid gateway session');
        }
        if (!this.#presencePending && isPresencePreference(ready.presence_preference)) {
          this.#preferredPresence = ready.presence_preference;
        }
        sessionStorage.setItem('kaede.gateway.session', ready.session_id);
        this.#retry = 0;
        this.#gatewayReady = true;
        this.#reportStatus({ state: 'connected', message: '' });
      } else if (envelope.t === 'RESUMED') {
        const resumed = envelope.d as { presence_preference?: string };
        if (!this.#presencePending && isPresencePreference(resumed.presence_preference)) {
          this.#preferredPresence = resumed.presence_preference;
        }
        this.#retry = 0;
        this.#gatewayReady = true;
        this.#reportStatus({ state: 'connected', message: '' });
      }
      if (
        (envelope.t === 'READY' || envelope.t === 'RESUMED') &&
        this.#preferredPresence !== null
      ) {
        this.setPresence(this.#preferredPresence);
      }
      if (envelope.t === 'READY') {
        const subscription = this.#memberSubscription;
        if (subscription) this.#pendingCommands.set(subscription.key, subscription);
      }
      if (envelope.t === 'READY' || envelope.t === 'RESUMED') this.#flushPendingCommands();
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

  #queueCommand(command: QueuedGatewayCommand): void {
    this.#pendingCommands.set(command.key, command);
    if (this.#gatewayReady) this.#sendQueuedCommand(command);
  }

  #flushPendingCommands(): void {
    if (!this.#gatewayReady) return;
    for (const command of this.#pendingCommands.values()) this.#sendQueuedCommand(command);
  }

  #sendQueuedCommand(command: QueuedGatewayCommand): void {
    if (!this.#gatewayReady || this.#manualClose) return;
    if (!isNativeDesktop()) {
      if (this.#socket?.readyState !== WebSocket.OPEN) return;
      try {
        this.#socket.send(JSON.stringify(command.browserPayload));
      } catch {
        this.#socket.close(1012, 'Gateway command send failed');
        return;
      }
      if (this.#pendingCommands.get(command.key) === command) {
        this.#pendingCommands.delete(command.key);
      }
      if (command.key === 'presence') this.#presencePending = false;
      return;
    }
    const lifecycle = this.#lifecycle;
    const inFlightKey = `${lifecycle}:${command.key}`;
    if (this.#nativeCommandsInFlight.has(inFlightKey)) return;
    this.#nativeCommandsInFlight.add(inFlightKey);
    void nativeInvoke('native_gateway_command', {
      command: command.nativeCommand,
      payload: command.nativePayload
    })
      .then(() => {
        if (this.#manualClose || lifecycle !== this.#lifecycle) return;
        const isCurrent = this.#pendingCommands.get(command.key) === command;
        if (isCurrent) {
          this.#pendingCommands.delete(command.key);
        }
        if (command.key === 'presence' && isCurrent) this.#presencePending = false;
        if (isCurrent) this.#failedCommandKeys.delete(command.key);
        if (isCurrent && this.#gatewayReady && !this.#failedCommandKeys.size) {
          this.#commandRetryAttempt = 0;
          this.#reportStatus({ state: 'connected', message: '' });
        }
      })
      .catch((caught: unknown) => {
        if (
          this.#manualClose ||
          lifecycle !== this.#lifecycle ||
          this.#pendingCommands.get(command.key) !== command
        )
          return;
        this.#failedCommandKeys.add(command.key);
        this.#commandRetryAttempt += 1;
        const retryInMs = Math.min(1_000 * 2 ** (this.#commandRetryAttempt - 1), 30_000);
        this.#reportStatus({
          state: 'degraded',
          message: userErrorMessage(caught, command.failureMessage),
          retryInMs
        });
        this.#scheduleCommandRetry(retryInMs);
      })
      .finally(() => {
        this.#nativeCommandsInFlight.delete(inFlightKey);
        if (this.#manualClose || lifecycle !== this.#lifecycle) return;
        const replacement = this.#pendingCommands.get(command.key);
        if (replacement && replacement !== command && this.#gatewayReady) {
          this.#sendQueuedCommand(replacement);
        }
      });
  }

  #scheduleCommandRetry(delay: number): void {
    if (this.#commandRetryTimer || this.#manualClose) return;
    this.#commandRetryTimer = setTimeout(() => {
      this.#commandRetryTimer = null;
      this.#flushPendingCommands();
    }, delay);
  }

  #cleanup(): void {
    this.#gatewayReady = false;
    if (this.#heartbeat) clearInterval(this.#heartbeat);
    this.#heartbeat = null;
    this.#heartbeatAcknowledged = true;
    this.#socket = null;
  }

  #scheduleReconnect(
    message = 'Live updates were interrupted. Reconnecting automatically…',
    minimumDelayMs = 0
  ): void {
    if (this.#manualClose || this.#reconnectTimer) return;
    const ceiling = Math.min(1000 * 2 ** this.#retry, 15_000);
    const delay = Math.max(
      Math.round(ceiling * (0.75 + Math.random() * 0.5)),
      Math.min(Math.max(0, minimumDelayMs), 60_000)
    );
    this.#retry += 1;
    this.#reportStatus({
      state: this.#retry >= 5 ? 'offline' : 'reconnecting',
      message:
        this.#retry >= 5
          ? 'Live updates are unavailable. Messages may be out of date; Kaede is still retrying.'
          : message,
      retryInMs: delay
    });
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      this.#open();
    }, delay);
  }

  async #recoverAuthentication(lifecycle: number): Promise<void> {
    const refresh = await refreshSession();
    if (this.#manualClose || lifecycle !== this.#lifecycle) return;
    if (refresh === 'ok' || refresh === 'unavailable') {
      this.#scheduleReconnect(
        refresh === 'unavailable'
          ? 'The server could not refresh live updates. Reconnecting automatically…'
          : undefined
      );
    } else {
      expireBrowserSession();
    }
  }

  #reportStatus(status: GatewayStatus): void {
    this.dispatchEvent(new CustomEvent<GatewayStatus>(GATEWAY_STATUS_EVENT, { detail: status }));
  }
}

function gatewayCloseStatus(
  code: number,
  reason: string
): { message: string; retryAfterMs: number } {
  if (code === GatewayCloseCode.RATE_LIMITED) {
    let retryAfterMs = 0;
    let reasonCode = '';
    if (reason && reason.length <= 512) {
      try {
        const detail = JSON.parse(reason) as Record<string, unknown>;
        if (
          typeof detail.retry_after_ms === 'number' &&
          Number.isFinite(detail.retry_after_ms) &&
          detail.retry_after_ms >= 0
        ) {
          retryAfterMs = Math.min(Math.round(detail.retry_after_ms), 60_000);
        }
        if (detail.code === 'SESSION_LIMIT') reasonCode = detail.code;
      } catch {
        // Close reasons are untrusted wire data. Unknown text is deliberately not displayed.
      }
    }
    return {
      message:
        reasonCode === 'SESSION_LIMIT'
          ? 'This account has too many active live-update sessions. Close Kaede on another device; this session will keep retrying.'
          : 'Live updates are being requested too quickly. Kaede will retry automatically.',
      retryAfterMs: reasonCode === 'SESSION_LIMIT' && retryAfterMs === 0 ? 30_000 : retryAfterMs
    };
  }
  if (
    code === GatewayCloseCode.UNKNOWN_OPCODE ||
    code === GatewayCloseCode.DECODE_ERROR ||
    code === GatewayCloseCode.ALREADY_AUTHENTICATED
  ) {
    return {
      message:
        'The server could not continue the live-update connection. Kaede is reconnecting; reload if this keeps happening.',
      retryAfterMs: 0
    };
  }
  return {
    message: 'Live updates were interrupted. Reconnecting automatically…',
    retryAfterMs: 0
  };
}

function isPresencePreference(value: unknown): value is 'online' | 'idle' | 'dnd' | 'invisible' {
  return value === 'online' || value === 'idle' || value === 'dnd' || value === 'invisible';
}
