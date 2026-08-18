import { describe, expect, it, vi } from 'vitest';

import { establishVaultFirstIdentity } from './identity-enrollment';

describe('vault-first E2EE identity enrollment', () => {
  it('persists recovered private state before making its revoked identity claimable', async () => {
    const events: string[] = [];
    let durableVault = false;
    let claimableIdentity = false;

    await establishVaultFirstIdentity({
      vaultAlreadyDurable: false,
      registrationRequired: true,
      persistVault: vi.fn(async () => {
        events.push('vault-put');
        durableVault = true;
      }),
      registerIdentity: vi.fn(async () => {
        expect(durableVault).toBe(true);
        events.push('device-register');
        claimableIdentity = true;
      })
    });

    expect(events).toEqual(['vault-put', 'device-register']);
    expect(claimableIdentity).toBe(true);
    expect(durableVault).toBe(true);
  });

  it('never invokes registration when the vault PUT fails', async () => {
    const registerIdentity = vi.fn(async () => undefined);
    await expect(
      establishVaultFirstIdentity({
        vaultAlreadyDurable: false,
        registrationRequired: true,
        persistVault: vi.fn(async () => {
          throw new Error('vault PUT failed');
        }),
        registerIdentity
      })
    ).rejects.toThrow('vault PUT failed');
    expect(registerIdentity).not.toHaveBeenCalled();
  });

  it('leaves a durable vault if the client crashes immediately after registration', async () => {
    let durableVault = false;
    let claimableIdentity = false;

    await expect(
      establishVaultFirstIdentity({
        vaultAlreadyDurable: false,
        registrationRequired: true,
        persistVault: vi.fn(async () => {
          durableVault = true;
        }),
        registerIdentity: vi.fn(async () => {
          expect(durableVault).toBe(true);
          claimableIdentity = true;
          throw new Error('simulated crash after registration');
        })
      })
    ).rejects.toThrow('simulated crash');

    expect(claimableIdentity).toBe(true);
    expect(durableVault).toBe(true);
  });

  it('uses an existing durable vault without rewriting it before registration', async () => {
    const persistVault = vi.fn(async () => undefined);
    const registerIdentity = vi.fn(async () => undefined);
    await establishVaultFirstIdentity({
      vaultAlreadyDurable: true,
      registrationRequired: true,
      persistVault,
      registerIdentity
    });
    expect(persistVault).not.toHaveBeenCalled();
    expect(registerIdentity).toHaveBeenCalledOnce();
  });
});
