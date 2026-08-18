import { api, ApiError } from '$lib/api/client';
import type { Channel, Message, UserSummary } from '$lib/chat/types';
import { isNativeDesktop } from '$lib/platform/native';
import {
  base64url,
  clearBytes,
  concatBytes,
  decodeUtf8,
  fromBase64url,
  ownedBytes,
  randomBytes,
  sha256,
  utf8
} from './encoding';
import { establishVaultFirstIdentity } from './identity-enrollment';
import { encryptedManifestDigest, type EncryptedFileManifest } from './media';
import { isCanonicalRecoveryAuthorization, recoveryRegistrationFields } from './recovery-restore';
import {
  accountVaultChainRoot,
  accountVaultEnvelopeDigest,
  clearAllLocalE2EEState,
  compactDeviceState,
  confirmedDeviceState,
  isExactAccountVaultWriteAcknowledgement,
  loadAccountVaultKey,
  loadDeviceState,
  loadPendingAccountVaultWrite,
  loadVaultCheckpoint,
  nextVaultSequence,
  openAccountVaultState,
  saveDeviceState,
  savePendingAccountVaultWrite,
  saveVaultCheckpoint,
  sealAccountVaultState,
  vaultSequence,
  ZERO_VAULT_CHAIN,
  type AccountVaultEnvelope,
  type CachedPlaintextMessage,
  type DeviceState,
  type PendingRoomOperation,
  type VaultCheckpoint
} from './store';
import type { KaedeMlsClient } from './wasm/kaede_e2ee';

export const MLS_PROTOCOL = 'mls10';
export const MLS_SUITE = 'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519';
const KEY_PACKAGE_BATCH = 20;
const VAULT_LEASE_ATTEMPTS = 8;

interface DeviceRegistration {
  id: string;
  user_id: string;
  user_domain: string;
  identity_key: string;
  credential: string;
  revoked_at: string | null;
  available_key_packages?: number;
}

interface DeviceList {
  generation: string;
  devices: DeviceRegistration[];
}

interface Challenge {
  challenge_id: string;
  signing_input: string;
}

export interface E2EEInitializationOptions {
  /** One-time, session-bound authorization returned by an authenticated E2EE reset. */
  recoveryAuthorization?: string;
}

interface AccountVaultRecord {
  revision: string;
  envelope: AccountVaultEnvelope;
  digest: string;
  updated_at: string;
}

interface AccountVaultLease {
  lease_token: string;
  expires_in: number;
  vault: AccountVaultRecord | null;
}

interface AccountVaultWriteResult {
  vault: AccountVaultRecord;
}

interface AccountVaultReadResult {
  vault: AccountVaultRecord | null;
}

interface AccountVaultDigestPage {
  digests: Array<{ revision: string; digest: string }>;
  next_after: string | null;
}

interface OpenedAccountVault {
  state: DeviceState;
  checkpoint: VaultCheckpoint;
}

type EncryptedMessageRecord = Pick<
  Message,
  | 'id'
  | 'origin_domain'
  | 'channel_id'
  | 'channel_domain'
  | 'author_id'
  | 'author_domain'
  | 'edited_at'
  | 'e2ee'
  | 'encryption_policy_generation'
  | 'encryption_epoch'
>;

interface RoomControlRecord extends EncryptedMessageRecord {
  /** Server-authored control-log instruction; false retains an audit copy only. */
  apply: boolean;
  room_operation_id: string | null;
  room_operation_domain: string | null;
}

interface RoomControlLogPage {
  controls: RoomControlRecord[];
  next_after: string | null;
}

interface PreparedKeyPackageBatch {
  expiresAt: string;
  packages: Uint8Array[];
  signature: Uint8Array;
}

export interface ClaimedKeyPackage {
  user_id: string;
  user_domain: string;
  device_id: string;
  identity_key: string;
  credential: string;
  key_package: string;
}

export interface RoomProposal {
  operation_id: string;
  status: 'prepared';
  policy: {
    mode: 'plaintext' | 'e2ee';
    state: 'proposed' | 'rekeying';
    generation: string;
    protocol: typeof MLS_PROTOCOL;
    suite: typeof MLS_SUITE;
    group_id: string;
    epoch: null;
  };
  key_packages: ClaimedKeyPackage[];
}

interface CommittedRoomOperation extends Channel {
  operation_id: string;
  operation_status: 'committed';
  controls: Array<{
    id: string;
    origin_domain: string;
    operation: 'welcome' | 'commit';
    apply: boolean;
  }>;
}

interface RoomOperationStatus {
  operation_id: string;
  kind: 'activate' | 'rekey';
  status: 'claiming' | 'prepared' | 'committed' | 'failed';
  prepared: RoomProposal | null;
  committed: CommittedRoomOperation | null;
  expires_at: string;
  committed_at: string | null;
}

interface PlaintextApplication {
  version: 1;
  kind: 'message';
  content: string;
  attachments: EncryptedFileManifest[];
  context: MessageContext;
}

interface MessageContext {
  channel_ref: string;
  group_id: string;
  policy_generation: string;
  epoch: string;
  sender_device_id: string;
  operation: 'create' | 'edit';
  target_message: string | null;
  attachment_manifest_digest: string | null;
}

export interface DecryptedApplication {
  content: string;
  attachments: EncryptedFileManifest[];
}

export interface MlsEnvelope extends Record<string, unknown> {
  version: 2;
  protocol: typeof MLS_PROTOCOL;
  suite: typeof MLS_SUITE;
  group_id: string;
  policy_generation: string;
  epoch: string;
  sender_device_id: string;
  operation: 'create' | 'edit' | 'welcome' | 'commit';
  ciphertext: string;
  target_message?: string;
  attachment_manifest_digest?: string;
}

let wasmPromise: Promise<typeof import('./wasm/kaede_e2ee')> | null = null;

async function wasmModule(): Promise<typeof import('./wasm/kaede_e2ee')> {
  if (!wasmPromise) {
    wasmPromise = import('./wasm/kaede_e2ee').then(async (module) => {
      await module.default();
      return module;
    });
  }
  return wasmPromise;
}

function restoreMlsState(
  module: typeof import('./wasm/kaede_e2ee'),
  encodedState: string
): KaedeMlsClient {
  const state = fromBase64url(encodedState, 32 * 1024 * 1024);
  try {
    return module.KaedeMlsClient.restoreState(state);
  } finally {
    clearBytes(state);
  }
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolveWait) => window.setTimeout(resolveWait, milliseconds));
}

function parseControlCursor(value: string): readonly [bigint, string] {
  const separator = value.lastIndexOf('@');
  const id = value.slice(0, separator);
  const domain = value.slice(separator + 1);
  if (
    separator <= 0 ||
    !/^(?:0|[1-9][0-9]{0,18})$/u.test(id) ||
    !domain ||
    domain.length > 253 ||
    domain !== domain.toLowerCase() ||
    domain.includes('@') ||
    [...domain].some((character) => {
      const code = character.charCodeAt(0);
      return code <= 0x20 || code === 0x7f;
    })
  ) {
    throw new Error('The encrypted room control log cursor is invalid.');
  }
  return [BigInt(id), domain];
}

function controlCursorAfter(candidate: string, previous: string | null): boolean {
  if (previous === null) return true;
  const [candidateId, candidateDomain] = parseControlCursor(candidate);
  const [previousId, previousDomain] = parseControlCursor(previous);
  return (
    candidateId > previousId || (candidateId === previousId && candidateDomain > previousDomain)
  );
}

async function acquireAccountVaultLease(): Promise<AccountVaultLease> {
  for (let attempt = 0; attempt < VAULT_LEASE_ATTEMPTS; attempt += 1) {
    try {
      const lease = await api<AccountVaultLease>('/e2ee/vault/lease', {
        method: 'POST',
        body: '{}'
      });
      if (lease.vault) await verifyAccountVaultRecord(lease.vault);
      return lease;
    } catch (caught) {
      if (
        !(caught instanceof ApiError) ||
        caught.code !== 'E2EE_ACCOUNT_VAULT_BUSY' ||
        attempt === VAULT_LEASE_ATTEMPTS - 1
      ) {
        throw caught;
      }
      await wait(150 + attempt * 100 + Math.floor(Math.random() * 100));
    }
  }
  throw new Error('The encryption vault is busy. Try again.');
}

async function releaseAccountVaultLease(leaseToken: string): Promise<void> {
  try {
    await api('/e2ee/vault/lease/release', {
      method: 'POST',
      body: JSON.stringify({ lease_token: leaseToken })
    });
  } catch {
    // The server-side lease has a short TTL. A failed best-effort release
    // cannot expose plaintext and will expire without operator intervention.
  }
}

class AccountVaultConflictError extends Error {}

function sameAccountVaultEnvelope(
  left: AccountVaultEnvelope,
  right: AccountVaultEnvelope
): boolean {
  return (
    left.version === right.version &&
    left.cipher === right.cipher &&
    left.sequence === right.sequence &&
    left.nonce === right.nonce &&
    left.ciphertext === right.ciphertext
  );
}

function nextVaultRevision(revision: string): string {
  return nextVaultSequence(revision);
}

async function verifyAccountVaultRecord(record: AccountVaultRecord): Promise<AccountVaultRecord> {
  const revision = vaultSequence(record.revision);
  if (record.envelope.sequence !== revision) {
    throw new Error('The encryption vault sequence does not match its server revision.');
  }
  const expectedDigest = await accountVaultEnvelopeDigest(record.envelope);
  if (record.digest !== expectedDigest) {
    throw new Error('The encryption vault digest is invalid.');
  }
  return record;
}

function localVaultCheckpoint(state: DeviceState | null): VaultCheckpoint | null {
  const revision = state?.confirmedVaultRevision;
  const digest = state?.confirmedVaultDigest;
  const chainRoot = state?.confirmedVaultChainRoot;
  if (revision === undefined && digest === undefined && chainRoot === undefined) return null;
  if (!state || revision === undefined || digest === undefined || chainRoot === undefined) {
    throw new Error('The local encryption-vault high-water mark is invalid.');
  }
  return { revision, digest, chainRoot };
}

async function strongestVaultCheckpoint(
  ref: string,
  localState: DeviceState | null
): Promise<VaultCheckpoint | null> {
  const local = localVaultCheckpoint(localState);
  const persisted = await loadVaultCheckpoint(ref);
  if (!local) return persisted;
  if (!persisted) {
    await saveVaultCheckpoint(ref, local);
    return local;
  }
  const comparison = BigInt(local.revision) - BigInt(persisted.revision);
  if (
    comparison === 0n &&
    (local.digest !== persisted.digest || local.chainRoot !== persisted.chainRoot)
  ) {
    throw new Error('The local encryption-vault checkpoints conflict.');
  }
  if (comparison > 0n) {
    await saveVaultCheckpoint(ref, local);
    return local;
  }
  return persisted;
}

async function verifyVaultHighWater(
  ref: string,
  localState: DeviceState | null,
  remoteVault: AccountVaultRecord | null
): Promise<AccountVaultRecord | null> {
  const verified = remoteVault ? await verifyAccountVaultRecord(remoteVault) : null;
  const checkpoint = await strongestVaultCheckpoint(ref, localState);
  if (!checkpoint) return verified;
  if (!verified) {
    throw new Error(
      'The server returned an older or missing encryption vault. Encrypted changes are paused to prevent rollback.'
    );
  }
  const comparison = BigInt(verified.revision) - BigInt(vaultSequence(checkpoint.revision));
  if (comparison < 0n || (comparison === 0n && verified.digest !== checkpoint.digest)) {
    throw new Error(
      'The server returned an older or conflicting encryption vault. Encrypted changes are paused to prevent rollback.'
    );
  }
  return verified;
}

const MAX_VAULT_DIGEST_PAGES = 4_096;

async function openVerifiedVaultAncestry(
  ref: string,
  key: CryptoKey,
  record: AccountVaultRecord,
  localState: DeviceState | null
): Promise<OpenedAccountVault> {
  const verified = await verifyAccountVaultRecord(record);
  const checkpoint = await strongestVaultCheckpoint(ref, localState);
  const targetRevision = verified.revision;
  const targetDigest = verified.digest;
  if (checkpoint) {
    const comparison = BigInt(targetRevision) - BigInt(checkpoint.revision);
    if (comparison < 0n || (comparison === 0n && targetDigest !== checkpoint.digest)) {
      throw new Error(
        'The server returned an older or conflicting encryption vault. Encrypted changes are paused to prevent rollback.'
      );
    }
  }
  const opened = await openAccountVaultState(ref, key, verified.envelope);
  if (checkpoint?.revision === targetRevision) {
    return {
      state: confirmedDeviceState(opened, targetRevision, targetDigest, checkpoint.chainRoot),
      checkpoint
    };
  }

  let after = checkpoint?.revision ?? '0';
  let chainRoot = checkpoint?.chainRoot ?? ZERO_VAULT_CHAIN;
  for (let pageIndex = 0; pageIndex < MAX_VAULT_DIGEST_PAGES; pageIndex += 1) {
    const page = await api<AccountVaultDigestPage>(
      `/e2ee/vault/digests?after=${encodeURIComponent(after)}&limit=256`
    );
    if (
      !page ||
      Object.keys(page).sort().join('\0') !== 'digests\0next_after' ||
      !Array.isArray(page.digests) ||
      page.digests.length > 256 ||
      (page.next_after !== null && typeof page.next_after !== 'string')
    ) {
      throw new Error('The encryption-vault ancestry response is invalid.');
    }
    let expectedRevision = BigInt(after) + 1n;
    let lastRevision: string | null = null;
    for (const [itemIndex, item] of page.digests.entries()) {
      if (
        !item ||
        Object.keys(item).sort().join('\0') !== 'digest\0revision' ||
        item.revision !== expectedRevision.toString() ||
        BigInt(vaultSequence(item.revision)) > BigInt(targetRevision)
      ) {
        throw new Error('The encryption-vault ancestry is not consecutive.');
      }
      // accountVaultChainRoot validates the canonical 32-byte digest.
      if (item.revision === targetRevision) {
        if (
          item.digest !== targetDigest ||
          chainRoot !== opened.vaultParentChain ||
          page.next_after !== null ||
          itemIndex !== page.digests.length - 1
        ) {
          throw new Error(
            'The encrypted account vault does not descend from the trusted local checkpoint.'
          );
        }
        chainRoot = await accountVaultChainRoot(chainRoot, item.revision, item.digest);
        const accepted = { revision: targetRevision, digest: targetDigest, chainRoot };
        return {
          state: confirmedDeviceState(opened, targetRevision, targetDigest, chainRoot),
          checkpoint: accepted
        };
      }
      chainRoot = await accountVaultChainRoot(chainRoot, item.revision, item.digest);
      lastRevision = item.revision;
      expectedRevision += 1n;
    }
    if (!page.digests.length || page.next_after === null || page.next_after !== lastRevision) {
      throw new Error('The server did not provide a complete encryption-vault ancestry.');
    }
    after = vaultSequence(page.next_after);
  }
  throw new Error('The encryption-vault ancestry is too long to verify.');
}

async function readAccountVault(): Promise<AccountVaultRecord | null> {
  const vault = (await api<AccountVaultReadResult>('/e2ee/vault')).vault;
  return vault ? await verifyAccountVaultRecord(vault) : null;
}

async function writePendingAccountVault(
  leaseToken: string,
  baseRevision: string,
  envelope: AccountVaultEnvelope
): Promise<AccountVaultRecord> {
  let lastError: unknown = new Error('Could not save the encrypted account vault.');
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const result = await api<AccountVaultWriteResult>('/e2ee/vault', {
        method: 'PUT',
        body: JSON.stringify({
          lease_token: leaseToken,
          expected_revision: baseRevision,
          envelope
        })
      });
      const written = await verifyAccountVaultRecord(result.vault);
      if (
        !isExactAccountVaultWriteAcknowledgement(
          baseRevision,
          envelope,
          written.revision,
          written.envelope
        )
      ) {
        throw new AccountVaultConflictError(
          'The encryption server acknowledged a different account-vault update. Encrypted changes are paused to avoid accepting a substituted state.'
        );
      }
      return written;
    } catch (caught) {
      lastError = caught;
      try {
        const current = await readAccountVault();
        if (
          current?.revision === nextVaultRevision(baseRevision) &&
          sameAccountVaultEnvelope(current.envelope, envelope)
        ) {
          // The compare-and-swap committed and only its response was lost.
          return current;
        }
        const currentRevision = current?.revision ?? '0';
        if (currentRevision !== baseRevision) {
          throw new AccountVaultConflictError(
            'A newer encrypted account vault conflicts with an unfinished local update. Encrypted changes are paused to avoid overwriting either state.'
          );
        }
      } catch (reconcileError) {
        if (reconcileError instanceof AccountVaultConflictError) throw reconcileError;
        // A bounded retry is safe because the write uses an exact revision CAS.
        // The next read recognizes an earlier write whose response was lost.
      }
      if (attempt < 2) await wait(150 * (attempt + 1));
    }
  }
  throw lastError;
}

async function reconcilePendingAccountVault(
  ref: string,
  remoteVault: AccountVaultRecord | null,
  leaseToken: string
): Promise<AccountVaultRecord | null> {
  const localState = await loadDeviceState(ref);
  const verifiedRemote = await verifyVaultHighWater(ref, localState, remoteVault);
  const pending = await loadPendingAccountVaultWrite(ref);
  if (!pending) return verifiedRemote;
  const baseCheckpoint = await strongestVaultCheckpoint(ref, pending.state);
  const expectedParentChain = baseCheckpoint?.chainRoot ?? ZERO_VAULT_CHAIN;
  if (pending.state.vaultParentChain !== expectedParentChain) {
    throw new Error('The local encryption-vault ancestry journal is invalid.');
  }
  if (
    verifiedRemote?.revision === nextVaultRevision(pending.baseRevision) &&
    sameAccountVaultEnvelope(verifiedRemote.envelope, pending.envelope)
  ) {
    const chainRoot = await accountVaultChainRoot(
      expectedParentChain,
      verifiedRemote.revision,
      verifiedRemote.digest
    );
    const checkpoint = {
      revision: verifiedRemote.revision,
      digest: verifiedRemote.digest,
      chainRoot
    };
    await saveDeviceState(
      confirmedDeviceState(pending.state, verifiedRemote.revision, verifiedRemote.digest, chainRoot)
    );
    await saveVaultCheckpoint(ref, checkpoint);
    return verifiedRemote;
  }
  const remoteRevision = verifiedRemote?.revision ?? '0';
  if (remoteRevision !== pending.baseRevision) {
    throw new AccountVaultConflictError(
      'A newer encrypted account vault conflicts with an unfinished local update. Encrypted changes are paused to avoid overwriting either state.'
    );
  }
  const written = await writePendingAccountVault(
    leaseToken,
    pending.baseRevision,
    pending.envelope
  );
  const chainRoot = await accountVaultChainRoot(
    expectedParentChain,
    written.revision,
    written.digest
  );
  const checkpoint = {
    revision: written.revision,
    digest: written.digest,
    chainRoot
  };
  await saveDeviceState(
    confirmedDeviceState(pending.state, written.revision, written.digest, chainRoot)
  );
  await saveVaultCheckpoint(ref, checkpoint);
  return written;
}

function accountRef(user: UserSummary): string {
  return `${user.id}@${user.origin_domain}`;
}

async function accountIdentityDeviceId(ref: string, identityKey: Uint8Array): Promise<string> {
  const prefix = utf8(`${ref}\0`);
  const input = concatBytes(prefix, identityKey);
  let digest: Uint8Array | null = null;
  try {
    digest = await sha256(input);
    return `ked_${base64url(digest)}`;
  } finally {
    clearBytes(prefix);
    clearBytes(input);
    if (digest) clearBytes(digest);
  }
}

function assertDeviceIdentityMetadata(
  ref: string,
  expectedDeviceId: string,
  credential: string,
  identityKey: Uint8Array,
  device: DeviceRegistration
): void {
  const separator = ref.indexOf('@');
  const userId = ref.slice(0, separator);
  const userDomain = ref.slice(separator + 1);
  if (
    separator <= 0 ||
    device.id !== expectedDeviceId ||
    device.user_id !== userId ||
    device.user_domain !== userDomain ||
    device.identity_key !== base64url(identityKey) ||
    device.credential !== credential
  ) {
    throw new Error('The server returned different encryption identity metadata.');
  }
}

async function registerAccountIdentity(
  ref: string,
  expectedDeviceId: string,
  credential: string,
  mls: KaedeMlsClient,
  recoveryAuthorization?: string
): Promise<DeviceRegistration> {
  const identityKey = ownedBytes(mls.publicIdentityKey());
  const credentialBytes = fromBase64url(credential, 16_384);
  let credentialDigest: Uint8Array | null = null;
  let signingInput: Uint8Array | null = null;
  let signature: Uint8Array | null = null;
  try {
    if ((await accountIdentityDeviceId(ref, identityKey)) !== expectedDeviceId) {
      throw new Error('The account encryption identity is invalid.');
    }
    credentialDigest = await sha256(credentialBytes);
    const challenge = await api<Challenge>('/e2ee/devices/challenge', {
      method: 'POST',
      body: JSON.stringify({
        identity_key: base64url(identityKey),
        credential_digest: base64url(credentialDigest)
      })
    });
    signingInput = fromBase64url(challenge.signing_input, 2_048);
    signature = ownedBytes(mls.signServerChallenge(signingInput));
    const registered = await api<DeviceRegistration>('/e2ee/devices', {
      method: 'POST',
      body: JSON.stringify({
        challenge_id: challenge.challenge_id,
        identity_key: base64url(identityKey),
        credential,
        signature: base64url(signature),
        device_name: 'Account encryption identity',
        platform: isNativeDesktop() ? 'desktop' : 'web',
        capabilities: ['e2ee-mls/1', 'e2ee-media/1'],
        ...recoveryRegistrationFields(recoveryAuthorization)
      })
    });
    assertDeviceIdentityMetadata(ref, expectedDeviceId, credential, identityKey, registered);
    return registered;
  } finally {
    clearBytes(identityKey);
    clearBytes(credentialBytes);
    if (credentialDigest) clearBytes(credentialDigest);
    if (signingInput) clearBytes(signingInput);
    if (signature) clearBytes(signature);
  }
}

function expiryString(): string {
  const value = new Date(Date.now() + 28 * 24 * 60 * 60 * 1000).toISOString();
  return value.replace(/Z$/u, '+00:00');
}

function roomOperationId(): string {
  const bytes = randomBytes(32);
  try {
    return `keo_${base64url(bytes)}`;
  } finally {
    clearBytes(bytes);
  }
}

function roomOperationPath(
  channelRef: string,
  kind: PendingRoomOperation['kind'],
  action: 'propose' | 'activate'
): string {
  const suffix = kind === 'rekey' ? `/rekey/${action}` : `/${action}`;
  return `/e2ee/channels/${encodeURIComponent(channelRef)}${suffix}`;
}

function validateRoomProposal(value: RoomProposal, operation: PendingRoomOperation): RoomProposal {
  const expectedMode = operation.kind === 'activate' ? 'plaintext' : 'e2ee';
  const expectedState = operation.kind === 'activate' ? 'proposed' : 'rekeying';
  if (
    value.operation_id !== operation.operationId ||
    value.status !== 'prepared' ||
    value.policy?.mode !== expectedMode ||
    value.policy.state !== expectedState ||
    value.policy.protocol !== MLS_PROTOCOL ||
    value.policy.suite !== MLS_SUITE ||
    value.policy.epoch !== null ||
    !/^[1-9][0-9]{0,18}$/u.test(value.policy.generation) ||
    !Array.isArray(value.key_packages) ||
    !value.key_packages.length
  ) {
    throw new Error('The encrypted-room authority returned an invalid proposal.');
  }
  const groupId = fromBase64url(value.policy.group_id, 32);
  try {
    if (groupId.length !== 32) {
      throw new Error('The encrypted-room authority returned an invalid group identifier.');
    }
  } finally {
    clearBytes(groupId);
  }
  return value;
}

function validateCommittedRoomOperation(
  value: CommittedRoomOperation,
  operation: PendingRoomOperation
): CommittedRoomOperation {
  if (
    value.operation_id !== operation.operationId ||
    value.operation_status !== 'committed' ||
    `${value.id}@${value.origin_domain}` !== operation.channelRef ||
    value.encryption_mode !== 'e2ee' ||
    value.encryption_state !== 'active' ||
    value.encryption_protocol !== MLS_PROTOCOL ||
    value.encryption_suite !== MLS_SUITE ||
    value.encryption_policy_generation !== operation.policyGeneration ||
    value.encryption_group_id !== operation.groupId ||
    value.encryption_epoch !== '1' ||
    !Array.isArray(value.controls) ||
    value.controls.length !== 2 ||
    value.controls[0]?.operation !== 'welcome' ||
    value.controls[0]?.apply !== true ||
    value.controls[1]?.operation !== 'commit' ||
    value.controls[1]?.apply !== false
  ) {
    throw new Error('The encrypted-room authority returned an invalid commit result.');
  }
  return value;
}

function packageSigningInput(
  deviceId: string,
  expiry: string,
  digests: readonly Uint8Array[]
): Uint8Array {
  const separator = new Uint8Array([0]);
  const fields = [
    utf8('kaede-key-package-upload-v1'),
    utf8(deviceId),
    utf8(MLS_SUITE),
    utf8(expiry),
    ...digests
  ];
  try {
    return concatBytes(...fields.flatMap((field, index) => (index ? [separator, field] : [field])));
  } finally {
    separator.fill(0);
    fields.slice(0, 4).forEach(clearBytes);
  }
}

function messageContextBytes(context: MessageContext): Uint8Array {
  const fields = [
    'kaede-message-envelope-v2',
    context.channel_ref,
    context.group_id,
    context.policy_generation,
    context.epoch,
    context.sender_device_id,
    context.operation,
    context.target_message ?? '',
    context.attachment_manifest_digest ?? ''
  ];
  if (fields.some((field) => field.includes('\0')))
    throw new Error('Encrypted message context is invalid.');
  return utf8(fields.join('\0'));
}

function sameBytes(left: Uint8Array, right: Uint8Array): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function isCanonicalSnowflake(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /^(?:0|[1-9][0-9]{0,18})$/u.test(value) &&
    BigInt(value) <= 9_223_372_036_854_775_807n
  );
}

function isCanonicalFederationDomain(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/u.test(
      value
    )
  );
}

function processedMessageKey(
  channel: Channel,
  envelope: MlsEnvelope,
  message: Pick<Message, 'id' | 'origin_domain' | 'author_id' | 'author_domain'>
): string {
  return [
    `${channel.id}@${channel.origin_domain}`,
    envelope.group_id,
    envelope.policy_generation,
    envelope.epoch,
    `${message.id}@${message.origin_domain}`,
    `${message.author_id}@${message.author_domain}`,
    envelope.ciphertext
  ].join('\0');
}

function validateSenderCredential(
  credential: Uint8Array,
  message: Pick<Message, 'author_id' | 'author_domain'>
): void {
  const parsed = JSON.parse(decodeUtf8(credential)) as Record<string, unknown>;
  const expected = `${message.author_id}@${message.author_domain}`;
  if (
    parsed.version !== 1 ||
    parsed.account !== expected ||
    typeof parsed.nonce !== 'string' ||
    !/^[A-Za-z0-9_-]{43}$/u.test(parsed.nonce)
  ) {
    throw new Error('The encrypted message sender identity does not match its author.');
  }
}

function requireEncryptedChannel(channel: Channel): asserts channel is Channel & {
  encryption_policy_generation: string;
  encryption_group_id: string;
  encryption_epoch: string;
} {
  if (
    channel.encryption_mode !== 'e2ee' ||
    channel.encryption_protocol !== MLS_PROTOCOL ||
    channel.encryption_suite !== MLS_SUITE ||
    !channel.encryption_policy_generation ||
    !channel.encryption_group_id ||
    channel.encryption_epoch == null
  ) {
    throw new Error('This encrypted conversation is not ready.');
  }
}

function requireActiveChannel(channel: Channel): asserts channel is Channel & {
  encryption_policy_generation: string;
  encryption_group_id: string;
  encryption_epoch: string;
} {
  requireEncryptedChannel(channel);
  if (channel.encryption_state !== 'active')
    throw new Error('Encrypted messaging is paused while participant keys are secured.');
}

export class KaedeE2EEClient {
  readonly accountRef: string;
  readonly deviceId: string;
  readonly #credential: string;
  #mls: KaedeMlsClient;
  readonly #messageCache: Map<string, CachedPlaintextMessage>;
  readonly #controlCursors: Map<string, string>;
  readonly #pendingRoomOperations: Map<string, PendingRoomOperation>;
  readonly #reconciledRoomChannels = new Map<string, Channel>();
  readonly #processed = new Map<string, DecryptedApplication | null>();
  readonly #vaultKey: CryptoKey;
  #vaultRevision: string;
  #vaultDigest: string | null;
  #vaultChainRoot: string;
  #operationTail: Promise<void> = Promise.resolve();
  #closed = false;

  private constructor(
    state: DeviceState,
    mls: KaedeMlsClient,
    vaultKey: CryptoKey,
    vaultRevision: string,
    vaultDigest: string | null,
    vaultChainRoot: string
  ) {
    this.accountRef = state.accountRef;
    this.deviceId = state.deviceId;
    this.#credential = state.credential;
    this.#mls = mls;
    this.#messageCache = new Map(Object.entries(state.messageCache ?? {}));
    this.#controlCursors = new Map(Object.entries(state.controlCursors ?? {}));
    this.#pendingRoomOperations = new Map(
      Object.entries(state.pendingRoomOperations ?? {}).map(([key, value]) => [key, { ...value }])
    );
    this.#vaultKey = vaultKey;
    this.#vaultRevision = vaultRevision;
    this.#vaultDigest = vaultDigest;
    this.#vaultChainRoot = vaultChainRoot;
  }

  static async initialize(
    user: UserSummary,
    recoveryAuthorization?: string
  ): Promise<KaedeE2EEClient> {
    const module = await wasmModule();
    const ref = accountRef(user);
    const vaultKey = await loadAccountVaultKey(ref);
    if (!vaultKey) {
      throw new Error('Sign out and sign in again to unlock end-to-end encryption on this device.');
    }
    const lease = await acquireAccountVaultLease();
    try {
      const reconciledVault = await reconcilePendingAccountVault(
        ref,
        lease.vault,
        lease.lease_token
      );
      const localState = await loadDeviceState(ref);
      const ancestry = reconciledVault
        ? await openVerifiedVaultAncestry(ref, vaultKey, reconciledVault, localState)
        : null;
      const stored = ancestry?.state ?? localState;
      if (stored) {
        const restoredMls = restoreMlsState(module, stored.mlsState);
        let adopted = false;
        try {
          const listed = await api<DeviceList>('/e2ee/devices');
          let device = listed.devices.find((candidate) => candidate.id === stored.deviceId);
          if (device?.revoked_at) {
            // An authenticated reset revokes the deterministic row before a
            // recovery backup proves possession and re-enrolls that identity.
            device = undefined;
          }
          const identityKey = ownedBytes(restoredMls.publicIdentityKey());
          try {
            if ((await accountIdentityDeviceId(ref, identityKey)) !== stored.deviceId) {
              throw new Error('The portable encryption identity has an invalid device reference.');
            }
            if (device) {
              assertDeviceIdentityMetadata(
                ref,
                stored.deviceId,
                stored.credential,
                identityKey,
                device
              );
            }
          } finally {
            clearBytes(identityKey);
          }
          await saveDeviceState(stored);
          if (ancestry) await saveVaultCheckpoint(ref, ancestry.checkpoint);
          const client = new KaedeE2EEClient(
            stored,
            restoredMls,
            vaultKey,
            reconciledVault?.revision ?? '0',
            reconciledVault?.digest ?? null,
            ancestry?.checkpoint.chainRoot ?? stored.confirmedVaultChainRoot ?? ZERO_VAULT_CHAIN
          );
          await establishVaultFirstIdentity({
            vaultAlreadyDurable: reconciledVault !== null,
            registrationRequired: !device,
            persistVault: () => client.#persistVault(lease.lease_token),
            registerIdentity: () =>
              registerAccountIdentity(
                ref,
                stored.deviceId,
                stored.credential,
                restoredMls,
                recoveryAuthorization
              ).then(() => undefined)
          });
          await client.#reconcileRoomOperationsUnlocked(lease.lease_token);
          adopted = true;
          return client;
        } finally {
          if (!adopted) restoredMls.free();
        }
      }

      const existingDevices = await api<DeviceList>('/e2ee/devices');
      if (existingDevices.devices.some((device) => !device.revoked_at)) {
        throw new Error(
          'Your encrypted account vault needs recovery. Restore a recovery backup or explicitly start a new encryption identity; starting fresh permanently loses unavailable encrypted history.'
        );
      }

      const credentialNonce = randomBytes(32);
      let credentialBytes: Uint8Array;
      try {
        credentialBytes = utf8(
          JSON.stringify({ version: 1, account: ref, nonce: base64url(credentialNonce) })
        );
      } finally {
        clearBytes(credentialNonce);
      }
      const credential = base64url(credentialBytes);
      let mls: KaedeMlsClient;
      try {
        mls = new module.KaedeMlsClient(credentialBytes);
      } finally {
        clearBytes(credentialBytes);
      }
      const identityKey = ownedBytes(mls.publicIdentityKey());
      let deviceId: string;
      try {
        deviceId = await accountIdentityDeviceId(ref, identityKey);
      } finally {
        clearBytes(identityKey);
      }
      const state: DeviceState = {
        schema: 2,
        accountRef: ref,
        deviceId,
        credential,
        mlsState: '',
        vaultSequence: '1',
        vaultParentChain: ZERO_VAULT_CHAIN,
        messageCache: {},
        controlCursors: {},
        pendingRoomOperations: {}
      };
      const client = new KaedeE2EEClient(state, mls, vaultKey, '0', null, ZERO_VAULT_CHAIN);
      // Persist the private MLS identity to the password-encrypted account
      // vault before publishing its public device record. If registration is
      // interrupted, the next initialization can prove and retry the same
      // deterministic identity instead of orphaning its private keys.
      await establishVaultFirstIdentity({
        vaultAlreadyDurable: false,
        registrationRequired: true,
        persistVault: () => client.#persistVault(lease.lease_token),
        registerIdentity: () =>
          registerAccountIdentity(ref, deviceId, state.credential, mls).then(() => undefined)
      });
      return client;
    } finally {
      await releaseAccountVaultLease(lease.lease_token);
    }
  }

  async #restoreVault(record: AccountVaultRecord): Promise<void> {
    const comparison = BigInt(record.revision) - BigInt(this.#vaultRevision);
    if (
      comparison < 0n ||
      (comparison === 0n && this.#vaultDigest !== null && record.digest !== this.#vaultDigest)
    ) {
      throw new Error(
        'The server returned an older or conflicting encryption vault. Encrypted changes are paused to prevent rollback.'
      );
    }
    const ancestry = await openVerifiedVaultAncestry(
      this.accountRef,
      this.#vaultKey,
      record,
      await loadDeviceState(this.accountRef)
    );
    const state = ancestry.state;
    if (state.deviceId !== this.deviceId || state.credential !== this.#credential) {
      throw new Error('The encrypted account identity changed unexpectedly. Sign in again.');
    }
    await saveDeviceState(state);
    await saveVaultCheckpoint(this.accountRef, ancestry.checkpoint);
    const module = await wasmModule();
    const restoredMls = restoreMlsState(module, state.mlsState);
    const previousMls = this.#mls;
    this.#mls = restoredMls;
    previousMls.free();
    this.#messageCache.clear();
    for (const [ciphertext, plaintext] of Object.entries(state.messageCache ?? {})) {
      this.#messageCache.set(ciphertext, plaintext);
    }
    this.#controlCursors.clear();
    for (const [channelRef, cursor] of Object.entries(state.controlCursors ?? {})) {
      this.#controlCursors.set(channelRef, cursor);
    }
    this.#pendingRoomOperations.clear();
    for (const [operationId, operation] of Object.entries(state.pendingRoomOperations ?? {})) {
      this.#pendingRoomOperations.set(operationId, { ...operation });
    }
    this.#processed.clear();
    this.#vaultRevision = record.revision;
    this.#vaultDigest = record.digest;
    this.#vaultChainRoot = ancestry.checkpoint.chainRoot;
  }

  async #synchronized<T>(
    operation: (leaseToken: string) => Promise<T>,
    persist = true,
    rollbackOnOperationError = true
  ): Promise<T> {
    if (this.#closed) throw new Error('This encryption client has been closed.');
    let releaseQueue: () => void = () => undefined;
    const previous = this.#operationTail;
    this.#operationTail = new Promise<void>((resolveQueue) => {
      releaseQueue = resolveQueue;
    });
    await previous;
    try {
      if (this.#closed) throw new Error('This encryption client has been closed.');
      const lease = await acquireAccountVaultLease();
      try {
        const reconciledVault = await reconcilePendingAccountVault(
          this.accountRef,
          lease.vault,
          lease.lease_token
        );
        if (!reconciledVault) {
          throw new Error('The encrypted account vault is missing. Sign in again to recover it.');
        }
        await this.#restoreVault(reconciledVault);
        await this.#reconcileRoomOperationsUnlocked(lease.lease_token);
        const operationBaseVault =
          this.#vaultRevision === reconciledVault.revision
            ? reconciledVault
            : await readAccountVault();
        if (!operationBaseVault) {
          throw new Error('The encrypted account vault disappeared during synchronization.');
        }
        if (
          operationBaseVault.revision !== this.#vaultRevision ||
          operationBaseVault.digest !== this.#vaultDigest
        ) {
          throw new Error(
            'The encryption-vault rollback base changed unexpectedly. Encrypted changes are paused to prevent restoring a substituted state.'
          );
        }
        let result: T;
        try {
          result = await operation(lease.lease_token);
        } catch (caught) {
          if (rollbackOnOperationError) await this.#restoreVault(operationBaseVault);
          throw caught;
        }
        // A failed or ambiguously acknowledged write must leave the pending
        // encrypted journal intact. Restoring the base state here would erase
        // the recovery record that the next lease needs to reconcile.
        if (persist) await this.#persistVault(lease.lease_token);
        return result;
      } finally {
        await releaseAccountVaultLease(lease.lease_token);
      }
    } finally {
      releaseQueue();
    }
  }

  async #persistVault(leaseToken: string): Promise<void> {
    const exported = ownedBytes(this.#mls.exportState());
    try {
      const state = compactDeviceState({
        schema: 2,
        accountRef: this.accountRef,
        deviceId: this.deviceId,
        credential: this.#credential,
        mlsState: base64url(exported),
        vaultSequence: nextVaultRevision(this.#vaultRevision),
        vaultParentChain: this.#vaultChainRoot,
        messageCache: Object.fromEntries(this.#messageCache),
        controlCursors: Object.fromEntries([...this.#controlCursors.entries()].slice(-6_400)),
        pendingRoomOperations: Object.fromEntries(
          [...this.#pendingRoomOperations.entries()]
            .slice(-32)
            .map(([key, value]) => [key, { ...value }])
        )
      });
      this.#messageCache.clear();
      for (const [ciphertext, cached] of Object.entries(state.messageCache ?? {})) {
        this.#messageCache.set(ciphertext, cached);
      }
      if (
        this.#vaultRevision !== '0' &&
        (!this.#vaultDigest || this.#vaultChainRoot === ZERO_VAULT_CHAIN)
      ) {
        throw new Error('The local encryption-vault high-water mark is invalid.');
      }
      const localState: DeviceState =
        this.#vaultRevision === '0'
          ? state
          : {
              ...state,
              confirmedVaultRevision: this.#vaultRevision,
              confirmedVaultDigest: this.#vaultDigest!,
              confirmedVaultChainRoot: this.#vaultChainRoot
            };
      const envelope = await sealAccountVaultState(localState, this.#vaultKey);
      await savePendingAccountVaultWrite(localState, this.#vaultRevision, envelope);
      const written = await writePendingAccountVault(leaseToken, this.#vaultRevision, envelope);
      const chainRoot = await accountVaultChainRoot(
        this.#vaultChainRoot,
        written.revision,
        written.digest
      );
      const checkpoint = { revision: written.revision, digest: written.digest, chainRoot };
      await saveDeviceState(
        confirmedDeviceState(localState, written.revision, written.digest, chainRoot)
      );
      await saveVaultCheckpoint(this.accountRef, checkpoint);
      this.#vaultRevision = written.revision;
      this.#vaultDigest = written.digest;
      this.#vaultChainRoot = chainRoot;
    } finally {
      clearBytes(exported);
    }
  }

  async initializeKeyPackages(): Promise<void> {
    const listed = await api<DeviceList>('/e2ee/devices');
    const device = listed.devices.find((candidate) => candidate.id === this.deviceId);
    if (!device || device.revoked_at) {
      throw new Error('This encryption identity is no longer active.');
    }
    await this.replenishKeyPackages(device.available_key_packages ?? 0);
  }

  async replenishKeyPackages(available = 0): Promise<void> {
    const count = Math.max(0, KEY_PACKAGE_BATCH - available);
    if (!count) return;
    const batch = await this.#synchronized(() => this.#prepareKeyPackagesUnlocked(count));
    try {
      await api(`/e2ee/devices/${encodeURIComponent(this.deviceId)}/key-packages`, {
        method: 'POST',
        body: JSON.stringify({
          cipher_suite: MLS_SUITE,
          expires_at: batch.expiresAt,
          packages: batch.packages.map((value) => base64url(value)),
          signature: base64url(batch.signature)
        })
      });
    } finally {
      clearBytes(batch.signature);
      batch.packages.forEach(clearBytes);
    }
  }

  async #prepareKeyPackagesUnlocked(count: number): Promise<PreparedKeyPackageBatch> {
    const packages: Uint8Array[] = [];
    const digests: Uint8Array[] = [];
    let signature: Uint8Array | null = null;
    try {
      for (let index = 0; index < count; index += 1) {
        packages.push(ownedBytes(this.#mls.generateKeyPackage()));
      }
      digests.push(...(await Promise.all(packages.map((value) => sha256(value)))));
      const expiresAt = expiryString();
      const signingInput = packageSigningInput(this.deviceId, expiresAt, digests);
      try {
        signature = ownedBytes(this.#mls.signServerChallenge(signingInput));
      } finally {
        clearBytes(signingInput);
      }
      return { expiresAt, packages, signature };
    } catch (caught) {
      if (signature) clearBytes(signature);
      packages.forEach(clearBytes);
      throw caught;
    } finally {
      digests.forEach(clearBytes);
    }
  }

  async activateRoom(channelRef: string): Promise<Channel> {
    return this.#synchronized(
      (leaseToken) => this.#startRoomOperationUnlocked(channelRef, 'activate', leaseToken),
      false,
      false
    );
  }

  async rekeyRoom(channelRef: string): Promise<Channel> {
    return this.#synchronized(
      (leaseToken) => this.#startRoomOperationUnlocked(channelRef, 'rekey', leaseToken),
      false,
      false
    );
  }

  async #startRoomOperationUnlocked(
    channelRef: string,
    kind: PendingRoomOperation['kind'],
    leaseToken: string
  ): Promise<Channel> {
    const reconciled = this.#reconciledRoomChannels.get(channelRef);
    if (reconciled) {
      this.#reconciledRoomChannels.delete(channelRef);
      return reconciled;
    }
    const existing = [...this.#pendingRoomOperations.values()].find(
      (candidate) => candidate.channelRef === channelRef
    );
    if (existing) {
      if (existing.kind !== kind) {
        throw new Error('A different encrypted-room update is already being recovered.');
      }
      return this.#continueRoomOperationUnlocked(existing, leaseToken);
    }
    if (this.#pendingRoomOperations.size >= 32) {
      throw new Error('Too many encrypted-room updates are waiting for recovery.');
    }
    const operation: PendingRoomOperation = {
      version: 1,
      operationId: roomOperationId(),
      channelRef,
      kind,
      phase: 'proposing'
    };
    this.#pendingRoomOperations.set(operation.operationId, operation);
    // The operation ID must be portable before any authority can claim one-use
    // KeyPackages. A crash can then retry the exact same claim set.
    await this.#persistVault(leaseToken);
    return this.#continueRoomOperationUnlocked(operation, leaseToken);
  }

  async #continueRoomOperationUnlocked(
    operation: PendingRoomOperation,
    leaseToken: string
  ): Promise<Channel> {
    if (operation.phase === 'activating') {
      return this.#activatePendingRoomOperationUnlocked(operation, leaseToken);
    }
    const proposal = validateRoomProposal(
      await api<RoomProposal>(roomOperationPath(operation.channelRef, operation.kind, 'propose'), {
        method: 'POST',
        body: JSON.stringify({
          operation_id: operation.operationId,
          sender_device_id: this.deviceId
        })
      }),
      operation
    );
    return this.#prepareRoomOperationUnlocked(operation, proposal, leaseToken);
  }

  async #prepareRoomOperationUnlocked(
    operation: PendingRoomOperation,
    proposal: RoomProposal,
    leaseToken: string
  ): Promise<Channel> {
    validateRoomProposal(proposal, operation);
    const groupId = fromBase64url(proposal.policy.group_id, 32);
    const packages: Uint8Array[] = [];
    let pending: ReturnType<KaedeMlsClient['addMembers']> | null = null;
    let commit: Uint8Array | null = null;
    let welcome: Uint8Array | null = null;
    try {
      if (groupId.length !== 32 || this.#mls.hasGroup(groupId)) {
        throw new Error('The encrypted-room group conflicts with local encryption state.');
      }
      const seenDevices = new Set<string>();
      const seenAccounts = new Set<string>();
      for (const claimed of proposal.key_packages) {
        packages.push(await this.#validateClaimedKeyPackage(claimed, seenDevices, seenAccounts));
      }
      if (!packages.length) throw new Error('An encrypted room requires another active device.');
      this.#mls.createGroup(groupId);
      pending = this.#mls.addMembers(groupId, packages);
      commit = ownedBytes(pending.commit);
      welcome = ownedBytes(pending.welcome);
      this.#mls.mergePendingCommit(groupId);
      const activating: PendingRoomOperation = {
        version: 1,
        operationId: operation.operationId,
        channelRef: operation.channelRef,
        kind: operation.kind,
        phase: 'activating',
        policyGeneration: proposal.policy.generation,
        groupId: proposal.policy.group_id,
        commit: base64url(commit),
        welcome: base64url(welcome)
      };
      this.#pendingRoomOperations.set(operation.operationId, activating);
      // Persist the post-commit MLS state before telling the authority to make
      // the policy visible. The activation request is cryptographically bound
      // to this exact account-vault revision and digest.
      await this.#persistVault(leaseToken);
      return await this.#activatePendingRoomOperationUnlocked(activating, leaseToken);
    } finally {
      pending?.free();
      packages.forEach(clearBytes);
      if (commit) clearBytes(commit);
      if (welcome) clearBytes(welcome);
      clearBytes(groupId);
    }
  }

  async #validateClaimedKeyPackage(
    claimed: ClaimedKeyPackage,
    seenDevices: Set<string>,
    seenAccounts: Set<string>
  ): Promise<Uint8Array> {
    const claimedAccount = `${claimed?.user_id}@${claimed?.user_domain}`;
    if (
      !claimed ||
      typeof claimed !== 'object' ||
      Object.keys(claimed).sort().join(',') !==
        'credential,device_id,identity_key,key_package,user_domain,user_id' ||
      !isCanonicalSnowflake(claimed.user_id) ||
      !isCanonicalFederationDomain(claimed.user_domain) ||
      typeof claimed.device_id !== 'string' ||
      !/^ked_[A-Za-z0-9_-]{43}$/u.test(claimed.device_id) ||
      claimed.device_id === this.deviceId ||
      seenDevices.has(claimed.device_id) ||
      seenAccounts.has(claimedAccount) ||
      typeof claimed.identity_key !== 'string' ||
      typeof claimed.credential !== 'string' ||
      typeof claimed.key_package !== 'string'
    ) {
      throw new Error('The encrypted-room authority returned an invalid key package.');
    }

    const keyPackage = fromBase64url(claimed.key_package, 32 * 1024);
    const expectedIdentity = fromBase64url(claimed.identity_key, 32);
    const expectedCredential = fromBase64url(claimed.credential, 16 * 1024);
    let inspected: ReturnType<KaedeMlsClient['inspectKeyPackage']> | null = null;
    let embeddedIdentity: Uint8Array | null = null;
    let embeddedCredential: Uint8Array | null = null;
    let nonce: Uint8Array | null = null;
    try {
      if (expectedIdentity.length !== 32) {
        throw new Error('A claimed key package has an invalid participant identity.');
      }
      inspected = this.#mls.inspectKeyPackage(keyPackage);
      embeddedIdentity = ownedBytes(inspected.signatureKey);
      embeddedCredential = ownedBytes(inspected.credential);
      if (
        !sameBytes(embeddedIdentity, expectedIdentity) ||
        !sameBytes(embeddedCredential, expectedCredential)
      ) {
        throw new Error('A claimed key package does not authenticate its participant.');
      }
      const parsed = JSON.parse(decodeUtf8(expectedCredential)) as Record<string, unknown>;
      if (
        !parsed ||
        Array.isArray(parsed) ||
        Object.keys(parsed).sort().join(',') !== 'account,nonce,version' ||
        parsed.version !== 1 ||
        parsed.account !== claimedAccount ||
        typeof parsed.nonce !== 'string'
      ) {
        throw new Error('A claimed key package has an invalid participant credential.');
      }
      nonce = fromBase64url(parsed.nonce, 32);
      if (
        nonce.length !== 32 ||
        (await accountIdentityDeviceId(claimedAccount, embeddedIdentity)) !== claimed.device_id
      ) {
        throw new Error('A claimed key package has an invalid participant identity.');
      }
      seenDevices.add(claimed.device_id);
      seenAccounts.add(claimedAccount);
      return keyPackage;
    } catch (caught) {
      clearBytes(keyPackage);
      throw caught;
    } finally {
      inspected?.free();
      clearBytes(expectedIdentity);
      clearBytes(expectedCredential);
      if (embeddedIdentity) clearBytes(embeddedIdentity);
      if (embeddedCredential) clearBytes(embeddedCredential);
      if (nonce) clearBytes(nonce);
    }
  }

  async #activatePendingRoomOperationUnlocked(
    operation: PendingRoomOperation,
    leaseToken: string
  ): Promise<Channel> {
    if (
      operation.phase !== 'activating' ||
      !operation.policyGeneration ||
      !operation.commit ||
      !operation.welcome ||
      !this.#vaultDigest ||
      this.#vaultRevision === '0'
    ) {
      throw new Error('The encrypted-room recovery record is incomplete.');
    }
    const committed = validateCommittedRoomOperation(
      await api<CommittedRoomOperation>(
        roomOperationPath(operation.channelRef, operation.kind, 'activate'),
        {
          method: 'POST',
          body: JSON.stringify({
            operation_id: operation.operationId,
            sender_device_id: this.deviceId,
            policy_generation: operation.policyGeneration,
            group_id: operation.groupId,
            epoch: '1',
            commit: operation.commit,
            welcome: operation.welcome,
            prepared_vault_revision: this.#vaultRevision,
            prepared_vault_digest: this.#vaultDigest,
            vault_lease_token: leaseToken
          })
        }
      ),
      operation
    );
    this.#pendingRoomOperations.delete(operation.operationId);
    await this.#persistVault(leaseToken);
    return committed;
  }

  async #roomOperationStatusUnlocked(
    operation: PendingRoomOperation
  ): Promise<RoomOperationStatus> {
    const status = await api<RoomOperationStatus>(
      `/e2ee/channels/${encodeURIComponent(operation.channelRef)}/operations/${encodeURIComponent(operation.operationId)}`
    );
    if (
      status.operation_id !== operation.operationId ||
      status.kind !== operation.kind ||
      !['claiming', 'prepared', 'committed', 'failed'].includes(status.status)
    ) {
      throw new Error('The encrypted-room authority returned an invalid recovery status.');
    }
    return status;
  }

  async #reconcileRoomOperationsUnlocked(leaseToken: string): Promise<void> {
    for (const operation of [...this.#pendingRoomOperations.values()]) {
      let status: RoomOperationStatus;
      try {
        status = await this.#roomOperationStatusUnlocked(operation);
      } catch (caught) {
        if (
          operation.phase === 'proposing' &&
          caught instanceof ApiError &&
          caught.code === 'E2EE_OPERATION_NOT_FOUND'
        ) {
          const channel = await this.#continueRoomOperationUnlocked(operation, leaseToken);
          this.#reconciledRoomChannels.set(operation.channelRef, channel);
          continue;
        }
        throw caught;
      }
      if (status.status === 'claiming') {
        if (operation.phase !== 'proposing') {
          throw new Error('The encrypted-room authority lost a prepared operation.');
        }
        const channel = await this.#continueRoomOperationUnlocked(operation, leaseToken);
        this.#reconciledRoomChannels.set(operation.channelRef, channel);
        continue;
      }
      if (status.status === 'prepared') {
        if (operation.phase === 'proposing') {
          if (!status.prepared) {
            throw new Error('The encrypted-room authority lost its prepared proposal.');
          }
          const channel = await this.#prepareRoomOperationUnlocked(
            operation,
            validateRoomProposal(status.prepared, operation),
            leaseToken
          );
          this.#reconciledRoomChannels.set(operation.channelRef, channel);
        } else {
          const channel = await this.#activatePendingRoomOperationUnlocked(operation, leaseToken);
          this.#reconciledRoomChannels.set(operation.channelRef, channel);
        }
        continue;
      }
      if (status.status === 'committed') {
        if (operation.phase !== 'activating' || !status.committed) {
          throw new Error(
            'The authority committed an encrypted room without its portable MLS state.'
          );
        }
        const channel = validateCommittedRoomOperation(status.committed, operation);
        this.#pendingRoomOperations.delete(operation.operationId);
        await this.#persistVault(leaseToken);
        this.#reconciledRoomChannels.set(operation.channelRef, channel);
        continue;
      }
      this.#pendingRoomOperations.delete(operation.operationId);
      await this.#persistVault(leaseToken);
      throw new Error(
        'The encrypted-room update expired or became stale. Review the member list and try again.'
      );
    }
  }

  async encryptMessage(
    channel: Channel,
    content: string,
    options: {
      operation?: 'create' | 'edit';
      targetMessage?: string;
      attachments?: EncryptedFileManifest[];
    } = {}
  ): Promise<MlsEnvelope> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      return this.#encryptMessageUnlocked(channel, content, options);
    });
  }

  async #encryptMessageUnlocked(
    channel: Channel,
    content: string,
    options: {
      operation?: 'create' | 'edit';
      targetMessage?: string;
      attachments?: EncryptedFileManifest[];
    }
  ): Promise<MlsEnvelope> {
    requireActiveChannel(channel);
    const attachments = options.attachments ?? [];
    const operation = options.operation ?? 'create';
    if (operation === 'edit' && !options.targetMessage)
      throw new Error('Encrypted edits require a target message.');
    const attachmentDigest = attachments.length ? await encryptedManifestDigest(attachments) : null;
    const context: MessageContext = {
      channel_ref: `${channel.id}@${channel.origin_domain}`,
      group_id: channel.encryption_group_id,
      policy_generation: channel.encryption_policy_generation,
      epoch: channel.encryption_epoch,
      sender_device_id: this.deviceId,
      operation,
      target_message: options.targetMessage ?? null,
      attachment_manifest_digest: attachmentDigest
    };
    const plaintext: PlaintextApplication = {
      version: 1,
      kind: 'message',
      content,
      attachments,
      context
    };
    const encoded = utf8(JSON.stringify(plaintext));
    const groupId = fromBase64url(channel.encryption_group_id, 128);
    const aad = messageContextBytes(context);
    let ciphertext: Uint8Array | null = null;
    try {
      ciphertext = ownedBytes(this.#mls.encrypt(groupId, encoded, aad));
      const envelope: MlsEnvelope = {
        version: 2,
        protocol: MLS_PROTOCOL,
        suite: MLS_SUITE,
        group_id: channel.encryption_group_id,
        policy_generation: channel.encryption_policy_generation,
        epoch: channel.encryption_epoch,
        sender_device_id: this.deviceId,
        operation,
        ciphertext: base64url(ciphertext)
      };
      if (context.target_message) envelope.target_message = context.target_message;
      if (attachmentDigest) envelope.attachment_manifest_digest = attachmentDigest;
      this.#messageCache.set(envelope.ciphertext, {
        plaintext: JSON.stringify(plaintext),
        authorRef: this.accountRef,
        messageRef: null
      });
      return envelope;
    } finally {
      clearBytes(encoded);
      clearBytes(groupId);
      clearBytes(aad);
      if (ciphertext) clearBytes(ciphertext);
    }
  }

  async exportMediaKey(channel: Channel, mediaContext: string): Promise<ArrayBuffer> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      return this.#exportMediaKeyUnlocked(channel, mediaContext);
    });
  }

  async #exportMediaKeyUnlocked(channel: Channel, mediaContext: string): Promise<ArrayBuffer> {
    requireActiveChannel(channel);
    if (!mediaContext || mediaContext.length > 256)
      throw new Error('Encrypted media context is invalid.');
    const groupId = fromBase64url(channel.encryption_group_id, 128);
    const context = utf8(mediaContext);
    let secret: Uint8Array | null = null;
    try {
      secret = ownedBytes(this.#mls.exportEpochSecret(groupId, 'kaede livekit v1', context, 32));
      return ownedBytes(secret).buffer;
    } finally {
      clearBytes(groupId);
      clearBytes(context);
      if (secret) clearBytes(secret);
    }
  }

  async syncRoomState(channel: Channel): Promise<void> {
    requireEncryptedChannel(channel);
    const messages = await api<Message[]>(
      `/channels/${encodeURIComponent(`${channel.id}@${channel.origin_domain}`)}/messages?limit=100`
    );
    await this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      for (const message of [...messages].reverse()) {
        if (!message.e2ee) continue;
        try {
          await this.#decryptMessageUnlocked(channel, message);
        } catch {
          // An application may predate this account's first Welcome. Continue
          // after processing the complete durable control log.
        }
      }
    });
  }

  async #syncControlLogUnlocked(channel: Channel): Promise<void> {
    requireEncryptedChannel(channel);
    const channelRef = `${channel.id}@${channel.origin_domain}`;
    const seenCursors = new Set<string>();
    let after: string | null = this.#controlCursors.get(channelRef) ?? null;
    if (after) parseControlCursor(after);
    for (let pageNumber = 0; pageNumber < 256; pageNumber += 1) {
      const query = after ? `?limit=25&after=${encodeURIComponent(after)}` : '?limit=25';
      const page = await api<RoomControlLogPage>(
        `/e2ee/channels/${encodeURIComponent(channelRef)}/control-log${query}`
      );
      if (!Array.isArray(page.controls) || page.controls.length > 25) {
        throw new Error('The encrypted room control log is invalid.');
      }
      for (const control of page.controls) {
        if (typeof control.apply !== 'boolean') {
          throw new Error('The encrypted room control instruction is invalid.');
        }
        const cursor = `${control.id}@${control.origin_domain}`;
        parseControlCursor(cursor);
        if (!controlCursorAfter(cursor, this.#controlCursors.get(channelRef) ?? null)) {
          throw new Error('The encrypted room control log is out of order.');
        }
        await this.#decryptMessageUnlocked(channel, control);
        // Refresh insertion order so the bounded portable cursor map evicts
        // the least-recently synchronized channel, not an active old one.
        this.#controlCursors.delete(channelRef);
        this.#controlCursors.set(channelRef, cursor);
      }
      if (page.next_after == null) return;
      if (
        typeof page.next_after !== 'string' ||
        page.next_after.length > 512 ||
        seenCursors.has(page.next_after) ||
        !controlCursorAfter(page.next_after, after)
      ) {
        throw new Error('The encrypted room control log cursor is invalid.');
      }
      seenCursors.add(page.next_after);
      after = page.next_after;
      if (pageNumber === 255) {
        throw new Error('The encrypted room control log is too large to process safely.');
      }
    }
  }

  async safetyNumber(channel: Channel): Promise<string> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      return this.#safetyNumberUnlocked(channel);
    });
  }

  async #safetyNumberUnlocked(channel: Channel): Promise<string> {
    requireEncryptedChannel(channel);
    const groupId = fromBase64url(channel.encryption_group_id, 128);
    let roster: Uint8Array | null = null;
    let digest: Uint8Array | null = null;
    try {
      roster = ownedBytes(this.#mls.memberRoster(groupId));
      digest = await sha256(roster);
      const digits = [...digest]
        .slice(0, 15)
        .map((value) => value.toString().padStart(3, '0'))
        .join('');
      return digits.match(/.{1,5}/gu)?.join(' ') ?? digits;
    } finally {
      clearBytes(groupId);
      if (roster) clearBytes(roster);
      if (digest) clearBytes(digest);
    }
  }

  async decryptMessage(channel: Channel, message: Message): Promise<DecryptedApplication | null> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      return this.#decryptMessageUnlocked(channel, message);
    });
  }

  async decryptMessages(
    channel: Channel,
    messages: readonly Message[]
  ): Promise<(DecryptedApplication | null)[]> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      const applications: (DecryptedApplication | null)[] = [];
      for (const message of messages) {
        if (!message.e2ee) {
          applications.push(null);
          continue;
        }
        try {
          applications.push(await this.#decryptMessageUnlocked(channel, message));
        } catch {
          // A message can predate this account's first Welcome. Keep processing
          // later records so one unavailable epoch cannot hide current history.
          applications.push(null);
        }
      }
      return applications;
    });
  }

  async #decryptMessageUnlocked(
    channel: Channel,
    message: EncryptedMessageRecord & { apply?: boolean }
  ): Promise<DecryptedApplication | null> {
    requireEncryptedChannel(channel);
    const envelope = message.e2ee as MlsEnvelope | null | undefined;
    if (!envelope || envelope.version !== 2) {
      return null;
    }
    if (
      envelope.protocol !== MLS_PROTOCOL ||
      envelope.suite !== MLS_SUITE ||
      `${message.channel_id}@${message.channel_domain}` !==
        `${channel.id}@${channel.origin_domain}` ||
      message.encryption_policy_generation !== envelope.policy_generation ||
      message.encryption_epoch !== envelope.epoch
    ) {
      throw new Error('The encrypted message context does not match this conversation.');
    }
    // A room can retain messages from older MLS generations after a rekey.
    // Include their immutable channel/policy context in the transient cache
    // key instead of comparing them with only the channel's current policy.
    // Any altered group then misses both caches and fails MLS/AAD validation.
    const messageRef = `${message.id}@${message.origin_domain}`;
    const authorRef = `${message.author_id}@${message.author_domain}`;
    const processedKey = processedMessageKey(channel, envelope, message);
    if (this.#processed.has(processedKey)) return this.#processed.get(processedKey) ?? null;
    const groupId = fromBase64url(envelope.group_id, 128);
    const ciphertext = fromBase64url(envelope.ciphertext, 60 * 1024);
    if (envelope.operation === 'welcome') {
      try {
        if (!this.#mls.hasGroup(groupId)) {
          const joinedGroupId = ownedBytes(this.#mls.joinGroup(ciphertext));
          try {
            if (!sameBytes(joinedGroupId, groupId)) {
              throw new Error('The encrypted room Welcome targets a different group.');
            }
          } finally {
            clearBytes(joinedGroupId);
          }
        }
      } finally {
        clearBytes(groupId);
        clearBytes(ciphertext);
      }
      this.#processed.set(processedKey, null);
      return null;
    }
    if (envelope.operation === 'commit') {
      try {
        if (!this.#mls.hasGroup(groupId)) {
          throw new Error('The encrypted room commit arrived before its Welcome.');
        }
        // Initial activation and full-room rekey create a fresh group. The
        // Welcome already contains the post-add state, so the authority marks
        // its paired add commit audit-only. Older message projections do not
        // carry that instruction, so epoch one remains the fail-safe fallback.
        if (message.apply !== false && !(message.apply === undefined && envelope.epoch === '1')) {
          const processed = this.#mls.process(groupId, ciphertext);
          try {
            if (processed.kind !== 'commit') {
              throw new Error('The encrypted room control record is invalid.');
            }
          } finally {
            processed.free();
          }
        }
      } finally {
        clearBytes(groupId);
        clearBytes(ciphertext);
      }
      this.#processed.set(processedKey, null);
      return null;
    }
    const isEditedProjection = message.edited_at != null;
    if (
      (envelope.operation === 'create' &&
        (isEditedProjection || envelope.target_message !== undefined)) ||
      (envelope.operation === 'edit' &&
        (!isEditedProjection || envelope.target_message !== messageRef)) ||
      !['create', 'edit'].includes(envelope.operation)
    ) {
      throw new Error('Encrypted message operation does not match its server projection.');
    }
    const expectedContext: MessageContext = {
      channel_ref: `${channel.id}@${channel.origin_domain}`,
      group_id: envelope.group_id,
      policy_generation: envelope.policy_generation,
      epoch: envelope.epoch,
      sender_device_id: envelope.sender_device_id,
      operation: envelope.operation as 'create' | 'edit',
      target_message: envelope.target_message ?? null,
      attachment_manifest_digest: envelope.attachment_manifest_digest ?? null
    };
    const expectedAad = messageContextBytes(expectedContext);
    const cached = this.#messageCache.get(envelope.ciphertext);
    if (cached) {
      try {
        if (cached.authorRef !== authorRef) {
          throw new Error('Encrypted message author was modified.');
        }
        if (cached.messageRef === null) {
          if (cached.authorRef !== this.accountRef) {
            throw new Error('Encrypted message cache binding is invalid.');
          }
          this.#messageCache.delete(envelope.ciphertext);
          this.#messageCache.set(envelope.ciphertext, { ...cached, messageRef });
        } else if (cached.messageRef !== messageRef) {
          throw new Error('Encrypted message ciphertext was replayed under a different message.');
        }
        const parsed = JSON.parse(cached.plaintext) as PlaintextApplication;
        if (
          parsed.version !== 1 ||
          parsed.kind !== 'message' ||
          typeof parsed.content !== 'string' ||
          !Array.isArray(parsed.attachments) ||
          JSON.stringify(parsed.context) !== JSON.stringify(expectedContext)
        )
          throw new Error('Encrypted message context was modified.');
        const application = { content: parsed.content, attachments: parsed.attachments };
        this.#processed.set(processedKey, application);
        return application;
      } finally {
        clearBytes(groupId);
        clearBytes(ciphertext);
        clearBytes(expectedAad);
      }
    }
    let processed: ReturnType<KaedeMlsClient['process']> | null = null;
    try {
      processed = this.#mls.process(groupId, ciphertext);
      if (processed.kind !== 'application' || !processed.application) {
        return null;
      }
      if (!processed.aad) throw new Error('Encrypted message authenticated context is missing.');
      const receivedAad = ownedBytes(processed.aad);
      try {
        if (!sameBytes(receivedAad, expectedAad))
          throw new Error('Encrypted message authenticated context was modified.');
      } finally {
        clearBytes(receivedAad);
      }
      if (!processed.credential) throw new Error('Encrypted message sender identity is missing.');
      const senderCredential = ownedBytes(processed.credential);
      try {
        validateSenderCredential(senderCredential, message);
      } finally {
        clearBytes(senderCredential);
      }
      const parsed = JSON.parse(decodeUtf8(processed.application)) as PlaintextApplication;
      if (
        parsed.version !== 1 ||
        parsed.kind !== 'message' ||
        typeof parsed.content !== 'string' ||
        !Array.isArray(parsed.attachments) ||
        JSON.stringify(parsed.context) !== JSON.stringify(expectedContext)
      ) {
        throw new Error('Encrypted message plaintext is invalid.');
      }
      if (envelope.attachment_manifest_digest) {
        const digest = await encryptedManifestDigest(parsed.attachments);
        if (digest !== envelope.attachment_manifest_digest) {
          throw new Error('Encrypted attachment manifest was modified.');
        }
      } else if (parsed.attachments.length) {
        throw new Error('Encrypted attachment manifest is not authenticated.');
      }
      const application = { content: parsed.content, attachments: parsed.attachments };
      this.#messageCache.delete(envelope.ciphertext);
      this.#messageCache.set(envelope.ciphertext, {
        plaintext: JSON.stringify(parsed),
        authorRef,
        messageRef
      });
      this.#processed.set(processedKey, application);
      return application;
    } finally {
      processed?.free();
      clearBytes(groupId);
      clearBytes(ciphertext);
      clearBytes(expectedAad);
    }
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    await this.#operationTail;
    this.#processed.clear();
    this.#messageCache.clear();
    this.#controlCursors.clear();
    this.#pendingRoomOperations.clear();
    this.#reconciledRoomChannels.clear();
    this.#mls.free();
  }
}

let activeClient: Promise<KaedeE2EEClient> | null = null;
let activeAccount = '';
let activeGeneration = 0;

export function initializeE2EE(
  user: UserSummary,
  options: E2EEInitializationOptions = {}
): Promise<KaedeE2EEClient> {
  const ref = accountRef(user);
  if (
    options.recoveryAuthorization !== undefined &&
    !isCanonicalRecoveryAuthorization(options.recoveryAuthorization)
  ) {
    return Promise.reject(new Error('The encryption-recovery authorization is invalid.'));
  }
  if (options.recoveryAuthorization !== undefined && activeClient) {
    return Promise.reject(
      new Error('The active encryption client must be reset before restoring a recovery backup.')
    );
  }
  if (!activeClient || activeAccount !== ref) {
    const previous = activeClient;
    const generation = ++activeGeneration;
    activeAccount = ref;
    if (previous) void previous.then((client) => client.close()).catch(() => undefined);
    const initializing = KaedeE2EEClient.initialize(user, options.recoveryAuthorization)
      .then(async (client) => {
        try {
          await client.initializeKeyPackages();
          if (generation !== activeGeneration || activeClient !== initializing) {
            throw new Error('Encryption initialization was superseded.');
          }
          return client;
        } catch (error) {
          await client.close();
          throw error;
        }
      })
      .catch((error) => {
        if (activeClient === initializing) {
          activeClient = null;
          activeAccount = '';
        }
        throw error;
      });
    activeClient = initializing;
  }
  return activeClient;
}

export async function resetE2EEClient(): Promise<void> {
  const previous = activeClient;
  activeGeneration += 1;
  activeClient = null;
  activeAccount = '';
  if (!previous) return;
  try {
    const client = await previous;
    await client.close();
  } catch {
    // Failed or superseded initializations clean up their own partial client.
  }
}

export async function clearActiveE2EEState(): Promise<void> {
  await resetE2EEClient();
  await clearAllLocalE2EEState();
}

export async function decryptConversationMessages(
  client: KaedeE2EEClient,
  channel: Channel,
  messages: readonly Message[]
): Promise<Message[]> {
  const applications = await client.decryptMessages(channel, messages);
  return messages.map((message, index) => {
    const application = applications[index];
    return application
      ? {
          ...message,
          decrypted_content: application.content,
          decrypted_attachments: application.attachments
        }
      : message;
  });
}
