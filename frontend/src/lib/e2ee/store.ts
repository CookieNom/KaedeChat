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
const VAULT_KEY_STORE = 'account-vault-keys';
const CHECKPOINT_STORE = 'account-vault-checkpoints';
const DATABASE_VERSION = 4;
const MAX_STATE_BYTES = 32 * 1024 * 1024;
const TARGET_STATE_BYTES = 31 * 1024 * 1024;
// A crash journal contains both the portable plaintext state and its base64
// sealed envelope. Keep the local-only wrapper large enough for that bounded
// duplication without increasing the remotely accepted vault size.
const MAX_LOCAL_RECORD_BYTES = 80 * 1024 * 1024;
export const MAX_MESSAGE_CACHE_ENTRIES = 2_000;
export const MAX_MESSAGE_CACHE_BYTES = 8 * 1024 * 1024;
const RECOVERY_ITERATIONS = 600_000;
export const ZERO_VAULT_CHAIN = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';

interface StoredRecord {
  accountRef: string;
  wrappingKey: CryptoKey;
  nonce: Bytes;
  ciphertext: ArrayBuffer;
  updatedAt: string;
}

interface StoredVaultKey {
  accountRef: string;
  key: CryptoKey;
  updatedAt: string;
}

interface StoredVaultCheckpoint {
  schema: 1;
  accountHash: string;
  revision: string;
  digest: string;
  chainRoot: string;
  updatedAt: string;
}

export interface VaultCheckpoint {
  revision: string;
  digest: string;
  chainRoot: string;
}

export interface DeviceState {
  schema: 2;
  accountRef: string;
  deviceId: string;
  credential: string;
  mlsState: string;
  /** Monotonic sequence authenticated inside the portable vault ciphertext. */
  vaultSequence: string;
  /** Compact authenticated commitment to every opaque vault through sequence - 1. */
  vaultParentChain: string;
  /** Ciphertext-keyed plaintext cache, sealed and bound to its first canonical server projection. */
  messageCache?: Record<string, CachedPlaintextMessage>;
  /** Last fully applied immutable MLS control record for each canonical channel reference. */
  controlCursors?: Record<string, string>;
  /** Crash-recoverable, idempotent room activations staged before authority commit. */
  pendingRoomOperations?: Record<string, PendingRoomOperation>;
  /** Local-only crash journal, sealed by this device's non-extractable wrapping key. */
  pendingVaultBaseRevision?: string;
  /** Local-only exact envelope paired with pendingVaultBaseRevision. */
  pendingVaultEnvelope?: AccountVaultEnvelope;
  /** Local-only authenticated rollback high-water mark. */
  confirmedVaultRevision?: string;
  /** Local-only digest paired with confirmedVaultRevision. */
  confirmedVaultDigest?: string;
  /** Local-only ancestry root paired with confirmedVaultRevision. */
  confirmedVaultChainRoot?: string;
}

export interface CachedPlaintextMessage {
  plaintext: string;
  authorRef: string;
  messageRef: string | null;
}

export interface PendingRoomOperation {
  version: 1;
  operationId: string;
  channelRef: string;
  kind: 'activate' | 'rekey';
  phase: 'proposing' | 'activating';
  policyGeneration?: string;
  groupId?: string;
  commit?: string;
  welcome?: string;
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

export interface AccountVaultEnvelope {
  version: 2;
  cipher: 'AES-256-GCM';
  sequence: string;
  nonce: string;
  ciphertext: string;
}

export interface PendingAccountVaultWrite {
  baseRevision: string;
  envelope: AccountVaultEnvelope;
  state: DeviceState;
}

const MAX_VAULT_SEQUENCE = 9_223_372_036_854_775_807n;

export function vaultSequence(value: unknown, allowZero = false): string {
  if (
    typeof value !== 'string' ||
    !(allowZero ? /^(?:0|[1-9][0-9]{0,18})$/u : /^[1-9][0-9]{0,18}$/u).test(value)
  ) {
    throw new Error('The encryption-vault sequence is invalid.');
  }
  const parsed = BigInt(value);
  if (parsed > MAX_VAULT_SEQUENCE) {
    throw new Error('The encryption-vault sequence is invalid.');
  }
  return value;
}

export function nextVaultSequence(value: string): string {
  const parsed = BigInt(vaultSequence(value, true));
  if (parsed >= MAX_VAULT_SEQUENCE) {
    throw new Error('The encryption-vault sequence is invalid.');
  }
  return (parsed + 1n).toString();
}

export function isExactAccountVaultWriteAcknowledgement(
  baseRevision: string,
  pending: AccountVaultEnvelope,
  acknowledgedRevision: string,
  acknowledged: AccountVaultEnvelope
): boolean {
  validateAccountVaultEnvelope(pending);
  validateAccountVaultEnvelope(acknowledged);
  return (
    acknowledgedRevision === nextVaultSequence(baseRevision) &&
    pending.version === acknowledged.version &&
    pending.cipher === acknowledged.cipher &&
    pending.sequence === acknowledged.sequence &&
    pending.nonce === acknowledged.nonce &&
    pending.ciphertext === acknowledged.ciphertext
  );
}

function validateVaultDigest(value: unknown): string {
  if (typeof value !== 'string') throw new Error('The encryption-vault digest is invalid.');
  const decoded = fromBase64url(value, 32);
  try {
    if (decoded.length !== 32) throw new Error('The encryption-vault digest is invalid.');
  } finally {
    clearBytes(decoded);
  }
  return value;
}

function validateVaultChain(value: unknown): string {
  if (typeof value !== 'string') throw new Error('The encryption-vault chain is invalid.');
  const decoded = fromBase64url(value, 32);
  try {
    if (decoded.length !== 32) throw new Error('The encryption-vault chain is invalid.');
  } finally {
    clearBytes(decoded);
  }
  return value;
}

function requireBrowserCrypto(): void {
  if (typeof indexedDB === 'undefined' || !crypto?.subtle) {
    throw new Error('Secure E2EE storage is unavailable in this environment.');
  }
}

function openDatabase(): Promise<IDBDatabase> {
  requireBrowserCrypto();
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE, { keyPath: 'accountRef' });
      }
      if (!request.result.objectStoreNames.contains(VAULT_KEY_STORE)) {
        request.result.createObjectStore(VAULT_KEY_STORE, { keyPath: 'accountRef' });
      }
      if (!request.result.objectStoreNames.contains(CHECKPOINT_STORE)) {
        request.result.createObjectStore(CHECKPOINT_STORE, { keyPath: 'accountHash' });
      }
    };
    request.onerror = () => reject(request.error ?? new Error('Could not open E2EE storage.'));
    request.onsuccess = () => resolve(request.result);
  });
}

export async function saveAccountVaultKey(accountRef: string, key: CryptoKey): Promise<void> {
  requireBrowserCrypto();
  if (!accountRef || key.extractable || key.algorithm.name !== 'AES-GCM') {
    throw new Error('The encryption-vault key is invalid.');
  }
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(VAULT_KEY_STORE, 'readwrite');
      transaction.objectStore(VAULT_KEY_STORE).put({
        accountRef,
        key,
        updatedAt: new Date().toISOString()
      } satisfies StoredVaultKey);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(transaction.error ?? new Error('Could not save the encryption-vault key.'));
      transaction.onabort = () =>
        reject(transaction.error ?? new Error('Could not save the encryption-vault key.'));
    });
  } finally {
    database.close();
  }
}

export async function loadAccountVaultKey(accountRef: string): Promise<CryptoKey | null> {
  const database = await openDatabase();
  try {
    const record = await new Promise<StoredVaultKey | undefined>((resolve, reject) => {
      const request = database
        .transaction(VAULT_KEY_STORE, 'readonly')
        .objectStore(VAULT_KEY_STORE)
        .get(accountRef);
      request.onerror = () =>
        reject(request.error ?? new Error('Could not read the encryption-vault key.'));
      request.onsuccess = () => resolve(request.result as StoredVaultKey | undefined);
    });
    return record?.key ?? null;
  } finally {
    database.close();
  }
}

export async function clearAccountVaultKey(accountRef: string): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(VAULT_KEY_STORE, 'readwrite');
      transaction.objectStore(VAULT_KEY_STORE).delete(accountRef);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(transaction.error ?? new Error('Could not clear the encryption-vault key.'));
    });
  } finally {
    database.close();
  }
}

async function checkpointAccountHash(accountRef: string): Promise<string> {
  requireBrowserCrypto();
  const encoded = utf8(accountRef);
  let digest: Uint8Array | null = null;
  try {
    digest = new Uint8Array(await crypto.subtle.digest('SHA-256', encoded));
    return base64url(digest);
  } finally {
    clearBytes(encoded);
    if (digest) clearBytes(digest);
  }
}

function validateVaultCheckpoint(record: StoredVaultCheckpoint): VaultCheckpoint {
  if (
    !record ||
    Object.keys(record).sort().join('\0') !==
      ['accountHash', 'chainRoot', 'digest', 'revision', 'schema', 'updatedAt'].join('\0') ||
    record.schema !== 1 ||
    vaultSequence(record.revision) !== record.revision ||
    typeof record.updatedAt !== 'string'
  ) {
    throw new Error('The encryption-vault checkpoint is invalid.');
  }
  return {
    revision: record.revision,
    digest: validateVaultDigest(record.digest),
    chainRoot: validateVaultChain(record.chainRoot)
  };
}

function containsAsciiControl(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code <= 0x1f || code === 0x7f) return true;
  }
  return false;
}

/** This non-secret checkpoint deliberately survives logout and session expiry. */
export async function loadVaultCheckpoint(accountRef: string): Promise<VaultCheckpoint | null> {
  const accountHash = await checkpointAccountHash(accountRef);
  const database = await openDatabase();
  try {
    const record = await new Promise<StoredVaultCheckpoint | undefined>((resolve, reject) => {
      const request = database
        .transaction(CHECKPOINT_STORE, 'readonly')
        .objectStore(CHECKPOINT_STORE)
        .get(accountHash);
      request.onerror = () =>
        reject(request.error ?? new Error('Could not read the encryption-vault checkpoint.'));
      request.onsuccess = () => resolve(request.result as StoredVaultCheckpoint | undefined);
    });
    if (!record) return null;
    if (record.accountHash !== accountHash) {
      throw new Error('The encryption-vault checkpoint belongs to another account.');
    }
    return validateVaultCheckpoint(record);
  } finally {
    database.close();
  }
}

export async function saveVaultCheckpoint(
  accountRef: string,
  checkpoint: VaultCheckpoint
): Promise<void> {
  const accountHash = await checkpointAccountHash(accountRef);
  const revision = vaultSequence(checkpoint.revision);
  const digest = validateVaultDigest(checkpoint.digest);
  const chainRoot = validateVaultChain(checkpoint.chainRoot);
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(CHECKPOINT_STORE, 'readwrite');
      const store = transaction.objectStore(CHECKPOINT_STORE);
      const request = store.get(accountHash);
      request.onerror = () => transaction.abort();
      request.onsuccess = () => {
        try {
          const current = request.result as StoredVaultCheckpoint | undefined;
          if (current) {
            const validated = validateVaultCheckpoint(current);
            const comparison = BigInt(revision) - BigInt(validated.revision);
            if (
              comparison < 0n ||
              (comparison === 0n &&
                (digest !== validated.digest || chainRoot !== validated.chainRoot))
            ) {
              throw new Error('Refusing to lower or replace the encryption-vault checkpoint.');
            }
          }
          store.put({
            schema: 1,
            accountHash,
            revision,
            digest,
            chainRoot,
            updatedAt: new Date().toISOString()
          } satisfies StoredVaultCheckpoint);
        } catch {
          transaction.abort();
        }
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(transaction.error ?? new Error('Could not save the encryption-vault checkpoint.'));
      transaction.onabort = () =>
        reject(transaction.error ?? new Error('Could not save the encryption-vault checkpoint.'));
    });
  } finally {
    database.close();
  }
}

/** Call only after an authenticated password or encryption reset succeeds. */
export async function clearVaultCheckpoint(accountRef: string): Promise<void> {
  const accountHash = await checkpointAccountHash(accountRef);
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(CHECKPOINT_STORE, 'readwrite');
      transaction.objectStore(CHECKPOINT_STORE).delete(accountHash);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(transaction.error ?? new Error('Could not clear the encryption-vault checkpoint.'));
      transaction.onabort = () =>
        reject(transaction.error ?? new Error('Could not clear the encryption-vault checkpoint.'));
    });
  } finally {
    database.close();
  }
}

export async function clearAllLocalE2EEState(): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction([STORE, VAULT_KEY_STORE], 'readwrite');
      transaction.objectStore(STORE).clear();
      transaction.objectStore(VAULT_KEY_STORE).clear();
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(transaction.error ?? new Error('Could not clear local encryption state.'));
      transaction.onabort = () =>
        reject(transaction.error ?? new Error('Could not clear local encryption state.'));
    });
  } finally {
    database.close();
  }
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

function portableState(state: DeviceState): DeviceState {
  return {
    schema: 2,
    accountRef: state.accountRef,
    deviceId: state.deviceId,
    credential: state.credential,
    mlsState: state.mlsState,
    vaultSequence: state.vaultSequence,
    vaultParentChain: state.vaultParentChain,
    messageCache: state.messageCache ?? {},
    controlCursors: state.controlCursors ?? {},
    pendingRoomOperations: state.pendingRoomOperations ?? {}
  };
}

function serializePortable(state: DeviceState): Bytes {
  const encoded = utf8(JSON.stringify(portableState(state)));
  if (encoded.length > MAX_STATE_BYTES) {
    clearBytes(encoded);
    throw new Error('E2EE state is too large.');
  }
  return encoded;
}

function serializeLocal(state: DeviceState): Bytes {
  const encoded = utf8(JSON.stringify(state));
  if (encoded.length > MAX_LOCAL_RECORD_BYTES) {
    clearBytes(encoded);
    throw new Error('Local E2EE recovery state is too large.');
  }
  return encoded;
}

function encodedLength(value: string): number {
  const encoded = utf8(value);
  try {
    return encoded.length;
  } finally {
    clearBytes(encoded);
  }
}

function canonicalEntityRef(value: string): boolean {
  const separator = value.lastIndexOf('@');
  if (separator <= 0) return false;
  const id = value.slice(0, separator);
  const domain = value.slice(separator + 1);
  return (
    /^(?:0|[1-9][0-9]{0,18})$/u.test(id) &&
    BigInt(id) <= 9_223_372_036_854_775_807n &&
    /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/u.test(
      domain
    )
  );
}

function cacheEntryBytes(
  ciphertext: string,
  entry: CachedPlaintextMessage,
  first: boolean
): number {
  return (
    (first ? 0 : 1) +
    encodedLength(JSON.stringify(ciphertext)) +
    1 +
    encodedLength(JSON.stringify(entry))
  );
}

function validateMessageCache(value: Record<string, CachedPlaintextMessage> | undefined): void {
  if (value === undefined) return;
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Stored E2EE message cache is invalid.');
  }
  const entries = Object.entries(value);
  if (entries.length > MAX_MESSAGE_CACHE_ENTRIES) {
    throw new Error('Stored E2EE message cache is invalid.');
  }
  let byteLength = 2;
  for (const [ciphertext, entry] of entries) {
    if (
      !entry ||
      typeof entry !== 'object' ||
      Array.isArray(entry) ||
      Object.keys(entry).sort().join('\0') !== 'authorRef\0messageRef\0plaintext' ||
      typeof entry.plaintext !== 'string' ||
      typeof entry.authorRef !== 'string' ||
      !canonicalEntityRef(entry.authorRef) ||
      (entry.messageRef !== null &&
        (typeof entry.messageRef !== 'string' || !canonicalEntityRef(entry.messageRef)))
    ) {
      throw new Error('Stored E2EE message cache is invalid.');
    }
    const decoded = fromBase64url(ciphertext, 60 * 1024);
    try {
      if (!decoded.length) throw new Error('Stored E2EE message cache is invalid.');
    } finally {
      clearBytes(decoded);
    }
    byteLength += cacheEntryBytes(ciphertext, entry, byteLength === 2);
    if (byteLength > MAX_MESSAGE_CACHE_BYTES) {
      throw new Error('Stored E2EE message cache is invalid.');
    }
  }
}

export function compactDeviceState(state: DeviceState): DeviceState {
  const source = Object.entries(state.messageCache ?? {}).slice(-MAX_MESSAGE_CACHE_ENTRIES);
  const selected: Array<[string, CachedPlaintextMessage]> = [];
  let cacheBytes = 2;
  for (const entry of [...source].reverse()) {
    const nextBytes = cacheEntryBytes(entry[0], entry[1], selected.length === 0);
    if (cacheBytes + nextBytes > MAX_MESSAGE_CACHE_BYTES) continue;
    selected.push(entry);
    cacheBytes += nextBytes;
  }
  selected.reverse();

  const candidate = (): DeviceState => ({
    ...state,
    messageCache: Object.fromEntries(selected)
  });
  let compacted = candidate();
  let totalBytes = encodedLength(JSON.stringify(portableState(compacted)));
  while (totalBytes > TARGET_STATE_BYTES && selected.length) {
    let removedBytes = 0;
    const required = totalBytes - TARGET_STATE_BYTES;
    do {
      const [ciphertext, entry] = selected.shift()!;
      removedBytes += cacheEntryBytes(ciphertext, entry, false);
    } while (selected.length && removedBytes < required);
    compacted = candidate();
    totalBytes = encodedLength(JSON.stringify(portableState(compacted)));
  }
  if (totalBytes > MAX_STATE_BYTES) throw new Error('E2EE state is too large.');
  validateMessageCache(compacted.messageCache);
  return compacted;
}

function parseState(value: ArrayBuffer, accountRef: string, local = false): DeviceState {
  if (value.byteLength > (local ? MAX_LOCAL_RECORD_BYTES : MAX_STATE_BYTES)) {
    throw new Error('E2EE state is too large.');
  }
  const parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(value)) as DeviceState;
  const portableKeys = [
    'accountRef',
    'controlCursors',
    'credential',
    'deviceId',
    'messageCache',
    'mlsState',
    'pendingRoomOperations',
    'schema',
    'vaultParentChain',
    'vaultSequence'
  ];
  const localKeys = [
    ...portableKeys,
    ...(parsed.pendingVaultBaseRevision === undefined
      ? []
      : ['pendingVaultBaseRevision', 'pendingVaultEnvelope']),
    ...(parsed.confirmedVaultRevision === undefined
      ? []
      : ['confirmedVaultChainRoot', 'confirmedVaultDigest', 'confirmedVaultRevision'])
  ];
  if (
    Object.keys(parsed).sort().join('\0') !==
      (local ? localKeys : portableKeys).sort().join('\0') ||
    parsed.schema !== 2 ||
    parsed.accountRef !== accountRef ||
    !/^ked_[A-Za-z0-9_-]{43}$/u.test(parsed.deviceId) ||
    !parsed.credential ||
    !parsed.mlsState ||
    vaultSequence(parsed.vaultSequence) !== parsed.vaultSequence ||
    validateVaultChain(parsed.vaultParentChain) !== parsed.vaultParentChain
  ) {
    throw new Error('Stored E2EE state is invalid.');
  }
  validateMessageCache(parsed.messageCache);
  if (
    parsed.controlCursors !== undefined &&
    (parsed.controlCursors === null ||
      typeof parsed.controlCursors !== 'object' ||
      Array.isArray(parsed.controlCursors) ||
      Object.keys(parsed.controlCursors).length > 6_400 ||
      Object.entries(parsed.controlCursors).some(
        ([channelRef, cursor]) =>
          !channelRef ||
          channelRef.length > 512 ||
          typeof cursor !== 'string' ||
          cursor.length > 512 ||
          containsAsciiControl(channelRef) ||
          containsAsciiControl(cursor)
      ))
  ) {
    throw new Error('Stored E2EE control cursors are invalid.');
  }
  validatePendingRoomOperations(parsed.pendingRoomOperations);
  if (!local) return parsed;

  const hasPendingBase = parsed.pendingVaultBaseRevision !== undefined;
  const hasPendingEnvelope = parsed.pendingVaultEnvelope !== undefined;
  const hasConfirmedRevision = parsed.confirmedVaultRevision !== undefined;
  const hasConfirmedDigest = parsed.confirmedVaultDigest !== undefined;
  const hasConfirmedChain = parsed.confirmedVaultChainRoot !== undefined;
  if (
    hasPendingBase !== hasPendingEnvelope ||
    hasConfirmedRevision !== hasConfirmedDigest ||
    hasConfirmedRevision !== hasConfirmedChain
  ) {
    throw new Error('The local encryption-vault journal is invalid.');
  }
  const confirmedRevision = hasConfirmedRevision
    ? vaultSequence(parsed.confirmedVaultRevision)
    : null;
  if (hasConfirmedDigest) validateVaultDigest(parsed.confirmedVaultDigest);
  if (hasConfirmedChain) validateVaultChain(parsed.confirmedVaultChainRoot);
  if (hasPendingBase && parsed.pendingVaultEnvelope) {
    const baseRevision = vaultSequence(parsed.pendingVaultBaseRevision, true);
    validateAccountVaultEnvelope(parsed.pendingVaultEnvelope);
    const next = nextVaultSequence(baseRevision);
    if (
      parsed.vaultSequence !== next ||
      parsed.pendingVaultEnvelope.sequence !== next ||
      (confirmedRevision === null ? baseRevision !== '0' : confirmedRevision !== baseRevision) ||
      parsed.vaultParentChain !==
        (confirmedRevision === null ? ZERO_VAULT_CHAIN : parsed.confirmedVaultChainRoot)
    ) {
      throw new Error('The local encryption-vault journal is invalid.');
    }
  } else if (confirmedRevision !== null && confirmedRevision !== parsed.vaultSequence) {
    throw new Error('The local encryption-vault high-water mark is invalid.');
  }
  return parsed;
}

function validatePendingRoomOperations(
  value: Record<string, PendingRoomOperation> | undefined
): void {
  if (value === undefined) return;
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Stored E2EE room operations are invalid.');
  }
  const entries = Object.entries(value);
  if (entries.length > 32) throw new Error('Stored E2EE room operations are invalid.');
  for (const [operationId, operation] of entries) {
    if (
      !operation ||
      typeof operation !== 'object' ||
      Array.isArray(operation) ||
      !/^keo_[A-Za-z0-9_-]{43}$/u.test(operationId) ||
      operation.operationId !== operationId ||
      operation.version !== 1 ||
      !operation.channelRef ||
      operation.channelRef.length > 512 ||
      containsAsciiControl(operation.channelRef) ||
      !['activate', 'rekey'].includes(operation.kind) ||
      !['proposing', 'activating'].includes(operation.phase)
    ) {
      throw new Error('Stored E2EE room operations are invalid.');
    }
    const expectedKeys =
      operation.phase === 'proposing'
        ? ['channelRef', 'kind', 'operationId', 'phase', 'version']
        : [
            'channelRef',
            'commit',
            'groupId',
            'kind',
            'operationId',
            'phase',
            'policyGeneration',
            'version',
            'welcome'
          ];
    if (Object.keys(operation).sort().join('\0') !== expectedKeys.sort().join('\0')) {
      throw new Error('Stored E2EE room operations are invalid.');
    }
    if (operation.phase === 'activating') {
      if (
        typeof operation.policyGeneration !== 'string' ||
        !/^[1-9][0-9]{0,18}$/u.test(operation.policyGeneration) ||
        typeof operation.groupId !== 'string' ||
        typeof operation.commit !== 'string' ||
        typeof operation.welcome !== 'string'
      ) {
        throw new Error('Stored E2EE room operations are invalid.');
      }
      const groupId = fromBase64url(operation.groupId, 32);
      const commit = fromBase64url(operation.commit, 64 * 1024);
      const welcome = fromBase64url(operation.welcome, 64 * 1024);
      try {
        if (groupId.length !== 32 || !commit.length || !welcome.length) {
          throw new Error('Stored E2EE room operations are invalid.');
        }
      } finally {
        clearBytes(groupId);
        clearBytes(commit);
        clearBytes(welcome);
      }
    }
  }
}

async function saveStateRecord(state: DeviceState): Promise<void> {
  const prior = await recordFor(state.accountRef);
  const wrappingKey =
    prior?.wrappingKey ??
    (await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, [
      'encrypt',
      'decrypt'
    ]));
  const nonce = randomBytes(12);
  const plaintext = serializeLocal(state);
  const aad = utf8(`kaede e2ee local state v2\0${state.accountRef}`);
  try {
    const ciphertext = await crypto.subtle.encrypt(
      {
        name: 'AES-GCM',
        iv: nonce,
        additionalData: aad
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
    clearBytes(aad);
    clearBytes(nonce);
  }
}

async function openStoredState(record: StoredRecord, accountRef: string): Promise<DeviceState> {
  const aad = utf8(`kaede e2ee local state v2\0${accountRef}`);
  let plaintext: ArrayBuffer | null = null;
  try {
    plaintext = await crypto.subtle.decrypt(
      {
        name: 'AES-GCM',
        iv: record.nonce,
        additionalData: aad
      },
      record.wrappingKey,
      record.ciphertext
    );
    return parseState(plaintext, accountRef, true);
  } finally {
    if (plaintext) clearBytes(new Uint8Array(plaintext));
    clearBytes(aad);
  }
}

export async function saveDeviceState(state: DeviceState): Promise<void> {
  await saveStateRecord(state);
}

export async function savePendingAccountVaultWrite(
  state: DeviceState,
  baseRevision: string,
  envelope: AccountVaultEnvelope
): Promise<void> {
  if (!/^(?:0|[1-9][0-9]{0,18})$/u.test(baseRevision)) {
    throw new Error('The encryption-vault revision is invalid.');
  }
  validateAccountVaultEnvelope(envelope);
  const normalizedBase = vaultSequence(baseRevision, true);
  const pending: DeviceState = {
    ...portableState(state),
    pendingVaultBaseRevision: normalizedBase,
    pendingVaultEnvelope: { ...envelope },
    ...(state.confirmedVaultRevision === undefined
      ? {}
      : {
          confirmedVaultRevision: state.confirmedVaultRevision,
          confirmedVaultDigest: state.confirmedVaultDigest,
          confirmedVaultChainRoot: state.confirmedVaultChainRoot
        })
  };
  // The local parser enforces that the pending state, envelope, base revision,
  // and authenticated high-water form one monotonic transition.
  await saveStateRecord(pending);
}

export async function loadDeviceState(accountRef: string): Promise<DeviceState | null> {
  const record = await recordFor(accountRef);
  return record ? openStoredState(record, accountRef) : null;
}

export function confirmedDeviceState(
  state: DeviceState,
  revision: string,
  digest: string,
  chainRoot: string
): DeviceState {
  const normalizedRevision = vaultSequence(revision);
  const normalizedDigest = validateVaultDigest(digest);
  const normalizedChainRoot = validateVaultChain(chainRoot);
  if (state.vaultSequence !== normalizedRevision) {
    throw new Error('The local encryption-vault high-water mark is invalid.');
  }
  return {
    ...portableState(state),
    confirmedVaultRevision: normalizedRevision,
    confirmedVaultDigest: normalizedDigest,
    confirmedVaultChainRoot: normalizedChainRoot
  };
}

/**
 * Lower the local rollback checkpoint only after an authenticated password
 * reset response confirms that the remote opaque vault was intentionally
 * deleted. A missing remote vault alone is never authority to call this.
 */
export async function rebaseDeviceStateAfterPasswordReset(accountRef: string): Promise<boolean> {
  const state = await loadDeviceState(accountRef);
  if (state) {
    await saveDeviceState({
      ...portableState(state),
      vaultSequence: '1',
      vaultParentChain: ZERO_VAULT_CHAIN
    });
  }
  await clearVaultCheckpoint(accountRef);
  return state !== null;
}

export async function loadPendingAccountVaultWrite(
  accountRef: string
): Promise<PendingAccountVaultWrite | null> {
  const record = await recordFor(accountRef);
  if (!record) return null;
  const state = await openStoredState(record, accountRef);
  const baseRevision = state.pendingVaultBaseRevision;
  const envelope = state.pendingVaultEnvelope;
  if (baseRevision === undefined && envelope === undefined) return null;
  if (
    baseRevision === undefined ||
    envelope === undefined ||
    !/^(?:0|[1-9][0-9]{0,18})$/u.test(baseRevision)
  ) {
    throw new Error('The local encryption-vault journal is invalid.');
  }
  validateAccountVaultEnvelope(envelope);
  return {
    baseRevision,
    envelope,
    state
  };
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
  const encoded = utf8(passphrase);
  let material: CryptoKey;
  try {
    material = await crypto.subtle.importKey('raw', encoded, 'PBKDF2', false, ['deriveKey']);
  } finally {
    clearBytes(encoded);
  }
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
  const aad = utf8(`kaede recovery v1\0${accountRef}`);
  const plaintext = serializePortable(state);
  let ciphertext: Uint8Array | null = null;
  try {
    const key = await recoveryKey(passphrase, salt, ['encrypt']);
    ciphertext = ownedBytes(
      await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: nonce, additionalData: aad },
        key,
        plaintext
      )
    );
    return {
      version: 1,
      kdf: 'PBKDF2-SHA256',
      iterations: RECOVERY_ITERATIONS,
      salt: base64url(salt),
      cipher: 'AES-256-GCM',
      nonce: base64url(nonce),
      ciphertext: base64url(ciphertext)
    };
  } finally {
    clearBytes(plaintext);
    clearBytes(aad);
    clearBytes(salt);
    clearBytes(nonce);
    if (ciphertext) clearBytes(ciphertext);
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
  const ciphertext = fromBase64url(bundle.ciphertext, MAX_STATE_BYTES + 16);
  const aad = utf8(`kaede recovery v1\0${accountRef}`);
  let plaintext: ArrayBuffer | null = null;
  try {
    if (salt.length !== 16 || nonce.length !== 12) throw new Error('Invalid recovery bundle.');
    const key = await recoveryKey(passphrase, salt, ['decrypt']);
    plaintext = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: aad },
      key,
      ciphertext
    );
    const state = parseState(plaintext, accountRef);
    // Recovery is always written into an intentionally empty remote vault.
    // Never replay a backup's historical sequence under a new server label.
    return {
      ...portableState(state),
      vaultSequence: '1',
      vaultParentChain: ZERO_VAULT_CHAIN,
      pendingRoomOperations: {}
    };
  } finally {
    if (plaintext) clearBytes(new Uint8Array(plaintext));
    clearBytes(aad);
    clearBytes(salt);
    clearBytes(nonce);
    clearBytes(ciphertext);
  }
}

function validateAccountVaultEnvelope(envelope: AccountVaultEnvelope): void {
  if (
    !envelope ||
    Object.keys(envelope).sort().join('\0') !==
      ['cipher', 'ciphertext', 'nonce', 'sequence', 'version'].join('\0') ||
    envelope.version !== 2 ||
    envelope.cipher !== 'AES-256-GCM' ||
    vaultSequence(envelope.sequence) !== envelope.sequence ||
    typeof envelope.nonce !== 'string' ||
    typeof envelope.ciphertext !== 'string'
  ) {
    throw new Error('The encryption vault is invalid.');
  }
  const nonce = fromBase64url(envelope.nonce, 12);
  const ciphertext = fromBase64url(envelope.ciphertext, MAX_STATE_BYTES + 16);
  try {
    if (nonce.length !== 12 || ciphertext.length < 17) {
      throw new Error('The encryption vault is invalid.');
    }
  } finally {
    clearBytes(nonce);
    clearBytes(ciphertext);
  }
}

/** Match the backend's opaque-vault digest byte-for-byte. */
export async function accountVaultEnvelopeDigest(envelope: AccountVaultEnvelope): Promise<string> {
  validateAccountVaultEnvelope(envelope);
  const label = utf8('kaede-account-vault-envelope-v2\0');
  const revision = new Uint8Array(8);
  new DataView(revision.buffer).setBigUint64(0, BigInt(envelope.sequence), false);
  const nonce = fromBase64url(envelope.nonce, 12);
  const ciphertext = fromBase64url(envelope.ciphertext, MAX_STATE_BYTES + 16);
  const input = new Uint8Array(label.length + 2 + 8 + nonce.length + ciphertext.length);
  let digest: Uint8Array | null = null;
  try {
    let offset = 0;
    input.set(label, offset);
    offset += label.length;
    input[offset] = 0;
    input[offset + 1] = 2;
    offset += 2;
    input.set(revision, offset);
    offset += revision.length;
    input.set(nonce, offset);
    offset += nonce.length;
    input.set(ciphertext, offset);
    digest = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
    return base64url(digest);
  } finally {
    clearBytes(label);
    clearBytes(revision);
    clearBytes(nonce);
    clearBytes(ciphertext);
    clearBytes(input);
    if (digest) clearBytes(digest);
  }
}

/**
 * Extend the compact authenticated ancestry chain:
 * R_n = SHA256(label || R_(n-1) || u64BE(n) || D_n).
 */
export async function accountVaultChainRoot(
  parentChain: string,
  revision: string,
  digest: string
): Promise<string> {
  const normalizedRevision = vaultSequence(revision);
  const parent = fromBase64url(validateVaultChain(parentChain), 32);
  const envelopeDigest = fromBase64url(validateVaultDigest(digest), 32);
  const label = utf8('kaede-account-vault-chain-v2\0');
  const revisionBytes = new Uint8Array(8);
  new DataView(revisionBytes.buffer).setBigUint64(0, BigInt(normalizedRevision), false);
  const input = new Uint8Array(label.length + parent.length + revisionBytes.length + 32);
  let root: Uint8Array | null = null;
  try {
    let offset = 0;
    input.set(label, offset);
    offset += label.length;
    input.set(parent, offset);
    offset += parent.length;
    input.set(revisionBytes, offset);
    offset += revisionBytes.length;
    input.set(envelopeDigest, offset);
    root = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
    return base64url(root);
  } finally {
    clearBytes(parent);
    clearBytes(envelopeDigest);
    clearBytes(label);
    clearBytes(revisionBytes);
    clearBytes(input);
    if (root) clearBytes(root);
  }
}

export async function sealAccountVaultState(
  state: DeviceState,
  key: CryptoKey
): Promise<AccountVaultEnvelope> {
  const sequence = vaultSequence(state.vaultSequence);
  const nonce = randomBytes(12);
  const aad = utf8(`kaede account vault v2\0${state.accountRef}\0${sequence}`);
  const plaintext = serializePortable(state);
  let ciphertext: Uint8Array | null = null;
  try {
    ciphertext = ownedBytes(
      await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: nonce, additionalData: aad },
        key,
        plaintext
      )
    );
    return {
      version: 2,
      cipher: 'AES-256-GCM',
      sequence,
      nonce: base64url(nonce),
      ciphertext: base64url(ciphertext)
    };
  } finally {
    clearBytes(plaintext);
    clearBytes(nonce);
    clearBytes(aad);
    if (ciphertext) clearBytes(ciphertext);
  }
}

export async function openAccountVaultState(
  accountRef: string,
  key: CryptoKey,
  envelope: AccountVaultEnvelope
): Promise<DeviceState> {
  validateAccountVaultEnvelope(envelope);
  const sequence = vaultSequence(envelope.sequence);
  const nonce = fromBase64url(envelope.nonce, 12);
  const ciphertext = fromBase64url(envelope.ciphertext, MAX_STATE_BYTES + 16);
  const aad = utf8(`kaede account vault v2\0${accountRef}\0${sequence}`);
  let plaintext: ArrayBuffer | null = null;
  try {
    plaintext = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: aad },
      key,
      ciphertext
    );
    const state = parseState(plaintext, accountRef);
    if (state.vaultSequence !== sequence) {
      throw new Error('The encryption vault sequence does not match its ciphertext.');
    }
    return state;
  } finally {
    if (plaintext) clearBytes(new Uint8Array(plaintext));
    clearBytes(aad);
    clearBytes(nonce);
    clearBytes(ciphertext);
  }
}
