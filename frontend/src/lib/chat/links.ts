import { normalizeDirectoryProductLink } from './application-product-links';

const URL_PATTERN = /https?:\/\/[^\s<>"']+/gi;
const TRAILING_PUNCTUATION = /[),.!?:;\]}]+$/;

export function linksInMessage(content: string | null): string[] {
  if (!content) return [];
  const links: string[] = [];
  for (const raw of content.match(URL_PATTERN) ?? []) {
    const candidate = raw.replace(TRAILING_PUNCTUATION, '');
    try {
      const url = new URL(candidate);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') continue;
      if (url.username || url.password) continue;
      if (!links.includes(url.href)) links.push(url.href);
    } catch {
      // Markdown still renders malformed links as text; they are not unfurled.
    }
  }
  return links;
}

export function previewableLink(content: string | null): string | null {
  return (
    linksInMessage(content).find((value) => {
      const url = new URL(value);
      return (
        !['media.klipy.com', 'static.klipy.com'].includes(url.hostname) &&
        !/^\/invite\/[^/]+\/?$/i.test(url.pathname) &&
        !normalizeDirectoryProductLink(value)
      );
    }) ?? null
  );
}
