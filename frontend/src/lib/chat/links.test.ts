import { describe, expect, it } from 'vitest';
import { linksInMessage, previewableLink, spotifyEmbedUrl } from './links';

describe('Spotify player URLs', () => {
  const id = '11dFghVXANMlKmJXsNCbNl';
  it('accepts supported Spotify links and drops tracking and theme parameters', () => {
    for (const type of ['track', 'album', 'playlist', 'artist', 'episode', 'show']) {
      expect(
        spotifyEmbedUrl(`https://open.spotify.com/intl-de/${type}/${id}?si=abc&theme=0#x`)
      ).toBe(`https://open.spotify.com/embed/${type}/${id}`);
    }
  });
  it('rejects other origins, credentials, ports, and unsupported paths', () => {
    for (const url of [
      `http://open.spotify.com/track/${id}`,
      `https://open.spotify.com.evil.test/track/${id}`,
      `https://user@open.spotify.com/track/${id}`,
      `https://open.spotify.com:8443/track/${id}`,
      `https://open.spotify.com/embed/track/${id}`,
      `https://open.spotify.com/user/${id}`,
      'https://open.spotify.com/track/invalid',
      'not a URL'
    ])
      expect(spotifyEmbedUrl(url)).toBeNull();
  });
});

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
