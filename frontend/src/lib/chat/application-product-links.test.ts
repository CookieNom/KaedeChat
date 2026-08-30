import { describe, expect, it } from 'vitest';
import {
  directoryProductLinksInMessage,
  directoryProductShareUrl,
  normalizeDirectoryProductLink
} from './application-product-links';

describe('application product links', () => {
  it('accepts an exact HTTPS product path whose qualified ref belongs to the host', () => {
    expect(
      normalizeDirectoryProductLink('https://apps.example/application-directory/123%40apps.example')
    ).toEqual({ applicationRef: '123@apps.example', originDomain: 'apps.example' });
    expect(
      normalizeDirectoryProductLink(
        'https://apps.example:443/application-directory/123@apps.example'
      )
    ).toEqual({ applicationRef: '123@apps.example', originDomain: 'apps.example' });
  });

  it.each([
    'http://apps.example/application-directory/123@apps.example',
    'https://user@apps.example/application-directory/123@apps.example',
    'https://apps.example:444/application-directory/123@apps.example',
    'https://apps.example/application-directory/123@evil.example',
    'https://apps.example/application-directory/123@apps.example/more',
    'https://apps.example/application-directory//123@apps.example',
    'https://apps.example/application-directory/123%2Fapps.example',
    'https://apps.example/application-directory/123%2540apps.example',
    String.raw`https://apps.example/application-directory\123@apps.example`,
    'https://apps.example/application-directory/123@apps.example?install=1',
    'https://apps.example/application-directory/123@apps.example#about'
  ])('rejects a hostile or non-canonical product URL: %s', (value) => {
    expect(normalizeDirectoryProductLink(value)).toBeNull();
  });

  it('deduplicates qualified application refs and bounds native embeds', () => {
    const first = 'https://apps.example/application-directory/1@apps.example';
    const links = [first, first, 2, 3, 4]
      .map((value) =>
        typeof value === 'number'
          ? `https://apps.example/application-directory/${value}@apps.example`
          : value
      )
      .join(' ');
    expect(directoryProductLinksInMessage(links).map((item) => item.applicationRef)).toEqual([
      '1@apps.example',
      '2@apps.example',
      '3@apps.example'
    ]);
  });

  it('builds the share URL on the application home origin only', () => {
    expect(
      directoryProductShareUrl({ ref: '123@apps.example', origin_domain: 'apps.example' })
    ).toBe('https://apps.example/application-directory/123%40apps.example');
    expect(
      directoryProductShareUrl({ ref: '123@evil.example', origin_domain: 'apps.example' })
    ).toBeNull();
  });
});
