import { api } from '$lib/api/client';
import { uploadObject, type UploadTicket } from '$lib/media/uploads';
import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';
import { attachmentMediaPath } from '$lib/media/authenticated';
import {
  base64url,
  concatBytes,
  fromBase64url,
  ownedBytes,
  randomBytes,
  sha256,
  utf8,
  type Bytes
} from './encoding';

const MAGIC = utf8('KAEF');
const VERSION = 1;
const DEFAULT_CHUNK_SIZE = 256 * 1024;
const MAX_FILE_SIZE = 64 * 1024 * 1024;
const HEADER_SIZE = 4 + 1 + 4 + 8 + 16 + 8;

export interface EncryptedFileManifest {
  version: 1;
  protocol: 'kaede-file-v1';
  file_id: string;
  key: string;
  filename: string;
  content_type: string;
  plaintext_size: number;
  ciphertext_size: number;
  ciphertext_sha256: string;
  chunk_size: number;
  attachment_id?: string;
  attachment_domain?: string;
  preview?: EncryptedFileManifest;
}

function u32(value: number): Bytes {
  const result = new Uint8Array(4);
  new DataView(result.buffer).setUint32(0, value, false);
  return result;
}

function header(plainSize: number, chunkSize: number, salt: Bytes, noncePrefix: Bytes): Bytes {
  const result = new Uint8Array(HEADER_SIZE);
  result.set(MAGIC, 0);
  result[4] = VERSION;
  const view = new DataView(result.buffer);
  view.setUint32(5, chunkSize, false);
  view.setBigUint64(9, BigInt(plainSize), false);
  result.set(salt, 17);
  result.set(noncePrefix, 33);
  return result;
}

async function contentKey(rawKey: Bytes, salt: Bytes): Promise<CryptoKey> {
  const material = await crypto.subtle.importKey('raw', rawKey, 'HKDF', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt, info: utf8('kaede attachment content v1') },
    material,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt']
  );
}

function chunkNonce(prefix: Bytes, index: number): Bytes {
  return concatBytes(prefix, u32(index));
}

function chunkAad(fileHeader: Bytes, index: number, count: number): Bytes {
  return concatBytes(fileHeader, u32(index), u32(count));
}

export async function encryptFile(
  file: Blob & { name?: string; type: string },
  chunkSize = DEFAULT_CHUNK_SIZE
): Promise<{ ciphertext: Blob; manifest: EncryptedFileManifest }> {
  if (!Number.isInteger(chunkSize) || chunkSize < 64 * 1024 || chunkSize > 1024 * 1024) {
    throw new TypeError('Invalid encrypted file chunk size.');
  }
  if (file.size < 1 || file.size > MAX_FILE_SIZE) throw new Error('File size is not supported.');
  const fileId = randomBytes(16);
  const rawKey = randomBytes(32);
  const salt = randomBytes(16);
  const noncePrefix = randomBytes(8);
  const fileHeader = header(file.size, chunkSize, salt, noncePrefix);
  try {
    const key = await contentKey(rawKey, salt);
    const count = Math.ceil(file.size / chunkSize);
    const parts: BlobPart[] = [fileHeader];
    for (let index = 0; index < count; index += 1) {
      const plaintext = new Uint8Array(
        await file
          .slice(index * chunkSize, Math.min(file.size, (index + 1) * chunkSize))
          .arrayBuffer()
      );
      try {
        const encrypted = await crypto.subtle.encrypt(
          {
            name: 'AES-GCM',
            iv: chunkNonce(noncePrefix, index),
            additionalData: chunkAad(fileHeader, index, count),
            tagLength: 128
          },
          key,
          plaintext
        );
        parts.push(u32(encrypted.byteLength), encrypted);
      } finally {
        plaintext.fill(0);
      }
    }
    const ciphertext = new Blob(parts, { type: 'application/octet-stream' });
    const bytes = await ciphertext.arrayBuffer();
    const manifest: EncryptedFileManifest = {
      version: 1,
      protocol: 'kaede-file-v1',
      file_id: base64url(fileId),
      key: base64url(rawKey),
      filename: file.name?.trim() || 'file',
      content_type: file.type || 'application/octet-stream',
      plaintext_size: file.size,
      ciphertext_size: ciphertext.size,
      ciphertext_sha256: base64url(await sha256(bytes)),
      chunk_size: chunkSize
    };
    return { ciphertext, manifest };
  } finally {
    rawKey.fill(0);
  }
}

export async function decryptFile(
  ciphertext: Blob,
  manifest: EncryptedFileManifest
): Promise<Blob> {
  if (manifest.version !== 1 || manifest.protocol !== 'kaede-file-v1') {
    throw new Error('Unsupported encrypted file.');
  }
  const all = new Uint8Array(await ciphertext.arrayBuffer());
  if (all.length !== manifest.ciphertext_size || all.length < HEADER_SIZE) {
    throw new Error('Encrypted file size does not match its authenticated manifest.');
  }
  const digest = base64url(await sha256(all));
  if (digest !== manifest.ciphertext_sha256) throw new Error('Encrypted file was modified.');
  if (!MAGIC.every((byte, index) => all[index] === byte) || all[4] !== VERSION) {
    throw new Error('Encrypted file header is invalid.');
  }
  const view = new DataView(all.buffer, all.byteOffset, all.byteLength);
  const chunkSize = view.getUint32(5, false);
  const plainSize = Number(view.getBigUint64(9, false));
  if (chunkSize !== manifest.chunk_size || plainSize !== manifest.plaintext_size) {
    throw new Error('Encrypted file header does not match its authenticated manifest.');
  }
  const fileHeader = all.slice(0, HEADER_SIZE);
  const salt = all.slice(17, 33);
  const noncePrefix = all.slice(33, 41);
  const rawKey = fromBase64url(manifest.key, 32);
  let key: CryptoKey;
  try {
    if (rawKey.length !== 32) throw new Error('Encrypted file key is invalid.');
    key = await contentKey(rawKey, salt);
  } finally {
    rawKey.fill(0);
  }
  const count = Math.ceil(plainSize / chunkSize);
  const parts: ArrayBuffer[] = [];
  let offset = HEADER_SIZE;
  let produced = 0;
  for (let index = 0; index < count; index += 1) {
    if (offset + 4 > all.length) throw new Error('Encrypted file is truncated.');
    const length = view.getUint32(offset, false);
    offset += 4;
    if (length < 17 || offset + length > all.length)
      throw new Error('Encrypted file chunk is invalid.');
    const plaintext = await crypto.subtle.decrypt(
      {
        name: 'AES-GCM',
        iv: chunkNonce(noncePrefix, index),
        additionalData: chunkAad(fileHeader, index, count),
        tagLength: 128
      },
      key,
      ownedBytes(all.slice(offset, offset + length))
    );
    produced += plaintext.byteLength;
    parts.push(plaintext);
    offset += length;
  }
  if (offset !== all.length || produced !== plainSize)
    throw new Error('Encrypted file framing is invalid.');
  return new Blob(parts, { type: manifest.content_type });
}

export async function encryptedManifestDigest(
  manifests: readonly EncryptedFileManifest[]
): Promise<string> {
  return base64url(await sha256(utf8(JSON.stringify(manifests))));
}

export async function uploadEncryptedChannelFile(
  channelRef: string,
  file: File,
  onProgress: (progress: number) => void,
  signal?: AbortSignal
): Promise<{ ticket: UploadTicket; manifest: EncryptedFileManifest }> {
  const encrypted = await encryptFile(file);
  const ticket = await api<UploadTicket>(
    `/channels/${encodeURIComponent(channelRef)}/attachments`,
    {
      method: 'POST',
      signal,
      body: JSON.stringify({
        filename: 'encrypted-file',
        content_type: 'application/octet-stream',
        size: encrypted.ciphertext.size,
        encryption_mode: 'e2ee',
        encryption_protocol: 'kaede-file-v1'
      })
    }
  );
  const upload = new File([encrypted.ciphertext], 'encrypted-file', {
    type: 'application/octet-stream'
  });
  await uploadObject(ticket, upload, onProgress, signal);
  return {
    ticket,
    manifest: {
      ...encrypted.manifest,
      attachment_id: ticket.id,
      attachment_domain: ticket.origin_domain
    }
  };
}

export async function decryptEncryptedAttachment(
  manifest: EncryptedFileManifest,
  historyMediaUrl?: string | null
): Promise<Blob> {
  if (!manifest.attachment_id || !manifest.attachment_domain)
    throw new Error('Encrypted attachment reference is missing.');
  const path = attachmentMediaPath(
    manifest.attachment_domain,
    manifest.attachment_id,
    'original',
    historyMediaUrl
  );
  let ciphertext: Blob;
  if (isNativeDesktop()) {
    const response = await nativeInvoke<ArrayBuffer | Uint8Array | number[]>(
      'native_media_request',
      {
        path
      }
    );
    const bytes = response instanceof Uint8Array ? response : new Uint8Array(response);
    ciphertext = new Blob([ownedBytes(bytes)], { type: 'application/octet-stream' });
  } else {
    const response = await fetch(path, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Could not download the encrypted file.');
    const declared = Number(response.headers.get('content-length') ?? '0');
    if (Number.isFinite(declared) && declared > manifest.ciphertext_size)
      throw new Error('Encrypted file response is larger than its authenticated manifest.');
    ciphertext = await response.blob();
  }
  return decryptFile(ciphertext, manifest);
}

export async function downloadEncryptedFile(
  manifest: EncryptedFileManifest,
  historyMediaUrl?: string | null
): Promise<void> {
  const plaintext = await decryptEncryptedAttachment(manifest, historyMediaUrl);
  const objectUrl = URL.createObjectURL(plaintext);
  try {
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = manifest.filename;
    anchor.rel = 'noopener';
    anchor.click();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}
