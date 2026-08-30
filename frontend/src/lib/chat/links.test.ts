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
  });
});
