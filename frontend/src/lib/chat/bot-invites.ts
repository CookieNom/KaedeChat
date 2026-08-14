const DOMAIN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const BOT_INVITE_URL = /https?:\/\/[^\s<>()]+\/applications\/[^\s<>()/]+\/install\/[^\s<>()/]+/gi;
const TEMPLATE = /^[a-z0-9][a-z0-9_-]{1,63}$/;

export interface BotInviteReference {
  applicationRef: string;
  templateSlug: string;
}

function clean(value: string): string {
  return value.trim().replace(/[.,!?;:]+$/, '');
}

export function normalizeBotInvite(value: string): BotInviteReference | null {
  let url: URL;
  try {
    url = new URL(clean(value));
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
  )
    return null;
  const match = /^\/applications\/([^/]+)\/install\/([^/]+)\/?$/.exec(url.pathname);
  if (!match) return null;
  let rawRef: string;
  let slug: string;
  try {
    rawRef = decodeURIComponent(match[1]);
    slug = decodeURIComponent(match[2]);
  } catch {
    return null;
  }
  if (!TEMPLATE.test(slug)) return null;
  const separator = rawRef.lastIndexOf('@');
  const id = separator === -1 ? rawRef : rawRef.slice(0, separator);
  const domain = separator === -1 ? url.hostname : rawRef.slice(separator + 1).toLowerCase();
  if (!/^\d{1,20}$/.test(id) || !DOMAIN.test(domain)) return null;
  if (domain !== url.hostname.toLowerCase()) return null;
  return { applicationRef: `${id}@${domain}`, templateSlug: slug };
}

export function botInvitesInMessage(content: string): BotInviteReference[] {
  const found = new Map<string, BotInviteReference>();
  for (const match of content.matchAll(BOT_INVITE_URL)) {
    const reference = normalizeBotInvite(match[0]);
    if (reference) found.set(`${reference.applicationRef}/${reference.templateSlug}`, reference);
  }
  return [...found.values()].slice(0, 3);
}
