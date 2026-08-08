import { describe, expect, it } from 'vitest';

import { discardAttachments, pendingMessageSend, withoutSubmittedUploads } from './outbox';

describe('pending message sends', () => {
  it('preserves an explicit nonce and snapshots mutable input arrays', () => {
    const attachmentIds = ['10'];
    const mentionUserIds = ['20@example.test'];
    const send = pendingMessageSend('hello', attachmentIds, mentionUserIds, 'stable-nonce');

    attachmentIds.push('11');
    mentionUserIds.push('21@example.test');

    expect(send).toEqual({
      clientNonce: 'stable-nonce',
      content: 'hello',
      attachmentIds: ['10'],
      mentionUserIds: ['20@example.test'],
      referencedMessageId: null
    });
  });

  it('can discard consumed attachments without changing retry identity', () => {
    const send = pendingMessageSend(null, ['10'], [], 'stable-nonce');

    expect(discardAttachments(send)).toEqual({
      clientNonce: 'stable-nonce',
      content: null,
      attachmentIds: [],
      mentionUserIds: [],
      referencedMessageId: null
    });
    expect(send.attachmentIds).toEqual(['10']);
  });

  it('keeps the reply reference across attachment retries', () => {
    const send = pendingMessageSend('reply', ['10'], ['20@example.test'], 'stable-nonce', '30');

    expect(discardAttachments(send)).toEqual({
      clientNonce: 'stable-nonce',
      content: 'reply',
      attachmentIds: [],
      mentionUserIds: ['20@example.test'],
      referencedMessageId: '30'
    });
  });

  it('clears only uploads submitted by the completed request', () => {
    const uploads = [
      { key: 'submitted', attachmentId: '10' },
      { key: 'new', attachmentId: '11' },
      { key: 'uploading' }
    ];

    expect(withoutSubmittedUploads(uploads, ['10'])).toEqual([
      { key: 'new', attachmentId: '11' },
      { key: 'uploading' }
    ]);
  });
});
