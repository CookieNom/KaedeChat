import { webcrypto } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fromBase64url, utf8 } from '$lib/e2ee/encoding';

import { preparePassword, type ModernPasswordKdfContext } from './password-kdf';

const CONTEXT: ModernPasswordKdfContext = {
  version: 2,
  algorithm: 'PBKDF2-SHA256',
  iterations: 600_000,
  auth_salt: 'AAECAwQFBgcICQoLDA0ODw',
  vault_salt: 'EBESExQVFhcYGRobHB0eHw'
};

describe('password KDF v2', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('matches the mobile vectors and keeps auth and vault purposes distinct', async () => {
    vi.stubGlobal('crypto', webcrypto);
    vi.stubGlobal('window', { location: { hostname: 'kaede.example' } });

    const prepared = await preparePassword('correct horse battery staple', CONTEXT);
    expect(prepared.authenticationSecret).toBe('-Z__QIBecQeJPG4vVovIPtt-Oct4ZE8zUSWu3oyMG3s');

    const expectedVaultKey = await webcrypto.subtle.importKey(
      'raw',
      fromBase64url('ldmLIyIp7qlfGzCzvaUPzi4jvB3R8aDgFJUcQHZ2v70', 32),
      'AES-GCM',
      false,
      ['encrypt']
    );
    const iv = new Uint8Array(12);
    const plaintext = utf8('Kaede password KDF v2 interop');
    const [actual, expected] = await Promise.all([
      webcrypto.subtle.encrypt({ name: 'AES-GCM', iv }, prepared.vaultKey, plaintext),
      webcrypto.subtle.encrypt({ name: 'AES-GCM', iv }, expectedVaultKey, plaintext)
    ]);
    expect(new Uint8Array(actual)).toEqual(new Uint8Array(expected));
  });

  it('binds the authentication secret to the locally selected home', async () => {
    vi.stubGlobal('crypto', webcrypto);
    vi.stubGlobal('window', { location: { hostname: 'kaede.example' } });
    const first = await preparePassword('correct horse battery staple', CONTEXT);

    vi.stubGlobal('window', { location: { hostname: 'other.example' } });
    const second = await preparePassword('correct horse battery staple', CONTEXT);

    expect(first.authenticationSecret).not.toBe(second.authenticationSecret);
  });
});
