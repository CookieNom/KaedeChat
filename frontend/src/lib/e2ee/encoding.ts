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

export function base64urlByteLength(value: string, maximum: number): number {
  if (typeof value !== 'string') throw new TypeError('Invalid base64url value');
  const remainder = value.length % 4;
  const length = Math.floor((value.length * 3) / 4);
  if (length > maximum) throw new TypeError('Decoded value is too large');
  if (remainder === 1 || /[^A-Za-z0-9_-]/u.test(value)) {
    throw new TypeError('Invalid base64url value');
  }
  // Unused bits in the final sextet must be zero for canonical unpadded encoding.
  const last = value.charAt(value.length - 1);
  if (
    (remainder === 2 && !'AQgw'.includes(last)) ||
    (remainder === 3 && !'AEIMQUYcgkosw048'.includes(last))
  ) {
    throw new TypeError('Non-canonical base64url value');
  }
  return length;
}

export function fromBase64url(value: string, maximum = 4 * 1024 * 1024): Bytes {
  const length = base64urlByteLength(value, maximum);
  const raw = atob(value.replaceAll('-', '+').replaceAll('_', '/'));
  const result = new Uint8Array(length);
  for (let i = 0; i < length; i++) result[i] = raw.charCodeAt(i);
  return result;
}

/** Exact unpadded base64url encoding of 32 bytes (for SHA-256 and random capabilities). */
export function isCanonicalBase64url32(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/u.test(value);
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
