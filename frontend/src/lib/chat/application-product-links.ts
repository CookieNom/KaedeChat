import { entityRef, parseCanonicalEntityRef } from './refs';

const DIRECTORY_PRODUCT_URL = /https:\/\/[^\s<>"']+/giu;
const TRAILING_PUNCTUATION = /[),.!?:;\]}]+$/u;

export interface DirectoryProductReference {
  applicationRef: string;
  originDomain: string;
}

function clean(value: string): string {
  return value.trim().replace(TRAILING_PUNCTUATION, '');
}

export function normalizeDirectoryProductLink(value: string): DirectoryProductReference | null {
  const candidate = clean(value);
  if (candidate.includes('\\')) return null;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }
  if (
    url.protocol !== 'https:' ||
    url.username ||
    url.password ||
    url.port ||
    url.search ||
    url.hash
  ) {
    return null;
  }
  const match = /^\/application-directory\/([^/]+)$/u.exec(url.pathname);
  if (!match) return null;
  let rawRef: string;
  try {
    rawRef = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const parsed = parseCanonicalEntityRef(rawRef);
  if (!parsed || parsed.origin_domain !== url.hostname) return null;
  return { applicationRef: entityRef(parsed), originDomain: parsed.origin_domain };
}

export function directoryProductLinksInMessage(content: string): DirectoryProductReference[] {
  const found = new Map<string, DirectoryProductReference>();
  for (const match of content.matchAll(DIRECTORY_PRODUCT_URL)) {
    const reference = normalizeDirectoryProductLink(match[0]);
    if (reference) found.set(reference.applicationRef, reference);
  }
  return [...found.values()].slice(0, 3);
}

export function directoryProductShareUrl(application: {
  ref: string;
  origin_domain: string;
}): string | null {
  const parsed = parseCanonicalEntityRef(application.ref, application.origin_domain);
  if (!parsed) return null;
  return `https://${application.origin_domain}/application-directory/${encodeURIComponent(entityRef(parsed))}`;
}
