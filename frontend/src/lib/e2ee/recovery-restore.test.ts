import { describe, expect, it, vi } from 'vitest';

import type { UserSummary } from '$lib/chat/types';

import {
  isCanonicalRecoveryAuthorization,
  recoveryRegistrationFields,
  recoveryRestoreAvailability,
  restoreRecoveredIdentity
} from './recovery-restore';
import { ZERO_VAULT_CHAIN, type DeviceState } from './store';

const user: UserSummary = {
  id: '17',
  origin_domain: 'example.test',
  username: 'maple',
  display_name: null,
  avatar_hash: null,
  handle: 'maple@example.test'
};

const recovered: DeviceState = {
  schema: 2,
  accountRef: '17@example.test',
  deviceId: `ked_${'A'.repeat(43)}`,
  credential: 'recovered-credential',
  mlsState: 'recovered-mls-state',
  vaultSequence: '1',
  vaultParentChain: ZERO_VAULT_CHAIN,
  messageCache: {},
  controlCursors: {},
  pendingRoomOperations: {}
};

describe('E2EE recovery restore', () => {
  it('accepts only the canonical one-time reset authorization shape', () => {
    expect(isCanonicalRecoveryAuthorization(`ker_${'A'.repeat(43)}`)).toBe(true);
    expect(isCanonicalRecoveryAuthorization(`ker_${'A'.repeat(42)}B`)).toBe(false);
    expect(isCanonicalRecoveryAuthorization('not-a-recovery-authorization')).toBe(false);
  });

  it('sends the bearer only for recovered registration, never fresh enrollment', () => {
    const authorization = `ker_${'A'.repeat(43)}`;
    expect(recoveryRegistrationFields()).toEqual({});
    expect(recoveryRegistrationFields(authorization)).toEqual({
      recovery_authorization: authorization
    });
  });

  it('keeps restore enabled after login auto-enrolls a replacement identity', () => {
    expect(recoveryRestoreAvailability(false, 'twelve-chars', 'ked_new_identity')).toEqual({
      enabled: true,
      replacesActiveIdentity: true
    });
  });

  it('quiesces and replaces an auto-enrolled identity using only the reset authorization', async () => {
    const events: string[] = [];
    const recoveredClient = { deviceId: recovered.deviceId };
    const resetClient = vi.fn(async () => {
      events.push('reset-client');
    });
    const authorizeReset = vi.fn(async () => {
      events.push('authenticated-reset');
      return `ker_${'A'.repeat(43)}`;
    });
    const clearLocalState = vi.fn(async (accountRef: string) => {
      events.push(`clear:${accountRef}`);
    });
    const saveRecoveredState = vi.fn(async (state: DeviceState) => {
      events.push(`save:${state.deviceId}`);
    });
    const initializeRecoveredIdentity = vi.fn(
      async (_user: UserSummary, recoveryAuthorization: string) => {
        events.push(`initialize:${recoveryAuthorization}`);
        return recoveredClient;
      }
    );

    await expect(
      restoreRecoveredIdentity(user, recovered, {
        resetClient,
        authorizeReset,
        clearLocalState,
        saveRecoveredState,
        initializeRecoveredIdentity
      })
    ).resolves.toBe(recoveredClient);
    expect(events).toEqual([
      'reset-client',
      'authenticated-reset',
      'clear:17@example.test',
      `save:${recovered.deviceId}`,
      `initialize:ker_${'A'.repeat(43)}`
    ]);
  });

  it('does not clear local state when the authenticated reset fails', async () => {
    const clearLocalState = vi.fn(async () => undefined);
    await expect(
      restoreRecoveredIdentity(user, recovered, {
        resetClient: vi.fn(async () => undefined),
        authorizeReset: vi.fn(async () => {
          throw new Error('reset failed');
        }),
        clearLocalState,
        saveRecoveredState: vi.fn(async () => undefined),
        initializeRecoveredIdentity: vi.fn(async () => ({ deviceId: 'unused' }))
      })
    ).rejects.toThrow('reset failed');
    expect(clearLocalState).not.toHaveBeenCalled();
  });

  it('does not clear local state for a malformed reset authorization', async () => {
    const clearLocalState = vi.fn(async () => undefined);
    await expect(
      restoreRecoveredIdentity(user, recovered, {
        resetClient: vi.fn(async () => undefined),
        authorizeReset: vi.fn(async () => 'ker_not-canonical'),
        clearLocalState,
        saveRecoveredState: vi.fn(async () => undefined),
        initializeRecoveredIdentity: vi.fn(async () => ({ deviceId: 'unused' }))
      })
    ).rejects.toThrow('reset response was invalid');
    expect(clearLocalState).not.toHaveBeenCalled();
  });

  it('rejects a different-account backup before resetting the active identity', async () => {
    const resetClient = vi.fn(async () => undefined);
    await expect(
      restoreRecoveredIdentity(
        user,
        { ...recovered, accountRef: '19@example.test' },
        {
          resetClient,
          authorizeReset: vi.fn(async () => `ker_${'A'.repeat(43)}`),
          clearLocalState: vi.fn(async () => undefined),
          saveRecoveredState: vi.fn(async () => undefined),
          initializeRecoveredIdentity: vi.fn(async () => ({ deviceId: 'unused' }))
        }
      )
    ).rejects.toThrow('different account');
    expect(resetClient).not.toHaveBeenCalled();
  });
});
