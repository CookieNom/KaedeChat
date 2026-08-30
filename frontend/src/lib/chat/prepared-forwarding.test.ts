import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Channel, Message } from './types';
import {
  executePreparedForward,
  preparedForwardNeedsCopies,
  rebindForwardSnapshot,
  validateForwardMessageResult,
  validatePreparedForwardResponse
} from './prepared-forwarding';
import { encryptedForwardSnapshotProjectionDigest } from '$lib/e2ee/client';
import {
  prepareMessageForward,
  submitPreparedMessageForward,
  type PreparedForwardResponse
} from './interactions';

vi.mock('./interactions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./interactions')>();
  return {
    ...actual,
    prepareMessageForward: vi.fn(),
    submitPreparedMessageForward: vi.fn()
  };
});

const mockedPrepare = vi.mocked(prepareMessageForward);
const mockedSubmit = vi.mocked(submitPreparedMessageForward);

const channel = (id: string, domain: string, encrypted = false): Channel => ({
  id,
  origin_domain: domain,
  guild_id: '10',
  guild_domain: domain,
  type: 0,
  name: 'destination',
  topic: null,
  position: 0,
  parent_id: null,
  parent_domain: null,
  permissions: '2048',
  rate_limit_per_user: 0,
  last_message_id: null,
  last_message_domain: null,
  encryption_mode: encrypted ? 'e2ee' : 'plaintext'
});

const snapshot = {
  content: 'author-free body',
  embeds: [],
  components: [],
  attachments: [],
  mention_user_refs: [],
  sticker_items: [],
  message_snapshots: [],
  message_type: 0,
  flags: 0,
  created_at: '2026-08-28T20:00:00+00:00',
  edited_at: null
};

function authorization(
  destination: string,
  mode: 'plaintext' | 'e2ee',
  nonce: string,
  digest = 'A'.repeat(43)
) {
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  return {
    event_id: `kcfe_${'a'.repeat(16)}`,
    origin: 'source.example',
    type: 'message.forward.source.authorized',
    ts: 1,
    actor: { type: 'authority', id: 'source.example' },
    context: { source_channel_ref: '8@source.example' },
    content: {
      version: 1,
      requester_ref: '7@users.example',
      requester_type: 'human',
      source_message_ref: '9@source.example',
      source_channel_ref: '8@source.example',
      destination_channel_ref: destination,
      destination_encryption_mode: mode,
      source_encryption_mode: 'plaintext',
      source_projection_version: 2,
      source_projection_digest: digest,
      source_created_at: '2026-08-28T20:00:00+00:00',
      source_edited_at: null,
      source_flags: 0,
      source_message_type: 0,
      source_nsfw: false,
      source_attachment_refs: [],
      source_sticker_items: [
        {
          id: '5',
          origin_domain: 'stickers.example',
          name: 'Wave',
          format_type: 1,
          media_hash: 'a'.repeat(64)
        }
      ],
      source_custom_emoji_refs: ['<:wave:6@emoji.example>'],
      source_snapshot: snapshot,
      application_ref: null,
      e2ee_device_id: null,
      nonce,
      expires_at: expiresAt
    },
    signatures: { 'source.example': { ed25519_key: 'signature' } }
  };
}

function preparedPlaintext(
  destination: string,
  nonce: string,
  digest = 'A'.repeat(43)
): PreparedForwardResponse {
  return {
    source: {
      message_ref: '9@source.example',
      channel_ref: '8@source.example',
      encryption_mode: 'plaintext' as const,
      projection_version: 2 as const,
      projection_digest: digest,
      created_at: '2026-08-28T20:00:00+00:00',
      edited_at: null,
      flags: 0,
      message_type: 0,
      nsfw: false,
      attachment_refs: [],
      snapshot
    },
    destinations: [
      {
        channel_id: destination,
        client_nonce: nonce,
        encryption_mode: 'plaintext' as const,
        requires_plaintext_disclosure: false,
        authorization: authorization(destination, 'plaintext', nonce, digest)
      }
    ]
  };
}

describe('prepared secure forwarding', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('freshly copies plaintext attachments even between plaintext channels', () => {
    expect(preparedForwardNeedsCopies('plaintext', ['plaintext'], 1)).toBe(true);
    expect(preparedForwardNeedsCopies('plaintext', ['plaintext'], 0)).toBe(false);
  });

  it('accepts exact local and federated mixed-mode authority bindings', () => {
    const local = channel('2', 'local.example');
    const remote = channel('3', 'remote.example', true);
    const requested = new Map([
      ['2@local.example', 'local-nonce'],
      ['3@remote.example', 'remote-nonce']
    ]);
    const result = validatePreparedForwardResponse(
      {
        source: {
          message_ref: '9@source.example',
          channel_ref: '8@source.example',
          encryption_mode: 'plaintext',
          projection_version: 2,
          projection_digest: 'A'.repeat(43),
          created_at: '2026-08-28T20:00:00+00:00',
          edited_at: null,
          flags: 0,
          message_type: 0,
          nsfw: false,
          attachment_refs: [],
          snapshot
        },
        destinations: [
          {
            channel_id: '2@local.example',
            client_nonce: 'local-nonce',
            encryption_mode: 'plaintext',
            requires_plaintext_disclosure: false,
            authorization: authorization('2@local.example', 'plaintext', 'local-nonce')
          },
          {
            channel_id: '3@remote.example',
            client_nonce: 'remote-nonce',
            encryption_mode: 'e2ee',
            requires_plaintext_disclosure: false,
            authorization: authorization('3@remote.example', 'e2ee', 'remote-nonce')
          }
        ]
      },
      '8@source.example',
      '9@source.example',
      '7@users.example',
      requested,
      new Map([
        ['2@local.example', local],
        ['3@remote.example', remote]
      ])
    );

    expect(result.destinations.map((item) => item.channel_id)).toEqual([
      '2@local.example',
      '3@remote.example'
    ]);
  });

  it('rejects proof substitution before any destination encryption occurs', () => {
    const destination = channel('2', 'local.example');
    const proof = authorization('2@local.example', 'plaintext', 'nonce');
    proof.content.source_projection_digest = 'B'.repeat(43);
    expect(() =>
      validatePreparedForwardResponse(
        {
          source: {
            message_ref: '9@source.example',
            channel_ref: '8@source.example',
            encryption_mode: 'plaintext',
            projection_version: 2,
            projection_digest: 'A'.repeat(43),
            created_at: '2026-08-28T20:00:00+00:00',
            edited_at: null,
            flags: 0,
            message_type: 0,
            nsfw: false,
            attachment_refs: [],
            snapshot
          },
          destinations: [
            {
              channel_id: '2@local.example',
              client_nonce: 'nonce',
              encryption_mode: 'plaintext',
              requires_plaintext_disclosure: false,
              authorization: proof
            }
          ]
        },
        '8@source.example',
        '9@source.example',
        '7@users.example',
        new Map([['2@local.example', 'nonce']]),
        new Map([['2@local.example', destination]])
      )
    ).toThrow(/proof binding/u);
  });

  it('rejects a proof issued for another requester', () => {
    const destination = channel('2', 'local.example');
    expect(() =>
      validatePreparedForwardResponse(
        preparedPlaintext('2@local.example', 'nonce'),
        '8@source.example',
        '9@source.example',
        '8@users.example',
        new Map([['2@local.example', 'nonce']]),
        new Map([['2@local.example', destination]])
      )
    ).toThrow(/proof binding/u);
  });

  it('compares signed snapshot JSON canonically instead of trusting object key order', () => {
    const destination = channel('2', 'local.example');
    const prepared = preparedPlaintext('2@local.example', 'nonce');
    const proof = prepared.destinations[0].authorization as { content: Record<string, unknown> };
    proof.content.source_snapshot = Object.fromEntries(Object.entries(snapshot).reverse());

    expect(
      validatePreparedForwardResponse(
        prepared,
        '8@source.example',
        '9@source.example',
        '7@users.example',
        new Map([['2@local.example', 'nonce']]),
        new Map([['2@local.example', destination]])
      ).destinations
    ).toHaveLength(1);
  });

  it('rebinds every nested attachment and preserves only its semantic commitment', async () => {
    const sourceManifest = {
      version: 1,
      protocol: 'kaede-file-v1',
      file_id: 'AAECAwQFBgcICQoLDA0ODw',
      key: 'ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8',
      filename: 'résumé.txt',
      content_type: 'text/plain',
      plaintext_size: 12,
      plaintext_sha256: 'qUiQTy8PR5uPgZdpSzAYSw0u0cHNKh7A-4XSmaGSpEc',
      ciphertext_size: 73,
      ciphertext_sha256: 'YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE',
      chunk_size: 262144,
      attachment_id: '501',
      attachment_domain: 'source.example'
    };
    const destinationManifest = {
      ...sourceManifest,
      file_id: 'EBESExQVFhcYGRobHB0eHw',
      key: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
      ciphertext_sha256: 'iEe-Qwq3lhXb852ah3AIVFg6ekm-HyKnWycPo0sDWD8',
      attachment_id: '777',
      attachment_domain: 'destination.example'
    };
    const nestedSnapshot = {
      ...snapshot,
      attachments: [structuredClone(sourceManifest)]
    };
    const source = {
      ...snapshot,
      attachments: [sourceManifest],
      message_snapshots: [nestedSnapshot]
    };
    const before = await encryptedForwardSnapshotProjectionDigest(source);
    const rebound = rebindForwardSnapshot(source, [destinationManifest]);

    expect(await encryptedForwardSnapshotProjectionDigest(rebound)).toBe(before);
    expect((rebound.attachments as Record<string, unknown>[])[0].attachment_id).toBe('777');
    expect(
      (
        (rebound.message_snapshots as Record<string, unknown>[])[0].attachments as Record<
          string,
          unknown
        >[]
      )[0].attachment_id
    ).toBe('777');

    const staleNested = structuredClone(source);
    (staleNested.message_snapshots[0].attachments[0] as Record<string, unknown>).attachment_id =
      '999';
    expect(() => rebindForwardSnapshot(staleNested, [destinationManifest])).toThrow(
      /Nested forward attachment binding/u
    );
  });

  it('refreshes the same nonce-bound proof after preparation and submits only the fresh proof', async () => {
    const sourceChannel = channel('8', 'source.example');
    const destination = channel('2', 'local.example');
    const source = {
      id: '9',
      origin_domain: 'source.example',
      channel_id: '8',
      channel_domain: 'source.example',
      author_id: '7',
      author_domain: 'users.example',
      created_at: '2026-08-28T20:00:00+00:00',
      message_type: 0,
      flags: 0,
      attachments: []
    } as unknown as Message;
    const digest = await encryptedForwardSnapshotProjectionDigest(snapshot);
    let nonce = '';
    mockedPrepare.mockImplementation(async (_sourceChannel, _sourceMessage, requests) => {
      expect(requests).toHaveLength(1);
      if (!nonce) nonce = requests[0].client_nonce;
      expect(requests[0].client_nonce).toBe(nonce);
      const result = preparedPlaintext('2@local.example', nonce, digest);
      if (mockedPrepare.mock.calls.length === 2) {
        result.destinations[0].authorization.event_id = `kcfe_${'b'.repeat(16)}`;
      }
      return result;
    });
    mockedSubmit.mockResolvedValue({
      forwards: [],
      failures: [
        {
          destination_channel_ref: '2@local.example',
          status: 409,
          error: { code: 'TEST_FAILURE' }
        }
      ]
    });

    await executePreparedForward({
      source,
      sourceChannel,
      destinations: [destination],
      requesterRef: '7@users.example',
      note: 'snapshot note'
    });

    expect(mockedPrepare).toHaveBeenCalledTimes(2);
    expect(mockedSubmit).toHaveBeenCalledTimes(1);
    const submitted = mockedSubmit.mock.calls[0][2][0].message;
    expect((submitted.forward_source_proof as Record<string, unknown>).event_id).toBe(
      `kcfe_${'b'.repeat(16)}`
    );
    expect(submitted.client_nonce).toBe(nonce);
  });

  it('binds every forward result to one requested destination and source', () => {
    const result = {
      forwards: [
        {
          destination_channel_ref: '2@local.example',
          message: {
            id: '11',
            origin_domain: 'local.example',
            channel_id: '2',
            channel_domain: 'local.example',
            forwarded_message_id: '9',
            forwarded_message_domain: 'source.example'
          }
        }
      ],
      failures: []
    };
    expect(
      validateForwardMessageResult(result, '9@source.example', new Set(['2@local.example']))
    ).toEqual(result);
    expect(() =>
      validateForwardMessageResult(
        {
          ...result,
          forwards: [
            {
              ...result.forwards[0],
              message: { ...result.forwards[0].message, channel_id: '3' }
            }
          ]
        },
        '9@source.example',
        new Set(['2@local.example'])
      )
    ).toThrow(/requested lineage/u);
    expect(() =>
      validateForwardMessageResult(
        {
          ...result,
          forwards: [
            {
              ...result.forwards[0],
              message: {
                ...result.forwards[0].message,
                forwarded_message_ref: '91@source.example'
              }
            }
          ]
        },
        '9@source.example',
        new Set(['2@local.example'])
      )
    ).toThrow(/requested lineage/u);
    expect(() =>
      validateForwardMessageResult(
        { forwards: [], failures: [] },
        '9@source.example',
        new Set(['2@local.example'])
      )
    ).toThrow(/omitted/u);
    expect(() =>
      validateForwardMessageResult(
        {
          forwards: [],
          failures: [
            {
              destination_channel_ref: '2@local.example',
              status: 409,
              error: 'FORWARD_FAILED'
            }
          ]
        },
        '9@source.example',
        new Set(['2@local.example'])
      )
    ).toThrow(/requested lineage/u);
  });

  it('rejects a source mutation during proof refresh before submit', async () => {
    const sourceChannel = channel('8', 'source.example');
    const destination = channel('2', 'local.example');
    const source = {
      id: '9',
      origin_domain: 'source.example',
      channel_id: '8',
      channel_domain: 'source.example',
      author_id: '7',
      author_domain: 'users.example',
      created_at: '2026-08-28T20:00:00+00:00',
      message_type: 0,
      flags: 0,
      attachments: []
    } as unknown as Message;
    const digest = await encryptedForwardSnapshotProjectionDigest(snapshot);
    mockedPrepare.mockImplementation(async (_sourceChannel, _sourceMessage, requests) => {
      const result = preparedPlaintext('2@local.example', requests[0].client_nonce, digest);
      if (mockedPrepare.mock.calls.length === 2) {
        result.source.projection_digest = 'B'.repeat(43);
        const proof = result.destinations[0].authorization as {
          content: Record<string, unknown>;
        };
        proof.content.source_projection_digest = 'B'.repeat(43);
      }
      return result;
    });

    await expect(
      executePreparedForward({
        source,
        sourceChannel,
        destinations: [destination],
        requesterRef: '7@users.example'
      })
    ).rejects.toThrow(/source changed/u);
    expect(mockedSubmit).not.toHaveBeenCalled();
  });
});
