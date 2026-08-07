export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly detail: Record<string, unknown> = {}
  ) {
    super(message);
  }
}

const ERROR_MESSAGES: Record<string, string> = {
  MISSING_PERMISSIONS: "You don't have permission to do that.",
  CANNOT_MANAGE_PERMISSIONS: "You can't change those permissions.",
  CANNOT_GRANT_PERMISSIONS: "You can't grant permissions you don't have.",
  ROLE_HIERARCHY: 'That member or role is higher than your highest role.',
  OWNER_IMMUNE: "The guild owner can't be moderated or have their roles changed.",
  CANNOT_MANAGE_SELF: "You can't use that action on yourself.",
  TARGET_CANNOT_CONNECT: "That member doesn't have permission to join this voice channel.",
  VOICE_NOT_CONNECTED: 'That member is no longer connected to voice.',
  VOICE_DISABLED: 'Voice is disabled on this instance.',
  VOICE_HOME_UNREACHABLE: 'The voice server is temporarily unavailable. Try again shortly.'
};

function readableErrorMessage(
  code: string,
  status: number,
  detail: Record<string, unknown>
): string {
  const supplied = typeof detail.message === 'string' ? detail.message.trim() : '';
  if (supplied && !/^forbidden$/i.test(supplied)) return supplied;
  const configured = ERROR_MESSAGES[code];
  if (configured) return configured;
  if (status === 403) return "You don't have permission to do that.";
  return supplied || 'Request failed';
}

const PUBLIC_AUTH_PATHS = new Set([
  '/auth/config',
  '/auth/login',
  '/auth/mfa',
  '/auth/register',
  '/auth/refresh',
  '/auth/verify-email',
  '/auth/email/change/confirm',
  '/auth/password/forgot',
  '/auth/password/reset'
]);

export type RefreshResult = 'ok' | 'invalid' | 'unavailable';

let refreshPromise: Promise<RefreshResult> | null = null;

function requestHeaders(init: RequestInit): Headers {
  const headers = new Headers(init.headers);
  headers.set('X-Kaede-Client', 'web');
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return headers;
}

function requestSignal(init: RequestInit): AbortSignal {
  const timeout = AbortSignal.timeout(15_000);
  return init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
}

async function performRefresh(): Promise<RefreshResult> {
  try {
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
        'The server is temporarily unavailable. Your session was not cleared.',
        503
      );
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const rawDetail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as Record<string, unknown>).detail
        : body;
    const detail =
      typeof rawDetail === 'object' && rawDetail !== null
        ? (rawDetail as Record<string, unknown>)
        : typeof rawDetail === 'string'
          ? { message: rawDetail }
          : {};
    const code = typeof detail.code === 'string' ? detail.code : 'REQUEST_FAILED';
    throw new ApiError(
      code,
      readableErrorMessage(code, response.status, detail),
      response.status,
      detail
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
