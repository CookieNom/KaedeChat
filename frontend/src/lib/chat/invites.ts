const INVITE_CODE = /^[A-Za-z0-9]{8}$/;
const FEDERATION_DOMAIN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const INVITE_URL = /(?:https?:\/\/[^\s<>()]+|(?:[a-z0-9-]+\.)+[a-z0-9-]+\/invite\/[^\s<>()]+)/gi;

function cleanToken(value: string): string {
  return value.trim().replace(/[.,!?;:]+$/, '');
}

function normalizedCode(value: string, fallbackDomain?: string): string | null {
  let code = value;
  let domain = fallbackDomain;
  const separator = value.lastIndexOf('@');
  if (separator !== -1) {
    code = value.slice(0, separator);
    domain = value.slice(separator + 1);
  }
  if (!INVITE_CODE.test(code)) return null;
  if (!domain) return code;
  const normalizedDomain = domain.toLowerCase().replace(/\.$/, '');
  if (fallbackDomain && normalizedDomain !== fallbackDomain.toLowerCase().replace(/\.$/, '')) {
    return null;
  }
  return FEDERATION_DOMAIN.test(normalizedDomain) ? `${code}@${normalizedDomain}` : null;
}

export function normalizeInviteReference(value: string): string | null {
  const input = cleanToken(value);
  if (!input || input.length > 500) return null;

  if (!input.includes('/')) return normalizedCode(input);

  const candidate = /^https?:\/\//i.test(input) ? input : `https://${input}`;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.port) {
    return null;
  }
  const match = /^\/invite\/([^/]+)\/?$/.exec(url.pathname);
  if (!match || url.search || url.hash) return null;
  let routeCode: string;
  try {
    routeCode = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  return normalizedCode(routeCode, url.hostname);
}

export function inviteReferencesInMessage(content: string): string[] {
  const references = new Set<string>();
  for (const match of content.matchAll(INVITE_URL)) {
    const normalized = normalizeInviteReference(match[0]);
    if (normalized) references.add(normalized);
  }
  return [...references].slice(0, 3);
}
