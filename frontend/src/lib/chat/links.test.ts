import { describe, expect, it } from 'vitest';
import { linksInMessage, previewableLink } from './links';

describe('message links', () => {
  it('extracts safe web URLs and removes sentence punctuation', () => {
    expect(linksInMessage('See https://example.com/image.png, then http://example.net/a.')).toEqual(
      ['https://example.com/image.png', 'http://example.net/a']
    );
  });

  it('does not separately unfurl invite or KLIPY cards', () => {
    expect(previewableLink('https://chat.example/invite/abc')).toBeNull();
    expect(previewableLink('https://media.klipy.com/example.gif')).toBeNull();
    expect(
      previewableLink('https://apps.example/application-directory/123@apps.example')
    ).toBeNull();
    expect(previewableLink('https://example.com/article')).toBe('https://example.com/article');
  });

  it('skips bot invite cards, including federated links, while previewing other links', () => {
    for (const invite of [
      'https://kaede.chat/applications/91287871893315584@kaede.chat/install/install',
      'https://apps.example/applications/123/install/community',
      'https://chat.example/applications/123%40apps.example/install/community'
    ]) {
      expect(previewableLink(invite)).toBeNull();
      expect(previewableLink(`${invite} https://example.com/article`)).toBe(
        'https://example.com/article'
      );
    }
  });
});
