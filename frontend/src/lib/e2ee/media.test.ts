import { webcrypto } from 'node:crypto';
import { beforeAll, describe, expect, it } from 'vitest';

import { decryptFile, encryptFile, encryptedManifestDigest } from './media';
import { fromBase64url } from './encoding';
import fileVector from '../../../static/protocol/kaede-file-v1.json';

beforeAll(() => {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
});

describe('encrypted attachments', () => {
  it('round trips a multi-chunk file without exposing its metadata in ciphertext', async () => {
    const contents = new Uint8Array(180_000);
    for (let offset = 0; offset < contents.length; offset += 65_536)
      webcrypto.getRandomValues(
        contents.subarray(offset, Math.min(contents.length, offset + 65_536))
      );
    const file = Object.assign(new Blob([contents], { type: 'image/png' }), {
      name: 'private-photo.png'
    });
    const { ciphertext, manifest } = await encryptFile(file, 64 * 1024);

    expect(await ciphertext.text()).not.toContain('private-photo.png');
    expect(await ciphertext.text()).not.toContain('image/png');
    const restored = new Uint8Array(await (await decryptFile(ciphertext, manifest)).arrayBuffer());
    expect(restored).toEqual(contents);
    expect(manifest.plaintext_sha256).toMatch(/^[A-Za-z0-9_-]{43}$/u);
  });

  it('decrypts the shared cross-client vector and verifies its plaintext commitment', async () => {
    const ciphertext = new Blob([fromBase64url(fileVector.ciphertext_base64url)], {
      type: 'application/octet-stream'
    });
    const restored = new Uint8Array(
      await (
        await decryptFile(ciphertext, fileVector.manifest as Parameters<typeof decryptFile>[1])
      ).arrayBuffer()
    );
    expect(restored).toEqual(fromBase64url(fileVector.plaintext_base64url));
    await expect(
      decryptFile(ciphertext, {
        ...(fileVector.manifest as Parameters<typeof decryptFile>[1]),
        plaintext_sha256: 'A'.repeat(43)
      })
    ).rejects.toThrow(/plaintext/u);
  });

  it('rejects tampering, truncation, and unauthenticated manifest changes', async () => {
    const file = Object.assign(new Blob([new Uint8Array(90_000).fill(7)], { type: 'text/plain' }), {
      name: 'proof.txt'
    });
    const { ciphertext, manifest } = await encryptFile(file, 64 * 1024);
    const changed = new Uint8Array(await ciphertext.arrayBuffer());
    changed[changed.length - 1] ^= 1;
    await expect(
      decryptFile(new Blob([changed], { type: 'application/octet-stream' }), manifest)
    ).rejects.toThrow('modified');
    await expect(decryptFile(ciphertext.slice(0, -1), manifest)).rejects.toThrow('size');
    await expect(
      decryptFile(ciphertext, { ...manifest, filename: 'changed.txt', plaintext_size: 1 })
    ).rejects.toThrow('header');
  });

  it('binds attachment ordering and keys into the MLS envelope digest', async () => {
    const first = (
      await encryptFile(
        Object.assign(new Blob(['one'], { type: 'text/plain' }), { name: 'one.txt' })
      )
    ).manifest;
    const second = (
      await encryptFile(
        Object.assign(new Blob(['two'], { type: 'text/plain' }), { name: 'two.txt' })
      )
    ).manifest;
    expect(await encryptedManifestDigest([first, second])).not.toBe(
      await encryptedManifestDigest([second, first])
    );
    expect(await encryptedManifestDigest([first])).not.toBe(
      await encryptedManifestDigest([{ ...first, key: second.key }])
    );
  });

  it('clears the generated raw file key when key import fails', async () => {
    let imported: Uint8Array | null = null;
    const failingCrypto = {
      getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
      subtle: {
        importKey: async (
          _format: string,
          keyData: ArrayBuffer | ArrayBufferView
        ): Promise<CryptoKey> => {
          imported = keyData as Uint8Array;
          throw new Error('import failed');
        }
      }
    };
    Object.defineProperty(globalThis, 'crypto', { value: failingCrypto, configurable: true });
    try {
      const file = Object.assign(new Blob(['secret'], { type: 'text/plain' }), {
        name: 'secret.txt'
      });
      await expect(encryptFile(file)).rejects.toThrow('import failed');
      expect(imported).not.toBeNull();
      expect([...imported!]).toEqual(new Array(32).fill(0));
    } finally {
      Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
    }
  });
});
