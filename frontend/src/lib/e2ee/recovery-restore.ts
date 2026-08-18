import type { UserSummary } from '$lib/chat/types';

import type { DeviceState } from './store';
import { clearBytes, fromBase64url } from './encoding';

export interface RecoveryRestoreAvailability {
  enabled: boolean;
  replacesActiveIdentity: boolean;
}

export interface RecoveryRestoreOperations<Client> {
  resetClient: () => Promise<void>;
  authorizeReset: () => Promise<string>;
  clearLocalState: (accountRef: string) => Promise<void>;
  saveRecoveredState: (state: DeviceState) => Promise<void>;
  initializeRecoveredIdentity: (
    user: UserSummary,
    recoveryAuthorization: string
  ) => Promise<Client>;
}

export function isCanonicalRecoveryAuthorization(value: unknown): value is string {
  if (typeof value !== 'string' || !/^ker_[A-Za-z0-9_-]{43}$/u.test(value)) return false;
  let decoded: Uint8Array | null = null;
  try {
    decoded = fromBase64url(value.slice(4), 32);
    return decoded.length === 32;
  } catch {
    return false;
  } finally {
    if (decoded) clearBytes(decoded);
  }
}

export function recoveryRegistrationFields(recoveryAuthorization?: string): {
  recovery_authorization?: string;
} {
  if (recoveryAuthorization === undefined) return {};
  if (!isCanonicalRecoveryAuthorization(recoveryAuthorization)) {
    throw new Error('The encryption-recovery authorization is invalid.');
  }
  return { recovery_authorization: recoveryAuthorization };
}

export function recoveryRestoreAvailability(
  busy: boolean,
  passphrase: string,
  currentDeviceId: string
): RecoveryRestoreAvailability {
  return {
    enabled: !busy && passphrase.length >= 12,
    replacesActiveIdentity: currentDeviceId.length > 0
  };
}

/**
 * Quiesce the active MLS client before authorizing the destructive server
 * reset. This prevents the auto-enrolled identity from racing the recovered
 * state back into the account vault while it is being replaced.
 */
export async function restoreRecoveredIdentity<Client>(
  user: UserSummary,
  recovered: DeviceState,
  operations: RecoveryRestoreOperations<Client>
): Promise<Client> {
  const accountRef = `${user.id}@${user.origin_domain}`;
  if (recovered.accountRef !== accountRef) {
    throw new Error('The recovery backup belongs to a different account.');
  }
  await operations.resetClient();
  const recoveryAuthorization = await operations.authorizeReset();
  if (!isCanonicalRecoveryAuthorization(recoveryAuthorization)) {
    throw new Error('The encryption-reset response was invalid. Local keys were not changed.');
  }
  await operations.clearLocalState(accountRef);
  await operations.saveRecoveredState(recovered);
  return operations.initializeRecoveredIdentity(user, recoveryAuthorization);
}
