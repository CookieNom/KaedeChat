import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';

export const SOUNDBOARD_MAX_BYTES = 512 * 1024;

const SHA256_PATTERN = /^[0-9a-f]{64}$/u;
const SOUNDBOARD_CONTENT_TYPES = new Set(['audio/mpeg', 'audio/ogg']);

export interface SoundboardMediaRequest {
  downloadUrl: string;
  authorityDomain: string;
  mediaOrigin: string;
  expectedSha256: string;
  contentType: string;
}

export interface SoundboardPlaybackState {
  connected: boolean;
  canSpeak: boolean;
  selfMuted: boolean;
  selfDeafened: boolean;
  serverMuted?: boolean;
  serverDeafened?: boolean;
  suppressed?: boolean;
}

/** Discord exposes Soundboard playback only in guild voice channels, not DMs or Stages. */
export function soundboardChannelSupported(
  channelType: number | null | undefined,
  directCall: boolean
): boolean {
  return !directCall && channelType === 2;
}

export function soundboardPlaybackUnavailableReason(state: SoundboardPlaybackState): string | null {
  if (!state.connected) return 'Join this voice channel before using Soundboard.';
  if (state.serverDeafened) return 'A moderator must undeafen you before you can use Soundboard.';
  if (state.selfDeafened) return 'Undeafen before using Soundboard.';
  if (state.serverMuted) return 'A moderator must unmute you before you can use Soundboard.';
  if (state.suppressed) return 'Join the Stage speakers before using Soundboard.';
  if (!state.canSpeak) return 'You need permission to speak before using Soundboard.';
  if (state.selfMuted) return 'Unmute before using Soundboard.';
  return null;
}

export function soundboardSourceAllowed(
  targetGuildRef: string | null,
  sourceGuildRef: string | null,
  canUseExternalSounds: boolean
): boolean {
  return sourceGuildRef === null || sourceGuildRef === targetGuildRef || canUseExternalSounds;
}

function mediaError(message: string): Error {
  const error = new Error(message);
  error.name = 'SoundboardMediaError';
  return error;
}

/**
 * Bind a federation-supplied capability to the exact object-storage origin
 * signed by its media authority. Paths and query signatures remain opaque.
 */
export function validateSoundboardMediaUrl(
  value: string,
  authorityDomain: string,
  mediaOrigin: string
): URL {
  const authority = authorityDomain.trim().toLowerCase().replace(/\.$/u, '');
  if (
    !authority ||
    authority.length > 253 ||
    authority.split('.').some((label) => !/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/u.test(label))
  ) {
    throw mediaError('Kaede blocked an invalid guild sound source.');
  }

  let parsed: URL;
  let expectedOrigin: URL;
  try {
    parsed = new URL(value);
    expectedOrigin = new URL(mediaOrigin);
  } catch {
    throw mediaError('Kaede blocked an invalid guild sound link.');
  }
  const normalizedOrigin = mediaOrigin.replace(/\/$/u, '');
  if (
    parsed.protocol !== 'https:' ||
    expectedOrigin.protocol !== 'https:' ||
    expectedOrigin.origin !== normalizedOrigin ||
    expectedOrigin.pathname !== '/' ||
    expectedOrigin.search !== '' ||
    expectedOrigin.hash !== '' ||
    expectedOrigin.username !== '' ||
    expectedOrigin.password !== '' ||
    parsed.origin !== expectedOrigin.origin ||
    parsed.username !== '' ||
    parsed.password !== '' ||
    parsed.hash !== ''
  ) {
    throw mediaError('Kaede blocked a guild sound link from an unexpected media origin.');
  }
  return parsed;
}

function asOwnedBytes(value: ArrayBuffer | Uint8Array | number[]): Uint8Array<ArrayBuffer> {
  const source =
    value instanceof Uint8Array
      ? value
      : value instanceof ArrayBuffer
        ? new Uint8Array(value)
        : Uint8Array.from(value);
  const copy = new Uint8Array(source.byteLength);
  copy.set(source);
  return copy;
}

async function boundedResponseBytes(response: Response): Promise<Uint8Array<ArrayBuffer>> {
  const declaredValue = response.headers.get('content-length');
  if (declaredValue !== null) {
    const declared = Number(declaredValue);
    if (!Number.isSafeInteger(declared) || declared < 0 || declared > SOUNDBOARD_MAX_BYTES) {
      throw mediaError('The guild sound was larger than Kaede can play safely.');
    }
  }
  const reader = response.body?.getReader();
  if (!reader) throw mediaError('The guild sound response was empty.');

  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > SOUNDBOARD_MAX_BYTES) {
        throw mediaError('The guild sound was larger than Kaede can play safely.');
      }
      chunks.push(value);
    }
  } catch (error) {
    void reader.cancel();
    throw error;
  }
  if (total === 0) throw mediaError('The guild sound response was empty.');
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

async function browserSoundboardBytes(
  url: URL,
  contentType: string
): Promise<Uint8Array<ArrayBuffer>> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: { Accept: contentType },
      credentials: 'omit',
      redirect: 'manual',
      cache: 'no-store',
      signal: controller.signal
    });
    if (
      response.type === 'opaqueredirect' ||
      response.redirected ||
      (response.status >= 300 && response.status < 400)
    ) {
      throw mediaError('Kaede blocked a redirected guild sound link.');
    }
    if (!response.ok) {
      throw mediaError('The guild sound is unavailable or its download link expired.');
    }
    return await boundedResponseBytes(response);
  } catch (error) {
    if (error instanceof Error && error.name === 'SoundboardMediaError') throw error;
    if (controller.signal.aborted) {
      throw mediaError('The guild sound download timed out. Ask someone to play it again.');
    }
    throw mediaError('Could not download the guild sound safely. Check your connection and retry.');
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return Array.from(digest, (value) => value.toString(16).padStart(2, '0')).join('');
}

/** Download, bound, and authenticate one server-dispatched soundboard clip. */
export async function loadSoundboardMedia(request: SoundboardMediaRequest): Promise<Blob> {
  const url = validateSoundboardMediaUrl(
    request.downloadUrl,
    request.authorityDomain,
    request.mediaOrigin
  );
  if (!SHA256_PATTERN.test(request.expectedSha256)) {
    throw mediaError('Kaede blocked a guild sound with invalid integrity information.');
  }
  if (!SOUNDBOARD_CONTENT_TYPES.has(request.contentType)) {
    throw mediaError('This guild sound uses an unsupported audio format.');
  }

  const bytes = isNativeDesktop()
    ? asOwnedBytes(
        await nativeInvoke<ArrayBuffer | Uint8Array | number[]>('native_soundboard_media', {
          url: url.href,
          authorityDomain: request.authorityDomain,
          mediaOrigin: request.mediaOrigin,
          expectedSha256: request.expectedSha256
        })
      )
    : await browserSoundboardBytes(url, request.contentType);
  if (bytes.byteLength === 0) throw mediaError('The guild sound response was empty.');
  if (bytes.byteLength > SOUNDBOARD_MAX_BYTES) {
    throw mediaError('The guild sound was larger than Kaede can play safely.');
  }
  if ((await sha256Hex(bytes)) !== request.expectedSha256) {
    throw mediaError('Kaede blocked a guild sound that failed its integrity check.');
  }
  return new Blob([bytes], { type: request.contentType });
}
