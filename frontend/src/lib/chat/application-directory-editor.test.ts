import { describe, expect, it } from 'vitest';
import {
  directorySettingsPayload,
  moveDirectoryItem,
  parseYouTubeVideoId,
  syncDirectoryMediaWithAssets,
  youtubeEmbedUrl,
  type ApplicationAsset
} from './application-directory-editor';

describe('directory media helpers', () => {
  it.each([
    'dQw4w9WgXcQ',
    'https://youtu.be/dQw4w9WgXcQ',
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://m.youtube.com/shorts/dQw4w9WgXcQ',
    'https://www.youtube.com/embed/dQw4w9WgXcQ',
    'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'
  ])('extracts a canonical video ID from %s', (value) => {
    expect(parseYouTubeVideoId(value)).toBe('dQw4w9WgXcQ');
  });

  it.each([
    'http://youtube.com/watch?v=dQw4w9WgXcQ',
    'https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ',
    'https://youtube.com:444/watch?v=dQw4w9WgXcQ',
    'https://user@youtube.com/watch?v=dQw4w9WgXcQ',
    'https://youtu.be/dQw4w9WgXcQ/extra',
    'https://www.youtube-nocookie.com.evil.example/embed/dQw4w9WgXcQ',
    'too-short'
  ])('rejects an unsafe or malformed video source: %s', (value) => {
    expect(parseYouTubeVideoId(value)).toBeNull();
  });

  it('constructs embeds only on the privacy-enhanced hardcoded origin', () => {
    expect(youtubeEmbedUrl('dQw4w9WgXcQ')).toBe(
      'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?rel=0'
    );
    expect(youtubeEmbedUrl('../evil.example')).toBeNull();
  });

  it('reorders without mutating the source or crossing a boundary', () => {
    const source = ['first', 'second', 'third'];
    expect(moveDirectoryItem(source, 1, -1)).toEqual(['second', 'first', 'third']);
    expect(moveDirectoryItem(source, 0, -1)).toEqual(source);
    expect(source).toEqual(['first', 'second', 'third']);
  });
});

describe('directory settings validation', () => {
  it('normalizes links/locales/descriptions and preserves ordered media', () => {
    expect(
      directorySettingsPayload({
        media: [
          { type: 'youtube', video_id: 'dQw4w9WgXcQ' },
          { type: 'image', asset_id: '42' }
        ],
        externalLinks: [{ name: '  Documentation ', url: ' https://docs.example/path ' }],
        supportedLocales: ['fr', 'en-US'],
        descriptionLocalizations: { fr: ' Bonjour ', 'en-US': '  ' }
      })
    ).toEqual({
      directory_media: [
        { type: 'youtube', video_id: 'dQw4w9WgXcQ' },
        { type: 'image', asset_id: '42' }
      ],
      directory_external_links: [{ name: 'Documentation', url: 'https://docs.example/path' }],
      directory_supported_locales: ['en-US', 'fr'],
      directory_description_localizations: { fr: 'Bonjour' }
    });
  });

  it.each([
    { links: [{ name: 'Docs', url: 'http://docs.example' }] },
    { links: [{ name: 'Docs', url: 'https://user@docs.example' }] },
    { links: [{ name: 'Docs', url: 'https://docs.example/#fragment' }] },
    {
      links: [
        { name: 'Docs', url: 'https://docs.example/one' },
        { name: 'docs', url: 'https://docs.example/two' }
      ]
    }
  ])('rejects unsafe or duplicate external links', ({ links }) => {
    expect(() =>
      directorySettingsPayload({
        media: [],
        externalLinks: links,
        supportedLocales: [],
        descriptionLocalizations: {}
      })
    ).toThrow();
  });

  it('rejects a localization outside the selected language set', () => {
    expect(() =>
      directorySettingsPayload({
        media: [],
        externalLinks: [],
        supportedLocales: ['de'],
        descriptionLocalizations: { fr: 'Bonjour' }
      })
    ).toThrow(/selected languages/i);
  });

  it('tracks automatic store-asset additions, kind changes, and deletion', () => {
    const asset = (id: string, kind: ApplicationAsset['kind']): ApplicationAsset => ({
      id,
      application_ref: '9@apps.example',
      kind,
      name: id,
      media_hash: 'a'.repeat(64),
      content_type: 'image/png',
      width: 1,
      height: 1,
      version: 1
    });
    const initial = [{ type: 'youtube' as const, video_id: 'dQw4w9WgXcQ' }];
    const added = syncDirectoryMediaWithAssets(initial, [], [asset('42', 'store')]);
    expect(added).toEqual([...initial, { type: 'image', asset_id: '42' }]);
    expect(syncDirectoryMediaWithAssets(added, [asset('42', 'store')], [])).toEqual(initial);
    expect(
      syncDirectoryMediaWithAssets(added, [asset('42', 'store')], [asset('42', 'other')])
    ).toEqual(initial);
  });
});
