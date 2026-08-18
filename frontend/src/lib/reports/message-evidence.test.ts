import { describe, expect, it } from 'vitest';

import type { Message } from '$lib/chat/types';
import { encryptedReportDisclosure } from './message-evidence';

function encryptedMessage(decrypted_content?: string | null): Pick<Message, 'decrypted_content'> {
  return {
    decrypted_content
  };
}

describe('encrypted report disclosure', () => {
  it('treats an authenticated empty plaintext as available evidence', () => {
    expect(encryptedReportDisclosure(encryptedMessage(''))).toEqual({
      available: true,
      content: ''
    });
  });

  it.each([undefined, null])('treats %s plaintext as decrypt-unavailable', (content) => {
    expect(encryptedReportDisclosure(encryptedMessage(content))).toEqual({
      available: false,
      content: null
    });
  });
});
