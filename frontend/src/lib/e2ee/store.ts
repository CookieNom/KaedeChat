import {
  base64url,
  clearBytes,
  fromBase64url,
  ownedBytes,
  randomBytes,
  utf8,
  type Bytes
} from './encoding';

const DATABASE = 'kaede-e2ee-v1';
const STORE = 'sealed-device-state';
const MAX_STATE_BYTES = 32 * 1024 * 1024;
const RECOVERY_ITERATIONS = 600_000;

interface StoredRecord {
  accountRef: string;
  wrappingKey: CryptoKey;
  nonce: Bytes;
  ciphertext: ArrayBuffer;
  updatedAt: string;
}

export interface DeviceState {
  schema: 1;
  accountRef: string;
  deviceId: string;
  credential: string;
  mlsState: string;
  /** Ciphertext-keyed plaintext cache, itself sealed by the non-extractable device wrapping key. */
  messageCache?: Record<string, string>;
}

export interface RecoveryBundle {
  version: 1;
  kdf: 'PBKDF2-SHA256';
  iterations: number;
  salt: string;
  cipher: 'AES-256-GCM';
  nonce: string;
  ciphertext: string;
}

function requireBrowserCrypto(): void {
  if (typeof indexedDB === 'undefined' || !crypto?.subtle) {
    throw new Error('Secure E2EE storage is unavailable in this environment.');
  }
}

function openDatabase(): Promise<IDBDatabase> {
  requireBrowserCrypto();
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE, { keyPath: 'accountRef' });
      }
    };
    request.onerror = () => reject(request.error ?? new Error('Could not open E2EE storage.'));
    request.onsuccess = () => resolve(request.result);
  });
}

async function recordFor(accountRef: string): Promise<StoredRecord | undefined> {
  const database = await openDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const request = database.transaction(STORE, 'readonly').objectStore(STORE).get(accountRef);
      request.onerror = () => reject(request.error ?? new Error('Could not read E2EE storage.'));
      request.onsuccess = () => resolve(request.result as StoredRecord | undefined);
    });
  } finally {
    database.close();
  }
}

async function putRecord(record: StoredRecord): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(STORE, 'readwrite');
      transaction.objectStore(STORE).put(record);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(transaction.error ?? new Error('Could not save E2EE state.'));
      transaction.onabort = () =>
        reject(transaction.error ?? new Error('Could not save E2EE state.'));
    });
  } finally {
    database.close();
  }
}

function serialize(state: DeviceState): Bytes {
  const encoded = utf8(JSON.stringify(state));
  if (encoded.length > MAX_STATE_BYTES) throw new Error('E2EE state is too large.');
  return encoded;
}

function parseState(value: ArrayBuffer, accountRef: string): DeviceState {
  if (value.byteLength > MAX_STATE_BYTES) throw new Error('E2EE state is too large.');
  const parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(value)) as DeviceState;
  if (
    parsed.schema !== 1 ||
    parsed.accountRef !== accountRef ||
    !/^ked_[A-Za-z0-9_-]{43}$/u.test(parsed.deviceId) ||
    !parsed.credential ||
    !parsed.mlsState
  ) {
    throw new Error('Stored E2EE state is invalid.');
  }
  return parsed;
}

export async function saveDeviceState(state: DeviceState): Promise<void> {
  const prior = await recordFor(state.accountRef);
  const wrappingKey =
    prior?.wrappingKey ??
    (await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, [
      'encrypt',
      'decrypt'
    ]));
  const nonce = randomBytes(12);
  const plaintext = serialize(state);
  try {
    const ciphertext = await crypto.subtle.encrypt(
      {
        name: 'AES-GCM',
        iv: nonce,
        additionalData: utf8(`kaede e2ee state v1\0${state.accountRef}`)
      },
      wrappingKey,
      plaintext
    );
    await putRecord({
      accountRef: state.accountRef,
      wrappingKey,
      nonce,
      ciphertext,
      updatedAt: new Date().toISOString()
    });
  } finally {
    clearBytes(plaintext);
  }
}

export async function loadDeviceState(accountRef: string): Promise<DeviceState | null> {
  const record = await recordFor(accountRef);
  if (!record) return null;
  const plaintext = await crypto.subtle.decrypt(
    {
      name: 'AES-GCM',
      iv: record.nonce,
      additionalData: utf8(`kaede e2ee state v1\0${accountRef}`)
    },
    record.wrappingKey,
    record.ciphertext
  );
  try {
    return parseState(plaintext, accountRef);
  } finally {
    clearBytes(new Uint8Array(plaintext));
  }
}

export async function clearDeviceState(accountRef: string): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(STORE, 'readwrite');
      transaction.objectStore(STORE).delete(accountRef);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(transaction.error ?? new Error('Could not clear E2EE state.'));
    });
  } finally {
    database.close();
  }
}

async function recoveryKey(passphrase: string, salt: Bytes, usage: KeyUsage[]): Promise<CryptoKey> {
  if (passphrase.length < 12)
    throw new Error('Recovery passphrase must be at least 12 characters.');
  const material = await crypto.subtle.importKey('raw', utf8(passphrase), 'PBKDF2', false, [
    'deriveKey'
  ]);
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', hash: 'SHA-256', salt, iterations: RECOVERY_ITERATIONS },
    material,
    { name: 'AES-GCM', length: 256 },
    false,
    usage
  );
}

export async function exportRecoveryBundle(
  accountRef: string,
  passphrase: string
): Promise<RecoveryBundle> {
  const state = await loadDeviceState(accountRef);
  if (!state) throw new Error('This device has no encryption state to back up.');
  const salt = randomBytes(16);
  const nonce = randomBytes(12);
  const key = await recoveryKey(passphrase, salt, ['encrypt']);
  const plaintext = serialize(state);
  try {
    const ciphertext = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: utf8(`kaede recovery v1\0${accountRef}`) },
      key,
      plaintext
    );
    return {
      version: 1,
      kdf: 'PBKDF2-SHA256',
      iterations: RECOVERY_ITERATIONS,
      salt: base64url(salt),
      cipher: 'AES-256-GCM',
      nonce: base64url(nonce),
      ciphertext: base64url(ownedBytes(ciphertext))
    };
  } finally {
    clearBytes(plaintext);
  }
}

export async function importRecoveryBundle(
  accountRef: string,
  passphrase: string,
  bundle: RecoveryBundle
): Promise<DeviceState> {
  if (
    bundle.version !== 1 ||
    bundle.kdf !== 'PBKDF2-SHA256' ||
    bundle.iterations !== RECOVERY_ITERATIONS ||
    bundle.cipher !== 'AES-256-GCM'
  ) {
    throw new Error('Unsupported recovery bundle.');
  }
  const salt = fromBase64url(bundle.salt, 16);
  const nonce = fromBase64url(bundle.nonce, 12);
  if (salt.length !== 16 || nonce.length !== 12) throw new Error('Invalid recovery bundle.');
  const ciphertext = fromBase64url(bundle.ciphertext, MAX_STATE_BYTES + 16);
  const key = await recoveryKey(passphrase, salt, ['decrypt']);
  const plaintext = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: utf8(`kaede recovery v1\0${accountRef}`) },
    key,
    ciphertext
  );
  try {
    const state = parseState(plaintext, accountRef);
    await saveDeviceState(state);
    return state;
  } finally {
    clearBytes(new Uint8Array(plaintext));
  }
}
