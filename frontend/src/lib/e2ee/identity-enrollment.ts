export interface VaultFirstIdentityEnrollment {
  vaultAlreadyDurable: boolean;
  registrationRequired: boolean;
  persistVault: () => Promise<void>;
  registerIdentity: () => Promise<void>;
}

/**
 * A public, claimable MLS identity must never exist without its matching
 * private state in the durable account vault. Keep this order shared by fresh
 * enrollment and same-identity recovery so neither path can regress alone.
 */
export async function establishVaultFirstIdentity(
  enrollment: VaultFirstIdentityEnrollment
): Promise<void> {
  if (!enrollment.vaultAlreadyDurable) await enrollment.persistVault();
  if (enrollment.registrationRequired) await enrollment.registerIdentity();
}
