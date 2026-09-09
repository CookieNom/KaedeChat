import { describe, expect, it } from 'vitest';

import type { Message } from './types';
import {
  applyBulkMessageDelete,
  bulkDeletedMessageKeys,
  tombstoneMessage
} from './message-deletions';

function message(id: string, domain = 'guild.example'): Message {
  return {
    id,
    origin_domain: domain,
    channel_id: '7',
    channel_domain: 'guild.example',
    author_id: '8',
    author_domain: 'remote.example',
    author: null,
    content: 'secret',
    e2ee: { ciphertext: 'opaque' },
    decrypted_content: 'decrypted secret',
    e2ee_verified: true,
    decrypted_attachments: [{ attachment_id: '9' } as never],
    attachments: [
      {
        id: '9',
        origin_domain: domain,
        filename: 'secret.png',
        content_type: 'image/png',
        size: 3,
        width: 1,
        height: 1,
        blurhash: null,
        scan_status: 'clean',
        variants: {}
      }
    ],
    embeds: [{ type: 'rich', description: 'cached' }],
    components: [{ type: 10, content: 'private component' }],
    poll: {
      question: { text: 'Private question', emoji: null },
      answers: [{ answer_id: 1, poll_media: { text: 'Private answer', emoji: null } }],
      expiry: '2026-08-30T00:00:00Z',
      allow_multiselect: false,
      layout_type: 1,
      results: { is_finalized: false, answer_counts: [{ id: 1, count: 2, me_voted: true }] }
    },
    reaction_counts: { '❤': 2 },
    reacted_emoji: ['❤'],
    message_type: 0,
    flags: 0,
    client_nonce: null,
    referenced_message_id: null,
    referenced_message_domain: null,
    mention_user_refs: [],
    edited_at: null,
    deleted_at: null,
    created_at: '2026-08-29T00:00:00Z'
  };
}

describe('message deletion reconciliation', () => {
  it('clears client-only plaintext and rich content from a tombstone', () => {
    const deleted = tombstoneMessage(message('1'), '2026-08-29T01:00:00Z');
    expect(deleted).toMatchObject({
      content: null,
      e2ee: null,
      decrypted_content: null,
      e2ee_verified: false,
      decrypted_attachments: [],
      embeds: [],
      attachments: [],
      components: [],
      poll: null,
      reaction_counts: {},
      reacted_emoji: [],
      deleted_at: '2026-08-29T01:00:00Z'
    });
  });

  it('bulk tombstones only exact composite message references', () => {
    const messages = [message('1'), message('1', 'other.example'), message('2')];
    const update = {
      ids: [
        { id: '1', origin_domain: 'guild.example' },
        { id: '2', origin_domain: 'guild.example' },
        { id: 3, origin_domain: 'guild.example' }
      ]
    };
    expect([...bulkDeletedMessageKeys(update)]).toEqual(['1@guild.example', '2@guild.example']);
    const applied = applyBulkMessageDelete(messages, update, '2026-08-29T01:00:00Z');
    expect(applied.map((item) => item.deleted_at)).toEqual([
      '2026-08-29T01:00:00Z',
      null,
      '2026-08-29T01:00:00Z'
    ]);
  });
});
