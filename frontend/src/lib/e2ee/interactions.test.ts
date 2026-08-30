import { describe, expect, it } from 'vitest';
import type { Channel } from '$lib/chat/types';
import vectors from '../../../static/protocol/interaction-aad-v1.json';
import responseVectors from '../../../static/protocol/interaction-response-aad-v1.json';
import routingVectors from '../../../static/protocol/interaction-routing-contract-v1.json';
import richMessageVectors from '../../../static/protocol/message-rich-aad-v1.json';
import {
  canonicalInteractionJson,
  encryptedForwardSnapshotDigest,
  encryptedForwardSnapshotProjectionDigest,
  type EncryptedInteractionInput,
  interactionAuthenticatedContext,
  interactionResponseAuthenticatedContext,
  interactionRoutingContract,
  interactionRoutingContractDigest,
  richMessageAuthenticatedData,
  richMessageCustomEmojiRefs,
  richMessageForwardProjectionDigest,
  richMessageMentionIntent,
  richMessagePayloadDigest,
  richMessageStickerRefs,
  MLS_PROTOCOL,
  MLS_SUITE,
  validateInteractionRoutingContract,
  validateEncryptedMessageSenderCredential,
  validateEncryptedRichMessageAttachments,
  validateEncryptedAllowedMentions,
  validateEncryptedForwardSnapshot,
  validateRichMessageAuthenticatedContext,
  webhookIdentityDeviceId
} from './client';
import { base64url, concatBytes, sha256, utf8 } from './encoding';

const channel = {
  id: '20',
  origin_domain: 'guild.example',
  guild_id: '10',
  guild_domain: 'guild.example',
  type: 0,
  name: 'private-apps',
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
  encryption_policy_generation: '3',
  encryption_group_id: 'Z2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2c',
  encryption_epoch: '7'
} satisfies Channel;

interface TestForwardSnapshot {
  content: string | null;
  embeds: Record<string, unknown>[];
  components: Record<string, unknown>[];
  attachments: Record<string, unknown>[];
  mention_user_refs: Array<{ id: string; origin_domain: string }>;
  sticker_items: Record<string, unknown>[];
  message_snapshots: TestForwardSnapshot[];
  message_type: number;
  flags: number;
  created_at: string;
  edited_at: string | null;
}

describe('encrypted interaction wire contract', () => {
  it('uses recursively sorted compact JSON for AAD and plaintext', () => {
    const bytes = canonicalInteractionJson({
      z: [{ b: 2, a: 1 }],
      a: { d: 4, c: 3 }
    });

    expect(new TextDecoder().decode(bytes)).toBe('{"a":{"c":3,"d":4},"z":[{"a":1,"b":2}]}');
  });

  it('binds the authority-selected install/context and numerically sorted files', () => {
    const context = interactionAuthenticatedContext(
      channel,
      '40@users.example',
      `ked_${'a'.repeat(43)}`,
      {
        applicationRef: '30@apps.example',
        integrationType: 'user_install',
        interactionContext: 'bot_dm',
        interactionType: 'command',
        commandId: '91',
        commandName: 'secure',
        commandType: 'chat_input',
        attachmentIds: ['100', '9'],
        options: { file: '100' }
      }
    );

    expect(context).toMatchObject({
      application_ref: '30@apps.example',
      attachment_ids: ['9', '100'],
      channel_ref: '20@guild.example',
      context: 'bot_dm',
      integration_type: 'user_install',
      interaction_type: 'command',
      invoker_ref: '40@users.example',
      command_id: '91',
      command_name: 'secure',
      command_type: 'chat_input',
      epoch: '7',
      policy_generation: '3'
    });
  });

  it('binds discovered bot-DM capabilities in encrypted commands', () => {
    const context = interactionAuthenticatedContext(
      channel,
      '40@users.example',
      `ked_${'a'.repeat(43)}`,
      {
        applicationRef: '30@apps.example',
        integrationType: 'dm_capability',
        interactionContext: 'bot_dm',
        interactionType: 'command',
        commandId: '91',
        commandName: 'secure',
        commandType: 'chat_input'
      }
    );

    expect(context.integration_type).toBe('dm_capability');
    expect(context.context).toBe('bot_dm');
  });

  it('matches the shared browser and Python AAD vectors for every interaction type', () => {
    const vectorChannel = {
      ...channel,
      ...vectors.channel
    } satisfies Channel;
    for (const vector of vectors.vectors) {
      const context = interactionAuthenticatedContext(
        vectorChannel,
        vectors.invoker_ref,
        vectors.sender_device_id,
        vector.input as unknown as EncryptedInteractionInput
      );
      expect(context, vector.name).toEqual(vector.context);
      expect(
        base64url(canonicalInteractionJson({ context, purpose: 'kaede.interaction.v1' })),
        vector.name
      ).toBe(vector.aad_base64url);
    }
  });

  it('matches the shared response AAD vectors with full-ref lexicographic file ordering', async () => {
    const vectorChannel = {
      ...channel,
      ...responseVectors.channel
    } satisfies Channel;
    for (const vector of responseVectors.vectors) {
      const input = vector.input;
      const context = interactionResponseAuthenticatedContext(vectorChannel, {
        authorityDomain: input.authorityDomain,
        interactionRef: input.interactionRef,
        responseRef: input.responseRef,
        invokerRef: input.invokerRef,
        channelRef: input.channelRef,
        applicationRef: input.applicationRef,
        sequence: input.sequence,
        revision: input.revision,
        callbackType: input.callbackType,
        operation: input.operation as 'CREATE' | 'UPDATE',
        attachmentRefs: input.attachmentRefs,
        interactionContractDigest: input.interactionContractDigest,
        senderDeviceId: input.senderDeviceId
      });
      expect(context, vector.name).toEqual(vector.context);
      expect(
        base64url(canonicalInteractionJson({ context, purpose: 'kaede.interaction.response.v1' })),
        vector.name
      ).toBe(vector.aad_base64url);
      const contract = await interactionRoutingContract(
        vector.data as Record<string, unknown>,
        input.callbackType
      );
      expect(contract, `${vector.name} routing contract`).toEqual(vector.interaction_contract);
      expect(
        contract ? await interactionRoutingContractDigest(contract) : null,
        `${vector.name} routing digest`
      ).toBe(input.interactionContractDigest);
    }
    expect(responseVectors.vectors[0].input.attachmentRefs).toEqual([
      '100@guild.example',
      '9@guild.example'
    ]);
  });

  it('matches shared privacy-preserving routing contracts and rejects public mutations', async () => {
    for (const vector of routingVectors.vectors) {
      const contract = await interactionRoutingContract(
        vector.input as Record<string, unknown>,
        vector.callback_type
      );
      expect(contract, vector.name).toEqual(vector.contract);
      expect(await interactionRoutingContractDigest(contract!), vector.name).toBe(vector.digest);
      expect(
        validateInteractionRoutingContract(vector.contract, vector.callback_type),
        vector.name
      ).toEqual(vector.contract);
    }
    expect(routingVectors.vectors[0].contract).toEqual(routingVectors.vectors[1].contract);
    for (const vector of routingVectors.invalid_contracts) {
      expect(
        () => validateInteractionRoutingContract(vector.contract, vector.callback_type),
        vector.name
      ).toThrow();
    }
    const changed = structuredClone(routingVectors.vectors[0].input) as Record<string, unknown>;
    const controls = (
      (changed.components as Record<string, unknown>[])[0].components as Record<string, unknown>[]
    )[0];
    (controls.options as Record<string, unknown>[])[0].value = 'different';
    const changedContract = await interactionRoutingContract(changed, 4);
    expect(await interactionRoutingContractDigest(changedContract!)).not.toBe(
      routingVectors.vectors[0].digest
    );
  });

  it('matches shared human and bot rich-message AAD vectors', async () => {
    for (const vector of richMessageVectors.vectors) {
      const context = validateRichMessageAuthenticatedContext(vector.context);
      expect(context, `${vector.name} context`).toEqual(vector.context);
      expect(
        await richMessagePayloadDigest(vector.rich_data as Record<string, unknown>),
        `${vector.name} rich digest`
      ).toBe(context.rich_payload_digest);
      expect(
        await richMessageForwardProjectionDigest(
          vector.rich_data as Record<string, unknown>,
          context.message_mention_refs
        ),
        `${vector.name} forward projection`
      ).toBe(context.forward_projection_digest);
      expect(richMessageCustomEmojiRefs(vector.rich_data), `${vector.name} custom emoji`).toEqual(
        context.message_custom_emoji_refs
      );
      const mentionIntent = richMessageMentionIntent(vector.rich_data as Record<string, unknown>);
      expect(mentionIntent.userRefs, `${vector.name} user mention intent`).toEqual(
        context.message_mention_user_refs
      );
      expect(mentionIntent.roleRefs, `${vector.name} role mention intent`).toEqual(
        context.message_mention_role_refs
      );
      expect(mentionIntent.everyone, `${vector.name} everyone mention intent`).toBe(
        context.message_mention_everyone
      );
      expect(richMessageStickerRefs(vector.rich_data), `${vector.name} sticker refs`).toEqual(
        context.message_sticker_refs
      );
      expect(Object.keys(context).sort(), `${vector.name} exact context fields`).toEqual(
        [...richMessageVectors.context_fields].sort()
      );
      expect(base64url(await sha256(richMessageAuthenticatedData(context))), vector.name).toBe(
        vector.aad_sha256
      );
      const contract = await interactionRoutingContract(
        vector.rich_data as Record<string, unknown>,
        null
      );
      expect(contract, `${vector.name} routing contract`).toEqual(
        'interaction_contract' in vector ? vector.interaction_contract : null
      );
      expect(
        contract ? await interactionRoutingContractDigest(contract) : null,
        `${vector.name} routing digest`
      ).toBe(context.interaction_contract_digest);
    }
  });

  it('rejects ambiguous encrypted mention tokens and overlapping policies', () => {
    expect(() =>
      richMessageMentionIntent({
        content: 'hi <@42>',
        components: [],
        allowed_mentions: { parse: ['users'], users: [], roles: [], replied_user: false }
      })
    ).toThrow(/origin-qualified/u);
    expect(() =>
      validateEncryptedAllowedMentions({
        parse: ['users'],
        users: ['42@example.test'],
        roles: [],
        replied_user: false
      })
    ).toThrow(/allowed mentions/u);
  });

  it('accepts only exact authenticated voice manifests for rich messages', () => {
    const manifest = {
      version: 1,
      protocol: 'kaede-file-v1',
      file_id: 'A'.repeat(22),
      key: 'A'.repeat(43),
      filename: 'voice.m4a',
      content_type: 'audio/mp4',
      plaintext_size: 1,
      plaintext_sha256: 'A'.repeat(43),
      ciphertext_size: 62,
      ciphertext_sha256: 'A'.repeat(43),
      chunk_size: 65_536,
      attachment_id: '1',
      attachment_domain: 'example.test',
      duration_millis: 1_250,
      waveform: 'AQ=='
    };
    expect(validateEncryptedRichMessageAttachments([manifest], true)).toEqual([manifest]);
    expect(() => validateEncryptedRichMessageAttachments([manifest], false)).toThrow(
      /voice message metadata/u
    );
    expect(() =>
      validateEncryptedRichMessageAttachments([{ ...manifest, waveform: 'AQ' }], true)
    ).toThrow(/voice message metadata/u);
    expect(() =>
      validateEncryptedRichMessageAttachments([{ ...manifest, duration_millis: undefined }], true)
    ).toThrow();
    expect(() => validateEncryptedRichMessageAttachments([manifest, 1], true)).toThrow();
  });

  it('binds forward semantics while stripping destination file transport identities', async () => {
    const manifest = {
      version: 1,
      protocol: 'kaede-file-v1',
      file_id: 'A'.repeat(22),
      key: 'A'.repeat(43),
      filename: 'proof.txt',
      content_type: 'text/plain',
      plaintext_size: 1,
      plaintext_sha256: 'A'.repeat(43),
      ciphertext_size: 62,
      ciphertext_sha256: 'A'.repeat(43),
      chunk_size: 65_536,
      attachment_id: '1',
      attachment_domain: 'one.example'
    };
    const snapshot: TestForwardSnapshot = {
      content: 'immutable source',
      embeds: [],
      components: [],
      attachments: [manifest],
      mention_user_refs: [{ id: '4', origin_domain: 'users.example' }],
      sticker_items: [],
      message_snapshots: [],
      message_type: 0,
      flags: 0,
      created_at: '2026-08-28T20:00:00+00:00',
      edited_at: null
    };
    const rebound = structuredClone(snapshot);
    rebound.attachments[0].attachment_id = '2';
    rebound.attachments[0].attachment_domain = 'two.example';

    expect(validateEncryptedForwardSnapshot(snapshot)).toEqual(snapshot);
    expect(await encryptedForwardSnapshotProjectionDigest(rebound)).toBe(
      await encryptedForwardSnapshotProjectionDigest(snapshot)
    );
    expect(await encryptedForwardSnapshotDigest(rebound)).not.toBe(
      await encryptedForwardSnapshotDigest(snapshot)
    );

    const tampered = structuredClone(rebound);
    tampered.attachments[0].plaintext_sha256 = 'qUiQTy8PR5uPgZdpSzAYSw0u0cHNKh7A-4XSmaGSpEc';
    expect(await encryptedForwardSnapshotProjectionDigest(tampered)).not.toBe(
      await encryptedForwardSnapshotProjectionDigest(snapshot)
    );
    const nested = structuredClone(snapshot);
    nested.message_snapshots = [structuredClone(snapshot)];
    nested.message_snapshots[0].message_snapshots = [structuredClone(snapshot)];
    expect(() => validateEncryptedForwardSnapshot(nested)).toThrow(/forward snapshot/u);

    const forwardedStickers = structuredClone(snapshot);
    forwardedStickers.sticker_items = [
      { id: '8', origin_domain: 'stickers.example', name: 'Source', format_type: 1 }
    ];
    forwardedStickers.message_snapshots = [
      {
        ...structuredClone(snapshot),
        sticker_items: [
          { id: '7', origin_domain: 'stickers.example', name: 'Nested', format_type: 2 }
        ]
      }
    ];
    expect(
      richMessageStickerRefs({
        sticker_items: [
          { id: '9', origin_domain: 'stickers.example', name: 'Current', format_type: 3 }
        ],
        forward_snapshot: forwardedStickers
      })
    ).toEqual(['7@stickers.example', '8@stickers.example', '9@stickers.example']);
  });

  it('rejects duplicate or lossy 64-bit identity fields before encryption', () => {
    expect(() =>
      interactionAuthenticatedContext(channel, '40@users.example', `ked_${'a'.repeat(43)}`, {
        applicationRef: '30@apps.example',
        integrationType: 'guild_install',
        interactionContext: 'guild',
        interactionType: 'command'
      })
    ).toThrow(/command identity/u);
    expect(() =>
      interactionAuthenticatedContext(channel, '40@users.example', `ked_${'a'.repeat(43)}`, {
        applicationRef: '30@apps.example',
        integrationType: 'guild_install',
        interactionContext: 'guild',
        interactionType: 'component',
        attachmentIds: ['9', '9']
      })
    ).toThrow(/unique/u);
    expect(() =>
      interactionAuthenticatedContext(channel, '40@users.example', `ked_${'a'.repeat(43)}`, {
        applicationRef: '30@apps.example',
        integrationType: 'guild_install',
        interactionContext: 'guild',
        interactionType: 'component',
        responseId: Number.MAX_SAFE_INTEGER + 1
      })
    ).toThrow(/response ID/u);
  });

  it('binds normal-message bot credentials to the exact projected application and device', () => {
    const deviceId = `kbe_${'A'.repeat(43)}`;
    const credential = new TextEncoder().encode(
      JSON.stringify({
        account: 'bot:10@apps.example:worker:7',
        application_ref: '10@apps.example',
        credential_type: 'kaede-bot-device-v2',
        device_id: deviceId,
        worker_id: '7'
      })
    );
    const message = {
      author_id: '50',
      author_domain: 'bots.example',
      application_id: '10',
      application_domain: 'apps.example'
    };
    expect(() =>
      validateEncryptedMessageSenderCredential(credential, message, deviceId)
    ).not.toThrow();
    expect(() =>
      validateEncryptedMessageSenderCredential(
        credential,
        { ...message, application_id: '11' },
        deviceId
      )
    ).toThrow(/author or app/u);
    expect(() =>
      validateEncryptedMessageSenderCredential(
        credential,
        { ...message, application_id: null, application_domain: null },
        deviceId
      )
    ).toThrow(/author or app/u);

    const humanCredential = new TextEncoder().encode(
      JSON.stringify({ version: 1, account: '50@bots.example', nonce: 'A'.repeat(43) })
    );
    expect(() =>
      validateEncryptedMessageSenderCredential(
        humanCredential,
        { ...message, application_id: null, application_domain: null },
        `ked_${'A'.repeat(43)}`
      )
    ).not.toThrow();
    expect(() =>
      validateEncryptedMessageSenderCredential(humanCredential, message, `ked_${'A'.repeat(43)}`)
    ).toThrow(/author or app/u);
  });

  it('binds normal-message webhook credentials to the exact projected webhook and device', () => {
    const deviceId = `kwe_${'W'.repeat(43)}`;
    const credential = new TextEncoder().encode(
      JSON.stringify({
        account: 'webhook:70@hooks.example',
        credential_type: 'kaede-webhook-device-v1',
        device_id: deviceId,
        webhook_ref: '70@hooks.example'
      })
    );
    const message = {
      author_id: '50',
      author_domain: 'hooks.example',
      application_id: null,
      application_domain: null,
      webhook_id: '70',
      webhook: {
        id: '70',
        origin_domain: 'hooks.example',
        ref: '70@hooks.example',
        name: 'Deploy hook',
        avatar_hash: null
      }
    };
    expect(() =>
      validateEncryptedMessageSenderCredential(credential, message, deviceId)
    ).not.toThrow();
    expect(() =>
      validateEncryptedMessageSenderCredential(
        credential,
        { ...message, webhook_id: '71' },
        deviceId
      )
    ).toThrow(/webhook attribution/u);
    expect(() =>
      validateEncryptedMessageSenderCredential(
        credential,
        { ...message, application_id: '10', application_domain: 'apps.example' },
        deviceId
      )
    ).toThrow(/author or app/u);
    expect(() =>
      validateEncryptedMessageSenderCredential(credential, message, `kwe_${'X'.repeat(43)}`)
    ).toThrow(/author or app/u);
  });

  it('derives webhook MLS devices from the exact federated webhook and identity key', async () => {
    const identityKey = Uint8Array.from({ length: 32 }, (_, index) => index);
    const expected = base64url(
      await sha256(
        concatBytes(utf8('kaede-webhook-e2ee-device-v1\0' + '70@hooks.example' + '\0'), identityKey)
      )
    );
    await expect(webhookIdentityDeviceId('70@hooks.example', identityKey)).resolves.toBe(
      `kwe_${expected}`
    );
    await expect(webhookIdentityDeviceId('070@hooks.example', identityKey)).rejects.toThrow(
      /identity/u
    );
  });
});
