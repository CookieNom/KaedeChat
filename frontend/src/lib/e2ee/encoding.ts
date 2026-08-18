const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });

export type Bytes = Uint8Array<ArrayBuffer>;
export type ByteSource = ArrayBufferLike | ArrayBufferView<ArrayBufferLike>;

export function ownedBytes(value: ByteSource): Bytes {
  if (ArrayBuffer.isView(value)) {
    return Uint8Array.from(new Uint8Array(value.buffer, value.byteOffset, value.byteLength));
  }
  return Uint8Array.from(new Uint8Array(value));
}

export function utf8(value: string): Bytes {
  return Uint8Array.from(encoder.encode(value));
}

export function decodeUtf8(value: ByteSource): string {
  const bytes = ownedBytes(value);
  try {
    return decoder.decode(bytes);
  } finally {
    clearBytes(bytes);
  }
}

export function base64url(value: ByteSource): string {
  const bytes = ownedBytes(value);
  try {
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/u, '');
  } finally {
    clearBytes(bytes);
  }
}

export function fromBase64url(value: string, maximum = 4 * 1024 * 1024): Bytes {
  if (!/^[A-Za-z0-9_-]*$/u.test(value) || value.length % 4 === 1) {
    throw new TypeError('Invalid base64url value');
  }
  const raw = atob(value.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat(-value.length & 3));
  if (raw.length > maximum) throw new TypeError('Decoded value is too large');
  const result = Uint8Array.from(raw, (character) => character.charCodeAt(0));
  if (base64url(result) !== value) throw new TypeError('Non-canonical base64url value');
  return result;
}

export function concatBytes(...values: readonly Uint8Array<ArrayBufferLike>[]): Bytes {
  const length = values.reduce((total, value) => total + value.length, 0);
  const result = new Uint8Array(length);
  let offset = 0;
  for (const value of values) {
    result.set(value, offset);
    offset += value.length;
  }
  return result;
}

export async function sha256(value: ByteSource): Promise<Bytes> {
  const bytes = ownedBytes(value);
  try {
    return new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  } finally {
    clearBytes(bytes);
  }
}

export function randomBytes(length: number): Bytes {
  if (!Number.isInteger(length) || length < 1 || length > 65_536) {
    throw new TypeError('Invalid random byte length');
  }
  return crypto.getRandomValues(new Uint8Array(length));
}

export function clearBytes(value: Uint8Array<ArrayBufferLike>): void {
  value.fill(0);
}
