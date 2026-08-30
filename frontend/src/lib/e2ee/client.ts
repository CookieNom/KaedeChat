import { api, ApiError } from '$lib/api/client';
import type { Channel, Message, MessageSnapshot, UserSummary } from '$lib/chat/types';
import { isNativeDesktop } from '$lib/platform/native';
import {
  base64url,
  clearBytes,
  concatBytes,
  decodeUtf8,
  fromBase64url,
  isCanonicalBase64url32,
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
  | 'application_id'
  | 'application_domain'
  | 'webhook_id'
  | 'webhook'
  | 'view_version'
  | 'view_persistent'
  | 'view_expires_at'
  | 'interaction_integration_type'
  | 'interaction_installation_ref'
  | 'interaction_installation_revision'
  | 'tts'
  | 'flags'
  | 'embeds'
  | 'components'
  | 'sticker_items'
  | 'mention_user_refs'
  | 'referenced_message_id'
  | 'referenced_message_domain'
  | 'message_reference'
  | 'referenced_message'
  | 'attachments'
  | 'poll'
  | 'forwarded_message_id'
  | 'forwarded_message_domain'
  | 'forwarded_message_ref'
  | 'forwarded_message'
  | 'message_snapshots'
  | 'created_at'
  | 'edited_at'
  | 'e2ee'
  | 'encryption_policy_generation'
  | 'encryption_epoch'
>;

interface RoomControlRecord extends EncryptedMessageRecord {
  /** Server-authored control-log instruction; false retains an audit copy only. */
  apply: boolean;
  room_operation_id: string;
  room_operation_domain: string;
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

export interface RichMessageAuthenticatedContext extends MessageContext {
  application_ref: string | null;
  author_ref: string;
  forward_projection_digest: string | null;
  forward_projection_version: 2 | null;
  forward_snapshot_digest: string | null;
  forward_source_projection_digest: string | null;
  forwarded_channel_ref: string | null;
  forwarded_created_at: string | null;
  forwarded_edited_at: string | null;
  forwarded_flags: number | null;
  forwarded_message_ref: string | null;
  forwarded_message_type: number | null;
  interaction_contract_digest: string | null;
  interaction_installation_ref: string | null;
  interaction_installation_revision: string | null;
  interaction_integration_type: InteractionIntegrationType | 'dm_capability' | null;
  message_attachment_refs: string[];
  message_custom_emoji_refs: string[];
  message_mention_everyone: boolean;
  message_mention_refs: string[];
  message_mention_role_refs: string[];
  message_mention_user_refs: string[];
  message_replied_user_ref: string | null;
  message_sticker_refs: string[];
  message_flags: number;
  message_revision: string;
  referenced_message_ref: string | null;
  rich_payload_digest: string;
  tts: boolean;
  view_persistent: boolean;
  view_version: string;
  voice_message: boolean;
}

export type EncryptedAllowedMentionParse = 'everyone' | 'roles' | 'users';

/** Discord-compatible notification policy kept inside authenticated rich ciphertext. */
export interface EncryptedAllowedMentions {
  parse: EncryptedAllowedMentionParse[];
  users: string[];
  roles: string[];
  replied_user: boolean;
}

interface RichPlaintextApplication {
  version: 2;
  kind: 'message';
  context: RichMessageAuthenticatedContext;
  data: Record<string, unknown>;
}

export interface EncryptedRichMessageOptions {
  embeds?: NonNullable<Message['embeds']>;
  components?: NonNullable<Message['components']>;
  poll?: Record<string, unknown> | null;
  stickerItems?: NonNullable<Message['sticker_items']>;
  tts?: boolean;
  voiceMessage?: boolean;
  flags?: number;
  allowedMentions?: EncryptedAllowedMentions;
  /** Authority-attested, author-free source body for an immutable forward. */
  forward?: EncryptedMessageForward;
  /** Positive monotonic revision; create is always one. */
  messageRevision?: string;
}

export interface EncryptedMessageForward {
  snapshot: Record<string, unknown>;
  sourceProjectionDigest: string;
  sourceMessageRef: string;
  sourceChannelRef: string;
  sourceCreatedAt: string;
  sourceEditedAt: string | null;
  sourceFlags: number;
  sourceMessageType: number;
}

interface MessageEncryptionOptions {
  operation?: 'create' | 'edit';
  targetMessage?: string;
  attachments?: EncryptedFileManifest[];
  mentionUserRefs?: string[];
  repliedUserRef?: string | null;
  referencedMessageRef?: string | null;
  rich?: EncryptedRichMessageOptions;
}

export type InteractionIntegrationType = 'guild_install' | 'user_install' | 'dm_capability';
export type InteractionChannelContext = 'guild' | 'bot_dm' | 'private_channel';
export type EncryptedInteractionType = 'command' | 'autocomplete' | 'component' | 'modal_submit';

export interface EncryptedInteractionInput {
  applicationRef: string;
  integrationType: InteractionIntegrationType;
  interactionContext: InteractionChannelContext;
  interactionType: EncryptedInteractionType;
  commandId?: string | null;
  commandName?: string | null;
  commandType?: 'chat_input' | 'user' | 'message' | null;
  componentType?: number | string | null;
  customId?: string | null;
  messageRef?: string | null;
  responseId?: string | number | null;
  targetRef?: string | null;
  viewVersion?: string | number | null;
  autocompleteGeneration?: string | number | null;
  focusedOption?: string | null;
  attachmentIds?: readonly string[];
  /** Authenticated encrypted-file manifests keyed by decimal attachment ID. */
  attachments?: Record<string, EncryptedFileManifest>;
  options?: Record<string, unknown>;
  values?: readonly string[];
  components?: readonly Record<string, unknown>[];
}

export interface InteractionAuthenticatedContext {
  application_ref: string;
  attachment_ids: string[];
  autocomplete_generation: string | null;
  channel_ref: string;
  command_id: string | null;
  command_name: string | null;
  command_type: 'chat_input' | 'user' | 'message' | null;
  component_type: number | string | null;
  context: InteractionChannelContext;
  custom_id: string | null;
  epoch: string;
  focused_option: string | null;
  group_id: string;
  integration_type: InteractionIntegrationType;
  interaction_type: EncryptedInteractionType;
  invoker_ref: string;
  message_ref: string | null;
  policy_generation: string;
  response_id: string | null;
  sender_device_id: string;
  target_ref: string | null;
  view_version: string | null;
}

export interface PreparedEncryptedInteraction {
  envelope: MlsEnvelope;
  context: InteractionAuthenticatedContext;
  attachmentIds: string[];
}

export interface EncryptedInteractionResponseInput {
  authorityDomain: string;
  interactionRef: string;
  responseRef: string;
  invokerRef: string;
  channelRef: string;
  applicationRef: string;
  sequence: number;
  revision: string;
  callbackType: number;
  operation: 'CREATE' | 'UPDATE';
  envelope: Record<string, unknown>;
  attachments: readonly unknown[];
}

export interface InteractionResponseAuthenticatedContext {
  application_ref: string;
  attachment_refs: string[];
  authority_domain: string;
  callback_type: number;
  channel_ref: string;
  epoch: string;
  group_id: string;
  interaction_ref: string;
  interaction_contract_digest: string | null;
  invoker_ref: string;
  operation: 'create' | 'edit';
  policy_generation: string;
  response_ref: string;
  revision: string;
  sender_device_id: string;
  sequence: string;
}

export interface DecryptedInteractionResponse {
  context: InteractionResponseAuthenticatedContext;
  data: Record<string, unknown>;
}

type InteractionAttachmentManifest = Omit<EncryptedFileManifest, 'preview'> & {
  attachment_id: string;
  attachment_domain: string;
};

export interface DecryptedApplication {
  content: string | null;
  attachments: EncryptedFileManifest[];
  embeds?: NonNullable<Message['embeds']>;
  components?: NonNullable<Message['components']>;
  poll?: Message['poll'];
  stickerItems?: NonNullable<Message['sticker_items']>;
  tts?: boolean;
  voiceMessage?: boolean;
  flags?: number;
  allowedMentions?: EncryptedAllowedMentions;
  forwardSnapshot?: Record<string, unknown> | null;
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

async function botIdentityDeviceId(
  applicationRef: string,
  workerId: string,
  identityKey: Uint8Array
): Promise<string> {
  const prefix = utf8(`kaede-bot-e2ee-device-v1\0${applicationRef}\0${workerId}\0`);
  const input = concatBytes(prefix, identityKey);
  let digest: Uint8Array | null = null;
  try {
    digest = await sha256(input);
    return `kbe_${base64url(digest)}`;
  } finally {
    clearBytes(prefix);
    clearBytes(input);
    if (digest) clearBytes(digest);
  }
}

export async function webhookIdentityDeviceId(
  webhookRef: string,
  identityKey: Uint8Array
): Promise<string> {
  const webhook = canonicalQualifiedRef(webhookRef);
  if (!webhook) throw new Error('The webhook encryption identity is invalid.');
  const prefix = utf8(`kaede-webhook-e2ee-device-v1\0${webhook.ref}\0`);
  const input = concatBytes(prefix, identityKey);
  let digest: Uint8Array | null = null;
  try {
    digest = await sha256(input);
    return `kwe_${base64url(digest)}`;
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

function canonicalJsonValue(value: unknown, seen: Set<object>): unknown {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('Encrypted interaction JSON must be finite.');
    return value;
  }
  if (Array.isArray(value)) return value.map((item) => canonicalJsonValue(item, seen));
  if (typeof value !== 'object')
    throw new TypeError('Encrypted interaction JSON contains an unsupported value.');
  if (seen.has(value)) throw new TypeError('Encrypted interaction JSON cannot be recursive.');
  seen.add(value);
  try {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>)
        .sort()
        .map((key) => [key, canonicalJsonValue((value as Record<string, unknown>)[key], seen)])
    );
  } finally {
    seen.delete(value);
  }
}

/** UTF-8 RFC-style canonical JSON used by the audited bot interaction contract. */
export function canonicalInteractionJson(value: unknown): Uint8Array {
  return utf8(JSON.stringify(canonicalJsonValue(value, new Set())));
}

const RICH_MESSAGE_CONTEXT_FIELDS = [
  'application_ref',
  'attachment_manifest_digest',
  'author_ref',
  'channel_ref',
  'epoch',
  'forward_projection_digest',
  'forward_projection_version',
  'forward_snapshot_digest',
  'forward_source_projection_digest',
  'forwarded_channel_ref',
  'forwarded_created_at',
  'forwarded_edited_at',
  'forwarded_flags',
  'forwarded_message_ref',
  'forwarded_message_type',
  'group_id',
  'interaction_contract_digest',
  'interaction_installation_ref',
  'interaction_installation_revision',
  'interaction_integration_type',
  'message_attachment_refs',
  'message_custom_emoji_refs',
  'message_mention_everyone',
  'message_mention_refs',
  'message_mention_role_refs',
  'message_mention_user_refs',
  'message_replied_user_ref',
  'message_sticker_refs',
  'message_flags',
  'message_revision',
  'operation',
  'policy_generation',
  'referenced_message_ref',
  'rich_payload_digest',
  'sender_device_id',
  'target_message',
  'tts',
  'view_persistent',
  'view_version',
  'voice_message'
] as const;

function canonicalUnsignedI63(value: unknown, positive: boolean): string | null {
  if (
    typeof value !== 'string' ||
    !(positive ? /^[1-9][0-9]{0,18}$/u : /^(?:0|[1-9][0-9]{0,18})$/u).test(value)
  ) {
    return null;
  }
  return BigInt(value) <= 9_223_372_036_854_775_807n ? value : null;
}

function canonicalSortedQualifiedRefList(value: unknown, maximum: number): value is string[] {
  if (!Array.isArray(value) || value.length > maximum) return false;
  const refs = value.filter((item): item is string => typeof item === 'string');
  return (
    refs.length === value.length &&
    refs.every((ref) => canonicalQualifiedRef(ref) !== null) &&
    new Set(refs).size === refs.length &&
    refs.every((ref, index) => index === 0 || refs[index - 1]! < ref)
  );
}

const CUSTOM_EMOJI_ROUTING_TOKEN =
  /^<(a?):([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>$/u;

function canonicalCustomEmojiRoutingToken(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  const match = CUSTOM_EMOJI_ROUTING_TOKEN.exec(value);
  return Boolean(match && canonicalQualifiedRef(`${match[3]}@${match[4]}`));
}

export function canonicalSortedCustomEmojiRefs(value: unknown): value is string[] {
  if (!Array.isArray(value) || value.length > 256) return false;
  const refs = value.filter((item): item is string => typeof item === 'string');
  return (
    refs.length === value.length &&
    refs.every(canonicalCustomEmojiRoutingToken) &&
    new Set(refs).size === refs.length &&
    refs.every((ref, index) => index === 0 || refs[index - 1]! < ref)
  );
}

const QUALIFIED_USER_MENTION = /<@([1-9][0-9]{0,18})@([a-z0-9.-]{1,253})>/giu;
const UNQUALIFIED_USER_MENTION = /<@[1-9][0-9]{0,18}>/u;
const QUALIFIED_ROLE_MENTION = /<@&([1-9][0-9]{0,18})@([a-z0-9.-]{1,253})>/giu;
const BROAD_EVERYONE_MENTION = /(?<![A-Za-z0-9_])@(?:everyone|here)\b/iu;

/** Validate the exact notification policy carried only in rich-message ciphertext. */
export function validateEncryptedAllowedMentions(value: unknown): EncryptedAllowedMentions {
  const raw = routingRecord(value, 'Encrypted message allowed mentions');
  if (
    !hasExactRoutingFields(raw, ['parse', 'users', 'roles', 'replied_user']) ||
    !Array.isArray(raw.parse) ||
    !Array.isArray(raw.users) ||
    !Array.isArray(raw.roles) ||
    typeof raw.replied_user !== 'boolean'
  ) {
    throw new Error('Encrypted message allowed mentions are invalid.');
  }
  const parse = raw.parse.filter(
    (item): item is EncryptedAllowedMentionParse =>
      item === 'everyone' || item === 'roles' || item === 'users'
  );
  if (
    parse.length !== raw.parse.length ||
    new Set(parse).size !== parse.length ||
    parse.some((item, index) => index > 0 && parse[index - 1]! >= item) ||
    !canonicalSortedQualifiedRefList(raw.users, 100) ||
    !canonicalSortedQualifiedRefList(raw.roles, 100) ||
    (parse.includes('users') && raw.users.length > 0) ||
    (parse.includes('roles') && raw.roles.length > 0)
  ) {
    throw new Error('Encrypted message allowed mentions are invalid.');
  }
  return {
    parse,
    users: [...raw.users],
    roles: [...raw.roles],
    replied_user: raw.replied_user
  };
}

export interface RichMessageMentionIntent {
  userRefs: string[];
  roleRefs: string[];
  everyone: boolean;
}

/** Derive only Discord notification-bearing mention intent from authenticated rich data. */
export function richMessageMentionIntent(data: Record<string, unknown>): RichMessageMentionIntent {
  const policy = validateEncryptedAllowedMentions(data.allowed_mentions);
  const texts: string[] = [];
  if (typeof data.content === 'string') texts.push(data.content);
  const seen = new Set<object>();
  const walkComponents = (value: unknown): void => {
    if (Array.isArray(value)) {
      if (seen.has(value)) throw new Error('Encrypted message components cannot be recursive.');
      seen.add(value);
      try {
        value.forEach(walkComponents);
      } finally {
        seen.delete(value);
      }
      return;
    }
    if (!value || typeof value !== 'object') return;
    if (seen.has(value)) throw new Error('Encrypted message components cannot be recursive.');
    seen.add(value);
    try {
      const raw = value as Record<string, unknown>;
      if (raw.type === 10 && typeof raw.content === 'string') texts.push(raw.content);
      Object.values(raw).forEach(walkComponents);
    } finally {
      seen.delete(value);
    }
  };
  walkComponents(data.components);

  const visibleUsers = new Set<string>();
  const visibleRoles = new Set<string>();
  let visibleEveryone = false;
  for (const text of texts) {
    if (UNQUALIFIED_USER_MENTION.test(text)) {
      throw new Error('Encrypted user mention tokens must be origin-qualified.');
    }
    for (const match of text.matchAll(QUALIFIED_USER_MENTION)) {
      const ref = canonicalQualifiedRef(`${match[1]}@${match[2]!.toLowerCase()}`);
      if (!ref) throw new Error('Encrypted user mention token is invalid.');
      visibleUsers.add(ref.ref);
    }
    for (const match of text.matchAll(QUALIFIED_ROLE_MENTION)) {
      const ref = canonicalQualifiedRef(`${match[1]}@${match[2]!.toLowerCase()}`);
      if (!ref) throw new Error('Encrypted role mention token is invalid.');
      visibleRoles.add(ref.ref);
    }
    visibleEveryone ||= BROAD_EVERYONE_MENTION.test(text);
  }
  const parse = new Set(policy.parse);
  const explicitUsers = new Set(policy.users);
  const explicitRoles = new Set(policy.roles);
  return {
    userRefs: [...visibleUsers]
      .filter((ref) => parse.has('users') || explicitUsers.has(ref))
      .sort(),
    roleRefs: [...visibleRoles]
      .filter((ref) => parse.has('roles') || explicitRoles.has(ref))
      .sort(),
    everyone: parse.has('everyone') && visibleEveryone
  };
}

function canonicalTimezoneTimestamp(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value) &&
    Number.isFinite(Date.parse(value))
  );
}

/** Validate the exact canonical context authenticated by rich Message v2 MLS payloads. */
export function validateRichMessageAuthenticatedContext(
  value: unknown
): RichMessageAuthenticatedContext {
  const raw = routingRecord(value, 'Encrypted rich message context');
  const attachmentRefs = raw.message_attachment_refs;
  const applicationRef = raw.application_ref;
  const integrationType = raw.interaction_integration_type;
  const installationRef = raw.interaction_installation_ref;
  const installationRevision = raw.interaction_installation_revision;
  const lineage = [applicationRef, integrationType, installationRef, installationRevision];
  const hasLineage = lineage.some((item) => item !== null);
  const operation = raw.operation;
  const revision = canonicalUnsignedI63(raw.message_revision, true);
  const viewVersion = canonicalUnsignedI63(raw.view_version, false);
  const forwardRequired = [
    raw.forwarded_message_ref,
    raw.forwarded_channel_ref,
    raw.forward_snapshot_digest,
    raw.forward_source_projection_digest,
    raw.forwarded_created_at,
    raw.forwarded_flags,
    raw.forwarded_message_type
  ];
  const hasForward = forwardRequired.some((item) => item !== null);
  const forwardMetadataValid = hasForward
    ? forwardRequired.every((item) => item !== null) &&
      canonicalQualifiedRef(raw.forwarded_message_ref) !== null &&
      canonicalQualifiedRef(raw.forwarded_channel_ref) !== null &&
      isCanonicalBase64url32(raw.forward_snapshot_digest) &&
      isCanonicalBase64url32(raw.forward_source_projection_digest) &&
      canonicalTimezoneTimestamp(raw.forwarded_created_at) &&
      (raw.forwarded_edited_at === null ||
        (canonicalTimezoneTimestamp(raw.forwarded_edited_at) &&
          Date.parse(raw.forwarded_edited_at) >= Date.parse(raw.forwarded_created_at as string))) &&
      Number.isSafeInteger(raw.forwarded_flags) &&
      (Number(raw.forwarded_flags) & ~((1 << 2) | (1 << 13) | (1 << 15))) === 0 &&
      [0, 19, 20, 23].includes(Number(raw.forwarded_message_type))
    : raw.forwarded_edited_at === null;
  if (
    !hasExactRoutingFields(raw, RICH_MESSAGE_CONTEXT_FIELDS) ||
    !canonicalQualifiedRef(raw.channel_ref) ||
    !canonicalQualifiedRef(raw.author_ref) ||
    typeof raw.group_id !== 'string' ||
    !canonicalUnsignedI63(raw.policy_generation, true) ||
    !canonicalUnsignedI63(raw.epoch, false) ||
    typeof raw.sender_device_id !== 'string' ||
    !/^(?:ked|kbe|kwe)_[A-Za-z0-9_-]{43}$/u.test(raw.sender_device_id) ||
    !['create', 'edit'].includes(String(operation)) ||
    !revision ||
    (operation === 'create' && (revision !== '1' || raw.target_message !== null)) ||
    (operation === 'edit' &&
      (BigInt(revision) <= 1n || !canonicalQualifiedRef(raw.target_message))) ||
    !Array.isArray(attachmentRefs) ||
    !canonicalSortedQualifiedRefList(attachmentRefs, 10) ||
    !canonicalSortedQualifiedRefList(raw.message_mention_refs, 5_000) ||
    !canonicalSortedQualifiedRefList(raw.message_mention_user_refs, 100) ||
    !canonicalSortedQualifiedRefList(raw.message_mention_role_refs, 100) ||
    typeof raw.message_mention_everyone !== 'boolean' ||
    (raw.message_replied_user_ref !== null &&
      !canonicalQualifiedRef(raw.message_replied_user_ref)) ||
    !canonicalSortedQualifiedRefList(raw.message_sticker_refs, 9) ||
    !canonicalSortedCustomEmojiRefs(raw.message_custom_emoji_refs) ||
    (raw.referenced_message_ref !== null && !canonicalQualifiedRef(raw.referenced_message_ref)) ||
    !isCanonicalBase64url32(raw.rich_payload_digest) ||
    (raw.attachment_manifest_digest !== null &&
      !isCanonicalBase64url32(raw.attachment_manifest_digest)) ||
    (raw.interaction_contract_digest !== null &&
      !isCanonicalBase64url32(raw.interaction_contract_digest)) ||
    (raw.forward_projection_digest !== null &&
      !isCanonicalBase64url32(raw.forward_projection_digest)) ||
    (raw.forward_projection_digest === null
      ? raw.forward_projection_version !== null
      : raw.forward_projection_version !== 2) ||
    !forwardMetadataValid ||
    Boolean(attachmentRefs.length) !== (raw.attachment_manifest_digest !== null) ||
    !Number.isSafeInteger(raw.message_flags) ||
    Number(raw.message_flags) < 0 ||
    Number(raw.message_flags) > 2_147_483_647 ||
    typeof raw.tts !== 'boolean' ||
    typeof raw.voice_message !== 'boolean' ||
    (raw.tts && raw.voice_message) ||
    (raw.voice_message && attachmentRefs.length !== 1) ||
    !viewVersion ||
    typeof raw.view_persistent !== 'boolean' ||
    (hasLineage
      ? lineage.some((item) => item === null) ||
        !canonicalQualifiedRef(applicationRef) ||
        !canonicalQualifiedRef(installationRef) ||
        !['guild_install', 'user_install', 'dm_capability'].includes(String(integrationType)) ||
        !canonicalUnsignedI63(installationRevision, true)
      : lineage.some((item) => item !== null))
  ) {
    throw new Error('Encrypted rich message context is invalid.');
  }
  return canonicalJsonValue(raw, new Set()) as unknown as RichMessageAuthenticatedContext;
}

export function richMessageAuthenticatedData(context: RichMessageAuthenticatedContext): Uint8Array {
  return canonicalInteractionJson({ context, purpose: 'kaede.message.rich.v1' });
}

export async function richMessagePayloadDigest(data: Record<string, unknown>): Promise<string> {
  const bytes = canonicalInteractionJson(data);
  try {
    return base64url(await sha256(bytes));
  } finally {
    clearBytes(bytes);
  }
}

function routingRecord(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value as Record<string, unknown>;
}

function hasExactRoutingFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  return Object.keys(value).sort().join(',') === [...fields].sort().join(',');
}

function routingText(value: unknown, label: string): string {
  if (typeof value !== 'string' || [...value].length < 1 || [...value].length > 100) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function routingInteger(value: unknown, label: string, minimum: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    throw new Error(`${label} is invalid.`);
  }
  return Number(value);
}

function validateRoutingOptionDigests(value: unknown, maximum: number): string[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > maximum) {
    throw new Error('Interaction routing option digests are invalid.');
  }
  const digests = value.map((item) => {
    if (!isCanonicalBase64url32(item)) {
      throw new Error('Interaction routing option digest is invalid.');
    }
    return item;
  });
  const sorted = [...digests].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
  if (
    new Set(digests).size !== digests.length ||
    digests.some((digest, index) => digest !== sorted[index])
  ) {
    throw new Error('Interaction routing option digests must be sorted and unique.');
  }
  return digests;
}

function validateRoutingPoll(value: unknown): Record<string, unknown> {
  const raw = routingRecord(value, 'Encrypted poll routing contract');
  const answerIds = raw.answer_ids;
  const durationSeconds = routingInteger(
    raw.duration_seconds,
    'Encrypted poll duration',
    3_600,
    2_764_800
  );
  if (
    !hasExactRoutingFields(raw, [
      'version',
      'answer_ids',
      'allow_multiselect',
      'duration_seconds',
      'layout_type'
    ]) ||
    raw.version !== 1 ||
    !Array.isArray(answerIds) ||
    answerIds.length < 2 ||
    answerIds.length > 10 ||
    answerIds.some((answerId, index) => answerId !== index + 1) ||
    typeof raw.allow_multiselect !== 'boolean' ||
    durationSeconds % 3_600 !== 0 ||
    raw.layout_type !== 1
  ) {
    throw new Error('Encrypted poll routing contract is invalid.');
  }
  return canonicalJsonValue(raw, new Set()) as Record<string, unknown>;
}

function routingPoll(data: unknown): Record<string, unknown> {
  const raw = routingRecord(data, 'Encrypted poll');
  const answers = raw.answers;
  const durationHours = routingInteger(raw.duration, 'Encrypted poll duration', 1, 768);
  if (
    !Array.isArray(answers) ||
    answers.length < 2 ||
    answers.length > 10 ||
    typeof raw.allow_multiselect !== 'boolean' ||
    raw.layout_type !== 1
  ) {
    throw new Error('Encrypted poll is invalid.');
  }
  return validateRoutingPoll({
    version: 1,
    answer_ids: answers.map((_, index) => index + 1),
    allow_multiselect: raw.allow_multiselect,
    duration_seconds: durationHours * 3_600,
    layout_type: 1
  });
}

async function routingOptionDigests(value: unknown, maximum: number): Promise<string[]> {
  if (!Array.isArray(value) || value.length < 1 || value.length > maximum) {
    throw new Error('Interaction routing options are invalid.');
  }
  const values = value.map((item) =>
    routingText(routingRecord(item, 'Interaction routing option').value, 'Interaction option value')
  );
  if (new Set(values).size !== values.length) {
    throw new Error('Interaction routing option values must be unique.');
  }
  const digests = await Promise.all(
    values.map(async (item) => {
      const bytes = utf8(item);
      try {
        return base64url(await sha256(bytes));
      } finally {
        clearBytes(bytes);
      }
    })
  );
  return digests.sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
}

function validateRoutingControl(value: unknown, modal: boolean): Record<string, unknown> {
  const raw = routingRecord(value, 'Interaction routing control');
  const type = routingInteger(raw.type, 'Interaction routing control type', 0, 2_147_483_647);
  const allowed = new Set([3, 5, 6, 7, 8, ...(modal ? [4, 19, 21, 22, 23] : [2])]);
  const customId = routingText(raw.custom_id, 'Interaction routing custom ID');
  if (!allowed.has(type)) throw new Error('Interaction routing control type is invalid.');
  if (type === 2) {
    if (
      !hasExactRoutingFields(raw, ['type', 'custom_id', 'disabled']) ||
      typeof raw.disabled !== 'boolean'
    ) {
      throw new Error('Interaction routing button is invalid.');
    }
  } else if ([3, 5, 6, 7, 8].includes(type)) {
    const fields = [
      'type',
      'custom_id',
      'disabled',
      'min_values',
      'max_values',
      ...(modal ? ['required'] : []),
      ...(type === 3 ? ['option_value_digests'] : []),
      ...(type === 8 ? ['channel_types'] : [])
    ];
    const minimum = routingInteger(raw.min_values, 'Interaction routing minimum values', 0, 25);
    const maximum = routingInteger(
      raw.max_values,
      'Interaction routing maximum values',
      minimum,
      25
    );
    if (
      !hasExactRoutingFields(raw, fields) ||
      typeof raw.disabled !== 'boolean' ||
      (modal && typeof raw.required !== 'boolean') ||
      (modal && raw.disabled) ||
      (modal && raw.required === true && minimum === 0)
    ) {
      throw new Error('Interaction routing select is invalid.');
    }
    if (type === 3 && maximum > validateRoutingOptionDigests(raw.option_value_digests, 25).length) {
      throw new Error('Interaction routing select range is invalid.');
    }
    if (type === 8) {
      if (
        !Array.isArray(raw.channel_types) ||
        raw.channel_types.length > 19 ||
        raw.channel_types.some(
          (item) => !Number.isSafeInteger(item) || Number(item) < 0 || Number(item) > 2_147_483_647
        ) ||
        new Set(raw.channel_types).size !== raw.channel_types.length
      ) {
        throw new Error('Interaction routing channel filter is invalid.');
      }
    }
  } else if (type === 4) {
    const minimum = routingInteger(raw.min_length, 'Interaction routing minimum length', 0, 4_000);
    routingInteger(raw.max_length, 'Interaction routing maximum length', minimum, 4_000);
    if (
      !hasExactRoutingFields(raw, ['type', 'custom_id', 'required', 'min_length', 'max_length']) ||
      typeof raw.required !== 'boolean'
    ) {
      throw new Error('Interaction routing text input is invalid.');
    }
  } else if (type === 19) {
    const minimum = routingInteger(raw.min_values, 'Interaction routing minimum files', 0, 10);
    routingInteger(raw.max_values, 'Interaction routing maximum files', minimum, 10);
    if (
      !hasExactRoutingFields(raw, [
        'type',
        'custom_id',
        'required',
        'min_values',
        'max_values',
        'file_types'
      ]) ||
      typeof raw.required !== 'boolean' ||
      (raw.required && minimum === 0) ||
      !Array.isArray(raw.file_types) ||
      raw.file_types.length > 10 ||
      raw.file_types.some((item) => typeof item !== 'string' || !item || [...item].length > 100) ||
      new Set(raw.file_types).size !== raw.file_types.length
    ) {
      throw new Error('Interaction routing file input is invalid.');
    }
  } else if (type === 21 || type === 22) {
    const fields = [
      'type',
      'custom_id',
      'required',
      'option_value_digests',
      ...(type === 22 ? ['min_values', 'max_values'] : [])
    ];
    const optionDigests = validateRoutingOptionDigests(raw.option_value_digests, 10);
    if (!hasExactRoutingFields(raw, fields) || typeof raw.required !== 'boolean') {
      throw new Error('Interaction routing choice input is invalid.');
    }
    if (type === 22) {
      const minimum = routingInteger(
        raw.min_values,
        'Interaction routing minimum choices',
        0,
        optionDigests.length
      );
      routingInteger(
        raw.max_values,
        'Interaction routing maximum choices',
        minimum,
        optionDigests.length
      );
      if (raw.required && minimum === 0) {
        throw new Error('Interaction routing required choices are invalid.');
      }
    }
  } else if (!hasExactRoutingFields(raw, ['type', 'custom_id'])) {
    throw new Error('Interaction routing checkbox is invalid.');
  }
  return canonicalJsonValue({ ...raw, custom_id: customId }, new Set()) as Record<string, unknown>;
}

/** Validate and canonicalize the sole public routing metadata allowed beside encrypted content. */
export function validateInteractionRoutingContract(
  value: unknown,
  callbackType: number | null
): Record<string, unknown> {
  const raw = routingRecord(value, 'Interaction routing contract');
  if (raw.version !== 1) throw new Error('Interaction routing contract is invalid.');
  if (raw.kind === 'message') {
    const hasPoll = Object.hasOwn(raw, 'poll');
    if (
      ![null, 4, 7].includes(callbackType) ||
      !hasExactRoutingFields(raw, [
        'version',
        'kind',
        'view_timeout_seconds',
        'components',
        ...(hasPoll ? ['poll'] : [])
      ]) ||
      !Array.isArray(raw.components) ||
      (!raw.components.length && !hasPoll) ||
      raw.components.length > 40
    ) {
      throw new Error('Interaction message routing contract is invalid.');
    }
    routingInteger(raw.view_timeout_seconds, 'Interaction view timeout', 1, 86_400);
    const controls = raw.components.map((item) => validateRoutingControl(item, false));
    const ids = controls.map((item) => String(item.custom_id));
    if (new Set(ids).size !== ids.length) {
      throw new Error('Interaction routing custom IDs must be unique.');
    }
    if (hasPoll) validateRoutingPoll(raw.poll);
  } else if (raw.kind === 'modal') {
    if (
      callbackType !== 9 ||
      !hasExactRoutingFields(raw, ['version', 'kind', 'custom_id', 'components']) ||
      !Array.isArray(raw.components) ||
      raw.components.length < 1 ||
      raw.components.length > 5
    ) {
      throw new Error('Interaction modal routing contract is invalid.');
    }
    routingText(raw.custom_id, 'Modal custom ID');
    const ids: string[] = [];
    for (const item of raw.components) {
      const row = routingRecord(item, 'Interaction modal routing row');
      if (row.type === 1) {
        if (
          !hasExactRoutingFields(row, ['type', 'components']) ||
          !Array.isArray(row.components) ||
          row.components.length !== 1
        ) {
          throw new Error('Interaction modal routing row is invalid.');
        }
        ids.push(String(validateRoutingControl(row.components[0], true).custom_id));
      } else if (row.type === 18) {
        if (!hasExactRoutingFields(row, ['type', 'component'])) {
          throw new Error('Interaction modal routing row is invalid.');
        }
        ids.push(String(validateRoutingControl(row.component, true).custom_id));
      } else {
        throw new Error('Interaction modal routing row is invalid.');
      }
    }
    if (new Set(ids).size !== ids.length) {
      throw new Error('Interaction routing custom IDs must be unique.');
    }
  } else {
    throw new Error('Interaction routing contract kind is invalid.');
  }
  return canonicalJsonValue(raw, new Set()) as Record<string, unknown>;
}

async function routingControl(
  value: unknown,
  modal: boolean
): Promise<Record<string, unknown> | null> {
  const raw = routingRecord(value, 'Interaction routing control');
  const type = routingInteger(raw.type, 'Interaction routing control type', 0, 2_147_483_647);
  if (type === 2 && raw.custom_id == null) return null;
  const allowed = new Set([3, 5, 6, 7, 8, ...(modal ? [4, 19, 21, 22, 23] : [2])]);
  if (!allowed.has(type)) return null;
  const customId = routingText(raw.custom_id, 'Interaction routing custom ID');
  if (type === 2) {
    if (raw.disabled !== undefined && typeof raw.disabled !== 'boolean') {
      throw new Error('Interaction routing button state is invalid.');
    }
    return { type: 2, custom_id: customId, disabled: raw.disabled ?? false };
  }
  if ([3, 5, 6, 7, 8].includes(type)) {
    const minimum = routingInteger(
      raw.min_values === undefined ? 1 : raw.min_values,
      'Interaction routing minimum values',
      0,
      25
    );
    const maximum = routingInteger(
      raw.max_values === undefined ? 1 : raw.max_values,
      'Interaction routing maximum values',
      minimum,
      25
    );
    if (raw.disabled !== undefined && typeof raw.disabled !== 'boolean') {
      throw new Error('Interaction routing select state is invalid.');
    }
    const disabled = raw.disabled ?? false;
    const required = raw.required !== false;
    if (modal && (disabled || (required && minimum === 0))) {
      throw new Error('Interaction routing modal select state is invalid.');
    }
    const result: Record<string, unknown> = {
      type,
      custom_id: customId,
      disabled,
      min_values: minimum,
      max_values: maximum
    };
    if (modal) result.required = required;
    if (type === 3) {
      const digests = await routingOptionDigests(raw.options, 25);
      if (maximum > digests.length) throw new Error('Interaction routing select range is invalid.');
      result.option_value_digests = digests;
    }
    if (type === 8) {
      const channelTypes = raw.channel_types === undefined ? [] : raw.channel_types;
      if (
        !Array.isArray(channelTypes) ||
        channelTypes.length > 19 ||
        channelTypes.some(
          (item) => !Number.isSafeInteger(item) || Number(item) < 0 || Number(item) > 2_147_483_647
        ) ||
        new Set(channelTypes).size !== channelTypes.length
      ) {
        throw new Error('Interaction routing channel types are invalid.');
      }
      result.channel_types = [...channelTypes];
    }
    return result;
  }
  if (type === 4) {
    const minimum = routingInteger(
      raw.min_length ?? 0,
      'Interaction routing minimum length',
      0,
      4_000
    );
    const maximum = routingInteger(
      raw.max_length ?? 4_000,
      'Interaction routing maximum length',
      1,
      4_000
    );
    if (minimum > maximum) throw new Error('Interaction routing text length range is invalid.');
    return {
      type: 4,
      custom_id: customId,
      required: raw.required !== false,
      min_length: minimum,
      max_length: maximum
    };
  }
  if (type === 19) {
    const fileTypes = raw.file_types === undefined ? [] : raw.file_types;
    if (
      !Array.isArray(fileTypes) ||
      fileTypes.length > 10 ||
      fileTypes.some((item) => typeof item !== 'string' || !item || [...item].length > 100) ||
      new Set(fileTypes).size !== fileTypes.length
    ) {
      throw new Error('Interaction routing file types are invalid.');
    }
    const minimum = routingInteger(
      raw.min_values === undefined ? 1 : raw.min_values,
      'Interaction routing minimum files',
      0,
      10
    );
    const maximum = routingInteger(
      raw.max_values === undefined ? 1 : raw.max_values,
      'Interaction routing maximum files',
      1,
      10
    );
    const required = raw.required !== false;
    if (minimum > maximum || (required && minimum === 0)) {
      throw new Error('Interaction routing file range is invalid.');
    }
    return {
      type: 19,
      custom_id: customId,
      required,
      min_values: minimum,
      max_values: maximum,
      file_types: [...fileTypes]
    };
  }
  if (type === 21 || type === 22) {
    const digests = await routingOptionDigests(raw.options, 10);
    const required = raw.required !== false;
    const result: Record<string, unknown> = {
      type,
      custom_id: customId,
      required,
      option_value_digests: digests
    };
    if (type === 22) {
      const minimum = routingInteger(
        raw.min_values === undefined ? 1 : raw.min_values,
        'Interaction routing minimum choices',
        0,
        digests.length
      );
      const maximum = routingInteger(
        raw.max_values === undefined ? digests.length : raw.max_values,
        'Interaction routing maximum choices',
        minimum,
        digests.length
      );
      if (required && minimum === 0) {
        throw new Error('Interaction routing required choices are invalid.');
      }
      result.min_values = minimum;
      result.max_values = maximum;
    }
    return result;
  }
  return { type: 23, custom_id: customId };
}

function routingControlNodes(
  value: unknown,
  seen: Set<object>,
  depth = 0
): Record<string, unknown>[] {
  if (depth > 8) throw new Error('Interaction routing components are too deeply nested.');
  const raw = routingRecord(value, 'Interaction component');
  if (!seen.add(raw)) throw new Error('Interaction routing components cannot be recursive.');
  try {
    const result = [raw];
    if (raw.components !== undefined) {
      if (!Array.isArray(raw.components))
        throw new Error('Interaction component children are invalid.');
      for (const child of raw.components)
        result.push(...routingControlNodes(child, seen, depth + 1));
    }
    for (const key of ['component', 'accessory'] as const) {
      if (raw[key] != null) result.push(...routingControlNodes(raw[key], seen, depth + 1));
    }
    return result;
  } finally {
    seen.delete(raw);
  }
}

/** Derive the privacy-preserving routing contract from authenticated decrypted content. */
export async function interactionRoutingContract(
  data: Record<string, unknown>,
  callbackType: number | null
): Promise<Record<string, unknown> | null> {
  if (callbackType === 8) return null;
  if (callbackType === 9) {
    const customId = routingText(data.custom_id, 'Modal custom ID');
    if (!Array.isArray(data.components)) throw new Error('Modal components are invalid.');
    const rows: Record<string, unknown>[] = [];
    const ids: string[] = [];
    for (const item of data.components) {
      const row = routingRecord(item, 'Modal row');
      if (row.type === 10) continue;
      if (row.type === 1) {
        if (!Array.isArray(row.components) || row.components.length !== 1) {
          throw new Error('Modal row is invalid.');
        }
        const field = await routingControl(row.components[0], true);
        if (!field) throw new Error('Modal input is invalid.');
        rows.push({ type: 1, components: [field] });
        ids.push(String(field.custom_id));
      } else if (row.type === 18) {
        const field = await routingControl(row.component, true);
        if (!field) throw new Error('Modal input is invalid.');
        rows.push({ type: 18, component: field });
        ids.push(String(field.custom_id));
      } else {
        throw new Error('Modal row is invalid.');
      }
    }
    if (rows.length < 1 || rows.length > 5 || new Set(ids).size !== ids.length) {
      throw new Error('Modal routing contract is invalid.');
    }
    return validateInteractionRoutingContract(
      { version: 1, kind: 'modal', custom_id: customId, components: rows },
      callbackType
    );
  }
  if (callbackType !== null && callbackType !== 4 && callbackType !== 7) {
    throw new Error('Interaction routing callback type is invalid.');
  }
  const components = data.components ?? [];
  if (!Array.isArray(components)) throw new Error('Message components are invalid.');
  const controls: Record<string, unknown>[] = [];
  for (const layout of components) {
    for (const item of routingControlNodes(layout, new Set())) {
      const control = await routingControl(item, false);
      if (control) controls.push(control);
    }
  }
  const poll = data.poll == null ? null : routingPoll(data.poll);
  if (!controls.length && !poll) return null;
  const ids = controls.map((item) => String(item.custom_id));
  if (new Set(ids).size !== ids.length) {
    throw new Error('Interaction routing custom IDs must be unique.');
  }
  return validateInteractionRoutingContract(
    {
      version: 1,
      kind: 'message',
      view_timeout_seconds: routingInteger(
        data.view_timeout_seconds === undefined ? 900 : data.view_timeout_seconds,
        'Interaction view timeout',
        1,
        86_400
      ),
      components: controls,
      ...(poll ? { poll } : {})
    },
    callbackType
  );
}

export async function interactionRoutingContractDigest(
  contract: Record<string, unknown>
): Promise<string> {
  const bytes = canonicalInteractionJson(contract);
  try {
    return base64url(await sha256(bytes));
  } finally {
    clearBytes(bytes);
  }
}

async function validateInteractionRoutingContractForData(
  data: Record<string, unknown>,
  callbackType: number,
  expectedContract: Record<string, unknown> | null,
  expectedDigest: string | null
): Promise<void> {
  const derived = await interactionRoutingContract(data, callbackType);
  if (!derived || !expectedContract || !expectedDigest) {
    if (derived !== null || expectedContract !== null || expectedDigest !== null) {
      throw new Error('The encrypted bot response routing contract does not match its content.');
    }
    return;
  }
  const expected = canonicalInteractionJson(expectedContract);
  const actual = canonicalInteractionJson(derived);
  try {
    if (
      !sameBytes(expected, actual) ||
      (await interactionRoutingContractDigest(derived)) !== expectedDigest
    ) {
      throw new Error('The encrypted bot response routing contract does not match its content.');
    }
  } finally {
    clearBytes(expected);
    clearBytes(actual);
  }
}

function optionalInteractionInteger(
  value: string | number | null | undefined,
  label: string
): string | null {
  if (value == null) return null;
  if (typeof value === 'number' && (!Number.isSafeInteger(value) || value < 1)) {
    throw new Error(`Encrypted interaction ${label} is invalid.`);
  }
  const rendered = typeof value === 'number' ? String(value) : value;
  if (!/^[1-9][0-9]{0,18}$/u.test(rendered) || BigInt(rendered) > 9_223_372_036_854_775_807n) {
    throw new Error(`Encrypted interaction ${label} is invalid.`);
  }
  return rendered;
}

function canonicalInteractionAttachmentIds(values: readonly string[]): string[] {
  if (values.length > 10) throw new Error('Encrypted interactions accept at most 10 files.');
  const unique = new Set<string>();
  for (const value of values) {
    if (!/^[1-9][0-9]{0,18}$/u.test(value) || BigInt(value) > 9_223_372_036_854_775_807n)
      throw new Error('Encrypted interaction attachment ID is invalid.');
    if (unique.has(value)) throw new Error('Encrypted interaction attachment IDs must be unique.');
    unique.add(value);
  }
  return [...unique].sort((left, right) => (BigInt(left) < BigInt(right) ? -1 : 1));
}

function interactionAttachmentManifests(
  attachmentIds: readonly string[],
  values: Record<string, unknown>,
  allowVoiceMetadata = false
): Record<string, InteractionAttachmentManifest> {
  if (
    Object.keys(values).length !== attachmentIds.length ||
    Object.keys(values).some((key) => !attachmentIds.includes(key))
  ) {
    throw new Error('Encrypted interaction file manifests must match the uploaded files exactly.');
  }
  return Object.fromEntries(
    attachmentIds.map((attachmentId) => {
      const value = values[attachmentId];
      if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error('Encrypted interaction file manifest authority is invalid.');
      }
      const manifest = value as Record<string, unknown>;
      const hasDuration = Object.hasOwn(manifest, 'duration_millis');
      const hasWaveform = Object.hasOwn(manifest, 'waveform');
      const voiceMetadata = hasDuration && hasWaveform;
      const fields = [
        'attachment_domain',
        'attachment_id',
        'chunk_size',
        'ciphertext_sha256',
        'ciphertext_size',
        'content_type',
        ...(voiceMetadata ? ['duration_millis'] : []),
        'file_id',
        'filename',
        'key',
        'plaintext_sha256',
        'plaintext_size',
        'protocol',
        'version',
        ...(voiceMetadata ? ['waveform'] : [])
      ];
      if (
        hasDuration !== hasWaveform ||
        (voiceMetadata && !allowVoiceMetadata) ||
        Object.keys(manifest).sort().join(',') !== fields.sort().join(',') ||
        manifest.version !== 1 ||
        manifest.protocol !== 'kaede-file-v1' ||
        manifest.attachment_id !== attachmentId ||
        typeof manifest.attachment_domain !== 'string' ||
        !isCanonicalFederationDomain(manifest.attachment_domain)
      ) {
        throw new Error('Encrypted interaction file manifest authority is invalid.');
      }
      const filename = manifest.filename;
      const contentType = manifest.content_type;
      const plaintextSize = manifest.plaintext_size;
      const ciphertextSize = manifest.ciphertext_size;
      const chunkSize = manifest.chunk_size;
      if (
        typeof filename !== 'string' ||
        !filename ||
        filename !== filename.trim() ||
        filename.length > 255 ||
        [...filename].some((character) => {
          const code = character.codePointAt(0) ?? 0;
          return code <= 0x1f || code === 0x7f;
        }) ||
        typeof contentType !== 'string' ||
        contentType !== contentType.toLowerCase() ||
        contentType.length > 100 ||
        !/^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/u.test(contentType) ||
        !Number.isInteger(plaintextSize) ||
        Number(plaintextSize) < 1 ||
        Number(plaintextSize) > 64 * 1024 * 1024 ||
        !Number.isInteger(chunkSize) ||
        Number(chunkSize) < 64 * 1024 ||
        Number(chunkSize) > 1024 * 1024 ||
        !Number.isInteger(ciphertextSize) ||
        Number(ciphertextSize) !==
          Number(plaintextSize) + 41 + Math.ceil(Number(plaintextSize) / Number(chunkSize)) * 20 ||
        typeof manifest.file_id !== 'string' ||
        !/^[A-Za-z0-9_-]{21}[AQgw]$/u.test(manifest.file_id) ||
        typeof manifest.key !== 'string' ||
        !isCanonicalBase64url32(manifest.key) ||
        typeof manifest.ciphertext_sha256 !== 'string' ||
        !isCanonicalBase64url32(manifest.ciphertext_sha256) ||
        typeof manifest.plaintext_sha256 !== 'string' ||
        !isCanonicalBase64url32(manifest.plaintext_sha256)
      ) {
        throw new Error('Encrypted interaction file manifest is invalid.');
      }
      if (voiceMetadata) {
        const durationMillis = manifest.duration_millis;
        const waveform = manifest.waveform;
        let decodedWaveform: Uint8Array | null = null;
        try {
          decodedWaveform =
            typeof waveform === 'string' &&
            waveform.length >= 4 &&
            waveform.length <= 344 &&
            /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u.test(waveform)
              ? Uint8Array.from(atob(waveform), (character) => character.charCodeAt(0))
              : null;
          if (
            !Number.isSafeInteger(durationMillis) ||
            Number(durationMillis) < 1 ||
            Number(durationMillis) > 1_200_000 ||
            !decodedWaveform ||
            decodedWaveform.length < 1 ||
            decodedWaveform.length > 256 ||
            btoa(String.fromCharCode(...decodedWaveform)) !== waveform
          ) {
            throw new Error('Encrypted voice message metadata is invalid.');
          }
        } finally {
          if (decodedWaveform) clearBytes(decodedWaveform);
        }
      }
      return [
        attachmentId,
        {
          version: 1 as const,
          protocol: 'kaede-file-v1' as const,
          file_id: manifest.file_id,
          key: manifest.key,
          filename,
          content_type: contentType,
          plaintext_size: Number(plaintextSize),
          plaintext_sha256: manifest.plaintext_sha256,
          ciphertext_size: Number(ciphertextSize),
          ciphertext_sha256: manifest.ciphertext_sha256,
          chunk_size: Number(chunkSize),
          attachment_id: attachmentId,
          attachment_domain: manifest.attachment_domain,
          ...(voiceMetadata
            ? {
                duration_millis: Number(manifest.duration_millis),
                waveform: manifest.waveform as string
              }
            : {})
        }
      ];
    })
  );
}

function validatedEncryptedMessageAttachments(
  value: unknown,
  allowVoiceMetadata = false
): EncryptedFileManifest[] {
  if (!Array.isArray(value) || value.length > 10) {
    throw new Error('Encrypted message attachment manifests are invalid.');
  }
  const refs = new Set<string>();
  return value.map((raw) => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new Error('Encrypted message attachment manifests are invalid.');
    }
    const manifest = raw as Record<string, unknown>;
    const attachmentId = manifest.attachment_id;
    const attachmentDomain = manifest.attachment_domain;
    if (
      typeof attachmentId !== 'string' ||
      typeof attachmentDomain !== 'string' ||
      !canonicalQualifiedRef(`${attachmentId}@${attachmentDomain}`) ||
      !refs.add(`${attachmentId}@${attachmentDomain}`)
    ) {
      throw new Error('Encrypted message attachment identity is invalid.');
    }
    return interactionAttachmentManifests(
      [attachmentId],
      { [attachmentId]: manifest },
      allowVoiceMetadata
    )[attachmentId];
  });
}

/** Strict manifest validator shared by rich-message send and receive paths. */
export function validateEncryptedRichMessageAttachments(
  value: unknown,
  voiceMessage: boolean
): EncryptedFileManifest[] {
  const attachments = validatedEncryptedMessageAttachments(value, true);
  const voiceManifest = attachments[0];
  if (
    voiceMessage
      ? attachments.length !== 1 ||
        !voiceManifest ||
        voiceManifest.duration_millis === undefined ||
        voiceManifest.waveform === undefined ||
        !voiceManifest.content_type.startsWith('audio/')
      : attachments.some(
          (manifest) => manifest.duration_millis !== undefined || manifest.waveform !== undefined
        )
  ) {
    throw new Error('Encrypted voice message metadata does not match its authenticated body.');
  }
  return attachments;
}

interface RichMessageProjection {
  context: RichMessageAuthenticatedContext;
  contract: Record<string, unknown> | null;
}

function exactRichMessageEnvelopeFields(envelope: MlsEnvelope): Set<string> {
  const hasContract =
    Object.hasOwn(envelope, 'interaction_contract') &&
    Object.hasOwn(envelope, 'interaction_contract_digest');
  return new Set([
    'version',
    'protocol',
    'suite',
    'group_id',
    'policy_generation',
    'epoch',
    'forward_projection_digest',
    'forward_projection_version',
    'forward_snapshot_digest',
    'forward_source_projection_digest',
    'forwarded_channel_ref',
    'forwarded_created_at',
    'forwarded_edited_at',
    'forwarded_flags',
    'forwarded_message_ref',
    'forwarded_message_type',
    'sender_device_id',
    'operation',
    'ciphertext',
    'author_ref',
    'message_revision',
    'message_attachment_refs',
    'message_custom_emoji_refs',
    'message_mention_everyone',
    'message_mention_refs',
    'message_mention_role_refs',
    'message_mention_user_refs',
    'message_replied_user_ref',
    'message_sticker_refs',
    'referenced_message_ref',
    'rich_payload_digest',
    'application_ref',
    'interaction_integration_type',
    'interaction_installation_ref',
    'interaction_installation_revision',
    'view_version',
    'view_persistent',
    'tts',
    'voice_message',
    'message_flags',
    ...(Object.hasOwn(envelope, 'attachment_manifest_digest')
      ? ['attachment_manifest_digest']
      : []),
    ...(hasContract ? ['interaction_contract', 'interaction_contract_digest'] : []),
    ...(envelope.operation === 'edit' ? ['target_message'] : [])
  ]);
}

function projectionAttachmentRefs(message: EncryptedMessageRecord): string[] {
  const values = message.attachments;
  if (!Array.isArray(values) || values.length > 10) {
    throw new Error('Encrypted rich message attachment projection is invalid.');
  }
  const refs = values.map((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('Encrypted rich message attachment projection is invalid.');
    }
    const raw = value as unknown as Record<string, unknown>;
    const ref = canonicalQualifiedRef(
      `${typeof raw.id === 'string' ? raw.id : ''}@${
        typeof raw.origin_domain === 'string' ? raw.origin_domain : ''
      }`
    );
    if (!ref) throw new Error('Encrypted rich message attachment identity is invalid.');
    return ref.ref;
  });
  if (new Set(refs).size !== refs.length) {
    throw new Error('Encrypted rich message attachment identity is duplicated.');
  }
  return refs.sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
}

function projectionMentionRefs(message: EncryptedMessageRecord): string[] {
  const values = message.mention_user_refs;
  if (!Array.isArray(values) || values.length > 5_000) {
    throw new Error('Encrypted rich message mention projection is invalid.');
  }
  const refs = values.map((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('Encrypted rich message mention projection is invalid.');
    }
    const raw = value as unknown as Record<string, unknown>;
    if (!hasExactRoutingFields(raw, ['id', 'origin_domain'])) {
      throw new Error('Encrypted rich message mention projection is invalid.');
    }
    const ref = canonicalQualifiedRef(`${raw.id}@${raw.origin_domain}`);
    if (!ref) throw new Error('Encrypted rich message mention projection is invalid.');
    return ref.ref;
  });
  const sorted = [...refs].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
  if (new Set(refs).size !== refs.length || refs.some((ref, index) => ref !== sorted[index])) {
    throw new Error('Encrypted rich message mention projection is invalid.');
  }
  return refs;
}

function projectionReferencedMessageRef(message: EncryptedMessageRecord): string | null {
  const directId = message.referenced_message_id ?? null;
  const directDomain = message.referenced_message_domain ?? null;
  const nestedId = message.message_reference?.message_id ?? null;
  const nestedDomain = message.message_reference?.message_domain ?? null;
  const direct =
    directId === null && directDomain === null
      ? null
      : directId !== null && directDomain !== null
        ? canonicalQualifiedRef(`${directId}@${directDomain}`)?.ref
        : undefined;
  const nested =
    nestedId === null && nestedDomain === null
      ? null
      : nestedId !== null && nestedDomain !== null
        ? canonicalQualifiedRef(`${nestedId}@${nestedDomain}`)?.ref
        : undefined;
  if (direct === undefined || nested === undefined || (direct && nested && direct !== nested)) {
    throw new Error('Encrypted rich message reply projection is invalid.');
  }
  return direct ?? nested ?? null;
}

function projectionReferencedMessageAuthorRef(message: EncryptedMessageRecord): string | null {
  const referenced = message.referenced_message;
  if (referenced == null) return null;
  const direct = canonicalQualifiedRef(`${referenced.author_id}@${referenced.author_domain}`);
  const summary = referenced.author
    ? canonicalQualifiedRef(`${referenced.author.id}@${referenced.author.origin_domain}`)
    : null;
  if (!direct || (summary && summary.ref !== direct.ref)) {
    throw new Error('Encrypted rich message reply author projection is invalid.');
  }
  return direct.ref;
}

export function encryptedMessageEditBindings(message: Message): {
  mentionUserRefs: string[];
  repliedUserRef: string | null;
  referencedMessageRef: string | null;
  rich?: EncryptedRichMessageOptions;
} {
  const mentionUserRefs = projectionMentionRefs(message);
  const referencedMessageRef = projectionReferencedMessageRef(message);
  const envelope = message.e2ee;
  if (!envelope || !Object.hasOwn(envelope, 'rich_payload_digest')) {
    return { mentionUserRefs, repliedUserRef: null, referencedMessageRef };
  }
  if (message.poll !== null && message.poll !== undefined) {
    throw new Error('Encrypted polls cannot be edited after publication.');
  }
  const revision = canonicalUnsignedI63(envelope.message_revision, true);
  if (!revision || BigInt(revision) >= 9_223_372_036_854_775_807n) {
    throw new Error('Encrypted message revision is invalid.');
  }
  const repliedUserRef =
    envelope.message_replied_user_ref === null
      ? null
      : canonicalQualifiedRef(envelope.message_replied_user_ref)?.ref;
  if (
    repliedUserRef === undefined ||
    !message.decrypted_allowed_mentions ||
    message.decrypted_allowed_mentions.replied_user !== (repliedUserRef !== null)
  ) {
    throw new Error('Encrypted message reply notification policy is unavailable.');
  }
  return {
    mentionUserRefs,
    repliedUserRef,
    referencedMessageRef,
    rich: {
      embeds: message.embeds ?? [],
      components: message.components ?? [],
      poll: null,
      stickerItems: message.sticker_items ?? [],
      tts: message.tts === true,
      voiceMessage: Boolean(message.flags & (1 << 13)),
      flags: message.flags,
      allowedMentions: message.decrypted_allowed_mentions,
      messageRevision: String(BigInt(revision) + 1n)
    }
  };
}

async function richMessageProjection(
  channel: Channel,
  message: EncryptedMessageRecord,
  envelope: MlsEnvelope
): Promise<RichMessageProjection> {
  const hasContract =
    Object.hasOwn(envelope, 'interaction_contract') &&
    Object.hasOwn(envelope, 'interaction_contract_digest');
  const expectedFields = exactRichMessageEnvelopeFields(envelope);
  if (
    Object.hasOwn(envelope, 'interaction_contract') !==
      Object.hasOwn(envelope, 'interaction_contract_digest') ||
    Object.keys(envelope).some((field) => !expectedFields.has(field)) ||
    Object.keys(envelope).length !== expectedFields.size ||
    envelope.version !== 2 ||
    envelope.protocol !== MLS_PROTOCOL ||
    envelope.suite !== MLS_SUITE ||
    typeof envelope.ciphertext !== 'string'
  ) {
    throw new Error('Encrypted rich message envelope is invalid.');
  }
  const applicationRef =
    message.application_id == null && message.application_domain == null
      ? null
      : message.application_id && message.application_domain
        ? `${message.application_id}@${message.application_domain}`
        : undefined;
  if (applicationRef === undefined || (applicationRef && !canonicalQualifiedRef(applicationRef))) {
    throw new Error('Encrypted rich message application projection is invalid.');
  }
  const projectedForwardRef =
    message.forwarded_message_id == null &&
    message.forwarded_message_domain == null &&
    message.forwarded_message_ref == null
      ? null
      : message.forwarded_message_id &&
          message.forwarded_message_domain &&
          message.forwarded_message_ref ===
            `${message.forwarded_message_id}@${message.forwarded_message_domain}` &&
          canonicalQualifiedRef(message.forwarded_message_ref)
        ? message.forwarded_message_ref
        : undefined;
  if (projectedForwardRef === undefined) {
    throw new Error('Encrypted rich message forward projection is invalid.');
  }
  const attachmentRefs = projectionAttachmentRefs(message);
  const mentionRefs = projectionMentionRefs(message);
  const referencedMessageRef = projectionReferencedMessageRef(message);
  const voiceMessage = Boolean(message.flags & (1 << 13));
  const context = validateRichMessageAuthenticatedContext({
    application_ref: envelope.application_ref,
    attachment_manifest_digest: envelope.attachment_manifest_digest ?? null,
    author_ref: envelope.author_ref,
    channel_ref: `${message.channel_id}@${message.channel_domain}`,
    epoch: envelope.epoch,
    forward_projection_digest: envelope.forward_projection_digest,
    forward_projection_version: envelope.forward_projection_version,
    forward_snapshot_digest: envelope.forward_snapshot_digest,
    forward_source_projection_digest: envelope.forward_source_projection_digest,
    forwarded_channel_ref: envelope.forwarded_channel_ref,
    forwarded_created_at: envelope.forwarded_created_at,
    forwarded_edited_at: envelope.forwarded_edited_at,
    forwarded_flags: envelope.forwarded_flags,
    forwarded_message_ref: envelope.forwarded_message_ref,
    forwarded_message_type: envelope.forwarded_message_type,
    group_id: envelope.group_id,
    interaction_contract_digest: hasContract ? envelope.interaction_contract_digest : null,
    interaction_installation_ref: envelope.interaction_installation_ref,
    interaction_installation_revision: envelope.interaction_installation_revision,
    interaction_integration_type: envelope.interaction_integration_type,
    message_attachment_refs: envelope.message_attachment_refs,
    message_custom_emoji_refs: envelope.message_custom_emoji_refs,
    message_mention_everyone: envelope.message_mention_everyone,
    message_mention_refs: envelope.message_mention_refs,
    message_mention_role_refs: envelope.message_mention_role_refs,
    message_mention_user_refs: envelope.message_mention_user_refs,
    message_replied_user_ref: envelope.message_replied_user_ref,
    message_sticker_refs: envelope.message_sticker_refs,
    message_flags: envelope.message_flags,
    message_revision: envelope.message_revision,
    operation: envelope.operation,
    policy_generation: envelope.policy_generation,
    referenced_message_ref: envelope.referenced_message_ref,
    rich_payload_digest: envelope.rich_payload_digest,
    sender_device_id: envelope.sender_device_id,
    target_message: envelope.target_message ?? null,
    tts: envelope.tts,
    view_persistent: envelope.view_persistent,
    view_version: envelope.view_version,
    voice_message: envelope.voice_message
  });
  const viewVersion = message.view_version;
  const viewPersistent = message.view_persistent;
  if (
    context.channel_ref !== `${channel.id}@${channel.origin_domain}` ||
    context.author_ref !== `${message.author_id}@${message.author_domain}` ||
    context.application_ref !== applicationRef ||
    context.message_flags !== message.flags ||
    context.tts !== message.tts ||
    context.voice_message !== voiceMessage ||
    context.forwarded_message_ref !== projectedForwardRef ||
    message.forwarded_message != null ||
    Boolean(message.message_snapshots?.length) ||
    JSON.stringify(context.message_attachment_refs) !== JSON.stringify(attachmentRefs) ||
    JSON.stringify(context.message_mention_refs) !== JSON.stringify(mentionRefs) ||
    context.referenced_message_ref !== referencedMessageRef ||
    context.interaction_integration_type !== (message.interaction_integration_type ?? null) ||
    context.interaction_installation_ref !== (message.interaction_installation_ref ?? null) ||
    context.interaction_installation_revision !==
      (message.interaction_installation_revision ?? null) ||
    typeof viewVersion !== 'number' ||
    !Number.isSafeInteger(viewVersion) ||
    viewVersion < 0 ||
    context.view_version !== String(viewVersion) ||
    typeof viewPersistent !== 'boolean' ||
    context.view_persistent !== viewPersistent
  ) {
    throw new Error('Encrypted rich message context does not match its projection.');
  }
  const contract = hasContract
    ? validateInteractionRoutingContract(envelope.interaction_contract, null)
    : null;
  if (
    (contract ? await interactionRoutingContractDigest(contract) : null) !==
    context.interaction_contract_digest
  ) {
    throw new Error('Encrypted rich message routing contract digest is invalid.');
  }
  if (
    (contract?.poll !== undefined && contract.poll !== null) !==
      (context.forward_projection_digest === null) ||
    (context.forward_projection_digest === null
      ? context.forward_projection_version !== null
      : context.forward_projection_version !== 2)
  ) {
    throw new Error('Encrypted rich message forward projection metadata is invalid.');
  }
  const hasControls =
    contract !== null && Array.isArray(contract.components) && contract.components.length > 0;
  if (
    hasControls !== viewVersion > 0 ||
    (hasControls && applicationRef === null) ||
    (!hasControls && (viewPersistent || message.view_expires_at != null)) ||
    (hasControls && viewPersistent && message.view_expires_at != null) ||
    (hasControls &&
      !viewPersistent &&
      (typeof message.view_expires_at !== 'string' ||
        !Number.isFinite(Date.parse(message.view_expires_at))))
  ) {
    throw new Error('Encrypted rich message view projection is invalid.');
  }
  return { context, contract };
}

function validateRichStickerItems(value: unknown): NonNullable<Message['sticker_items']> {
  if (!Array.isArray(value) || value.length > 3) {
    throw new Error('Encrypted rich message stickers are invalid.');
  }
  const refs = new Set<string>();
  return value.map((item) => {
    const raw = routingRecord(item, 'Encrypted rich message sticker');
    const ref = canonicalQualifiedRef(`${raw.id}@${raw.origin_domain}`);
    if (
      !hasExactRoutingFields(raw, ['id', 'origin_domain', 'name', 'format_type']) ||
      !ref ||
      refs.has(ref.ref) ||
      typeof raw.name !== 'string' ||
      raw.name !== raw.name.trim() ||
      [...raw.name].length < 2 ||
      [...raw.name].length > 30 ||
      !Number.isSafeInteger(raw.format_type) ||
      ![1, 2, 3, 4].includes(raw.format_type as number)
    ) {
      throw new Error('Encrypted rich message sticker is invalid.');
    }
    refs.add(ref.ref);
    return {
      id: ref.id,
      origin_domain: ref.domain,
      name: raw.name,
      format_type: raw.format_type as 1 | 2 | 3 | 4,
      media_hash: ''
    };
  });
}

/**
 * Collect every sticker identity whose private presentation is carried by a
 * rich message, including the immutable snapshots retained by a forward.
 */
export function richMessageStickerRefs(value: unknown): string[] {
  const data = routingRecord(value, 'Encrypted rich message body');
  const refs = new Set<string>();
  const addItems = (items: unknown): void => {
    for (const item of validateRichStickerItems(items)) {
      refs.add(`${item.id}@${item.origin_domain}`);
    }
  };
  const addSnapshot = (value: unknown): void => {
    const snapshot = routingRecord(value, 'Encrypted forward snapshot');
    addItems(snapshot.sticker_items);
    const nested = snapshot.message_snapshots;
    if (!Array.isArray(nested)) {
      throw new Error('Encrypted forward snapshot stickers are invalid.');
    }
    nested.forEach(addSnapshot);
  };

  addItems(data.sticker_items);
  if (data.forward_snapshot !== null && data.forward_snapshot !== undefined) {
    addSnapshot(validateEncryptedForwardSnapshot(data.forward_snapshot));
  }
  if (refs.size > 9) {
    throw new Error('Encrypted rich message has too many routed stickers.');
  }
  return [...refs].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
}

/** Extract the exact authority-visible custom-emoji tokens from rich plaintext. */
export function richMessageCustomEmojiRefs(value: unknown): string[] {
  const refs = new Set<string>();
  const tokenPattern = /<(a?):([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>/gu;
  const walk = (item: unknown): void => {
    if (typeof item === 'string') {
      for (const match of item.matchAll(tokenPattern)) {
        const ref = canonicalQualifiedRef(`${match[3]}@${match[4]}`);
        if (!ref) throw new Error('Encrypted rich message custom emoji is invalid.');
        refs.add(`<${match[1]}:${match[2]}:${ref.ref}>`);
      }
      return;
    }
    if (Array.isArray(item)) {
      item.forEach(walk);
      return;
    }
    if (!item || typeof item !== 'object') return;
    const raw = item as Record<string, unknown>;
    const animated = raw.animated ?? false;
    if (
      typeof raw.id === 'string' &&
      raw.id.includes('@') &&
      typeof raw.name === 'string' &&
      /^[A-Za-z0-9_]{2,32}$/u.test(raw.name) &&
      typeof animated === 'boolean'
    ) {
      const ref = canonicalQualifiedRef(raw.id);
      if (!ref) throw new Error('Encrypted rich message custom emoji is invalid.');
      refs.add(`<${animated ? 'a' : ''}:${raw.name}:${ref.ref}>`);
    }
    Object.values(raw).forEach(walk);
  };
  walk(value);
  const result = [...refs].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
  if (result.length > 256) {
    throw new Error('Encrypted rich message has too many custom emoji references.');
  }
  return result;
}

function stableForwardManifest(manifest: EncryptedFileManifest): Record<string, unknown> {
  if (!manifest.plaintext_sha256) {
    throw new Error('Legacy encrypted attachments cannot be forwarded safely.');
  }
  return {
    filename: manifest.filename,
    content_type: manifest.content_type,
    plaintext_size: manifest.plaintext_size,
    plaintext_sha256: manifest.plaintext_sha256,
    ...(manifest.duration_millis === undefined
      ? {}
      : { duration_millis: manifest.duration_millis, waveform: manifest.waveform })
  };
}

const FORWARDABLE_MESSAGE_TYPES = new Set([0, 19, 20, 23]);
const FORWARD_SNAPSHOT_FLAG_MASK = (1 << 2) | (1 << 13) | (1 << 15);
const FORWARD_SNAPSHOT_FIELDS = new Set([
  'content',
  'embeds',
  'components',
  'attachments',
  'mention_user_refs',
  'sticker_items',
  'message_snapshots',
  'message_type',
  'flags',
  'created_at',
  'edited_at'
]);

function forwardTimestamp(value: unknown, label: string): string {
  if (
    typeof value !== 'string' ||
    !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value) ||
    !Number.isFinite(Date.parse(value))
  ) {
    throw new Error(`Encrypted forward snapshot ${label} is invalid.`);
  }
  return value;
}

function stableForwardSnapshotAttachment(value: unknown): Record<string, unknown> {
  const raw = routingRecord(value, 'Encrypted forward snapshot attachment');
  if (raw.protocol === 'kaede-file-v1') {
    const [manifest] = validateEncryptedRichMessageAttachments(
      [raw],
      Object.hasOwn(raw, 'duration_millis') || Object.hasOwn(raw, 'waveform')
    );
    return stableForwardManifest(manifest);
  }
  const allowed = new Set([
    'id',
    'origin_domain',
    'filename',
    'content_type',
    'size',
    'plaintext_sha256',
    'width',
    'height',
    'duration_secs',
    'waveform',
    'blurhash',
    'scan_status',
    'encryption_mode',
    'encryption_protocol',
    'variants'
  ]);
  const ref = canonicalQualifiedRef(`${raw.id}@${raw.origin_domain}`);
  const duration = raw.duration_secs;
  if (
    Object.keys(raw).some((field) => !allowed.has(field)) ||
    !ref ||
    raw.id !== ref.id ||
    raw.origin_domain !== ref.domain ||
    typeof raw.filename !== 'string' ||
    !raw.filename ||
    [...raw.filename].length > 255 ||
    typeof raw.content_type !== 'string' ||
    !raw.content_type ||
    [...raw.content_type].length > 100 ||
    !Number.isSafeInteger(raw.size) ||
    Number(raw.size) < 0 ||
    Number(raw.size) > 100 * 1024 * 1024 ||
    typeof raw.plaintext_sha256 !== 'string' ||
    !isCanonicalBase64url32(raw.plaintext_sha256) ||
    raw.encryption_mode !== 'plaintext' ||
    (duration === null || duration === undefined) !==
      (raw.waveform === null || raw.waveform === undefined) ||
    (duration !== null &&
      duration !== undefined &&
      (typeof duration !== 'number' ||
        !Number.isFinite(duration) ||
        duration <= 0 ||
        duration > 1_200 ||
        typeof raw.waveform !== 'string'))
  ) {
    throw new Error('Encrypted forward snapshot attachment is invalid.');
  }
  return {
    filename: raw.filename,
    content_type: raw.content_type,
    plaintext_size: raw.size,
    plaintext_sha256: raw.plaintext_sha256,
    ...(duration === null || duration === undefined
      ? {}
      : { duration_millis: Math.round(duration * 1_000), waveform: raw.waveform })
  };
}

export function encryptedForwardAttachmentSemantics(value: unknown): Record<string, unknown> {
  return stableForwardSnapshotAttachment(value);
}

function forwardSnapshotProjection(
  value: unknown,
  depth = 0
): { snapshot: Record<string, unknown>; projection: Record<string, unknown> } {
  const raw = routingRecord(value, 'Encrypted forward snapshot');
  const fields = new Set(Object.keys(raw));
  const withoutEdited = new Set(FORWARD_SNAPSHOT_FIELDS);
  withoutEdited.delete('edited_at');
  if (fields.size !== FORWARD_SNAPSHOT_FIELDS.size && fields.size !== withoutEdited.size) {
    throw new Error('Encrypted forward snapshot fields are invalid.');
  }
  const expected = fields.has('edited_at') ? FORWARD_SNAPSHOT_FIELDS : withoutEdited;
  if ([...fields].some((field) => !expected.has(field))) {
    throw new Error('Encrypted forward snapshot fields are invalid.');
  }
  const content = raw.content;
  const embeds = raw.embeds;
  const components = raw.components;
  const attachments = raw.attachments;
  const mentionRefs = raw.mention_user_refs;
  const stickerItems = raw.sticker_items;
  const nested = raw.message_snapshots;
  const messageType = raw.message_type;
  const flags = raw.flags;
  if (
    (content !== null &&
      (typeof content !== 'string' || !content.length || [...content].length > 4_000)) ||
    !Array.isArray(embeds) ||
    embeds.length > 10 ||
    embeds.some((item) => !item || typeof item !== 'object' || Array.isArray(item)) ||
    !Array.isArray(components) ||
    components.length > 40 ||
    components.some((item) => !item || typeof item !== 'object' || Array.isArray(item)) ||
    !Array.isArray(attachments) ||
    attachments.length > 10 ||
    !Array.isArray(mentionRefs) ||
    mentionRefs.length > 5_000 ||
    !Array.isArray(stickerItems) ||
    stickerItems.length > 3 ||
    stickerItems.some((item) => !item || typeof item !== 'object' || Array.isArray(item)) ||
    !Array.isArray(nested) ||
    nested.length > 1 ||
    (depth > 0 && nested.length > 0) ||
    !Number.isSafeInteger(messageType) ||
    !FORWARDABLE_MESSAGE_TYPES.has(Number(messageType)) ||
    !Number.isSafeInteger(flags) ||
    Number(flags) < 0 ||
    (Number(flags) & ~FORWARD_SNAPSHOT_FLAG_MASK) !== 0
  ) {
    throw new Error('Encrypted forward snapshot is invalid.');
  }
  validateBoundedRichTree(embeds, 0);
  validateBoundedRichTree(components, 0);
  const createdAt = forwardTimestamp(raw.created_at, 'creation timestamp');
  const editedAt = raw.edited_at == null ? null : forwardTimestamp(raw.edited_at, 'edit timestamp');
  if (editedAt !== null && Date.parse(editedAt) < Date.parse(createdAt)) {
    throw new Error('Encrypted forward snapshot edit timestamp predates creation.');
  }
  const normalizedMentionRefs = mentionRefs.map((item) => {
    const mention = routingRecord(item, 'Encrypted forward snapshot mention');
    if (!hasExactRoutingFields(mention, ['id', 'origin_domain'])) {
      throw new Error('Encrypted forward snapshot mention is invalid.');
    }
    const ref = canonicalQualifiedRef(`${mention.id}@${mention.origin_domain}`);
    if (!ref || mention.id !== ref.id || mention.origin_domain !== ref.domain) {
      throw new Error('Encrypted forward snapshot mention is invalid.');
    }
    return { ref: ref.ref, value: { id: ref.id, origin_domain: ref.domain } };
  });
  const mentionStrings = normalizedMentionRefs.map((item) => item.ref);
  if (
    JSON.stringify(mentionStrings) !==
    JSON.stringify(
      [...new Set(mentionStrings)].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0))
    )
  ) {
    throw new Error('Encrypted forward snapshot mentions are invalid.');
  }
  const attachmentProjection = attachments.map(stableForwardSnapshotAttachment);
  const nestedValidated = nested.map((item) => forwardSnapshotProjection(item, depth + 1));
  if (
    content === null &&
    !embeds.length &&
    !components.length &&
    !attachments.length &&
    !stickerItems.length &&
    !nested.length
  ) {
    throw new Error('Encrypted forward snapshot has no body.');
  }
  const snapshot: Record<string, unknown> = {
    content,
    embeds,
    components,
    attachments,
    mention_user_refs: normalizedMentionRefs.map((item) => item.value),
    sticker_items: stickerItems,
    message_snapshots: nestedValidated.map((item) => item.snapshot),
    message_type: messageType,
    flags,
    created_at: createdAt,
    edited_at: editedAt
  };
  return {
    snapshot,
    projection: {
      version: 2,
      content,
      embeds,
      components,
      attachments: attachmentProjection,
      mention_user_refs: normalizedMentionRefs.map((item) => item.value),
      sticker_items: stickerItems,
      message_snapshots: nestedValidated.map((item) => item.projection),
      flags
    }
  };
}

export function validateEncryptedForwardSnapshot(value: unknown): Record<string, unknown> {
  return forwardSnapshotProjection(value).snapshot;
}

export async function encryptedForwardSnapshotProjectionDigest(value: unknown): Promise<string> {
  const encoded = canonicalInteractionJson(forwardSnapshotProjection(value).projection);
  try {
    return base64url(await sha256(encoded));
  } finally {
    clearBytes(encoded);
  }
}

export async function encryptedForwardSnapshotDigest(value: unknown): Promise<string> {
  const encoded = canonicalInteractionJson(value);
  try {
    return base64url(await sha256(encoded));
  } finally {
    clearBytes(encoded);
  }
}

/** Digest of the author-free body eligible for a secure immutable forward. */
export async function richMessageForwardProjectionDigest(
  data: Record<string, unknown>,
  mentionRefs: readonly string[]
): Promise<string | null> {
  if (data.poll !== null) return null;
  if (!canonicalSortedQualifiedRefList(mentionRefs, 5_000)) {
    throw new Error('Encrypted rich message mention references are invalid.');
  }
  const attachments = validateEncryptedRichMessageAttachments(
    data.attachments,
    data.voice_message === true
  );
  const mentions = mentionRefs.map((value) => {
    const ref = canonicalQualifiedRef(value)!;
    return { id: ref.id, origin_domain: ref.domain };
  });
  const projection = {
    version: 2,
    content: data.content,
    embeds: data.embeds,
    components: data.components,
    attachments: attachments.map(stableForwardManifest),
    mention_user_refs: mentions,
    sticker_items: data.sticker_items,
    message_snapshots:
      data.forward_snapshot === null
        ? []
        : [forwardSnapshotProjection(data.forward_snapshot).projection],
    flags: Number(data.flags) & ((1 << 2) | (1 << 13) | (1 << 15))
  };
  const encoded = canonicalInteractionJson(projection);
  try {
    return base64url(await sha256(encoded));
  } finally {
    clearBytes(encoded);
  }
}

function richPollMedia(value: unknown, answer: boolean): Record<string, unknown> {
  const raw = routingRecord(value, 'Encrypted poll media');
  if (
    Object.keys(raw).some((field) => !['text', 'emoji'].includes(field)) ||
    (!Object.hasOwn(raw, 'text') && !Object.hasOwn(raw, 'emoji')) ||
    (raw.text !== undefined &&
      (typeof raw.text !== 'string' ||
        !raw.text.trim() ||
        [...raw.text].length > (answer ? 55 : 300)))
  ) {
    throw new Error('Encrypted poll media is invalid.');
  }
  if (raw.emoji !== undefined) {
    const emoji = routingRecord(raw.emoji, 'Encrypted poll emoji');
    if (
      Object.keys(emoji).some((field) => !['id', 'name', 'animated'].includes(field)) ||
      (emoji.id == null && emoji.name == null) ||
      (emoji.id != null && !canonicalQualifiedRef(emoji.id)) ||
      (emoji.name != null &&
        (typeof emoji.name !== 'string' || !emoji.name.trim() || [...emoji.name].length > 64)) ||
      (emoji.animated !== undefined && typeof emoji.animated !== 'boolean') ||
      (emoji.animated === true && emoji.id == null)
    ) {
      throw new Error('Encrypted poll emoji is invalid.');
    }
  }
  return raw;
}

function mergedRichPoll(
  dataValue: unknown,
  projectionValue: unknown,
  contractValue: unknown,
  createdAt: string
): NonNullable<Message['poll']> {
  const data = routingRecord(dataValue, 'Encrypted poll');
  const contract = validateRoutingPoll(contractValue);
  const answers = data.answers;
  if (
    !hasExactRoutingFields(data, [
      'question',
      'answers',
      'duration',
      'allow_multiselect',
      'layout_type'
    ]) ||
    !Array.isArray(answers) ||
    answers.length !== (contract.answer_ids as unknown[]).length ||
    data.allow_multiselect !== contract.allow_multiselect ||
    data.layout_type !== 1 ||
    Number(data.duration) * 3_600 !== contract.duration_seconds
  ) {
    throw new Error('Encrypted poll does not match its routing contract.');
  }
  const question = richPollMedia(data.question, false);
  if (typeof question.text !== 'string' || question.emoji !== undefined) {
    throw new Error('Encrypted poll question is invalid.');
  }
  const presentationAnswers = answers.map((value, index) => {
    const answerValue = routingRecord(value, 'Encrypted poll answer');
    if (!hasExactRoutingFields(answerValue, ['poll_media'])) {
      throw new Error('Encrypted poll answer is invalid.');
    }
    return {
      answer_id: index + 1,
      poll_media: richPollMedia(answerValue.poll_media, true)
    };
  });
  const projection = routingRecord(projectionValue, 'Encrypted poll projection');
  const createdAtMillis = Date.parse(createdAt);
  const expiryMillis =
    typeof projection.expiry === 'string' ? Date.parse(projection.expiry) : Number.NaN;
  const result = routingRecord(projection.results, 'Encrypted poll results');
  const counts = result.answer_counts;
  const answerIds = contract.answer_ids as number[];
  if (
    !hasExactRoutingFields(projection, [
      'encrypted',
      'answer_ids',
      'expiry',
      'allow_multiselect',
      'layout_type',
      'finalized_at',
      'results'
    ]) ||
    projection.encrypted !== true ||
    JSON.stringify(projection.answer_ids) !== JSON.stringify(answerIds) ||
    projection.allow_multiselect !== contract.allow_multiselect ||
    projection.layout_type !== 1 ||
    typeof projection.expiry !== 'string' ||
    !Number.isFinite(createdAtMillis) ||
    !Number.isFinite(expiryMillis) ||
    Math.abs(expiryMillis - (createdAtMillis + Number(contract.duration_seconds) * 1_000)) >
      2_000 ||
    (projection.finalized_at !== null &&
      (typeof projection.finalized_at !== 'string' ||
        !Number.isFinite(Date.parse(projection.finalized_at)))) ||
    !hasExactRoutingFields(result, ['is_finalized', 'answer_counts']) ||
    typeof result.is_finalized !== 'boolean' ||
    !Array.isArray(counts) ||
    counts.length !== answerIds.length
  ) {
    throw new Error('Encrypted poll projection is invalid.');
  }
  const answerCounts = counts.map((value, index) => {
    const count = routingRecord(value, 'Encrypted poll count');
    if (
      !hasExactRoutingFields(count, ['id', 'count', 'me_voted']) ||
      count.id !== answerIds[index] ||
      !Number.isSafeInteger(count.count) ||
      Number(count.count) < 0 ||
      typeof count.me_voted !== 'boolean'
    ) {
      throw new Error('Encrypted poll count is invalid.');
    }
    return { id: count.id as number, count: count.count as number, me_voted: count.me_voted };
  });
  if (projection.finalized_at !== null && result.is_finalized !== true) {
    throw new Error('Encrypted poll finalization is invalid.');
  }
  return {
    question,
    answers: presentationAnswers,
    expiry: projection.expiry,
    allow_multiselect: contract.allow_multiselect as boolean,
    layout_type: 1,
    results: { is_finalized: result.is_finalized, answer_counts: answerCounts }
  } as NonNullable<Message['poll']>;
}

async function authenticatedRichMessageApplication(
  value: unknown,
  context: RichMessageAuthenticatedContext,
  contract: Record<string, unknown> | null,
  message: EncryptedMessageRecord
): Promise<DecryptedApplication> {
  const data = routingRecord(value, 'Encrypted rich message body');
  if (
    !hasExactRoutingFields(data, [
      'content',
      'embeds',
      'components',
      'poll',
      'sticker_items',
      'tts',
      'voice_message',
      'flags',
      'allowed_mentions',
      'forward_snapshot',
      'attachments'
    ]) ||
    (data.content !== null &&
      (typeof data.content !== 'string' ||
        !data.content.trim() ||
        [...data.content].length > 4_000)) ||
    !Array.isArray(data.embeds) ||
    data.embeds.length > 10 ||
    data.embeds.some((item) => !item || typeof item !== 'object' || Array.isArray(item)) ||
    !Array.isArray(data.components) ||
    data.components.length > 40 ||
    data.components.some((item) => !item || typeof item !== 'object' || Array.isArray(item)) ||
    typeof data.tts !== 'boolean' ||
    typeof data.voice_message !== 'boolean' ||
    !Number.isSafeInteger(data.flags) ||
    Number(data.flags) < 0 ||
    Number(data.flags) > 2_147_483_647 ||
    data.tts !== context.tts ||
    data.voice_message !== context.voice_message ||
    data.flags !== context.message_flags
  ) {
    throw new Error('Encrypted rich message body is invalid.');
  }
  const forwardSnapshot =
    data.forward_snapshot === null ? null : validateEncryptedForwardSnapshot(data.forward_snapshot);
  if (forwardSnapshot === null) {
    if (
      context.forward_snapshot_digest !== null ||
      context.forward_source_projection_digest !== null ||
      context.forwarded_message_ref !== null ||
      context.forwarded_channel_ref !== null ||
      context.forwarded_created_at !== null ||
      context.forwarded_edited_at !== null ||
      context.forwarded_flags !== null ||
      context.forwarded_message_type !== null
    ) {
      throw new Error('Encrypted rich message forward metadata is incomplete.');
    }
  } else {
    const forwardedMessage = canonicalQualifiedRef(context.forwarded_message_ref ?? '');
    const forwardedChannel = canonicalQualifiedRef(context.forwarded_channel_ref ?? '');
    if (
      !forwardedMessage ||
      !forwardedChannel ||
      context.forward_snapshot_digest !==
        (await encryptedForwardSnapshotDigest(data.forward_snapshot)) ||
      context.forward_source_projection_digest !==
        (await encryptedForwardSnapshotProjectionDigest(forwardSnapshot)) ||
      context.forwarded_created_at !== forwardSnapshot.created_at ||
      context.forwarded_edited_at !== forwardSnapshot.edited_at ||
      context.forwarded_flags !== forwardSnapshot.flags ||
      context.forwarded_message_type !== forwardSnapshot.message_type
    ) {
      throw new Error('Encrypted rich message forward source was modified.');
    }
  }
  validateBoundedRichTree(data.embeds, 0);
  validateBoundedRichTree(data.components, 0);
  const attachments = validateEncryptedRichMessageAttachments(
    data.attachments,
    context.voice_message
  );
  const attachmentRefs = attachments.map(
    (item) => `${item.attachment_id}@${item.attachment_domain}`
  );
  if (JSON.stringify(attachmentRefs) !== JSON.stringify(context.message_attachment_refs)) {
    throw new Error('Encrypted rich message files do not match their authenticated refs.');
  }
  if (context.attachment_manifest_digest !== null) {
    const encoded = canonicalInteractionJson(data.attachments);
    try {
      if (base64url(await sha256(encoded)) !== context.attachment_manifest_digest) {
        throw new Error('Encrypted rich message file manifest was modified.');
      }
    } finally {
      clearBytes(encoded);
    }
  }
  if ((await richMessagePayloadDigest(data)) !== context.rich_payload_digest) {
    throw new Error('Encrypted rich message body digest was modified.');
  }
  if (
    (await richMessageForwardProjectionDigest(data, context.message_mention_refs)) !==
    context.forward_projection_digest
  ) {
    throw new Error('Encrypted rich message forward projection was modified.');
  }
  const derivedContract = await interactionRoutingContract(data, null);
  const expectedContract = contract ? canonicalInteractionJson(contract) : null;
  const actualContract = derivedContract ? canonicalInteractionJson(derivedContract) : null;
  try {
    if (
      Boolean(expectedContract) !== Boolean(actualContract) ||
      (expectedContract && actualContract && !sameBytes(expectedContract, actualContract)) ||
      (derivedContract ? await interactionRoutingContractDigest(derivedContract) : null) !==
        context.interaction_contract_digest
    ) {
      throw new Error('Encrypted rich message routing contract does not match its body.');
    }
  } finally {
    if (expectedContract) clearBytes(expectedContract);
    if (actualContract) clearBytes(actualContract);
  }
  const stickerItems = validateRichStickerItems(data.sticker_items);
  const allowedMentions = validateEncryptedAllowedMentions(data.allowed_mentions);
  const mentionIntent = richMessageMentionIntent(data);
  if (
    JSON.stringify(richMessageStickerRefs(data)) !== JSON.stringify(context.message_sticker_refs) ||
    JSON.stringify(richMessageCustomEmojiRefs(data)) !==
      JSON.stringify(context.message_custom_emoji_refs) ||
    JSON.stringify(mentionIntent.userRefs) !== JSON.stringify(context.message_mention_user_refs) ||
    JSON.stringify(mentionIntent.roleRefs) !== JSON.stringify(context.message_mention_role_refs) ||
    mentionIntent.everyone !== context.message_mention_everyone ||
    allowedMentions.replied_user !== (context.message_replied_user_ref !== null) ||
    (context.message_replied_user_ref !== null && context.referenced_message_ref === null)
  ) {
    throw new Error('Encrypted rich message routing metadata was modified.');
  }
  if (
    context.message_replied_user_ref !== null &&
    projectionReferencedMessageAuthorRef(message) !== context.message_replied_user_ref
  ) {
    throw new Error('Encrypted rich message replied-user reference was modified.');
  }
  const requiredRecipients = new Set([
    ...mentionIntent.userRefs,
    ...(context.message_replied_user_ref ? [context.message_replied_user_ref] : [])
  ]);
  const resolvedRecipients = new Set(context.message_mention_refs);
  if (
    [...requiredRecipients].some((ref) => !resolvedRecipients.has(ref)) ||
    (!mentionIntent.roleRefs.length &&
      !mentionIntent.everyone &&
      (requiredRecipients.size !== resolvedRecipients.size ||
        [...resolvedRecipients].some((ref) => !requiredRecipients.has(ref))))
  ) {
    throw new Error('Encrypted rich message resolved mention routing was modified.');
  }
  const pollContract = contract?.poll ?? null;
  const poll =
    data.poll === null && pollContract === null && message.poll == null
      ? null
      : data.poll !== null && pollContract !== null && message.poll != null
        ? mergedRichPoll(data.poll, message.poll, pollContract, message.created_at)
        : (() => {
            throw new Error('Encrypted poll projection does not match its authenticated body.');
          })();
  const componentsV2 = data.components.some((item) => (item as Record<string, unknown>).type !== 1);
  if (
    Boolean(context.message_flags & (1 << 15)) !== componentsV2 ||
    (componentsV2 &&
      (data.content !== null || data.embeds.length || poll !== null || stickerItems.length)) ||
    (context.voice_message &&
      (data.content !== null ||
        data.embeds.length ||
        data.components.length ||
        poll !== null ||
        stickerItems.length ||
        attachments.length !== 1)) ||
    (data.content === null &&
      !data.embeds.length &&
      !data.components.length &&
      poll === null &&
      !stickerItems.length &&
      !attachments.length)
  ) {
    throw new Error('Encrypted rich message content combination is invalid.');
  }
  return {
    content: data.content,
    attachments,
    embeds: data.embeds as NonNullable<Message['embeds']>,
    components: data.components as NonNullable<Message['components']>,
    poll,
    stickerItems,
    tts: context.tts,
    voiceMessage: context.voice_message,
    flags: context.message_flags,
    allowedMentions,
    forwardSnapshot
  };
}

export function interactionAuthenticatedContext(
  channel: Channel & {
    encryption_policy_generation: string;
    encryption_group_id: string;
    encryption_epoch: string;
  },
  invokerRef: string,
  senderDeviceId: string,
  input: EncryptedInteractionInput
): InteractionAuthenticatedContext {
  const attachmentIds = canonicalInteractionAttachmentIds(input.attachmentIds ?? []);
  const commandInteraction =
    input.interactionType === 'command' || input.interactionType === 'autocomplete';
  const commandId = input.commandId ?? null;
  if (
    (commandInteraction && (!commandId || !/^[1-9]\d{0,18}$/.test(commandId))) ||
    (!commandInteraction && commandId !== null)
  ) {
    throw new Error('Encrypted interaction command identity is invalid.');
  }
  return {
    application_ref: input.applicationRef,
    attachment_ids: attachmentIds,
    autocomplete_generation: optionalInteractionInteger(
      input.autocompleteGeneration,
      'autocomplete generation'
    ),
    channel_ref: `${channel.id}@${channel.origin_domain}`,
    command_id: commandId,
    command_name: input.commandName ?? null,
    command_type: input.commandType ?? null,
    component_type: input.componentType ?? null,
    context: input.interactionContext,
    custom_id: input.customId ?? null,
    epoch: channel.encryption_epoch,
    focused_option: input.focusedOption ?? null,
    group_id: channel.encryption_group_id,
    integration_type: input.integrationType,
    interaction_type: input.interactionType,
    invoker_ref: invokerRef,
    message_ref: input.messageRef ?? null,
    policy_generation: channel.encryption_policy_generation,
    response_id: optionalInteractionInteger(input.responseId, 'response ID'),
    sender_device_id: senderDeviceId,
    target_ref: input.targetRef ?? null,
    view_version: optionalInteractionInteger(input.viewVersion, 'view version')
  };
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

function canonicalQualifiedRef(
  value: unknown,
  expectedDomain?: string
): { id: string; domain: string; ref: string } | null {
  if (typeof value !== 'string') return null;
  const separator = value.lastIndexOf('@');
  if (separator <= 0 || separator === value.length - 1) return null;
  const id = value.slice(0, separator);
  const domain = value.slice(separator + 1);
  if (
    id === '0' ||
    !isCanonicalSnowflake(id) ||
    !isCanonicalFederationDomain(domain) ||
    (expectedDomain !== undefined && domain !== expectedDomain)
  ) {
    return null;
  }
  return { id, domain, ref: value };
}

function canonicalResponseAttachmentRefs(
  values: readonly unknown[],
  authorityDomain: string,
  interactionRef: string,
  responseRef: string
): { refs: string[]; transport: Record<string, unknown>[] } {
  if (values.length > 10) throw new Error('Encrypted bot response has too many attachments.');
  const transport: Record<string, unknown>[] = [];
  const refs = new Set<string>();
  for (const value of values) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('Encrypted bot response attachment transport is invalid.');
    }
    const raw = value as Record<string, unknown>;
    const ref = canonicalQualifiedRef(
      `${typeof raw.id === 'string' ? raw.id : ''}@${
        typeof raw.origin_domain === 'string' ? raw.origin_domain : ''
      }`,
      authorityDomain
    );
    if (!ref || refs.has(ref.ref)) {
      throw new Error('Encrypted bot response attachment identity is invalid.');
    }
    const expectedPrivatePath = `/api/v1/interactions/${interactionRef}/responses/${responseRef}/attachments/${ref.ref}`;
    if (raw.private_media_url !== expectedPrivatePath) {
      throw new Error('Encrypted bot response attachment capability is invalid.');
    }
    refs.add(ref.ref);
    transport.push(raw);
  }
  return {
    refs: [...refs].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0)),
    transport
  };
}

export function interactionResponseAuthenticatedContext(
  channel: Channel & {
    encryption_policy_generation: string;
    encryption_group_id: string;
    encryption_epoch: string;
  },
  input: Omit<EncryptedInteractionResponseInput, 'envelope' | 'attachments'> & {
    attachmentRefs: readonly string[];
    interactionContractDigest: string | null;
    senderDeviceId: string;
  }
): InteractionResponseAuthenticatedContext {
  const interaction = canonicalQualifiedRef(input.interactionRef, input.authorityDomain);
  const response = canonicalQualifiedRef(input.responseRef, input.authorityDomain);
  const channelIdentity = canonicalQualifiedRef(input.channelRef, input.authorityDomain);
  const application = canonicalQualifiedRef(input.applicationRef);
  const invoker = canonicalQualifiedRef(input.invokerRef);
  const attachmentRefs = [...input.attachmentRefs];
  const sortedAttachmentRefs = [...attachmentRefs].sort((left, right) =>
    left < right ? -1 : left > right ? 1 : 0
  );
  if (
    !interaction ||
    !response ||
    !channelIdentity ||
    !application ||
    !invoker ||
    input.channelRef !== `${channel.id}@${channel.origin_domain}` ||
    !Number.isSafeInteger(input.sequence) ||
    input.sequence < 0 ||
    ![4, 7, 8, 9].includes(input.callbackType) ||
    !/^[1-9][0-9]{0,18}$/u.test(input.revision) ||
    BigInt(input.revision) > 9_223_372_036_854_775_807n ||
    !/^kbe_[A-Za-z0-9_-]{43}$/u.test(input.senderDeviceId) ||
    (input.interactionContractDigest !== null &&
      !isCanonicalBase64url32(input.interactionContractDigest)) ||
    attachmentRefs.length > 10 ||
    attachmentRefs.some(
      (ref, index) =>
        !canonicalQualifiedRef(ref, input.authorityDomain) || ref !== sortedAttachmentRefs[index]
    ) ||
    new Set(attachmentRefs).size !== attachmentRefs.length ||
    (input.operation === 'CREATE' && input.revision !== '1') ||
    (input.operation === 'UPDATE' && BigInt(input.revision) <= 1n)
  ) {
    throw new Error('Encrypted bot response authority projection is invalid.');
  }
  return {
    application_ref: application.ref,
    attachment_refs: attachmentRefs,
    authority_domain: input.authorityDomain,
    callback_type: input.callbackType,
    channel_ref: channelIdentity.ref,
    epoch: channel.encryption_epoch,
    group_id: channel.encryption_group_id,
    interaction_ref: interaction.ref,
    interaction_contract_digest: input.interactionContractDigest,
    invoker_ref: invoker.ref,
    operation: input.operation === 'CREATE' ? 'create' : 'edit',
    policy_generation: channel.encryption_policy_generation,
    response_ref: response.ref,
    revision: input.revision,
    sender_device_id: input.senderDeviceId,
    sequence: String(input.sequence)
  };
}

function processedMessageKey(
  channel: Channel,
  envelope: MlsEnvelope,
  message: Pick<
    Message,
    | 'id'
    | 'origin_domain'
    | 'author_id'
    | 'author_domain'
    | 'application_id'
    | 'application_domain'
    | 'webhook_id'
    | 'webhook'
  >
): string {
  const webhookRef = projectedWebhookRef(message);
  return [
    `${channel.id}@${channel.origin_domain}`,
    envelope.group_id,
    envelope.policy_generation,
    envelope.epoch,
    `${message.id}@${message.origin_domain}`,
    `${message.author_id}@${message.author_domain}`,
    message.application_id && message.application_domain
      ? `${message.application_id}@${message.application_domain}`
      : '',
    webhookRef ?? '',
    JSON.stringify(canonicalJsonValue(envelope, new Set()))
  ].join('\0');
}

function projectedWebhookRef(message: Pick<Message, 'webhook_id' | 'webhook'>): string | null {
  if ((message.webhook_id === null || message.webhook_id === undefined) && !message.webhook) {
    return null;
  }
  const webhook = message.webhook;
  if (
    typeof message.webhook_id !== 'string' ||
    !webhook ||
    webhook.id !== message.webhook_id ||
    typeof webhook.origin_domain !== 'string' ||
    typeof webhook.ref !== 'string'
  ) {
    throw new Error('The encrypted message webhook attribution is invalid.');
  }
  const parsed = canonicalQualifiedRef(webhook.ref);
  if (
    !parsed ||
    parsed.id !== webhook.id ||
    parsed.domain !== webhook.origin_domain ||
    parsed.ref !== webhook.ref
  ) {
    throw new Error('The encrypted message webhook attribution is invalid.');
  }
  return parsed.ref;
}

export function validateEncryptedMessageSenderCredential(
  credential: Uint8Array,
  message: Pick<
    Message,
    | 'author_id'
    | 'author_domain'
    | 'application_id'
    | 'application_domain'
    | 'webhook_id'
    | 'webhook'
  >,
  senderDeviceId: string
): void {
  const parsed = JSON.parse(decodeUtf8(credential)) as Record<string, unknown>;
  const expected = `${message.author_id}@${message.author_domain}`;
  const applicationRef =
    message.application_id && message.application_domain
      ? `${message.application_id}@${message.application_domain}`
      : null;
  const webhookRef = projectedWebhookRef(message);
  const humanValid =
    Object.keys(parsed).sort().join(',') === 'account,nonce,version' &&
    parsed.version === 1 &&
    parsed.account === expected &&
    typeof parsed.nonce === 'string' &&
    isCanonicalBase64url32(parsed.nonce) &&
    /^ked_[A-Za-z0-9_-]{43}$/u.test(senderDeviceId) &&
    applicationRef === null &&
    webhookRef === null;
  const workerId = typeof parsed.worker_id === 'string' ? parsed.worker_id : '';
  const botValid =
    webhookRef === null &&
    Boolean(applicationRef && canonicalQualifiedRef(applicationRef)) &&
    parsed.credential_type === 'kaede-bot-device-v2' &&
    parsed.application_ref === applicationRef &&
    parsed.device_id === senderDeviceId &&
    /^kbe_[A-Za-z0-9_-]{43}$/u.test(senderDeviceId) &&
    /^[1-9][0-9]{0,18}$/u.test(workerId) &&
    BigInt(workerId) <= 9_223_372_036_854_775_807n &&
    parsed.account === `bot:${applicationRef}:worker:${workerId}` &&
    Object.keys(parsed).sort().join(',') ===
      'account,application_ref,credential_type,device_id,worker_id';
  const webhookValid =
    applicationRef === null &&
    webhookRef !== null &&
    Object.keys(parsed).sort().join(',') === 'account,credential_type,device_id,webhook_ref' &&
    parsed.account === `webhook:${webhookRef}` &&
    parsed.credential_type === 'kaede-webhook-device-v1' &&
    parsed.device_id === senderDeviceId &&
    parsed.webhook_ref === webhookRef &&
    /^kwe_[A-Za-z0-9_-]{43}$/u.test(senderDeviceId);
  if (!humanValid && !botValid && !webhookValid) {
    throw new Error(
      'The encrypted message sender identity does not match its author or app/webhook actor.'
    );
  }
}

function validateBotResponseCredential(
  credential: Uint8Array,
  context: InteractionResponseAuthenticatedContext
): void {
  const parsed = JSON.parse(decodeUtf8(credential)) as Record<string, unknown>;
  const workerId = typeof parsed.worker_id === 'string' ? parsed.worker_id : '';
  if (
    Object.keys(parsed).sort().join(',') !==
      'account,application_ref,credential_type,device_id,worker_id' ||
    parsed.credential_type !== 'kaede-bot-device-v2' ||
    parsed.application_ref !== context.application_ref ||
    parsed.device_id !== context.sender_device_id ||
    !/^[1-9][0-9]{0,18}$/u.test(workerId) ||
    BigInt(workerId) > 9_223_372_036_854_775_807n ||
    parsed.account !== `bot:${context.application_ref}:worker:${workerId}`
  ) {
    throw new Error('The encrypted bot response sender identity is invalid.');
  }
}

function authenticatedInteractionResponseData(
  value: unknown,
  context: InteractionResponseAuthenticatedContext,
  transport: readonly Record<string, unknown>[]
): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('The encrypted bot response body is invalid.');
  }
  const canonical = canonicalJsonValue(value, new Set()) as Record<string, unknown>;
  validateInteractionResponsePlaintext(canonical, context.callback_type);
  const rawManifests = canonical.attachments;
  if (
    rawManifests !== undefined &&
    (!rawManifests || typeof rawManifests !== 'object' || Array.isArray(rawManifests))
  ) {
    throw new Error('The encrypted bot response file manifests are invalid.');
  }
  const manifestMap = (rawManifests ?? {}) as Record<string, unknown>;
  if (
    Object.keys(manifestMap).length !== context.attachment_refs.length ||
    Object.keys(manifestMap).some((ref) => !context.attachment_refs.includes(ref))
  ) {
    throw new Error('The encrypted bot response files do not match their authenticated refs.');
  }
  if (!context.attachment_refs.length) {
    if (rawManifests === undefined) return canonical;
    return { ...canonical, attachments: [] };
  }
  const ids: string[] = [];
  const byId: Record<string, EncryptedFileManifest> = {};
  for (const ref of context.attachment_refs) {
    const parsed = canonicalQualifiedRef(ref, context.authority_domain);
    const manifest = manifestMap[ref];
    if (!parsed || !manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
      throw new Error('The encrypted bot response file manifest is invalid.');
    }
    ids.push(parsed.id);
    byId[parsed.id] = manifest as unknown as EncryptedFileManifest;
  }
  const manifests = interactionAttachmentManifests(ids, byId);
  const byRef = new Map(
    transport.map((item) => [`${String(item.id)}@${String(item.origin_domain)}`, item])
  );
  const attachments = context.attachment_refs.map((ref) => {
    const parsed = canonicalQualifiedRef(ref, context.authority_domain)!;
    const manifest = manifests[parsed.id];
    const projection = byRef.get(ref);
    if (
      !manifest ||
      manifest.attachment_domain !== parsed.domain ||
      !projection ||
      projection.private_media_url !==
        `/api/v1/interactions/${context.interaction_ref}/responses/${context.response_ref}/attachments/${ref}`
    ) {
      throw new Error('The encrypted bot response file transport is invalid.');
    }
    return {
      id: parsed.id,
      origin_domain: parsed.domain,
      filename: manifest.filename,
      content_type: manifest.content_type,
      size: manifest.plaintext_size,
      width: null,
      height: null,
      blurhash: null,
      scan_status: 'encrypted',
      encryption_mode: 'e2ee',
      encryption_protocol: 'kaede-file-v1',
      variants: {},
      private_media_url: projection.private_media_url,
      encrypted_manifest: manifest
    };
  });
  return { ...canonical, attachments };
}

function validateInteractionResponsePlaintext(
  data: Record<string, unknown>,
  callbackType: number
): void {
  const encoded = canonicalInteractionJson(data);
  try {
    if (encoded.length > 64 * 1024) {
      throw new Error('The encrypted bot response body is too large.');
    }
  } finally {
    clearBytes(encoded);
  }
  if (callbackType === 8) {
    if (Object.keys(data).join(',') !== 'choices' || !Array.isArray(data.choices)) {
      throw new Error('The encrypted autocomplete response is invalid.');
    }
    if (data.choices.length > 25)
      throw new Error('The encrypted autocomplete response is invalid.');
    for (const raw of data.choices) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        throw new Error('The encrypted autocomplete response is invalid.');
      }
      const choice = raw as Record<string, unknown>;
      const value = choice.value;
      if (
        Object.keys(choice).sort().join(',') !== 'name,value' ||
        typeof choice.name !== 'string' ||
        choice.name.length < 1 ||
        choice.name.length > 100 ||
        !(
          (typeof value === 'string' && value.length >= 1 && value.length <= 100) ||
          (typeof value === 'number' && Number.isFinite(value) && Number.isSafeInteger(value)
            ? Math.abs(value) <= Number.MAX_SAFE_INTEGER
            : typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e308)
        )
      ) {
        throw new Error('The encrypted autocomplete response is invalid.');
      }
    }
    return;
  }
  if (callbackType === 9) {
    if (
      Object.keys(data).sort().join(',') !== 'components,custom_id,title' ||
      typeof data.title !== 'string' ||
      data.title.length < 1 ||
      data.title.length > 45 ||
      typeof data.custom_id !== 'string' ||
      data.custom_id.length < 1 ||
      data.custom_id.length > 100 ||
      !Array.isArray(data.components) ||
      data.components.length < 1 ||
      data.components.length > 5 ||
      data.components.some(
        (component) =>
          !component ||
          typeof component !== 'object' ||
          Array.isArray(component) ||
          ![1, 10, 18].includes(Number((component as Record<string, unknown>).type))
      )
    ) {
      throw new Error('The encrypted modal response is invalid.');
    }
    validateBoundedRichTree(data.components, 0);
    return;
  }
  if (![4, 7].includes(callbackType)) {
    throw new Error('The encrypted bot response callback type is unsupported.');
  }
  const allowed = new Set([
    'content',
    'embeds',
    'components',
    'flags',
    'poll',
    'attachments',
    'view_timeout_seconds',
    'view_persistent'
  ]);
  if (Object.keys(data).some((key) => !allowed.has(key))) {
    throw new Error('The encrypted bot message contains a server-only field.');
  }
  if (
    (data.content !== undefined &&
      (typeof data.content !== 'string' ||
        data.content.length < 1 ||
        data.content.length > 4000)) ||
    (data.embeds !== undefined && (!Array.isArray(data.embeds) || data.embeds.length > 10)) ||
    (data.components !== undefined &&
      (!Array.isArray(data.components) || data.components.length > 40)) ||
    (data.flags !== undefined &&
      (!Number.isSafeInteger(data.flags) ||
        Number(data.flags) < 0 ||
        Number(data.flags) > 2_147_483_647)) ||
    (data.attachments !== undefined &&
      (!data.attachments ||
        typeof data.attachments !== 'object' ||
        Array.isArray(data.attachments))) ||
    (data.view_timeout_seconds !== undefined &&
      (!Number.isSafeInteger(data.view_timeout_seconds) ||
        Number(data.view_timeout_seconds) < 1 ||
        Number(data.view_timeout_seconds) > 86_400)) ||
    (data.view_persistent !== undefined && data.view_persistent !== false)
  ) {
    throw new Error('The encrypted bot message shape is invalid.');
  }
  validateBoundedRichTree(data.embeds ?? [], 0);
  validateBoundedRichTree(data.components ?? [], 0);
  if (data.poll !== undefined) validateBoundedRichTree(data.poll, 0);
}

function validateBoundedRichTree(value: unknown, depth: number): void {
  if (depth > 8) throw new Error('The encrypted bot rich content is too deeply nested.');
  if (value === null || typeof value === 'boolean' || typeof value === 'number') return;
  if (typeof value === 'string') {
    if (value.length > 4_000) throw new Error('The encrypted bot rich content is too large.');
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > 100) throw new Error('The encrypted bot rich content has too many items.');
    for (const item of value) validateBoundedRichTree(item, depth + 1);
    return;
  }
  if (!value || typeof value !== 'object' || Object.keys(value).length > 64) {
    throw new Error('The encrypted bot rich content is invalid.');
  }
  for (const child of Object.values(value as Record<string, unknown>)) {
    validateBoundedRichTree(child, depth + 1);
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
      for (const claimed of proposal.key_packages) {
        packages.push(await this.#validateClaimedKeyPackage(claimed, seenDevices));
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
    seenDevices: Set<string>
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
      !/^(?:ked|kbe|kwe)_[A-Za-z0-9_-]{43}$/u.test(claimed.device_id) ||
      claimed.device_id === this.deviceId ||
      seenDevices.has(claimed.device_id) ||
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
      if (!parsed || Array.isArray(parsed)) {
        throw new Error('A claimed key package has an invalid participant credential.');
      }
      if (claimed.device_id.startsWith('ked_')) {
        if (
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
      } else if (claimed.device_id.startsWith('kbe_')) {
        const application = canonicalQualifiedRef(parsed.application_ref);
        const workerId = typeof parsed.worker_id === 'string' ? parsed.worker_id : '';
        if (
          Object.keys(parsed).sort().join(',') !==
            'account,application_ref,credential_type,device_id,worker_id' ||
          !application ||
          application.domain !== claimed.user_domain ||
          parsed.credential_type !== 'kaede-bot-device-v2' ||
          parsed.device_id !== claimed.device_id ||
          !/^[1-9][0-9]{0,18}$/u.test(workerId) ||
          BigInt(workerId) > 9_223_372_036_854_775_807n ||
          parsed.account !== `bot:${application.ref}:worker:${workerId}` ||
          (await botIdentityDeviceId(application.ref, workerId, embeddedIdentity)) !==
            claimed.device_id
        ) {
          throw new Error('A claimed bot key package has an invalid device credential.');
        }
      } else {
        const webhook = canonicalQualifiedRef(parsed.webhook_ref);
        if (
          Object.keys(parsed).sort().join(',') !==
            'account,credential_type,device_id,webhook_ref' ||
          !webhook ||
          webhook.ref !== claimedAccount ||
          webhook.domain !== claimed.user_domain ||
          parsed.credential_type !== 'kaede-webhook-device-v1' ||
          parsed.device_id !== claimed.device_id ||
          parsed.account !== `webhook:${webhook.ref}` ||
          (await webhookIdentityDeviceId(webhook.ref, embeddedIdentity)) !== claimed.device_id
        ) {
          throw new Error('A claimed webhook key package has an invalid device credential.');
        }
      }
      seenDevices.add(claimed.device_id);
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
    options: MessageEncryptionOptions = {}
  ): Promise<MlsEnvelope> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      return this.#encryptMessageUnlocked(channel, content, options);
    });
  }

  async encryptInteraction(
    channel: Channel,
    input: EncryptedInteractionInput
  ): Promise<PreparedEncryptedInteraction> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      requireActiveChannel(channel);
      const options = input.options ?? {};
      const values = [...(input.values ?? [])];
      const components = [...(input.components ?? [])];
      const context = interactionAuthenticatedContext(
        channel,
        this.accountRef,
        this.deviceId,
        input
      );
      const attachments = interactionAttachmentManifests(
        context.attachment_ids,
        input.attachments ?? {}
      );
      if (
        (input.interactionType === 'command' || input.interactionType === 'autocomplete') &&
        (values.length || components.length)
      ) {
        throw new Error('Encrypted command interactions may contain only command options.');
      }
      if (
        input.interactionType === 'component' &&
        (Object.keys(options).length || components.length)
      )
        throw new Error('Encrypted component interactions may contain only selected values.');
      if (
        input.interactionType === 'modal_submit' &&
        (Object.keys(options).length || values.length)
      ) {
        throw new Error('Encrypted modal interactions may contain only submitted components.');
      }
      const plaintext = canonicalInteractionJson({
        context,
        data: { attachments, components, options, values },
        kind: 'interaction',
        version: 1
      });
      const aad = canonicalInteractionJson({
        context,
        purpose: 'kaede.interaction.v1'
      });
      const groupId = fromBase64url(channel.encryption_group_id, 128);
      let attachmentManifestDigest: string | null = null;
      if (Object.keys(attachments).length) {
        const manifestBytes = canonicalInteractionJson(attachments);
        try {
          attachmentManifestDigest = base64url(await sha256(manifestBytes));
        } finally {
          clearBytes(manifestBytes);
        }
      }
      let ciphertext: Uint8Array | null = null;
      try {
        ciphertext = ownedBytes(this.#mls.encrypt(groupId, plaintext, aad));
        const envelope: MlsEnvelope = {
          version: 2,
          protocol: MLS_PROTOCOL,
          suite: MLS_SUITE,
          group_id: channel.encryption_group_id,
          policy_generation: channel.encryption_policy_generation,
          epoch: channel.encryption_epoch,
          sender_device_id: this.deviceId,
          operation: 'create',
          ciphertext: base64url(ciphertext)
        };
        if (attachmentManifestDigest)
          envelope.attachment_manifest_digest = attachmentManifestDigest;
        return {
          context,
          attachmentIds: context.attachment_ids,
          envelope
        };
      } finally {
        clearBytes(plaintext);
        clearBytes(aad);
        clearBytes(groupId);
        if (ciphertext) clearBytes(ciphertext);
      }
    });
  }

  async decryptInteractionResponse(
    channel: Channel,
    input: EncryptedInteractionResponseInput
  ): Promise<DecryptedInteractionResponse> {
    return this.#synchronized(async () => {
      await this.#syncControlLogUnlocked(channel);
      requireEncryptedChannel(channel);
      if (input.invokerRef !== this.accountRef) {
        throw new Error('The encrypted bot response targets another account.');
      }
      const envelope = input.envelope;
      const operation = input.operation === 'CREATE' ? 'create' : 'edit';
      const hasContract =
        Object.hasOwn(envelope, 'interaction_contract') &&
        Object.hasOwn(envelope, 'interaction_contract_digest');
      const hasPartialContract =
        Object.hasOwn(envelope, 'interaction_contract') !==
        Object.hasOwn(envelope, 'interaction_contract_digest');
      const requiredFields = new Set([
        'version',
        'protocol',
        'suite',
        'group_id',
        'policy_generation',
        'epoch',
        'sender_device_id',
        'operation',
        'ciphertext',
        'interaction_ref',
        'response_ref',
        'sequence',
        'revision',
        'callback_type',
        'attachment_refs',
        ...(hasContract ? ['interaction_contract', 'interaction_contract_digest'] : []),
        ...(operation === 'edit' ? ['target_message'] : [])
      ]);
      if (
        hasPartialContract ||
        Object.keys(envelope).length !== requiredFields.size ||
        Object.keys(envelope).some((key) => !requiredFields.has(key)) ||
        envelope.version !== 2 ||
        envelope.protocol !== MLS_PROTOCOL ||
        envelope.suite !== MLS_SUITE ||
        envelope.operation !== operation ||
        typeof envelope.sender_device_id !== 'string' ||
        typeof envelope.ciphertext !== 'string' ||
        typeof envelope.interaction_ref !== 'string' ||
        typeof envelope.response_ref !== 'string' ||
        typeof envelope.sequence !== 'string' ||
        typeof envelope.revision !== 'string' ||
        typeof envelope.callback_type !== 'number' ||
        ![4, 7, 8, 9].includes(envelope.callback_type) ||
        !Array.isArray(envelope.attachment_refs) ||
        envelope.attachment_refs.some((ref) => typeof ref !== 'string') ||
        (operation === 'create'
          ? 'target_message' in envelope
          : envelope.target_message !== input.responseRef)
      ) {
        throw new Error('The encrypted bot response envelope is invalid.');
      }
      if ((input.callbackType === 9 && !hasContract) || (input.callbackType === 8 && hasContract)) {
        throw new Error('The encrypted bot response routing contract is invalid.');
      }
      const interactionContractDigest = hasContract
        ? (envelope.interaction_contract_digest as unknown)
        : null;
      if (
        interactionContractDigest !== null &&
        !isCanonicalBase64url32(interactionContractDigest)
      ) {
        throw new Error('The encrypted bot response routing contract digest is invalid.');
      }
      const interactionContract = hasContract
        ? validateInteractionRoutingContract(envelope.interaction_contract, input.callbackType)
        : null;
      if (
        interactionContract &&
        (await interactionRoutingContractDigest(interactionContract)) !== interactionContractDigest
      ) {
        throw new Error('The encrypted bot response routing contract digest is invalid.');
      }
      const { refs: attachmentRefs, transport } = canonicalResponseAttachmentRefs(
        input.attachments,
        input.authorityDomain,
        input.interactionRef,
        input.responseRef
      );
      const context = interactionResponseAuthenticatedContext(channel, {
        authorityDomain: input.authorityDomain,
        interactionRef: input.interactionRef,
        responseRef: input.responseRef,
        invokerRef: input.invokerRef,
        channelRef: input.channelRef,
        applicationRef: input.applicationRef,
        sequence: input.sequence,
        revision: input.revision,
        callbackType: input.callbackType,
        operation: input.operation,
        attachmentRefs,
        interactionContractDigest,
        senderDeviceId: envelope.sender_device_id
      });
      if (
        envelope.group_id !== context.group_id ||
        envelope.policy_generation !== context.policy_generation ||
        envelope.epoch !== context.epoch ||
        envelope.interaction_ref !== context.interaction_ref ||
        envelope.response_ref !== context.response_ref ||
        envelope.sequence !== context.sequence ||
        envelope.revision !== context.revision ||
        envelope.callback_type !== context.callback_type ||
        (hasContract ? envelope.interaction_contract_digest : null) !==
          context.interaction_contract_digest ||
        JSON.stringify(envelope.attachment_refs) !== JSON.stringify(context.attachment_refs)
      ) {
        throw new Error('The encrypted bot response context does not match its projection.');
      }
      const groupId = fromBase64url(context.group_id, 128);
      const ciphertext = fromBase64url(envelope.ciphertext, 64 * 1024);
      const expectedAad = canonicalInteractionJson({
        context,
        purpose: 'kaede.interaction.response.v1'
      });
      let processed: ReturnType<KaedeMlsClient['process']> | null = null;
      try {
        processed = this.#mls.process(groupId, ciphertext);
        if (processed.kind !== 'application' || !processed.application || !processed.aad) {
          throw new Error('The encrypted bot response is not an MLS application message.');
        }
        const receivedAad = ownedBytes(processed.aad);
        try {
          if (!sameBytes(receivedAad, expectedAad)) {
            throw new Error('The encrypted bot response authenticated context was modified.');
          }
        } finally {
          clearBytes(receivedAad);
        }
        if (!processed.credential) {
          throw new Error('The encrypted bot response sender identity is missing.');
        }
        const senderCredential = ownedBytes(processed.credential);
        try {
          validateBotResponseCredential(senderCredential, context);
        } finally {
          clearBytes(senderCredential);
        }
        const plaintext = JSON.parse(decodeUtf8(processed.application)) as Record<string, unknown>;
        if (
          !plaintext ||
          typeof plaintext !== 'object' ||
          Array.isArray(plaintext) ||
          Object.keys(plaintext).sort().join(',') !== 'context,data,kind,version' ||
          plaintext.version !== 1 ||
          plaintext.kind !== 'interaction_response' ||
          !plaintext.context ||
          typeof plaintext.context !== 'object' ||
          Array.isArray(plaintext.context)
        ) {
          throw new Error('The encrypted bot response plaintext is invalid.');
        }
        const receivedContext = canonicalInteractionJson(plaintext.context);
        const expectedContext = canonicalInteractionJson(context);
        try {
          if (!sameBytes(receivedContext, expectedContext)) {
            throw new Error('The encrypted bot response plaintext context was modified.');
          }
        } finally {
          clearBytes(receivedContext);
          clearBytes(expectedContext);
        }
        const data = authenticatedInteractionResponseData(plaintext.data, context, transport);
        await validateInteractionRoutingContractForData(
          data,
          context.callback_type,
          interactionContract,
          context.interaction_contract_digest
        );
        return { context, data };
      } finally {
        processed?.free();
        clearBytes(groupId);
        clearBytes(ciphertext);
        clearBytes(expectedAad);
      }
    });
  }

  async #encryptMessageUnlocked(
    channel: Channel,
    content: string,
    options: MessageEncryptionOptions
  ): Promise<MlsEnvelope> {
    requireActiveChannel(channel);
    const attachments = options.attachments ?? [];
    const operation = options.operation ?? 'create';
    if (operation === 'edit' && !options.targetMessage)
      throw new Error('Encrypted edits require a target message.');
    if (options.rich) {
      return this.#encryptRichMessageUnlocked(channel, content, options, operation);
    }
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
        messageRef: null,
        applicationRef: null
      });
      return envelope;
    } finally {
      clearBytes(encoded);
      clearBytes(groupId);
      clearBytes(aad);
      if (ciphertext) clearBytes(ciphertext);
    }
  }

  async #encryptRichMessageUnlocked(
    channel: Channel,
    content: string,
    options: MessageEncryptionOptions,
    operation: 'create' | 'edit'
  ): Promise<MlsEnvelope> {
    requireActiveChannel(channel);
    const rich = options.rich!;
    const sortedAttachments = [...(options.attachments ?? [])].sort((left, right) => {
      const leftRef = `${left.attachment_id}@${left.attachment_domain}`;
      const rightRef = `${right.attachment_id}@${right.attachment_domain}`;
      return leftRef < rightRef ? -1 : leftRef > rightRef ? 1 : 0;
    });
    const attachments = validateEncryptedRichMessageAttachments(
      sortedAttachments,
      rich.voiceMessage ?? false
    );
    const stickerItems = (rich.stickerItems ?? []).map((item) => ({
      id: item.id,
      origin_domain: item.origin_domain,
      name: item.name,
      format_type: item.format_type
    }));
    const mentionRefs = [...(options.mentionUserRefs ?? [])].sort((left, right) =>
      left < right ? -1 : left > right ? 1 : 0
    );
    if (!canonicalSortedQualifiedRefList(mentionRefs, 5_000)) {
      throw new Error('Encrypted rich message mention references are invalid.');
    }
    const referencedMessageRef = options.referencedMessageRef ?? null;
    if (referencedMessageRef !== null && !canonicalQualifiedRef(referencedMessageRef)) {
      throw new Error('Encrypted rich message reply reference is invalid.');
    }
    const repliedUserRef = options.repliedUserRef ?? null;
    if (repliedUserRef !== null && !canonicalQualifiedRef(repliedUserRef)) {
      throw new Error('Encrypted rich message replied-user reference is invalid.');
    }
    const allowedMentions = validateEncryptedAllowedMentions(
      rich.allowedMentions ?? {
        parse: ['everyone', 'roles', 'users'],
        users: [],
        roles: [],
        replied_user: repliedUserRef !== null
      }
    );
    if (
      allowedMentions.replied_user !== (repliedUserRef !== null) ||
      (repliedUserRef !== null && referencedMessageRef === null)
    ) {
      throw new Error('Encrypted rich message reply notification routing is incomplete.');
    }
    const forward = rich.forward;
    const forwardSnapshot = forward ? validateEncryptedForwardSnapshot(forward.snapshot) : null;
    if (forward) {
      const sourceMessage = canonicalQualifiedRef(forward.sourceMessageRef);
      const sourceChannel = canonicalQualifiedRef(forward.sourceChannelRef);
      if (
        !sourceMessage ||
        !sourceChannel ||
        !isCanonicalBase64url32(forward.sourceProjectionDigest) ||
        forward.sourceCreatedAt !== forwardSnapshot?.created_at ||
        forward.sourceEditedAt !== forwardSnapshot?.edited_at ||
        forward.sourceFlags !== forwardSnapshot?.flags ||
        forward.sourceMessageType !== forwardSnapshot?.message_type ||
        (await encryptedForwardSnapshotProjectionDigest(forwardSnapshot)) !==
          forward.sourceProjectionDigest
      ) {
        throw new Error('Encrypted forward source metadata is invalid.');
      }
      const snapshotAttachments = forwardSnapshot.attachments;
      const snapshotBytes = canonicalInteractionJson(snapshotAttachments);
      const attachmentBytes = canonicalInteractionJson(attachments);
      try {
        if (!sameBytes(snapshotBytes, attachmentBytes)) {
          throw new Error('Encrypted forward attachments do not match the destination uploads.');
        }
      } finally {
        clearBytes(snapshotBytes);
        clearBytes(attachmentBytes);
      }
    }
    const data: Record<string, unknown> = {
      content: content.trim() ? content : null,
      embeds: rich.embeds ?? [],
      components: rich.components ?? [],
      poll: rich.poll ?? null,
      sticker_items: stickerItems,
      tts: rich.tts ?? false,
      voice_message: rich.voiceMessage ?? false,
      flags: rich.flags ?? 0,
      allowed_mentions: allowedMentions,
      forward_snapshot: forwardSnapshot,
      attachments
    };
    if (
      !Number.isSafeInteger(data.flags) ||
      Number(data.flags) < 0 ||
      Number(data.flags) > 2_147_483_647 ||
      !Array.isArray(data.embeds) ||
      data.embeds.length > 10 ||
      !Array.isArray(data.components) ||
      data.components.length > 40
    ) {
      throw new Error('Encrypted rich message body is invalid.');
    }
    validateBoundedRichTree(data.embeds, 0);
    validateBoundedRichTree(data.components, 0);
    if (data.poll !== null) {
      const poll = routingRecord(data.poll, 'Encrypted poll');
      const answers = poll.answers;
      if (
        !hasExactRoutingFields(poll, [
          'question',
          'answers',
          'duration',
          'allow_multiselect',
          'layout_type'
        ]) ||
        !Array.isArray(answers) ||
        answers.length < 2 ||
        answers.length > 10
      ) {
        throw new Error('Encrypted poll is invalid.');
      }
      const question = richPollMedia(poll.question, false);
      if (typeof question.text !== 'string' || question.emoji !== undefined) {
        throw new Error('Encrypted poll question is invalid.');
      }
      for (const answer of answers) {
        const raw = routingRecord(answer, 'Encrypted poll answer');
        if (!hasExactRoutingFields(raw, ['poll_media'])) {
          throw new Error('Encrypted poll answer is invalid.');
        }
        richPollMedia(raw.poll_media, true);
      }
    }
    const componentsV2 = data.components.some(
      (item) => (item as Record<string, unknown>).type !== 1
    );
    if (
      Boolean(Number(data.flags) & (1 << 15)) !== componentsV2 ||
      (componentsV2 &&
        (data.content !== null ||
          data.embeds.length ||
          data.poll !== null ||
          stickerItems.length)) ||
      (data.voice_message === true &&
        (data.content !== null ||
          data.tts === true ||
          data.embeds.length ||
          data.components.length ||
          data.poll !== null ||
          stickerItems.length ||
          attachments.length !== 1)) ||
      (forwardSnapshot !== null && data.poll !== null) ||
      (data.content === null &&
        !data.embeds.length &&
        !data.components.length &&
        data.poll === null &&
        !stickerItems.length &&
        !attachments.length)
    ) {
      throw new Error('Encrypted rich message content combination is invalid.');
    }
    const contract = await interactionRoutingContract(data, null);
    const hasControls =
      contract !== null && Array.isArray(contract.components) && contract.components.length > 0;
    if (hasControls) {
      throw new Error('Human encrypted messages cannot own application interaction controls.');
    }
    const mentionIntent = richMessageMentionIntent(data);
    const requiredRecipients = new Set([
      ...mentionIntent.userRefs,
      ...(repliedUserRef ? [repliedUserRef] : [])
    ]);
    const resolvedRecipients = new Set(mentionRefs);
    if (
      [...requiredRecipients].some((ref) => !resolvedRecipients.has(ref)) ||
      (!mentionIntent.roleRefs.length &&
        !mentionIntent.everyone &&
        (requiredRecipients.size !== resolvedRecipients.size ||
          [...resolvedRecipients].some((ref) => !requiredRecipients.has(ref))))
    ) {
      throw new Error('Encrypted rich message resolved mention routing is invalid.');
    }
    const contractDigest = contract ? await interactionRoutingContractDigest(contract) : null;
    const attachmentRefs = attachments.map(
      (item) => `${item.attachment_id}@${item.attachment_domain}`
    );
    const attachmentBytes = attachments.length ? canonicalInteractionJson(attachments) : null;
    const attachmentDigest = attachmentBytes ? base64url(await sha256(attachmentBytes)) : null;
    if (attachmentBytes) clearBytes(attachmentBytes);
    const revision =
      operation === 'create' ? '1' : (canonicalUnsignedI63(rich.messageRevision, true) ?? '');
    if (operation === 'edit' && (!revision || BigInt(revision) <= 1n)) {
      throw new Error('Encrypted rich edits require the next positive message revision.');
    }
    const richDigest = await richMessagePayloadDigest(data);
    const forwardProjectionDigest = await richMessageForwardProjectionDigest(data, mentionRefs);
    const forwardSnapshotDigest = forwardSnapshot
      ? await encryptedForwardSnapshotDigest(forwardSnapshot)
      : null;
    const context = validateRichMessageAuthenticatedContext({
      application_ref: null,
      attachment_manifest_digest: attachmentDigest,
      author_ref: this.accountRef,
      channel_ref: `${channel.id}@${channel.origin_domain}`,
      epoch: channel.encryption_epoch,
      forward_projection_digest: forwardProjectionDigest,
      forward_projection_version: forwardProjectionDigest === null ? null : 2,
      forward_snapshot_digest: forwardSnapshotDigest,
      forward_source_projection_digest: forward?.sourceProjectionDigest ?? null,
      forwarded_channel_ref: forward?.sourceChannelRef ?? null,
      forwarded_created_at: forward?.sourceCreatedAt ?? null,
      forwarded_edited_at: forward?.sourceEditedAt ?? null,
      forwarded_flags: forward?.sourceFlags ?? null,
      forwarded_message_ref: forward?.sourceMessageRef ?? null,
      forwarded_message_type: forward?.sourceMessageType ?? null,
      group_id: channel.encryption_group_id,
      interaction_contract_digest: contractDigest,
      interaction_installation_ref: null,
      interaction_installation_revision: null,
      interaction_integration_type: null,
      message_attachment_refs: attachmentRefs,
      message_custom_emoji_refs: richMessageCustomEmojiRefs(data),
      message_mention_everyone: mentionIntent.everyone,
      message_mention_refs: mentionRefs,
      message_mention_role_refs: mentionIntent.roleRefs,
      message_mention_user_refs: mentionIntent.userRefs,
      message_replied_user_ref: repliedUserRef,
      message_sticker_refs: richMessageStickerRefs(data),
      message_flags: data.flags,
      message_revision: revision,
      operation,
      policy_generation: channel.encryption_policy_generation,
      referenced_message_ref: referencedMessageRef,
      rich_payload_digest: richDigest,
      sender_device_id: this.deviceId,
      target_message: options.targetMessage ?? null,
      tts: data.tts,
      view_persistent: false,
      view_version: '0',
      voice_message: data.voice_message
    });
    const plaintext: RichPlaintextApplication = { version: 2, kind: 'message', context, data };
    const encoded = canonicalInteractionJson(plaintext);
    const aad = richMessageAuthenticatedData(context);
    const groupId = fromBase64url(channel.encryption_group_id, 128);
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
        forward_projection_digest: context.forward_projection_digest,
        forward_projection_version: context.forward_projection_version,
        forward_source_projection_digest: context.forward_source_projection_digest,
        forwarded_created_at: context.forwarded_created_at,
        forwarded_edited_at: context.forwarded_edited_at,
        forwarded_flags: context.forwarded_flags,
        forwarded_message_type: context.forwarded_message_type,
        sender_device_id: this.deviceId,
        operation,
        ciphertext: base64url(ciphertext),
        author_ref: context.author_ref,
        message_revision: context.message_revision,
        message_attachment_refs: context.message_attachment_refs,
        message_custom_emoji_refs: context.message_custom_emoji_refs,
        message_mention_everyone: context.message_mention_everyone,
        message_mention_refs: context.message_mention_refs,
        message_mention_role_refs: context.message_mention_role_refs,
        message_mention_user_refs: context.message_mention_user_refs,
        message_replied_user_ref: context.message_replied_user_ref,
        message_sticker_refs: context.message_sticker_refs,
        referenced_message_ref: context.referenced_message_ref,
        rich_payload_digest: context.rich_payload_digest,
        application_ref: null,
        interaction_integration_type: null,
        interaction_installation_ref: null,
        interaction_installation_revision: null,
        view_version: '0',
        view_persistent: false,
        tts: context.tts,
        voice_message: context.voice_message,
        message_flags: context.message_flags,
        forward_snapshot_digest: context.forward_snapshot_digest,
        forwarded_channel_ref: context.forwarded_channel_ref,
        forwarded_message_ref: context.forwarded_message_ref
      };
      if (context.target_message) envelope.target_message = context.target_message;
      if (attachmentDigest) envelope.attachment_manifest_digest = attachmentDigest;
      if (contract && contractDigest) {
        envelope.interaction_contract = contract;
        envelope.interaction_contract_digest = contractDigest;
      }
      this.#messageCache.set(envelope.ciphertext, {
        plaintext: decodeUtf8(encoded),
        authorRef: this.accountRef,
        messageRef: null,
        applicationRef: null
      });
      return envelope;
    } finally {
      clearBytes(encoded);
      clearBytes(aad);
      clearBytes(groupId);
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
        const envelope = control.e2ee as MlsEnvelope | null | undefined;
        if (
          typeof control.apply !== 'boolean' ||
          !isCanonicalSnowflake(control.id) ||
          !isCanonicalFederationDomain(control.origin_domain) ||
          control.origin_domain !== channel.origin_domain ||
          !isCanonicalSnowflake(control.channel_id) ||
          control.channel_id !== channel.id ||
          control.channel_domain !== channel.origin_domain ||
          typeof control.room_operation_id !== 'string' ||
          !/^keo_[A-Za-z0-9_-]{43}$/u.test(control.room_operation_id) ||
          control.room_operation_domain !== channel.origin_domain ||
          !envelope ||
          !['welcome', 'commit'].includes(envelope.operation) ||
          (envelope.operation === 'welcome' && control.apply !== true)
        ) {
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
    const applicationRef =
      message.application_id && message.application_domain
        ? `${message.application_id}@${message.application_domain}`
        : null;
    const webhookRef = projectedWebhookRef(message);
    const processedKey = processedMessageKey(channel, envelope, message);
    if (
      (envelope.operation === 'welcome' || envelope.operation === 'commit') &&
      this.#processed.has(processedKey)
    ) {
      return this.#processed.get(processedKey) ?? null;
    }
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
    const rich = Object.hasOwn(envelope, 'rich_payload_digest')
      ? await richMessageProjection(channel, message, envelope)
      : null;
    const expectedContext: MessageContext | RichMessageAuthenticatedContext = rich
      ? rich.context
      : {
          channel_ref: `${channel.id}@${channel.origin_domain}`,
          group_id: envelope.group_id,
          policy_generation: envelope.policy_generation,
          epoch: envelope.epoch,
          sender_device_id: envelope.sender_device_id,
          operation: envelope.operation as 'create' | 'edit',
          target_message: envelope.target_message ?? null,
          attachment_manifest_digest: envelope.attachment_manifest_digest ?? null
        };
    const expectedAad = rich
      ? richMessageAuthenticatedData(rich.context)
      : messageContextBytes(expectedContext as MessageContext);
    const cached = this.#messageCache.get(envelope.ciphertext);
    if (this.#processed.has(processedKey) && !cached) {
      throw new Error('Encrypted message plaintext is no longer available in the safe cache.');
    }
    if (rich && !cached) {
      const revision = BigInt(rich.context.message_revision);
      for (const candidate of this.#messageCache.values()) {
        if (candidate.messageRef !== messageRef) continue;
        try {
          const parsed = JSON.parse(candidate.plaintext) as Record<string, unknown>;
          if (parsed.version !== 2 || parsed.kind !== 'message') continue;
          const candidateContext = validateRichMessageAuthenticatedContext(parsed.context);
          if (BigInt(candidateContext.message_revision) >= revision) {
            throw new Error('Encrypted rich message revision is stale or replayed.');
          }
        } catch (caught) {
          if (caught instanceof Error && /stale or replayed/u.test(caught.message)) throw caught;
        }
      }
    }
    if (cached) {
      try {
        if (cached.authorRef !== authorRef) {
          throw new Error('Encrypted message author was modified.');
        }
        if ((cached.applicationRef ?? null) !== applicationRef) {
          throw new Error('Encrypted message app attribution was modified.');
        }
        if ((cached.webhookRef ?? null) !== webhookRef) {
          throw new Error('Encrypted message webhook attribution was modified.');
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
        const parsed = JSON.parse(cached.plaintext) as Record<string, unknown>;
        let application: DecryptedApplication;
        if (rich) {
          if (
            parsed.version !== 2 ||
            parsed.kind !== 'message' ||
            !parsed.context ||
            typeof parsed.context !== 'object' ||
            Array.isArray(parsed.context) ||
            !Object.hasOwn(parsed, 'data') ||
            Object.keys(parsed).sort().join(',') !== 'context,data,kind,version'
          ) {
            throw new Error('Encrypted rich message plaintext is invalid.');
          }
          const received = canonicalInteractionJson(parsed.context);
          const expected = canonicalInteractionJson(rich.context);
          try {
            if (!sameBytes(received, expected)) {
              throw new Error('Encrypted rich message plaintext context was modified.');
            }
          } finally {
            clearBytes(received);
            clearBytes(expected);
          }
          application = await authenticatedRichMessageApplication(
            parsed.data,
            rich.context,
            rich.contract,
            message
          );
        } else {
          const legacy = parsed as unknown as PlaintextApplication;
          if (
            legacy.version !== 1 ||
            legacy.kind !== 'message' ||
            typeof legacy.content !== 'string' ||
            !Array.isArray(legacy.attachments) ||
            JSON.stringify(legacy.context) !== JSON.stringify(expectedContext)
          ) {
            throw new Error('Encrypted message context was modified.');
          }
          application = {
            content: legacy.content,
            attachments: validatedEncryptedMessageAttachments(legacy.attachments)
          };
        }
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
        validateEncryptedMessageSenderCredential(
          senderCredential,
          message,
          envelope.sender_device_id
        );
      } finally {
        clearBytes(senderCredential);
      }
      const parsed = JSON.parse(decodeUtf8(processed.application)) as Record<string, unknown>;
      let application: DecryptedApplication;
      if (rich) {
        if (
          parsed.version !== 2 ||
          parsed.kind !== 'message' ||
          !parsed.context ||
          typeof parsed.context !== 'object' ||
          Array.isArray(parsed.context) ||
          !Object.hasOwn(parsed, 'data') ||
          Object.keys(parsed).sort().join(',') !== 'context,data,kind,version'
        ) {
          throw new Error('Encrypted rich message plaintext is invalid.');
        }
        const received = canonicalInteractionJson(parsed.context);
        const expected = canonicalInteractionJson(rich.context);
        try {
          if (!sameBytes(received, expected)) {
            throw new Error('Encrypted rich message plaintext context was modified.');
          }
        } finally {
          clearBytes(received);
          clearBytes(expected);
        }
        application = await authenticatedRichMessageApplication(
          parsed.data,
          rich.context,
          rich.contract,
          message
        );
      } else {
        const legacy = parsed as unknown as PlaintextApplication;
        if (
          legacy.version !== 1 ||
          legacy.kind !== 'message' ||
          typeof legacy.content !== 'string' ||
          !Array.isArray(legacy.attachments) ||
          JSON.stringify(legacy.context) !== JSON.stringify(expectedContext)
        ) {
          throw new Error('Encrypted message plaintext is invalid.');
        }
        const attachments = validatedEncryptedMessageAttachments(legacy.attachments);
        if (envelope.attachment_manifest_digest) {
          const digest = await encryptedManifestDigest(legacy.attachments);
          if (digest !== envelope.attachment_manifest_digest) {
            throw new Error('Encrypted attachment manifest was modified.');
          }
        } else if (attachments.length) {
          throw new Error('Encrypted attachment manifest is not authenticated.');
        }
        application = { content: legacy.content, attachments };
      }
      this.#messageCache.delete(envelope.ciphertext);
      this.#messageCache.set(envelope.ciphertext, {
        plaintext: JSON.stringify(parsed),
        authorRef,
        messageRef,
        applicationRef,
        webhookRef
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

function encryptedForwardSnapshotPresentation(value: unknown): MessageSnapshot {
  const snapshot = validateEncryptedForwardSnapshot(value);
  const attachments = (snapshot.attachments as unknown[]).map((item) => {
    const raw = routingRecord(item, 'Encrypted forward snapshot attachment');
    if (raw.protocol !== 'kaede-file-v1') {
      return raw as unknown as MessageSnapshot['attachments'][number];
    }
    const [manifest] = validateEncryptedRichMessageAttachments(
      [raw],
      Object.hasOwn(raw, 'duration_millis') || Object.hasOwn(raw, 'waveform')
    );
    return {
      id: manifest.attachment_id!,
      origin_domain: manifest.attachment_domain!,
      filename: manifest.filename,
      content_type: manifest.content_type,
      size: manifest.plaintext_size,
      width: null,
      height: null,
      blurhash: null,
      scan_status: 'encrypted' as const,
      encryption_mode: 'e2ee' as const,
      encryption_protocol: 'kaede-file-v1' as const,
      variants: {},
      duration_secs:
        manifest.duration_millis === undefined ? null : manifest.duration_millis / 1_000,
      waveform: manifest.waveform ?? null,
      encrypted_manifest: manifest
    };
  });
  return {
    content: snapshot.content as string | null,
    embeds: snapshot.embeds as MessageSnapshot['embeds'],
    components: snapshot.components as MessageSnapshot['components'],
    attachments,
    mention_user_refs: snapshot.mention_user_refs as MessageSnapshot['mention_user_refs'],
    sticker_items: snapshot.sticker_items as MessageSnapshot['sticker_items'],
    message_snapshots: (snapshot.message_snapshots as unknown[]).map((item) => ({
      message: encryptedForwardSnapshotPresentation(item)
    })),
    message_type: snapshot.message_type as number,
    flags: snapshot.flags as number,
    created_at: snapshot.created_at as string,
    edited_at: snapshot.edited_at as string | null
  };
}

export async function decryptConversationMessages(
  client: KaedeE2EEClient,
  channel: Channel,
  messages: readonly Message[]
): Promise<Message[]> {
  const applications = await client.decryptMessages(channel, messages);
  return messages.map((message, index) => {
    const application = applications[index];
    const sealed = message.e2ee
      ? {
          ...message,
          e2ee_verified: false,
          decrypted_content: null,
          decrypted_attachments: [],
          decrypted_allowed_mentions: undefined,
          decrypted_forward_snapshot: null
        }
      : message;
    return application
      ? {
          ...sealed,
          e2ee_verified: true,
          decrypted_content: application.content,
          decrypted_attachments: application.attachments,
          ...(application.allowedMentions
            ? { decrypted_allowed_mentions: application.allowedMentions }
            : {}),
          ...(application.embeds ? { embeds: application.embeds } : {}),
          ...(application.components ? { components: application.components } : {}),
          ...(Object.hasOwn(application, 'poll') ? { poll: application.poll ?? null } : {}),
          ...(application.stickerItems ? { sticker_items: application.stickerItems } : {}),
          ...(application.tts !== undefined ? { tts: application.tts } : {}),
          ...(application.flags !== undefined ? { flags: application.flags } : {}),
          ...(application.forwardSnapshot
            ? {
                decrypted_forward_snapshot: application.forwardSnapshot,
                message_snapshots: [
                  { message: encryptedForwardSnapshotPresentation(application.forwardSnapshot) }
                ]
              }
            : { decrypted_forward_snapshot: null })
        }
      : sealed;
  });
}
