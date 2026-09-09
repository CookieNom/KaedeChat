import { describe, expect, it } from 'vitest';

import { base64url, utf8 } from './encoding';
import {
  accountVaultChainRoot,
  accountVaultEnvelopeDigest,
  compactDeviceState,
  isExactAccountVaultWriteAcknowledgement,
  MAX_MESSAGE_CACHE_BYTES,
  MAX_MESSAGE_CACHE_ENTRIES,
  openAccountVaultState,
  sealAccountVaultState,
  ZERO_VAULT_CHAIN,
  type CachedPlaintextMessage,
  type DeviceState
} from './store';

function stateWith(cache: Record<string, CachedPlaintextMessage>): DeviceState {
  return {
    schema: 2,
    accountRef: '1@example.test',
    deviceId: `ked_${'A'.repeat(43)}`,
    credential: 'credential',
    mlsState: 'state',
    vaultSequence: '7',
    vaultParentChain: ZERO_VAULT_CHAIN,
    messageCache: cache,
    controlCursors: {},
    pendingRoomOperations: {}
  };
}

describe('portable E2EE message cache', () => {
  it('keeps the newest bound entries within both count and UTF-8 byte budgets', () => {
    const entries = Array.from(
      { length: MAX_MESSAGE_CACHE_ENTRIES + 1 },
      (_, index) =>
        [
          base64url(utf8(`ciphertext-${index}`)),
          {
            plaintext: 'small',
            authorRef: '1@example.test',
            messageRef: `${index + 1}@example.test`
          }
        ] as const
    );
    const countCache = compactDeviceState(stateWith(Object.fromEntries(entries))).messageCache!;
    expect(Object.keys(countCache)).toEqual(entries.slice(1).map(([key]) => key));
    expect(new TextEncoder().encode(JSON.stringify(countCache)).length).toBeLessThan(
      MAX_MESSAGE_CACHE_BYTES
    );
    const large = entries
      .slice(-2)
      .map(
        ([key, value]) =>
          [
            key,
            { ...value, plaintext: '界'.repeat(Math.floor(MAX_MESSAGE_CACHE_BYTES / 6)) }
          ] as const
      );
    const input = Object.fromEntries(large);
    expect(JSON.stringify(input).length).toBeLessThan(MAX_MESSAGE_CACHE_BYTES);
    expect(new TextEncoder().encode(JSON.stringify(input)).length).toBeGreaterThan(
      MAX_MESSAGE_CACHE_BYTES
    );
    const byteCache = compactDeviceState(stateWith(input)).messageCache!;
    expect(Object.keys(byteCache)).toEqual([large[1][0]]);
    expect(byteCache[large[1][0]]).toEqual(large[1][1]);
    expect(new TextEncoder().encode(JSON.stringify(byteCache)).length).toBeLessThanOrEqual(
      MAX_MESSAGE_CACHE_BYTES
    );
  });
  it('rejects unbound or malformed cache values', () => {
    const ciphertext = base64url(utf8('ciphertext'));
    const valid = { plaintext: '{}', authorRef: '1@example.test', messageRef: '2@example.test' };
    expect(compactDeviceState(stateWith({ [ciphertext]: valid })).messageCache).toEqual({
      [ciphertext]: valid
    });
    for (const mutation of [{ authorRef: 'not-an-account' }, { messageRef: 'not-a-message' }]) {
      expect(() =>
        compactDeviceState(stateWith({ [ciphertext]: { ...valid, ...mutation } }))
      ).toThrow(/message cache/u);
    }
  });
});

describe('account-vault v2 binding', () => {
  it('binds the exact sequence and opaque bytes into the backend-compatible digest', async () => {
    await expect(
      accountVaultEnvelopeDigest({
        version: 2,
        cipher: 'AES-256-GCM',
        sequence: '7',
        nonce: 'AAAAAAAAAAAAAAAA',
        ciphertext: 'AAAAAAAAAAAAAAAAAAAAAAA'
      })
    ).resolves.toBe('1Qqsw4GLQ5GWPDbCYCTvMU3EzjgMeZ8i1cYeCqUA9kU');
  });

  it('extends the exact shared authenticated vault ancestry chain', async () => {
    await expect(
      accountVaultChainRoot(ZERO_VAULT_CHAIN, '1', 'AqLF_ssQCwyJ5hsba6wmVQPoqzkzlY0ev9Vh4Cr2e5Y')
    ).resolves.toBe('CAEkikOBbzZQ0cRXCHB9tNKIKtLoERyk6okiTTReHcU');
  });

  it('accepts only the exact pending envelope at the exact next revision', () => {
    const pending = {
      version: 2 as const,
      cipher: 'AES-256-GCM' as const,
      sequence: '7',
      nonce: 'AAAAAAAAAAAAAAAA',
      ciphertext: 'AAAAAAAAAAAAAAAAAAAAAAA'
    };
    expect(isExactAccountVaultWriteAcknowledgement('6', pending, '7', pending)).toBe(true);
    expect(isExactAccountVaultWriteAcknowledgement('5', pending, '7', pending)).toBe(false);
    expect(
      isExactAccountVaultWriteAcknowledgement('6', pending, '7', {
        ...pending,
        nonce: 'AQAAAAAAAAAAAAAA'
      })
    ).toBe(false);
  });

  it('round-trips a portable sequence above one without a local checkpoint', async () => {
    const key = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, [
      'encrypt',
      'decrypt'
    ]);
    const state = stateWith({});
    const envelope = await sealAccountVaultState(state, key);
    await expect(openAccountVaultState(state.accountRef, key, envelope)).resolves.toEqual(state);
  });
});
