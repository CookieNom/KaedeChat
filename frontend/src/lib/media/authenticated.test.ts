import { describe, expect, it } from 'vitest';

import { attachmentMediaPath } from './authenticated';

describe('attachmentMediaPath', () => {
  it('builds the authenticated attachment route', () => {
    expect(attachmentMediaPath('chat.example', '75512661369970688', 'thumbnail_512')).toBe(
      '/media/chat.example/75512661369970688/thumbnail_512'
    );
  });

  it('encodes path components', () => {
    expect(attachmentMediaPath('chat.example', '12/34', 'original')).toBe(
      '/media/chat.example/12%2F34/original'
    );
  });
});
