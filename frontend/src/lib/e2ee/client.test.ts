import { readFileSync } from 'node:fs';
import { beforeAll, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api/errors';
import type { Channel, Message, UserSummary } from '$lib/chat/types';
import { buildTimeline } from '$lib/chat/timeline';
import {
  acceptAuthorizedMessage,
  applyContentSafetyUpdate,
  contentSafety
} from '$lib/chat/content-safety.svelte';
import { decryptConversationMessages, KaedeE2EEClient, MLS_PROTOCOL, MLS_SUITE } from './client';
import { base64url, concatBytes, decodeUtf8, fromBase64url, sha256, utf8 } from './encoding';
import { initSync, KaedeMlsClient } from './wasm/kaede_e2ee';
import {
  accountVaultChainRoot,
  accountVaultEnvelopeDigest,
  confirmedDeviceState,
  openAccountVaultState,
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
  let leaseBusyUntil = 0;
  io.api.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === '/safety/content-status') return { items: [] };
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
    if (path === '/e2ee/vault/lease') {
      if (Date.now() < leaseBusyUntil) {
        throw new ApiError('E2EE_ACCOUNT_VAULT_BUSY', 'Vault busy', 409, { retry_after_ms: 2_000 });
      }
      return { lease_token: 'test-lease', vault };
    }
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
  return {
    client,
    controls,
    other,
    key,
    getVault: () => vault,
    holdLease(milliseconds: number) {
      leaseBusyUntil = Date.now() + milliseconds;
    }
  };
}

const directMessage = { ...channel, type: 1, guild_id: null, guild_domain: null } satisfies Channel;

it('sends an encrypted DM when a previous vault lease expires', async () => {
  const { client, other, getVault, holdLease } = await senderClient(false);
  vi.useFakeTimers();
  try {
    holdLease(120_000);
    io.api.mockClear();
    const sending = client.encryptMessage(directMessage, 'Hello', { rich: {} });
    await vi.advanceTimersByTimeAsync(1_999);
    const earlyRequests = io.api.mock.calls.length;
    const [envelope] = await Promise.all([sending, vi.runAllTimersAsync()]);
    expect(earlyRequests).toBe(1);
    const acquisitions = io.api.mock.calls.filter(([path]) => path === '/e2ee/vault/lease');
    expect(acquisitions.length).toBeLessThanOrEqual(61);
    expect(getVault().revision).toBe('2');
    const received = other.process(
      fromBase64url(channel.encryption_group_id),
      fromBase64url(envelope.ciphertext)
    );
    try {
      expect(JSON.parse(decodeUtf8(received.application!)).data.content).toBe('Hello');
    } finally {
      received.free();
    }
  } finally {
    vi.useRealTimers();
    await client.close();
    other.free();
  }
});

it('surfaces sustained vault contention without changing the encrypted DM state', async () => {
  const { client, other, getVault, holdLease } = await senderClient(false);
  vi.useFakeTimers();
  try {
    holdLease(Infinity);
    const result = expect(client.encryptMessage(directMessage, 'Hello')).rejects.toMatchObject({
      code: 'E2EE_ACCOUNT_VAULT_BUSY'
    });
    await Promise.all([result, vi.runAllTimersAsync()]);
    expect(getVault().revision).toBe('1');
  } finally {
    vi.useRealTimers();
    await client.close();
    other.free();
  }
});

it('exports the peer media key after room rotation and rejects an unexpected MLS epoch', async () => {
  const { client, other, controls } = await senderClient(false);
  const groupId = fromBase64url(channel.encryption_group_id);
  try {
    await decryptConversationMessages(client, channel, controls);
    const rotated = { ...channel, encryption_epoch: '7' };
    const mediaContext = 'kaede-livekit-key-v1\0test';
    expect(new Uint8Array(await client.exportMediaKey(rotated, mediaContext))).toEqual(
      other.exportEpochSecret(groupId, 'kaede livekit v1', utf8(mediaContext), 32)
    );
    const epoch = vi.spyOn(KaedeMlsClient.prototype, 'groupEpoch').mockReturnValue(2n);
    try {
      await expect(client.exportMediaKey(rotated, mediaContext)).rejects.toThrow(
        'current encrypted call key'
      );
    } finally {
      epoch.mockRestore();
    }
  } finally {
    await client.close();
    other.free();
  }
});

it.each([false, true])(
  'verifies restored text then removes confirmed content from both vaults (recover activation: %s)',
  async (recoverActivation) => {
    const { client, controls, other, key, getVault } = await senderClient(recoverActivation);
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
        id: recoverActivation ? '31' : '30',
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
      io.api.mockClear();
      const [verified] = await decryptConversationMessages(client, channel, [message]);
      expect(verified).toMatchObject({ e2ee_verified: true, decrypted_content: 'Hello' });
      expect(io.api).not.toHaveBeenCalled();
      await client.close();
      const restored = await KaedeE2EEClient.initialize(user);
      try {
        io.api.mockClear();
        const reopened = await decryptConversationMessages(restored, channel, [
          ...controls,
          message
        ]);
        expect(reopened.at(-1)).toMatchObject({ e2ee_verified: true, decrypted_content: 'Hello' });
        const [altered] = await decryptConversationMessages(restored, channel, [
          { ...message, author_id: '8' }
        ]);
        expect(altered.e2ee_verified).toBe(false);
        expect(io.api).not.toHaveBeenCalled();
        const messageRef = `${message.id}@${message.origin_domain}`;
        for (const state of ['pending_review', 'unavailable'] as const) {
          applyContentSafetyUpdate({
            state,
            reason: 'Temporary',
            message_refs: [messageRef],
            attachment_refs: []
          });
          await restored.purgeSafetyCache();
          await expect(restored.decryptMessages(channel, [message])).resolves.toEqual([null]);
          const retained = await openAccountVaultState('7@chat.example', key, getVault().envelope);
          expect(
            Object.values(retained.messageCache ?? {}).some(
              (entry) => entry.messageRef === messageRef
            )
          ).toBe(true);
          if (state === 'pending_review')
            applyContentSafetyUpdate({
              state: 'released',
              reason: 'Dismissed',
              message_refs: [messageRef],
              attachment_refs: []
            });
          else acceptAuthorizedMessage(message, contentSafety.revision);
          await expect(restored.decryptMessage(channel, message)).resolves.toMatchObject({
            content: 'Hello'
          });
        }
        applyContentSafetyUpdate({
          state: 'removed',
          reason: 'Confirmed',
          message_refs: [messageRef],
          attachment_refs: []
        });
        await restored.purgeSafetyCache();
        const local = await io.state();
        expect(
          Object.values(local.messageCache ?? {}).some(
            (entry) => (entry as { messageRef: string }).messageRef === messageRef
          )
        ).toBe(false);
        const remote = await openAccountVaultState('7@chat.example', key, getVault().envelope);
        expect(
          Object.values(remote.messageCache ?? {}).some((entry) => entry.messageRef === messageRef)
        ).toBe(false);
        await expect(restored.decryptMessage(channel, message)).resolves.toBeNull();
      } finally {
        await restored.close();
      }
    } finally {
      await client.close();
      other.free();
    }
  }
);

it('keeps failed encrypted edits sealed for presentation', async () => {
  const client = { decryptMessages: vi.fn(async () => [null]) } as unknown as KaedeE2EEClient;
  const incoming = {
    e2ee: { ciphertext: 'invalid' },
    content: 'forged',
    e2ee_verified: true,
    decrypted_content: 'forged'
  } as unknown as Message;
  const [sealed] = await decryptConversationMessages(client, channel, [incoming]);
  expect(sealed.e2ee).toEqual(incoming.e2ee);
  expect(sealed.e2ee_verified).toBe(false);
  expect(sealed.decrypted_content).toBeNull();
});

it('retries a failed encryption engine download in the same session', async () => {
  vi.resetModules();
  const { initializeE2EE } = await import('./client');
  io.key.mockResolvedValue(null);
  const download = vi
    .spyOn(globalThis, 'fetch')
    .mockRejectedValueOnce(new TypeError('Failed to fetch'))
    .mockResolvedValueOnce(
      new Response(new Uint8Array(readFileSync('src/lib/e2ee/wasm/kaede_e2ee_bg.wasm')), {
        headers: { 'Content-Type': 'application/wasm' }
      })
    );
  try {
    await expect(initializeE2EE(user)).rejects.toThrow('Failed to fetch');
    // Reaching the vault key check proves the retry initialized the real engine.
    await expect(initializeE2EE(user)).rejects.toThrow('unlock end-to-end encryption');
    await expect(initializeE2EE(user)).rejects.toThrow('unlock end-to-end encryption');
    expect(download).toHaveBeenCalledTimes(2);
  } finally {
    download.mockRestore();
  }
});
