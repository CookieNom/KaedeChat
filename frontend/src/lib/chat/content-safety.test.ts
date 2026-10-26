import { describe, expect, it } from 'vitest';
import {
  applyContentSafetyUpdate,
  cachedContentReferences,
  cachedContentRestricted,
  contentSafety,
  protectMessage
} from './content-safety.svelte';
import type { Message } from './types';
import { stripNetworkClientState } from '$lib/api/network-boundary';

describe('content safety redaction', () => {
  it('clears plaintext, encrypted manifests and references and never releases a confirmed removal', () => {
    const message = {
      id: '9001',
      origin_domain: 'home.example',
      content: 'private',
      e2ee: { ciphertext: 'opaque' },
      decrypted_content: 'private',
      decrypted_attachments: [{ key: 'private-key' }],
      attachments: [{ id: '8001', origin_domain: 'home.example' }],
      referenced_message: { content: 'private reference' },
      embeds: [{ description: 'private embed' }],
      deleted_at: null
    } as unknown as Message;
    applyContentSafetyUpdate({
      state: 'removed',
      reason: 'Removed',
      message_refs: ['9001@home.example'],
      attachment_refs: ['8001@home.example']
    });
    applyContentSafetyUpdate({
      state: 'released',
      reason: 'Stale release',
      message_refs: ['9001@home.example'],
      attachment_refs: ['8001@home.example']
    });
    expect(protectMessage(message)).toMatchObject({
      safety_state: 'removed',
      content: null,
      e2ee: null,
      decrypted_content: null,
      decrypted_attachments: [],
      attachments: [],
      embeds: [],
      referenced_message: null
    });
  });

  it('releases a pending case but keeps reporter-only hiding', () => {
    const update = { reason: 'Review', message_refs: ['9002@home.example'], attachment_refs: [] };
    const message = { id: '9002', origin_domain: 'home.example', content: 'original' } as Message;
    applyContentSafetyUpdate({ ...update, state: 'pending_review' });
    expect(protectMessage(message).safety_locked).toBe(true);
    applyContentSafetyUpdate({ ...update, state: 'released' });
    expect(protectMessage(message)).toBe(message);
    applyContentSafetyUpdate({ ...update, state: 'hidden' });
    applyContentSafetyUpdate({ ...update, state: 'released' });
    expect(protectMessage(message)).toMatchObject({ safety_hidden: true, content: null });
  });

  it('finds encrypted preview and forward references in sealed cached plaintext', () => {
    const cached = {
      messageRef: '9003@home.example',
      plaintext: JSON.stringify({
        attachments: [
          {
            attachment_id: '8002',
            attachment_domain: 'home.example',
            preview: { attachment_id: '8003', attachment_domain: 'home.example', key: 'key' }
          }
        ],
        snapshot: { source_message_ref: '9004@remote.example' }
      })
    };
    expect(cachedContentReferences({ cached })).toEqual({
      messageRefs: ['9003@home.example', '9004@remote.example'],
      attachmentRefs: ['8002@home.example', '8003@home.example']
    });
    applyContentSafetyUpdate({
      state: 'removed',
      reason: 'Removed',
      message_refs: [],
      attachment_refs: ['8003@home.example']
    });
    expect(cachedContentRestricted(cached)).toBe(true);
    expect(cachedContentRestricted({ messageRef: '9005@home.example', plaintext: '{}' })).toBe(
      false
    );
  });

  it('rejects malformed safety updates without accepting ambiguous identities', () => {
    expect(() =>
      applyContentSafetyUpdate({
        state: 'removed',
        reason: 'bad',
        message_refs: ['1'],
        attachment_refs: []
      })
    ).toThrow('invalid');
  });

  it('purges unavailable history until a fresh complete response proves access, without banning it', () => {
    const message = {
      id: '9101',
      origin_domain: 'home.example',
      channel_id: '1',
      channel_domain: 'home.example',
      author_id: '2',
      author_domain: 'home.example',
      created_at: '2026-10-26T00:00:00Z',
      content: 'authorized message',
      e2ee: null,
      attachments: [{ id: '8101', origin_domain: 'home.example' }]
    } as Message;
    const entry = { messageRef: '9101@home.example', plaintext: '{}' };
    const before = contentSafety.revision;
    const unavailable = {
      state: 'unavailable',
      reason: 'Unavailable',
      message_refs: [entry.messageRef],
      attachment_refs: ['8101@home.example']
    };
    applyContentSafetyUpdate(unavailable);
    expect(cachedContentRestricted(entry)).toBe(true);
    expect(protectMessage(message).safety_unavailable).toBe(true);
    // A response begun before the revocation cannot unlock the old body.
    expect(stripNetworkClientState(message, before).safety_unavailable).toBe(true);
    // Gateway replay sanitization alone is never a fresh authorization proof.
    expect(stripNetworkClientState(message).safety_unavailable).toBe(true);
    const current = stripNetworkClientState(message, contentSafety.revision);
    expect(current.content).toBe('authorized message');
    expect(current.safety_unavailable).toBe(false);
    expect(cachedContentRestricted(entry)).toBe(false);
    // Vault reconciliation cannot override fresh authority proof for remote-only rows.
    applyContentSafetyUpdate(unavailable);
    expect(cachedContentRestricted(entry)).toBe(false);
    expect(protectMessage({ ...message, content: 'stale body' }).safety_unavailable).toBe(true);
    expect(
      protectMessage({ ...message, embeds: [{ description: 'stale rich body' }] })
        .safety_unavailable
    ).toBe(true);
    applyContentSafetyUpdate({ ...unavailable, state: 'removed' });
    expect(stripNetworkClientState(message).safety_state).toBe('removed');
    expect(cachedContentRestricted(entry)).toBe(true);
  });
});
