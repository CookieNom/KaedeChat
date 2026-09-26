import { describe, expect, it, vi } from 'vitest';
import { loadDownloads, releasesUrl } from './downloads';

function release(tag: string, suffixes: string[]) {
  return {
    tag_name: tag,
    draft: false,
    prerelease: false,
    assets: suffixes.map((suffix) => ({
      name: `Kaede-Chat-${tag}${suffix}`,
      browser_download_url: `${releasesUrl}/download/${tag}/Kaede-Chat-${tag}${suffix}`,
      size: 1024,
      state: 'uploaded'
    }))
  };
}

const signal = () => new AbortController().signal;

describe('homepage downloads', () => {
  it('keeps latest assets and walks older pages per platform, ignoring unsafe or unfinished assets', async () => {
    const newest = release('v3.0.0', ['-android.apk', '-android.apk.sha256']);
    const invalid = release('v2.9.0', [
      '-linux-x86_64.rpm',
      '-windows-x86_64-setup.exe',
      '-macos-arm64.dmg'
    ]);
    invalid.assets[0].browser_download_url = 'https://example.com/untrusted.rpm';
    invalid.assets[1].size = 0;
    invalid.assets[2].state = 'new';
    const older = release('desktop-v2.0.0', [
      '-android.apk',
      '-linux-x86_64.rpm',
      '-windows-x86_64-setup.exe',
      '-ios.ipa',
      '-macos-arm64.dmg',
      '-macos-x86_64.dmg',
      '-linux-x86_64.deb',
      '-linux-x86_64.AppImage'
    ]);
    const page = [
      { ...older, draft: true },
      { ...older, prerelease: true },
      invalid,
      ...Array.from({ length: 97 }, () => release('v2.8.0', []))
    ];
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(newest))
      .mockResolvedValueOnce(Response.json(page))
      .mockResolvedValueOnce(Response.json([older]));
    const { downloads, failed } = await loadDownloads(signal(), request);
    expect(failed).toBe(false);
    expect(Object.keys(downloads)).toHaveLength(8);
    expect(downloads.android?.version).toBe('3.0.0');
    expect(downloads.android?.checksum).toBe(newest.assets[1].browser_download_url);
    expect(downloads.rpm?.version).toBe('2.0.0');
    expect(downloads.macArm?.url).toContain('macos-arm64.dmg');
    expect(downloads.macIntel?.url).toContain('macos-x86_64.dmg');
    expect(request.mock.calls[2][0]).toContain('page=2');
  });

  it('preserves found downloads when history is rate limited, and reports network failures', async () => {
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(release('v3.0.0', ['-android.apk'])))
      .mockResolvedValueOnce(new Response(null, { status: 403 }));
    const result = await loadDownloads(signal(), request);
    expect(result.failed).toBe(true);
    expect(result.downloads.android?.version).toBe('3.0.0');
    expect(result.downloads.rpm).toBeUndefined();
    expect(await loadDownloads(signal(), vi.fn().mockRejectedValue(new Error('offline')))).toEqual({
      downloads: {},
      failed: true
    });
  });

  it('leaves absent platforms unavailable when no stable releases exist', async () => {
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(Response.json([]));
    expect(await loadDownloads(signal(), request)).toEqual({ downloads: {}, failed: false });
  });
});
