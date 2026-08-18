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
    const entries: Array<[string, CachedPlaintextMessage]> = [];
    for (let index = 0; index < MAX_MESSAGE_CACHE_ENTRIES + 100; index += 1) {
      entries.push([
        base64url(utf8(`ciphertext-${index}`)),
        {
          plaintext: `${index}:${'x'.repeat(5_000)}`,
          authorRef: '1@example.test',
          messageRef: `${index}@example.test`
        }
      ]);
    }

    const compacted = compactDeviceState(stateWith(Object.fromEntries(entries)));
    const cache = compacted.messageCache ?? {};
    expect(Object.keys(cache).length).toBeLessThanOrEqual(MAX_MESSAGE_CACHE_ENTRIES);
    expect(new TextEncoder().encode(JSON.stringify(cache)).length).toBeLessThanOrEqual(
      MAX_MESSAGE_CACHE_BYTES
    );
    expect(cache[entries.at(-1)![0]]?.messageRef).toBe(
      `${MAX_MESSAGE_CACHE_ENTRIES + 99}@example.test`
    );
    expect(cache[entries[0][0]]).toBeUndefined();
  });

  it('rejects unbound or malformed cache values', () => {
    const ciphertext = base64url(utf8('ciphertext'));
    expect(() =>
      compactDeviceState(
        stateWith({
          [ciphertext]: {
            plaintext: '{}',
            authorRef: 'not-an-account',
            messageRef: null
          }
        })
      )
    ).toThrow(/message cache/u);
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
