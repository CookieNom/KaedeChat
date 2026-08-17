import { api } from '$lib/api/client';
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
import { encryptedManifestDigest, type EncryptedFileManifest } from './media';
import { loadDeviceState, saveDeviceState, type DeviceState } from './store';
import type { KaedeMlsClient } from './wasm/kaede_e2ee';

export const MLS_PROTOCOL = 'mls10';
export const MLS_SUITE = 'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519';
const KEY_PACKAGE_BATCH = 20;

interface DeviceRegistration {
  id: string;
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

export interface ClaimedKeyPackage {
  user_id: string;
  user_domain: string;
  device_id: string;
  identity_key: string;
  credential: string;
  key_package: string;
}

export interface RoomProposal {
  proposal_id?: string;
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

function accountRef(user: UserSummary): string {
  return `${user.id}@${user.origin_domain}`;
}

function expiryString(): string {
  const value = new Date(Date.now() + 28 * 24 * 60 * 60 * 1000).toISOString();
  return value.replace(/Z$/u, '+00:00');
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
  return concatBytes(...fields.flatMap((field, index) => (index ? [separator, field] : [field])));
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

function validateSenderCredential(credential: Uint8Array, message: Message): void {
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
  readonly #mls: KaedeMlsClient;
  readonly #messageCache: Map<string, string>;
  readonly #processed = new Map<string, DecryptedApplication | null>();

  private constructor(state: DeviceState, mls: KaedeMlsClient) {
    this.accountRef = state.accountRef;
    this.deviceId = state.deviceId;
    this.#credential = state.credential;
    this.#mls = mls;
    this.#messageCache = new Map(Object.entries(state.messageCache ?? {}));
  }

  static async initialize(user: UserSummary): Promise<KaedeE2EEClient> {
    const module = await wasmModule();
    const ref = accountRef(user);
    const stored = await loadDeviceState(ref);
    if (stored) {
      const listed = await api<DeviceList>('/e2ee/devices');
      const device = listed.devices.find((candidate) => candidate.id === stored.deviceId);
      if (!device || device.revoked_at) {
        throw new Error(
          'This encryption device was revoked. Clear its local keys before registering again.'
        );
      }
      const client = new KaedeE2EEClient(
        stored,
        module.KaedeMlsClient.restoreState(fromBase64url(stored.mlsState, 32 * 1024 * 1024))
      );
      await client.replenishKeyPackages(device.available_key_packages ?? 0);
      return client;
    }

    const credentialBytes = utf8(
      JSON.stringify({ version: 1, account: ref, nonce: base64url(randomBytes(32)) })
    );
    const mls = new module.KaedeMlsClient(credentialBytes);
    const identityKey = ownedBytes(mls.publicIdentityKey());
    const challenge = await api<Challenge>('/e2ee/devices/challenge', {
      method: 'POST',
      body: JSON.stringify({
        identity_key: base64url(identityKey),
        credential_digest: base64url(await sha256(credentialBytes))
      })
    });
    const signature = ownedBytes(
      mls.signServerChallenge(fromBase64url(challenge.signing_input, 2048))
    );
    const registered = await api<DeviceRegistration>('/e2ee/devices', {
      method: 'POST',
      body: JSON.stringify({
        challenge_id: challenge.challenge_id,
        identity_key: base64url(identityKey),
        credential: base64url(credentialBytes),
        signature: base64url(signature),
        device_name: navigator.userAgent.slice(0, 100) || 'Kaede device',
        platform: isNativeDesktop() ? 'desktop' : 'web',
        capabilities: ['e2ee-mls/1', 'e2ee-media/1']
      })
    });
    clearBytes(signature);
    const state: DeviceState = {
      schema: 1,
      accountRef: ref,
      deviceId: registered.id,
      credential: base64url(credentialBytes),
      mlsState: '',
      messageCache: {}
    };
    clearBytes(credentialBytes);
    const client = new KaedeE2EEClient(state, mls);
    await client.#persist();
    await client.replenishKeyPackages(0);
    return client;
  }

  async #persist(): Promise<void> {
    const state = ownedBytes(this.#mls.exportState());
    try {
      await saveDeviceState({
        schema: 1,
        accountRef: this.accountRef,
        deviceId: this.deviceId,
        credential: this.#credential,
        mlsState: base64url(state),
        messageCache: Object.fromEntries([...this.#messageCache.entries()].slice(-2_000))
      });
    } finally {
      clearBytes(state);
    }
  }

  async replenishKeyPackages(available = 0): Promise<void> {
    const count = Math.max(0, KEY_PACKAGE_BATCH - available);
    if (!count) return;
    const packages = [];
    for (let index = 0; index < count; index += 1) {
      packages.push(ownedBytes(this.#mls.generateKeyPackage()));
    }
    const digests = await Promise.all(packages.map((value) => sha256(value)));
    const expiresAt = expiryString();
    const signingInput = packageSigningInput(this.deviceId, expiresAt, digests);
    const signature = ownedBytes(this.#mls.signServerChallenge(signingInput));
    try {
      await api(`/e2ee/devices/${encodeURIComponent(this.deviceId)}/key-packages`, {
        method: 'POST',
        body: JSON.stringify({
          cipher_suite: MLS_SUITE,
          expires_at: expiresAt,
          packages: packages.map((value) => base64url(value)),
          signature: base64url(signature)
        })
      });
      await this.#persist();
    } finally {
      clearBytes(signature);
      packages.forEach(clearBytes);
      digests.forEach(clearBytes);
    }
  }

  async createRoomProposal(channelRef: string): Promise<RoomProposal> {
    return api<RoomProposal>(`/e2ee/channels/${encodeURIComponent(channelRef)}/propose`, {
      method: 'POST',
      body: JSON.stringify({ sender_device_id: this.deviceId })
    });
  }

  async activateRoom(channelRef: string, proposal: RoomProposal): Promise<Channel> {
    const groupId = fromBase64url(proposal.policy.group_id, 128);
    this.#mls.createGroup(groupId);
    const packages = proposal.key_packages
      .filter((item) => item.device_id !== this.deviceId)
      .map((item) => fromBase64url(item.key_package, 32 * 1024));
    if (!packages.length) throw new Error('An encrypted room requires another active device.');
    const pending = this.#mls.addMembers(groupId, packages);
    try {
      const commit = ownedBytes(pending.commit);
      const welcome = ownedBytes(pending.welcome);
      try {
        const channel = await api<Channel>(
          `/e2ee/channels/${encodeURIComponent(channelRef)}/activate`,
          {
            method: 'POST',
            body: JSON.stringify({
              sender_device_id: this.deviceId,
              policy_generation: proposal.policy.generation,
              epoch: '1',
              commit: base64url(commit),
              welcome: base64url(welcome)
            })
          }
        );
        this.#mls.mergePendingCommit(groupId);
        await this.#persist();
        return channel;
      } finally {
        clearBytes(commit);
        clearBytes(welcome);
      }
    } finally {
      pending.free();
      packages.forEach(clearBytes);
    }
  }

  async rekeyRoom(channelRef: string): Promise<Channel> {
    const proposal = await api<RoomProposal>(
      `/e2ee/channels/${encodeURIComponent(channelRef)}/rekey/propose`,
      {
        method: 'POST',
        body: JSON.stringify({ sender_device_id: this.deviceId })
      }
    );
    if (!proposal.proposal_id) throw new Error('Encrypted rekey proposal is invalid.');
    const groupId = fromBase64url(proposal.policy.group_id, 128);
    this.#mls.createGroup(groupId);
    const packages = proposal.key_packages
      .filter((item) => item.device_id !== this.deviceId)
      .map((item) => fromBase64url(item.key_package, 32 * 1024));
    if (!packages.length) throw new Error('An encrypted room requires another active device.');
    const pending = this.#mls.addMembers(groupId, packages);
    try {
      const commit = ownedBytes(pending.commit);
      const welcome = ownedBytes(pending.welcome);
      try {
        const channel = await api<Channel>(
          `/e2ee/channels/${encodeURIComponent(channelRef)}/rekey/activate`,
          {
            method: 'POST',
            body: JSON.stringify({
              proposal_id: proposal.proposal_id,
              sender_device_id: this.deviceId,
              policy_generation: proposal.policy.generation,
              epoch: '1',
              commit: base64url(commit),
              welcome: base64url(welcome)
            })
          }
        );
        this.#mls.mergePendingCommit(groupId);
        await this.#persist();
        return channel;
      } finally {
        clearBytes(commit);
        clearBytes(welcome);
      }
    } finally {
      pending.free();
      packages.forEach(clearBytes);
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
    const ciphertext = ownedBytes(this.#mls.encrypt(groupId, encoded, aad));
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
    this.#messageCache.set(envelope.ciphertext, JSON.stringify(plaintext));
    clearBytes(encoded);
    clearBytes(ciphertext);
    await this.#persist();
    return envelope;
  }

  async exportMediaKey(channel: Channel, mediaContext: string): Promise<ArrayBuffer> {
    requireActiveChannel(channel);
    if (!mediaContext || mediaContext.length > 256)
      throw new Error('Encrypted media context is invalid.');
    const groupId = fromBase64url(channel.encryption_group_id, 128);
    const secret = ownedBytes(
      this.#mls.exportEpochSecret(groupId, 'kaede livekit v1', utf8(mediaContext), 32)
    );
    try {
      return ownedBytes(secret).buffer;
    } finally {
      clearBytes(secret);
    }
  }

  async syncRoomState(channel: Channel): Promise<void> {
    requireEncryptedChannel(channel);
    const messages = await api<Message[]>(
      `/channels/${encodeURIComponent(`${channel.id}@${channel.origin_domain}`)}/messages?limit=100`
    );
    for (const message of [...messages].reverse()) {
      if (!message.e2ee) continue;
      try {
        await this.decryptMessage(channel, message);
      } catch {
        // Earlier generations may predate this device. The newest Welcome is
        // still processed independently and is the only state required for a
        // newly enrolled voice participant.
      }
    }
  }

  async safetyNumber(channel: Channel): Promise<string> {
    requireEncryptedChannel(channel);
    const roster = ownedBytes(
      this.#mls.memberRoster(fromBase64url(channel.encryption_group_id, 128))
    );
    try {
      const digest = await sha256(roster);
      const digits = [...digest]
        .slice(0, 15)
        .map((value) => value.toString().padStart(3, '0'))
        .join('');
      return digits.match(/.{1,5}/gu)?.join(' ') ?? digits;
    } finally {
      clearBytes(roster);
    }
  }

  async decryptMessage(channel: Channel, message: Message): Promise<DecryptedApplication | null> {
    requireEncryptedChannel(channel);
    const envelope = message.e2ee as MlsEnvelope | null | undefined;
    if (!envelope || envelope.version !== 2) {
      return null;
    }
    if (
      envelope.protocol !== MLS_PROTOCOL ||
      envelope.suite !== MLS_SUITE ||
      message.encryption_policy_generation !== envelope.policy_generation ||
      message.encryption_epoch !== envelope.epoch
    ) {
      throw new Error('The encrypted message context does not match this conversation.');
    }
    const groupId = fromBase64url(envelope.group_id, 128);
    const ciphertext = fromBase64url(envelope.ciphertext, 60 * 1024);
    if (this.#processed.has(envelope.ciphertext))
      return this.#processed.get(envelope.ciphertext) ?? null;
    if (envelope.operation === 'welcome') {
      if (!this.#mls.hasGroup(groupId)) this.#mls.joinGroup(ciphertext);
      this.#processed.set(envelope.ciphertext, null);
      await this.#persist();
      return null;
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
    if (!['create', 'edit'].includes(expectedContext.operation))
      throw new Error('Encrypted message operation is invalid.');
    const expectedAad = messageContextBytes(expectedContext);
    const cached = this.#messageCache.get(envelope.ciphertext);
    if (cached) {
      const parsed = JSON.parse(cached) as PlaintextApplication;
      if (JSON.stringify(parsed.context) !== JSON.stringify(expectedContext))
        throw new Error('Encrypted message context was modified.');
      const application = { content: parsed.content, attachments: parsed.attachments };
      this.#processed.set(envelope.ciphertext, application);
      clearBytes(ciphertext);
      return application;
    }
    const processed = this.#mls.process(groupId, ciphertext);
    try {
      if (processed.kind !== 'application' || !processed.application) {
        await this.#persist();
        return null;
      }
      if (!processed.aad || !sameBytes(ownedBytes(processed.aad), expectedAad))
        throw new Error('Encrypted message authenticated context was modified.');
      if (!processed.credential) throw new Error('Encrypted message sender identity is missing.');
      validateSenderCredential(ownedBytes(processed.credential), message);
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
      this.#messageCache.set(envelope.ciphertext, JSON.stringify(parsed));
      this.#processed.set(envelope.ciphertext, application);
      await this.#persist();
      return application;
    } finally {
      processed.free();
      clearBytes(ciphertext);
    }
  }
}

let activeClient: Promise<KaedeE2EEClient> | null = null;
let activeAccount = '';

export function initializeE2EE(user: UserSummary): Promise<KaedeE2EEClient> {
  const ref = accountRef(user);
  if (!activeClient || activeAccount !== ref) {
    activeAccount = ref;
    activeClient = KaedeE2EEClient.initialize(user).catch((error) => {
      activeClient = null;
      activeAccount = '';
      throw error;
    });
  }
  return activeClient;
}

export function resetE2EEClient(): void {
  activeClient = null;
  activeAccount = '';
}

export async function decryptConversationMessages(
  client: KaedeE2EEClient,
  channel: Channel,
  messages: readonly Message[]
): Promise<Message[]> {
  const result: Message[] = [];
  for (const message of messages) {
    if (!message.e2ee) {
      result.push(message);
      continue;
    }
    try {
      const application = await client.decryptMessage(channel, message);
      result.push(
        application
          ? {
              ...message,
              decrypted_content: application.content,
              decrypted_attachments: application.attachments
            }
          : message
      );
    } catch {
      // An older message can predate this device's Welcome, and duplicate
      // federation deliveries can be replayed. Keep it visibly locked without
      // allowing one undecryptable item to hide later valid messages.
      result.push(message);
    }
  }
  return result;
}
