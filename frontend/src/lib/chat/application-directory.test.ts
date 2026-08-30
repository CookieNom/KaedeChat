import { describe, expect, it } from 'vitest';
import {
  applicationInstallPath,
  canonicalDirectoryDomain,
  canonicalDirectoryFilters,
  directoryDetailPath,
  directoryEntryPath,
  directoryFiltersFromSearchParams,
  directoryPagePath,
  directoryQuery,
  directoryRestoredPageCount,
  parseDirectoryBotProfileApplication,
  safeDirectoryApplicationReturnPath,
  safeDirectoryListReturnPath,
  type DirectoryApplicationSummary
} from './application-directory';

describe('application directory navigation', () => {
  it('accepts only canonicalizable federation domains', () => {
    expect(canonicalDirectoryDomain(' Apps.Remote... ')).toBe('apps.remote');
    expect(canonicalDirectoryDomain('https://apps.remote')).toBe('');
    expect(canonicalDirectoryDomain('apps.remote:443')).toBe('');
    expect(canonicalDirectoryDomain('apps..remote')).toBe('');
    expect(canonicalDirectoryDomain('localhost')).toBe('');
    expect(canonicalDirectoryDomain(`apps.${'a'.repeat(248)}.remote`)).toBe('');
  });

  it('keeps search cursors bounded to query parameters', () => {
    expect(
      directoryQuery(
        {
          query: ' weather ',
          category: 'utilities',
          domain: ' Apps.Remote ',
          collection: 'staff-picks'
        },
        '123'
      )
    ).toBe(
      '/application-directory?q=weather&category=utilities&domain=apps.remote&collection=staff-picks&limit=24&after=123'
    );
  });

  it('round-trips canonical filters, source context, and bounded restored pages', () => {
    const filters = canonicalDirectoryFilters({
      query: ' weather ',
      category: 'utilities',
      domain: ' Apps.Remote ',
      collection: 'featured'
    });
    const path = directoryPagePath(filters, '/g/7@apps.remote/8@apps.remote?around=9', 99);
    const params = new URL(path, 'https://chat.example').searchParams;

    expect(directoryFiltersFromSearchParams(params)).toEqual(filters);
    expect(directoryRestoredPageCount(params)).toBe(10);
    expect(params.get('from')).toBe('/g/7@apps.remote/8@apps.remote?around=9');
  });

  it('rejects cross-origin and wrong-surface return paths', () => {
    const origin = 'https://chat.example';
    expect(safeDirectoryListReturnPath('/application-directory?q=weather', origin)).toBe(
      '/application-directory?q=weather'
    );
    expect(
      safeDirectoryApplicationReturnPath('/application-directory/42%40apps.remote', origin)
    ).toBe('/application-directory/42%40apps.remote');
    expect(safeDirectoryListReturnPath('//evil.example/application-directory', origin)).toBeNull();
    expect(safeDirectoryListReturnPath('/application-directory/42', origin)).toBeNull();
    expect(safeDirectoryApplicationReturnPath('/settings', origin)).toBeNull();
  });

  it.each([
    '//evil.example/application-directory',
    String.raw`/\evil.example/application-directory`,
    '/application-directory/../../settings',
    '/application-directory/%2e%2e/settings',
    '/application-directory/%2E%2E%2Fsettings',
    '/application-directory/%252e%252e/settings',
    '/application-directory/%5c%5cevil.example',
    '/application-directory/%255c%255cevil.example',
    '/application-directory//evil.example',
    '/application-directory/%2f%2fevil.example',
    '/application-directory/%252f%252fevil.example',
    '/application-directory/%00',
    '/application-directory/%0d%0aLocation:%20https://evil.example'
  ])('rejects a malformed encoded application return path: %s', (value) => {
    expect(safeDirectoryApplicationReturnPath(value, 'https://chat.example')).toBeNull();
  });

  it('canonicalizes the single federated application reference segment', () => {
    expect(
      safeDirectoryApplicationReturnPath(
        '/application-directory/42@apps.remote?from=install#overview',
        'https://chat.example'
      )
    ).toBe('/application-directory/42%40apps.remote?from=install#overview');
  });

  it('preserves a selected federated guild settings route as directory source context', () => {
    expect(directoryEntryPath('/g/7%40apps.remote/settings')).toBe(
      '/application-directory?from=%2Fg%2F7%2540apps.remote%2Fsettings'
    );
  });

  it('offers Add App for a remote user-only app with an active template', () => {
    const application = {
      id: '42',
      ref: '42@apps.remote',
      origin_domain: 'apps.remote',
      name: 'Weather',
      summary: 'Federated forecasts',
      category: 'utilities',
      tags: ['weather'],
      collections: ['featured'],
      icon_hash: null,
      banner_hash: null,
      verified: true,
      user_install_supported: true,
      install_template: {
        slug: 'add app',
        name: 'Add Weather',
        description: null,
        install_types: ['user_install'],
        default_install_type: 'user_install'
      }
    } satisfies DirectoryApplicationSummary;
    expect(application.collections).toEqual(['featured']);
    expect(directoryDetailPath(application.ref)).toBe('/application-directory/42%40apps.remote');
    expect(applicationInstallPath(application)).toBe(
      '/applications/42%40apps.remote/install/add%20app'
    );
    expect(directoryDetailPath(application.ref, '/application-directory?q=weather')).toBe(
      '/application-directory/42%40apps.remote?return_to=%2Fapplication-directory%3Fq%3Dweather'
    );
    expect(applicationInstallPath(application, '/application-directory/42%40apps.remote')).toBe(
      '/applications/42%40apps.remote/install/add%20app?return_to=%2Fapplication-directory%2F42%2540apps.remote'
    );
  });

  it('does not offer Add App without an active template', () => {
    expect(
      applicationInstallPath({ install_template: null } as unknown as DirectoryApplicationSummary)
    ).toBeNull();
  });
});

describe('bot profile application projection', () => {
  const profile = {
    bot_ref: '7@apps.remote',
    application_ref: '42@apps.remote',
    origin_domain: 'apps.remote',
    name: 'Weather',
    install_template: {
      slug: 'install',
      name: 'Add Weather',
      description: null,
      install_types: ['guild_install', 'user_install'],
      default_install_type: 'guild_install'
    },
    directory_listed: true
  };

  it('accepts a strict authority-bound Add App projection', () => {
    expect(parseDirectoryBotProfileApplication(profile, '7@apps.remote')).toEqual(profile);
  });

  it.each([
    { ...profile, bot_ref: '8@apps.remote' },
    { ...profile, application_ref: '42@evil.remote' },
    { ...profile, origin_domain: 'evil.remote' },
    { ...profile, extra: true },
    {
      ...profile,
      install_template: {
        ...profile.install_template,
        install_types: ['guild_install', 'guild_install']
      }
    },
    {
      ...profile,
      install_template: {
        ...profile.install_template,
        default_install_type: 'user_install',
        install_types: ['guild_install']
      }
    }
  ])('rejects an inconsistent or non-strict projection', (value) => {
    expect(parseDirectoryBotProfileApplication(value, '7@apps.remote')).toBeNull();
  });
});
