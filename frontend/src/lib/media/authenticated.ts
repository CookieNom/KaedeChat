import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';
import { apiErrorMessage } from '$lib/api/errors';
import { parseCanonicalEntityRef } from '$lib/chat/refs';

export interface AuthenticatedMediaSource {
  path: string;
  contentType: string;
}

type MediaElement = HTMLAudioElement | HTMLImageElement | HTMLVideoElement;
const MAX_THUMBNAIL_BYTES = 16 * 1024 * 1024;
const MAX_ERROR_BYTES = 64 * 1024;
const RETRYABLE_MEDIA_CAPACITY_CODES = new Set(['REMOTE_MEDIA_BUSY', 'REMOTE_MEDIA_CACHE_FULL']);

function retryAfterMilliseconds(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? Math.round(seconds * 1000) : null;
}

function stableJitter(value: string): number {
  if (!value) return 0;
  let hash = 2166136261;
  for (const code of value) hash = Math.imul(hash ^ code.charCodeAt(0), 16777619);
  return Math.abs(hash) % 251;
}

export function mediaCapacityRetryDelay(
  status: number,
  code: string,
  retryAfterHeader: string | null,
  attempt: number,
  key = ''
): number | null {
  if (
    attempt >= 2 ||
    !RETRYABLE_MEDIA_CAPACITY_CODES.has(code) ||
    ![429, 503, 507].includes(status)
  )
    return null;
  const requested = retryAfterMilliseconds(retryAfterHeader) ?? 1000 * (attempt + 1);
  return Math.min(5000, Math.max(500, requested)) + stableJitter(key);
}

async function boundedBytes(response: Response, maximum: number): Promise<Uint8Array> {
  const reader = response.body?.getReader();
  if (!reader) return new Uint8Array();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximum) throw new Error('Media response exceeded its safe in-memory limit.');
      chunks.push(value);
    }
  } catch (error) {
    void reader.cancel();
    throw error;
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

async function errorDetail(response: Response): Promise<Record<string, unknown>> {
  const contentLength = Number(response.headers.get('content-length') ?? '0');
  if (Number.isFinite(contentLength) && contentLength > MAX_ERROR_BYTES) return {};
  try {
    const bytes = await boundedBytes(response, MAX_ERROR_BYTES);
    const parsed = JSON.parse(new TextDecoder().decode(bytes)) as unknown;
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer);
        reject(new DOMException('Media request cancelled', 'AbortError'));
      },
      { once: true }
    );
  });
}

function isBoundedThumbnail(source: AuthenticatedMediaSource): boolean {
  return source.contentType.startsWith('image/') && /\/thumbnail_(128|512|1024)$/.test(source.path);
}

function asBytes(value: ArrayBuffer | Uint8Array | number[]): Uint8Array {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  return Uint8Array.from(value);
}

function asBlobPart(value: ArrayBuffer | Uint8Array | number[]): ArrayBuffer {
  const bytes = asBytes(value);
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

export function attachmentMediaPath(
  originDomain: string,
  attachmentId: string,
  variant: 'original' | 'thumbnail_128' | 'thumbnail_512' | 'thumbnail_1024' | 'poster',
  historyMediaUrl?: string | null,
  privateMediaUrl?: string | null
): string {
  const privatePath = privateInteractionAttachmentMediaPath(
    originDomain,
    attachmentId,
    variant,
    privateMediaUrl
  );
  if (privatePath) return privatePath;
  const historyPath = dmHistoryAttachmentMediaPath(
    originDomain,
    attachmentId,
    variant,
    historyMediaUrl
  );
  if (historyPath) return historyPath;
  return `/media/${encodeURIComponent(originDomain)}/${encodeURIComponent(attachmentId)}/${variant}`;
}

/**
 * Accept only the signed, identity-bound DM-history route projected by the
 * account authority. A federated attachment may not turn an authenticated
 * media fetch into an arbitrary same-origin GET.
 */
export function dmHistoryAttachmentMediaPath(
  originDomain: string,
  attachmentId: string,
  variant: 'original' | 'thumbnail_128' | 'thumbnail_512' | 'thumbnail_1024' | 'poster',
  value: string | null | undefined
): string | null {
  if (!isSafeSameOriginMediaPath(value)) return null;
  const parsed = new URL(value, 'https://kaede.invalid');
  const match =
    /^\/api\/v1\/dms\/([^/]+)\/history-media\/([^/]+)\/([^/]+)\/(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$/u.exec(
      parsed.pathname
    );
  if (!match || match[4] !== variant || parsed.hash) return null;
  const conversation = parseCanonicalEntityRef(match[1]);
  const message = parseCanonicalEntityRef(match[2]);
  const attachment = parseCanonicalEntityRef(match[3]);
  const expires = parsed.searchParams.getAll('expires');
  const tokens = parsed.searchParams.getAll('token');
  if (
    !conversation ||
    !message ||
    !attachment ||
    message.origin_domain !== attachment.origin_domain ||
    attachment.id !== attachmentId ||
    attachment.origin_domain !== originDomain ||
    [...parsed.searchParams.keys()].some((key) => key !== 'expires' && key !== 'token') ||
    expires.length !== 1 ||
    !/^[1-9][0-9]*$/u.test(expires[0]) ||
    tokens.length !== 1 ||
    !/^[A-Za-z0-9_-]{40,48}$/u.test(tokens[0])
  ) {
    return null;
  }
  return value;
}

export function privateInteractionAttachmentMediaPath(
  originDomain: string,
  attachmentId: string,
  variant: 'original' | 'thumbnail_128' | 'thumbnail_512' | 'thumbnail_1024' | 'poster',
  value: string | null | undefined
): string | null {
  if (!isSafeSameOriginMediaPath(value)) return null;
  const match =
    /^\/api\/v1\/interactions\/([^/]+)\/responses\/([^/]+)\/attachments\/([^/]+)$/u.exec(value);
  if (!match) return null;
  const interaction = parseCanonicalEntityRef(match[1]);
  const response = parseCanonicalEntityRef(match[2]);
  const attachment = parseCanonicalEntityRef(match[3]);
  if (
    !interaction ||
    !response ||
    !attachment ||
    interaction.origin_domain !== response.origin_domain ||
    response.origin_domain !== attachment.origin_domain ||
    attachment.id !== attachmentId ||
    attachment.origin_domain !== originDomain
  ) {
    return null;
  }
  return `${value}/${variant}`;
}

/**
 * Federation history may supply a temporary authenticated proxy path. Never
 * accept a URL-like value here: authorization is reserved for the signed-in
 * Kaede API and must not be sent to a remote media host.
 */
export function isSafeSameOriginMediaPath(value: string | null | undefined): value is string {
  return Boolean(value?.startsWith('/') && !value.startsWith('//') && !value.includes('\\'));
}

export function authenticatedMedia(node: MediaElement, source: AuthenticatedMediaSource) {
  let generation = 0;
  let objectUrl = '';
  let controller: AbortController | null = null;

  function revokeObjectUrl() {
    if (!objectUrl) return;
    URL.revokeObjectURL(objectUrl);
    objectUrl = '';
  }

  async function load(next: AuthenticatedMediaSource) {
    const current = ++generation;
    controller?.abort();
    const requestController = new AbortController();
    controller = requestController;
    revokeObjectUrl();
    node.removeAttribute('data-media-error');
    node.removeAttribute('data-media-error-message');

    if (!isNativeDesktop() && !isBoundedThumbnail(next)) {
      node.src = next.path;
      if (node instanceof HTMLMediaElement) node.load();
      return;
    }

    node.removeAttribute('src');
    try {
      let response: ArrayBuffer | Uint8Array | number[];
      if (isNativeDesktop()) {
        response = await nativeInvoke<ArrayBuffer | Uint8Array | number[]>('native_media_request', {
          path: next.path
        });
      } else {
        let loaded: Uint8Array | null = null;
        for (let attempt = 0; loaded === null; attempt += 1) {
          const fetched = await fetch(next.path, {
            credentials: 'same-origin',
            headers: { Accept: next.contentType },
            signal: requestController.signal
          });
          if (fetched.ok) {
            const declared = Number(fetched.headers.get('content-length') ?? '0');
            if (Number.isFinite(declared) && declared > MAX_THUMBNAIL_BYTES)
              throw new Error('Media response exceeded its safe in-memory limit.');
            loaded = await boundedBytes(fetched, MAX_THUMBNAIL_BYTES);
            break;
          }
          const detail = await errorDetail(fetched);
          const nested =
            typeof detail.detail === 'object' && detail.detail !== null
              ? (detail.detail as Record<string, unknown>)
              : detail;
          const code = typeof nested.code === 'string' ? nested.code : `HTTP_${fetched.status}`;
          const delay = mediaCapacityRetryDelay(
            fetched.status,
            code,
            fetched.headers.get('retry-after'),
            attempt,
            next.path
          );
          if (delay === null) {
            const failure = new Error(apiErrorMessage(code, fetched.status, nested));
            failure.name = 'MediaLoadError';
            throw failure;
          }
          await abortableDelay(delay, requestController.signal);
        }
        response = loaded ?? new Uint8Array();
      }
      if (current !== generation) return;
      objectUrl = URL.createObjectURL(new Blob([asBlobPart(response)], { type: next.contentType }));
      node.src = objectUrl;
      if (node instanceof HTMLMediaElement) node.load();
    } catch (error) {
      if (current === generation && !requestController.signal.aborted) {
        node.setAttribute('data-media-error', 'true');
        if (error instanceof Error && error.name === 'MediaLoadError') {
          node.setAttribute('data-media-error-message', error.message);
        }
        node.dispatchEvent(new Event('error'));
      }
    }
  }

  void load(source);
  return {
    update(next: AuthenticatedMediaSource) {
      void load(next);
    },
    destroy() {
      generation += 1;
      controller?.abort();
      revokeObjectUrl();
    }
  };
}

/** Read authenticated same-origin media without ever forwarding session credentials off-origin. */
export async function authenticatedMediaBlob(
  source: AuthenticatedMediaSource,
  maximumBytes = 100 * 1024 * 1024,
  signal?: AbortSignal
): Promise<Blob> {
  if (!Number.isSafeInteger(maximumBytes) || maximumBytes < 1) {
    throw new TypeError('Authenticated media size limit is invalid.');
  }
  if (signal?.aborted) throw new DOMException('Media request cancelled', 'AbortError');
  if (isNativeDesktop()) {
    const response = await nativeInvoke<ArrayBuffer | Uint8Array | number[]>(
      'native_media_request',
      {
        path: source.path
      }
    );
    const bytes = asBytes(response);
    if (bytes.byteLength > maximumBytes) {
      throw new Error('Media response exceeded its safe in-memory limit.');
    }
    if (signal?.aborted) throw new DOMException('Media request cancelled', 'AbortError');
    return new Blob([asBlobPart(bytes)], { type: source.contentType });
  }
  const response = await fetch(source.path, {
    credentials: 'same-origin',
    headers: { Accept: source.contentType },
    signal
  });
  if (!response.ok) throw new Error('Could not download the authenticated media.');
  const declared = Number(response.headers.get('content-length') ?? '0');
  if (Number.isFinite(declared) && declared > maximumBytes) {
    throw new Error('Media response exceeded its safe in-memory limit.');
  }
  const bytes = await boundedBytes(response, maximumBytes);
  return new Blob([asBlobPart(bytes)], { type: source.contentType });
}

export async function downloadAuthenticatedMedia(
  source: AuthenticatedMediaSource,
  filename: string
): Promise<void> {
  if (!isNativeDesktop()) {
    const anchor = document.createElement('a');
    anchor.href = source.path;
    anchor.download = filename;
    anchor.click();
    return;
  }

  const response = await nativeInvoke<ArrayBuffer | Uint8Array | number[]>('native_media_request', {
    path: source.path
  });
  const objectUrl = URL.createObjectURL(
    new Blob([asBlobPart(response)], { type: source.contentType })
  );
  try {
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.click();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}
