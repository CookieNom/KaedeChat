const repository = 'CookieNom/KaedeChat';
export const releasesUrl = `https://github.com/${repository}/releases`;
const apiUrl = `https://api.github.com/repos/${repository}/releases`;

const suffixes = {
  windows: '-windows-x86_64-setup.exe',
  macArm: '-macos-arm64.dmg',
  macIntel: '-macos-x86_64.dmg',
  deb: '-linux-x86_64.deb',
  rpm: '-linux-x86_64.rpm',
  appimage: '-linux-x86_64.AppImage',
  android: '-android.apk',
  ios: '-ios.ipa'
} as const;

export type DownloadPlatform = keyof typeof suffixes;
export type Download = { url: string; version: string; size: number; checksum?: string };
export type Downloads = Partial<Record<DownloadPlatform, Download>>;

type Asset = { name: string; browser_download_url: string; size: number; state: string };
type Release = { draft: boolean; prerelease: boolean; tag_name: string; assets: Asset[] };

function assetUrl(asset: Asset | undefined): string | undefined {
  // Only link to this project's uploaded release assets, including checksum files.
  if (
    asset?.state === 'uploaded' &&
    asset.size > 0 &&
    asset.browser_download_url.startsWith(`${releasesUrl}/download/`)
  ) {
    return asset.browser_download_url;
  }
}

/** Each format keeps its newest published build, even on mobile-only releases. */
export async function loadDownloads(signal: AbortSignal, request = fetch) {
  const downloads: Downloads = {};
  function collect(release: Release) {
    if (release.draft || release.prerelease) return;
    for (const platform of Object.keys(suffixes) as DownloadPlatform[]) {
      if (downloads[platform]) continue;
      const asset = release.assets.find(
        (asset) =>
          asset.name === `Kaede-Chat-${release.tag_name}${suffixes[platform]}` && assetUrl(asset)
      );
      const url = assetUrl(asset);
      if (asset && url) {
        downloads[platform] = {
          url,
          version: release.tag_name.replace(/^(desktop-)?v/, ''),
          size: asset.size,
          checksum: assetUrl(release.assets.find((item) => item.name === `${asset.name}.sha256`))
        };
      }
    }
  }
  const options = { signal, headers: { Accept: 'application/vnd.github+json' } };
  try {
    const latest = await request(`${apiUrl}/latest`, options);
    if (latest.ok) collect(await latest.json());
    else if (latest.status !== 404) throw new Error('Release lookup failed');

    // Follow all history pages so infrequently updated platforms remain available.
    for (let page = 1; Object.keys(downloads).length < Object.keys(suffixes).length; page++) {
      const response = await request(`${apiUrl}?per_page=100&page=${page}`, options);
      if (!response.ok) throw new Error('Release history lookup failed');
      const releases: Release[] = await response.json();
      for (const release of releases) collect(release);
      if (releases.length < 100) break;
    }
    return { downloads, failed: false };
  } catch {
    // Keep any working links if a subsequent history request fails or times out.
    return { downloads, failed: true };
  }
}
