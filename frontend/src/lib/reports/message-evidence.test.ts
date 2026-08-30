import { describe, expect, it } from 'vitest';

import type { Message } from '$lib/chat/types';
import { encryptedReportDisclosure } from './message-evidence';

function encryptedMessage(
  decrypted_content?: string | null,
  verified = true
): Pick<Message, 'e2ee' | 'e2ee_verified' | 'decrypted_content'> {
  return {
    e2ee: { ciphertext: 'opaque' },
    e2ee_verified: verified,
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

  it('never discloses network-projected plaintext without local verification', () => {
    expect(encryptedReportDisclosure(encryptedMessage('injected', false))).toEqual({
      available: false,
      content: null
    });
  });
});
