import { readFileSync } from 'node:fs';
import { beforeAll, expect, it, vi } from 'vitest';
import type { Channel, Message, UserSummary } from '$lib/chat/types';
import { buildTimeline } from '$lib/chat/timeline';
import { decryptConversationMessages, KaedeE2EEClient, MLS_PROTOCOL, MLS_SUITE } from './client';
import { base64url, concatBytes, decodeUtf8, fromBase64url, sha256, utf8 } from './encoding';
import { initSync, KaedeMlsClient } from './wasm/kaede_e2ee';
import {
  accountVaultChainRoot,
  accountVaultEnvelopeDigest,
  confirmedDeviceState,
  sealAccountVaultState,
  ZERO_VAULT_CHAIN,
  type DeviceState,
  type VaultCheckpoint,
  type AccountVaultEnvelope
} from './store';

const io = vi.hoisted(() => ({ api: vi.fn(), state: vi.fn(), checkpoint: vi.fn(), key: vi.fn() }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: io.api
}));
vi.mock('./store', async (original) => ({
  ...(await original<typeof import('./store')>()),
  loadAccountVaultKey: io.key,
  loadDeviceState: io.state,
  loadVaultCheckpoint: io.checkpoint,
  loadPendingAccountVaultWrite: async () => null,
  saveDeviceState: async (state: DeviceState) => {
    io.state.mockResolvedValue(state);
  },
  saveVaultCheckpoint: async (_ref: string, checkpoint: VaultCheckpoint) => {
    io.checkpoint.mockResolvedValue(checkpoint);
  },
  savePendingAccountVaultWrite: async () => {}
}));

beforeAll(() => {
  initSync({ module: readFileSync('src/lib/e2ee/wasm/kaede_e2ee_bg.wasm') });
});

const user = {
  id: '7',
  origin_domain: 'chat.example',
  username: 'sender',
  display_name: null,
  avatar_hash: null,
  handle: 'sender@chat.example'
} satisfies UserSummary;

const channel = {
  id: '20',
  origin_domain: 'chat.example',
  guild_id: '10',
  guild_domain: 'chat.example',
  type: 0,
  name: 'encrypted',
  topic: null,
  position: 0,
  parent_id: null,
  parent_domain: null,
  rate_limit_per_user: 0,
  last_message_id: null,
  last_message_domain: null,
  encryption_mode: 'e2ee',
  encryption_state: 'active',
  encryption_protocol: MLS_PROTOCOL,
  encryption_suite: MLS_SUITE,
  encryption_policy_generation: '1',
  encryption_epoch: '1',
  encryption_group_id: base64url(new Uint8Array(32).fill(7))
} satisfies Channel;

async function senderClient(recoverActivation: boolean) {
  const accountRef = '7@chat.example';
  const credential = utf8(
    JSON.stringify({ version: 1, account: accountRef, nonce: base64url(new Uint8Array(32)) })
  );
  const mls = new KaedeMlsClient(credential);
  const other = new KaedeMlsClient(utf8('other'));
  const groupId = new Uint8Array(32).fill(7);
  mls.createGroup(groupId);
  const pending = mls.addMembers(groupId, [other.generateKeyPackage()]);
  mls.mergePendingCommit(groupId);
  other.joinGroup(pending.welcome);
  const controlCiphertexts = [base64url(pending.welcome), base64url(pending.commit)];
  pending.free();
  const identity = mls.publicIdentityKey();
  const deviceId = `ked_${base64url(await sha256(concatBytes(utf8(`${accountRef}\0`), identity)))}`;
  const controls = ['welcome', 'commit'].map((operation, index) => ({
    id: String(21 + index),
    origin_domain: channel.origin_domain,
    channel_id: channel.id,
    channel_domain: channel.origin_domain,
    author_id: '7',
    author_domain: 'chat.example',
    author: user,
    client_nonce: null,
    referenced_message_id: null,
    referenced_message_domain: null,
    mention_user_refs: [],
    edited_at: null,
    deleted_at: null,
    encryption_policy_generation: '1',
    encryption_epoch: '1',
    message_type: 7,
    flags: 4,
    created_at: new Date().toISOString(),
    content: null,
    apply: index === 0,
    room_operation_id: `keo_${'a'.repeat(43)}`,
    room_operation_domain: channel.origin_domain,
    e2ee: {
      version: 2,
      protocol: MLS_PROTOCOL,
      suite: MLS_SUITE,
      operation,
      group_id: channel.encryption_group_id,
      policy_generation: '1',
      epoch: '1',
      sender_device_id: deviceId,
      ciphertext: controlCiphertexts[index]
    }
  }));
  const state: DeviceState = {
    schema: 2,
    accountRef,
    deviceId,
    credential: base64url(credential),
    mlsState: base64url(mls.exportState()),
    vaultSequence: '1',
    vaultParentChain: ZERO_VAULT_CHAIN
  };
  const operationId = `keo_${'a'.repeat(43)}`;
  if (recoverActivation) {
    state.pendingRoomOperations = {
      [operationId]: {
        version: 1,
        operationId,
        channelRef: '20@chat.example',
        kind: 'activate',
        phase: 'activating',
        policyGeneration: '1',
        groupId: channel.encryption_group_id,
        welcome: controlCiphertexts[0],
        commit: controlCiphertexts[1]
      }
    };
  }
  mls.free();
  const key = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, [
    'encrypt',
    'decrypt'
  ]);
  const envelope = await sealAccountVaultState(state, key);
  let vault = {
    revision: '1',
    envelope,
    digest: await accountVaultEnvelopeDigest(envelope),
    updated_at: new Date().toISOString()
  };
  const chainRoot = await accountVaultChainRoot(ZERO_VAULT_CHAIN, '1', vault.digest);
  io.key.mockResolvedValue(key);
  io.state.mockResolvedValue(confirmedDeviceState(state, '1', vault.digest, chainRoot));
  io.checkpoint.mockResolvedValue({ revision: '1', digest: vault.digest, chainRoot });
  let activationAttempts = 0;
  io.api.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === '/e2ee/devices')
      return {
        devices: [
          {
            id: deviceId,
            user_id: '7',
            user_domain: 'chat.example',
            identity_key: base64url(identity),
            credential: state.credential,
            revoked_at: null
          }
        ]
      };
    if (path === '/e2ee/vault/lease') return { lease_token: 'test-lease', vault };
    if (path === '/e2ee/vault/lease/release') return;
    if (path.endsWith(`/operations/${operationId}`)) {
      return { operation_id: operationId, kind: 'activate', status: 'prepared' };
    }
    if (path.endsWith('/activate')) {
      if (++activationAttempts <= 2) throw new Error('Activation failed');
      return {
        ...channel,
        operation_id: operationId,
        operation_status: 'committed',
        controls: controls.map((control) => ({
          id: control.id,
          origin_domain: control.origin_domain,
          operation: control.e2ee?.operation,
          apply: control.apply
        }))
      };
    }
    if (path.includes('/control-log')) {
      const after = new URL(path, 'https://chat.example').searchParams.get('after');
      return { controls: after ? [] : controls, next_after: null };
    }
    if (path === '/e2ee/vault' && options?.method === 'PUT') {
      const body = JSON.parse(options.body as string) as {
        expected_revision: string;
        envelope: AccountVaultEnvelope;
      };
      expect(body.expected_revision).toBe(vault.revision);
      vault = {
        ...vault,
        revision: body.envelope.sequence,
        envelope: body.envelope,
        digest: await accountVaultEnvelopeDigest(body.envelope)
      };
      return { vault };
    }
    throw new Error(`Unexpected API call: ${path}`);
  });
  if (recoverActivation) {
    await expect(KaedeE2EEClient.initialize(user)).rejects.toThrow('Activation failed');
    await expect(KaedeE2EEClient.initialize(user)).rejects.toThrow('Activation failed');
  }
  const client = await KaedeE2EEClient.initialize(user);
  expect(activationAttempts).toBe(recoverActivation ? 3 : 0);
  return { client, controls, other };
}

it.each([false, true])(
  'verifies sent text after vault restore (recover activation: %s)',
  async (recoverActivation) => {
    const { client, controls, other } = await senderClient(recoverActivation);
    try {
      const setup = await decryptConversationMessages(client, channel, controls);
      expect(buildTimeline(setup)).toEqual([]);
      const e2ee = await client.encryptMessage(channel, 'Hello', { rich: {} });
      const received = other.process(
        fromBase64url(channel.encryption_group_id),
        fromBase64url(e2ee.ciphertext)
      );
      try {
        expect(received.kind).toBe('application');
        expect(JSON.parse(decodeUtf8(received.application!)).data.content).toBe('Hello');
      } finally {
        received.free();
      }
      const message = {
        id: '30',
        origin_domain: 'chat.example',
        channel_id: channel.id,
        channel_domain: channel.origin_domain,
        author_id: '7',
        author_domain: 'chat.example',
        author: user,
        content: null,
        client_nonce: null,
        referenced_message_id: null,
        referenced_message_domain: null,
        deleted_at: null,
        e2ee,
        encryption_policy_generation: '1',
        encryption_epoch: '1',
        message_type: 0,
        flags: 0,
        tts: false,
        view_version: 0,
        view_persistent: false,
        attachments: [],
        mention_user_refs: [],
        edited_at: null,
        created_at: new Date().toISOString()
      } satisfies Message;
      await expect(client.decryptMessage(channel, message)).resolves.toMatchObject({
        content: 'Hello'
      });
      const [verified] = await decryptConversationMessages(client, channel, [message]);
      expect(verified).toMatchObject({ e2ee_verified: true, decrypted_content: 'Hello' });
      await client.close();
      const restored = await KaedeE2EEClient.initialize(user);
      try {
        await expect(restored.decryptMessage(channel, message)).resolves.toMatchObject({
          content: 'Hello'
        });
      } finally {
        await restored.close();
      }
    } finally {
      await client.close();
      other.free();
    }
  }
);
