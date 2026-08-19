import { api } from '$lib/api/client';
import type { UserSummary } from '$lib/chat/types';
import { saveAccountVaultKey } from '$lib/e2ee/store';
import {
  base64url,
  clearBytes,
  concatBytes,
  fromBase64url,
  randomBytes,
  utf8,
  type Bytes
} from '$lib/e2ee/encoding';
import { isNativeDesktop, storedNativeInstance } from '$lib/platform/native';

export const PASSWORD_KDF_VERSION = 2 as const;
export const PASSWORD_KDF_ALGORITHM = 'PBKDF2-SHA256' as const;
export const PASSWORD_KDF_ITERATIONS = 600_000;

export interface PasswordKdfContext {
  version: typeof PASSWORD_KDF_VERSION;
  algorithm: typeof PASSWORD_KDF_ALGORITHM;
  iterations: typeof PASSWORD_KDF_ITERATIONS;
  auth_salt: string;
  vault_salt: string;
}

export type PasswordKdfRegistration = PasswordKdfContext;

export interface PreparedPassword {
  authenticationSecret: string;
  vaultKey: CryptoKey;
  context: PasswordKdfContext;
}

function requireContext(value: PasswordKdfContext): PasswordKdfContext {
  const supported =
    value.version === PASSWORD_KDF_VERSION &&
    value.algorithm === PASSWORD_KDF_ALGORITHM &&
    value.iterations === PASSWORD_KDF_ITERATIONS;
  if (!supported) {
    throw new Error('This server uses an unsupported password protection scheme.');
  }
  validatePasswordSalt(value.auth_salt, 'authentication');
  validatePasswordSalt(value.vault_salt, 'encryption-vault');
  return value;
}

function validatePasswordSalt(value: string, label: string): void {
  let decoded: Bytes | null = null;
  try {
    decoded = fromBase64url(value, 16);
    if (decoded.length !== 16) throw new Error(`The server returned an invalid ${label} salt.`);
  } catch {
    throw new Error(`The server returned an invalid ${label} salt.`);
  } finally {
    if (decoded) clearBytes(decoded);
  }
}

function randomBase64url(length: number): string {
  const bytes = randomBytes(length);
  try {
    return base64url(bytes);
  } finally {
    clearBytes(bytes);
  }
}

export async function loadPasswordKdfContext(identifier: string): Promise<PasswordKdfContext> {
  return requireContext(
    await api<PasswordKdfContext>('/auth/key-derivation', {
      method: 'POST',
      body: JSON.stringify({ identifier })
    })
  );
}

async function passwordMaterial(password: string): Promise<CryptoKey> {
  if (!password) throw new Error('Enter your password.');
  const encoded = utf8(password);
  try {
    return await crypto.subtle.importKey('raw', encoded, 'PBKDF2', false, [
      'deriveBits',
      'deriveKey'
    ]);
  } finally {
    clearBytes(encoded);
  }
}

export function canonicalPasswordKdfInstance(): string {
  const selected = isNativeDesktop()
    ? storedNativeInstance()
    : typeof window !== 'undefined'
      ? window.location.hostname
      : '';
  if (!selected) throw new Error('Choose a Kaede instance before entering your password.');
  let parsed: URL;
  try {
    parsed = new URL(selected.includes('://') ? selected : `https://${selected}`);
  } catch {
    throw new Error('The selected Kaede instance is invalid.');
  }
  if (
    !parsed.hostname ||
    parsed.username ||
    parsed.password ||
    (parsed.pathname !== '' && parsed.pathname !== '/') ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error('The selected Kaede instance is invalid.');
  }
  return parsed.hostname.toLowerCase();
}

function purposeBoundSalt(salt: string, purpose: 'auth' | 'vault'): Bytes {
  const decodedSalt = fromBase64url(salt, 16);
  const prefix = utf8(`kaede-password-kdf-v2\0${purpose}\0${canonicalPasswordKdfInstance()}\0`);
  try {
    return concatBytes(prefix, decodedSalt);
  } finally {
    clearBytes(prefix);
    clearBytes(decodedSalt);
  }
}

async function authenticationSecret(material: CryptoKey, salt: string): Promise<string> {
  const boundSalt = purposeBoundSalt(salt, 'auth');
  const bits = new Uint8Array(
    await crypto.subtle.deriveBits(
      {
        name: 'PBKDF2',
        hash: 'SHA-256',
        salt: boundSalt,
        iterations: PASSWORD_KDF_ITERATIONS
      },
      material,
      256
    )
  );
  try {
    return base64url(bits);
  } finally {
    clearBytes(bits);
    clearBytes(boundSalt);
  }
}

async function encryptionVaultKey(material: CryptoKey, salt: string): Promise<CryptoKey> {
  const boundSalt = purposeBoundSalt(salt, 'vault');
  try {
    return await crypto.subtle.deriveKey(
      {
        name: 'PBKDF2',
        hash: 'SHA-256',
        salt: boundSalt,
        iterations: PASSWORD_KDF_ITERATIONS
      },
      material,
      { name: 'AES-GCM', length: 256 },
      false,
      ['encrypt', 'decrypt']
    );
  } finally {
    clearBytes(boundSalt);
  }
}

export async function preparePassword(
  password: string,
  context: PasswordKdfContext
): Promise<PreparedPassword> {
  const checked = requireContext(context);
  const material = await passwordMaterial(password);
  const [authSecret, vaultKey] = await Promise.all([
    authenticationSecret(material, checked.auth_salt),
    encryptionVaultKey(material, checked.vault_salt)
  ]);
  return { authenticationSecret: authSecret, vaultKey, context: checked };
}

export async function prepareRegistrationPassword(
  password: string
): Promise<PreparedPassword & { context: PasswordKdfRegistration }> {
  const context: PasswordKdfRegistration = {
    version: PASSWORD_KDF_VERSION,
    algorithm: PASSWORD_KDF_ALGORITHM,
    iterations: PASSWORD_KDF_ITERATIONS,
    auth_salt: randomBase64url(16),
    vault_salt: randomBase64url(16)
  };
  const prepared = await preparePassword(password, context);
  return { ...prepared, context };
}

export async function prepareResetPassword(password: string): Promise<{
  authenticationSecret: string;
  authKdf: Omit<PasswordKdfRegistration, 'vault_salt'>;
}> {
  const authSalt = randomBase64url(16);
  const material = await passwordMaterial(password);
  return {
    authenticationSecret: await authenticationSecret(material, authSalt),
    authKdf: {
      version: PASSWORD_KDF_VERSION,
      algorithm: PASSWORD_KDF_ALGORITHM,
      iterations: PASSWORD_KDF_ITERATIONS,
      auth_salt: authSalt
    }
  };
}

export async function savePreparedVaultKey(vaultKey: CryptoKey): Promise<string> {
  const user = await api<UserSummary>('/users/@me');
  const accountRef = `${user.id}@${user.origin_domain}`;
  await saveAccountVaultKey(accountRef, vaultKey);
  return accountRef;
}
