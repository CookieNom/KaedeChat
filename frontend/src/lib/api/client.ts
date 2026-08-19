import {
  initializeNativeInstance,
  isNativeDesktop,
  nativeError,
  nativeInvoke,
  type NativeError,
  type NativeResponse
} from '$lib/platform/native';
import {
  apiErrorMessage,
  ApiError,
  normalizeErrorDetail,
  trustedClientErrorMessage,
  validRetryAfterMs,
  validTraceId
} from './errors';

export { ApiError, userErrorMessage } from './errors';

const PUBLIC_AUTH_PATHS = new Set([
  '/auth/config',
  '/auth/key-derivation',
  '/auth/login',
  '/auth/mfa',
  '/auth/register',
  '/auth/refresh',
  '/auth/verify-email',
  '/auth/verify-email/resend',
  '/auth/email/change/confirm',
  '/auth/password/forgot',
  '/auth/password/reset'
]);

export type RefreshResult = 'ok' | 'invalid' | 'unavailable';

let refreshPromise: Promise<RefreshResult> | null = null;
const trustedNativeMessages = new WeakMap<Response, string>();

function isStructuredNativeError(value: unknown): value is NativeError & {
  code: string;
  message: string;
  status: number;
} {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  const validDetail =
    candidate.detail === undefined ||
    candidate.detail === null ||
    (typeof candidate.detail === 'object' &&
      candidate.detail !== null &&
      !Array.isArray(candidate.detail));
  return (
    typeof candidate.code === 'string' &&
    /^[A-Z][A-Z0-9_]{1,127}$/.test(candidate.code) &&
    typeof candidate.message === 'string' &&
    candidate.message.trim().length > 0 &&
    candidate.message.length <= 500 &&
    !/[\r\n\t]/.test(candidate.message) &&
    typeof candidate.status === 'number' &&
    Number.isInteger(candidate.status) &&
    (candidate.status === 0 || (candidate.status >= 400 && candidate.status <= 599)) &&
    validDetail
  );
}

function requestHeaders(init: RequestInit): Headers {
  const headers = new Headers(init.headers);
  headers.set('X-Kaede-Client', isNativeDesktop() ? 'desktop' : 'web');
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return headers;
}

function requestSignal(init: RequestInit): AbortSignal {
  const timeout = AbortSignal.timeout(15_000);
  return init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
}

async function performRefresh(): Promise<RefreshResult> {
  try {
    if (isNativeDesktop()) {
      await initializeNativeInstance();
      await nativeInvoke('native_api_request', {
        request: { method: 'POST', path: '/auth/refresh', body: {} }
      });
      return 'ok';
    }
    const response = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Kaede-Client': 'web' },
      body: '{}',
      credentials: 'include',
      signal: AbortSignal.timeout(15_000)
    });
    if (response.ok) return 'ok';
    return response.status === 401 ? 'invalid' : 'unavailable';
  } catch {
    return 'unavailable';
  }
}

async function accessSessionIsLive(): Promise<boolean | null> {
  try {
    if (isNativeDesktop()) {
      await initializeNativeInstance();
      await nativeInvoke('native_api_request', {
        request: { method: 'GET', path: '/users/@me', body: null }
      });
      return true;
    }
    const response = await fetch('/api/v1/users/@me', {
      headers: { 'X-Kaede-Client': 'web' },
      credentials: 'include',
      signal: AbortSignal.timeout(15_000)
    });
    if (response.ok) return true;
    return response.status === 401 ? false : null;
  } catch {
    return null;
  }
}

async function refreshIfNeeded(): Promise<RefreshResult> {
  // A different tab may have refreshed while this caller waited for the lock.
  const live = await accessSessionIsLive();
  if (live === true) return 'ok';
  if (live === null) return 'unavailable';
  return performRefresh();
}

async function refreshWithBrowserLock(): Promise<RefreshResult> {
  if (isNativeDesktop()) return refreshIfNeeded();
  if (typeof navigator !== 'undefined' && navigator.locks) {
    // Refresh cookies are shared by tabs. Serialize rotation across the origin so
    // two expired tabs cannot present the same token and trigger reuse revocation.
    return navigator.locks.request('kaede-auth-refresh', refreshIfNeeded);
  }
  return refreshIfNeeded();
}

export function expireBrowserSession(): void {
  if (typeof sessionStorage !== 'undefined') {
    sessionStorage.removeItem('kaede.gateway.session');
    sessionStorage.removeItem('kaede.gateway.sequence');
  }
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('kaede:session-expired'));
}

export function refreshSession(): Promise<RefreshResult> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = refreshWithBrowserLock().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

async function send(path: string, init: RequestInit): Promise<Response> {
  if (isNativeDesktop()) {
    await initializeNativeInstance();
    let body: unknown = null;
    if (typeof init.body === 'string' && init.body.length > 0) {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    try {
      const headers = requestHeaders(init);
      const result = await nativeInvoke<NativeResponse>('native_api_request', {
        request: {
          method: init.method ?? 'GET',
          path,
          body,
          if_match: headers.get('If-Match')
        }
      });
      return new Response(result.status === 204 ? null : JSON.stringify(result.body), {
        status: result.status,
        headers: { 'Content-Type': 'application/json', ...result.headers }
      });
    } catch (caught) {
      const error = nativeError(caught);
      const status = error.status && error.status > 0 ? error.status : 503;
      const detail = {
        ...(error.detail ?? {}),
        code: error.code ?? 'NATIVE_TRANSPORT_ERROR',
        message: error.message ?? 'The desktop transport could not complete the request.'
      };
      const response = new Response(JSON.stringify({ detail }), {
        status,
        headers: { 'Content-Type': 'application/json' }
      });
      // Only Rust's structured NativeError envelope contains wording that has
      // been deliberately written for users. Tauri can also reject with raw
      // strings or JavaScript errors; those must pass through normal filtering.
      if (isStructuredNativeError(caught)) {
        trustedNativeMessages.set(response, caught.message.trim());
      }
      return response;
    }
  }
  return fetch(`/api/v1${path}`, {
    ...init,
    headers: requestHeaders(init),
    signal: requestSignal(init),
    credentials: 'include'
  });
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response = await send(path, init);
  if (response.status === 401 && !PUBLIC_AUTH_PATHS.has(path)) {
    const refresh = await refreshSession();
    if (refresh === 'ok') {
      response = await send(path, init);
    } else if (refresh === 'invalid') {
      expireBrowserSession();
    } else {
      throw new ApiError(
        'SESSION_REFRESH_UNAVAILABLE',
        'The server is temporarily unavailable. Try again shortly; your session was not cleared.',
        503
      );
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = normalizeErrorDetail(body);
    if (!validTraceId(detail.trace_id)) {
      const headerTraceId = validTraceId(response.headers.get('X-Kaede-Trace-Id'));
      if (headerTraceId) detail.trace_id = headerTraceId;
    }
    if (validRetryAfterMs(detail.retry_after_ms) === null) {
      const retryAfterHeader = response.headers.get('Retry-After');
      const retryAfterSeconds = retryAfterHeader === null ? Number.NaN : Number(retryAfterHeader);
      if (Number.isFinite(retryAfterSeconds) && retryAfterSeconds >= 0) {
        detail.retry_after_ms = Math.min(retryAfterSeconds * 1000, 86_400_000);
      }
    }
    const code = typeof detail.code === 'string' ? detail.code : 'REQUEST_FAILED';
    const trustedNativeMessage = trustedNativeMessages.get(response);
    throw new ApiError(
      code,
      trustedNativeMessage
        ? trustedClientErrorMessage(trustedNativeMessage, code, response.status, detail)
        : apiErrorMessage(code, response.status, detail),
      response.status,
      detail
    );
  }
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    const traceId = validTraceId(response.headers.get('X-Kaede-Trace-Id'));
    const detail: Record<string, unknown> = traceId ? { trace_id: traceId } : {};
    throw new ApiError(
      'INVALID_SERVER_RESPONSE',
      apiErrorMessage('INVALID_SERVER_RESPONSE', 502, detail),
      502,
      detail
    );
  }
}
