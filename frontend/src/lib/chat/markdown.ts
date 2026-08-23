import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { entityRef } from './refs';
import { customEmojiUrl } from './emojis';
import type { Role, UserSummary } from './types';
import { roleColorCss } from './members';
import { userDisplayName, userPublicHandle } from './users';

const EXPLICIT_MENTION = /^<@(\d+)(?:@([a-z0-9.-]+))?>$/i;
const ROLE_MENTION = /^<@&(\d+)@([a-z0-9.-]+)>$/i;
const CUSTOM_EMOJI =
  /^<(?<animated>a?):(?<name>[A-Za-z0-9_]{2,32}):(?<id>[1-9][0-9]{0,18})@(?<domain>[a-z0-9.-]{1,253})>$/i;
const TOKEN =
  /(<a?:[A-Za-z0-9_]{2,32}:[1-9][0-9]{0,18}@[a-z0-9.-]{1,253}>|<@&\d+@[a-z0-9.-]+>|<@\d+(?:@[a-z0-9.-]+)?>|@[a-z0-9_.-]{1,64}@[a-z0-9.-]+|#[a-z0-9_-]{1,100}|:[a-z0-9_+-]{1,64}:)/gi;
const COMPLETE_TOKEN =
  /^(<a?:[A-Za-z0-9_]{2,32}:[1-9][0-9]{0,18}@[a-z0-9.-]{1,253}>|<@&\d+@[a-z0-9.-]+>|<@\d+(?:@[a-z0-9.-]+)?>|@[a-z0-9_.-]{1,64}@[a-z0-9.-]+|#[a-z0-9_-]{1,100}|:[a-z0-9_+-]{1,64}:)$/i;
const SPOILER = /(\|\|[^|](?:.|\n)*?\|\|)/g;

export function splitSpoilers(value: string): string[] {
  return value.split(SPOILER).filter(Boolean);
}

export function tokenKind(token: string): 'mention' | 'channel' | 'emoji' {
  if (token.startsWith('@') || token.startsWith('<@')) return 'mention';
  if (token.startsWith('#')) return 'channel';
  return 'emoji';
}

function customEmojiImage(token: string): HTMLImageElement | undefined {
  const match = CUSTOM_EMOJI.exec(token);
  if (!match?.groups) return undefined;
  const src = customEmojiUrl(match.groups.id, match.groups.domain);
  if (!src) return undefined;
  const image = document.createElement('img');
  image.className = 'chat-custom-emoji';
  image.src = src;
  image.alt = `:${match.groups.name}:`;
  image.title = `:${match.groups.name}:`;
  image.loading = 'lazy';
  image.decoding = 'async';
  return image;
}

export function tokenizeText(
  value: string
): { text: string; kind: 'text' | 'mention' | 'channel' | 'emoji' }[] {
  const parts = value.split(TOKEN);
  return parts
    .filter(Boolean)
    .map((part) =>
      COMPLETE_TOKEN.test(part)
        ? { text: part, kind: tokenKind(part) }
        : { text: part, kind: 'text' }
    );
}

function mentionUser(
  token: string,
  users: UserSummary[],
  localDomain: string
): UserSummary | undefined {
  const explicit = EXPLICIT_MENTION.exec(token);
  if (explicit) {
    const [, id, domain] = explicit;
    return users.find(
      (user) =>
        user.id === id &&
        (domain
          ? user.origin_domain.toLowerCase() === domain.toLowerCase()
          : user.origin_domain.toLowerCase() === localDomain.toLowerCase())
    );
  }
  const handle = token.slice(1).toLowerCase();
  return users.find((user) => user.handle.toLowerCase() === handle);
}

export function mentionPresentation(
  token: string,
  users: UserSummary[],
  localDomain: string
): {
  text: string;
  title: string;
  ariaLabel: string;
  userRef?: string;
  userHandle?: string;
} {
  const user = mentionUser(token, users, localDomain);
  const explicit = EXPLICIT_MENTION.exec(token);
  const reference = explicit ? `${explicit[1]}${explicit[2] ? `@${explicit[2]}` : ''}` : undefined;
  const handle = user
    ? (userPublicHandle(user) ?? undefined)
    : token.startsWith('@')
      ? token.slice(1)
      : undefined;
  const visibleName = user ? userDisplayName(user) : undefined;
  return {
    text: visibleName ? `@${visibleName}` : handle ? `@${handle}` : '@unknown-user',
    title: user ? (handle ? `@${handle}` : visibleName!) : (reference ?? token),
    ariaLabel: `View profile for ${visibleName ?? handle ?? reference ?? 'user'}`,
    userRef: user ? entityRef(user) : reference,
    userHandle: handle
  };
}

function mentionSpan(token: string, users: UserSummary[], localDomain: string): HTMLSpanElement {
  const presentation = mentionPresentation(token, users, localDomain);
  const span = document.createElement('span');
  span.className = 'chat-token chat-token-mention';
  span.textContent = presentation.text;
  span.tabIndex = 0;
  span.setAttribute('role', 'button');
  if (presentation.userRef) span.dataset.userRef = presentation.userRef;
  if (presentation.userHandle) span.dataset.userHandle = presentation.userHandle;
  span.title = presentation.title;
  span.setAttribute('aria-label', presentation.ariaLabel);
  return span;
}

export function roleMentionPresentation(
  token: string,
  roles: Role[]
): { text: string; title: string; color?: string } {
  const reference = ROLE_MENTION.exec(token);
  const role = reference
    ? roles.find(
        (candidate) =>
          candidate.id === reference[1] &&
          candidate.origin_domain.toLowerCase() === reference[2].toLowerCase()
      )
    : undefined;
  return {
    text: role ? `@${role.name}` : '@unknown-role',
    title: role ? `Role: ${role.name}` : token,
    color: role ? roleColorCss(role.color) : undefined
  };
}

function roleMentionSpan(token: string, roles: Role[]): HTMLSpanElement {
  const presentation = roleMentionPresentation(token, roles);
  const span = document.createElement('span');
  span.className = 'chat-token chat-token-mention chat-token-role-mention';
  span.textContent = presentation.text;
  span.title = presentation.title;
  if (presentation.color) span.style.setProperty('--mention-role-color', presentation.color);
  return span;
}

function replaceLegacyMentionLinks(
  root: DocumentFragment,
  users: UserSummary[],
  localDomain: string
): void {
  for (const anchor of root.querySelectorAll<HTMLAnchorElement>('a[href^="mailto:"]')) {
    const previous = anchor.previousSibling;
    const next = anchor.nextSibling;
    if (!(previous instanceof Text)) continue;
    const address = anchor.textContent?.trim() ?? '';
    if (
      previous.data.endsWith('<@') &&
      next instanceof Text &&
      next.data.startsWith('>') &&
      /^\d+@[a-z0-9.-]+$/i.test(address)
    ) {
      previous.data = previous.data.slice(0, -2);
      next.data = next.data.slice(1);
      anchor.replaceWith(mentionSpan(`<@${address}>`, users, localDomain));
      continue;
    }
    if (!previous.data.endsWith('@')) continue;
    if (!/^[a-z0-9_.-]{1,64}@[a-z0-9.-]+$/i.test(address)) continue;
    previous.data = previous.data.slice(0, -1);
    anchor.replaceWith(mentionSpan(`@${address}`, users, localDomain));
  }
}

export function renderMessageMarkdown(
  content: string,
  users: UserSummary[] = [],
  localDomain = '',
  roles: Role[] = []
): string {
  const parsed = marked.parse(content, { async: false, breaks: true, gfm: true });
  const sanitized = DOMPurify.sanitize(parsed, {
    ALLOWED_TAGS: [
      'p',
      'br',
      'strong',
      'em',
      'del',
      'code',
      'pre',
      'blockquote',
      'ul',
      'ol',
      'li',
      'a',
      'h1',
      'h2',
      'h3',
      'h4',
      'h5',
      'h6',
      'hr',
      'table',
      'thead',
      'tbody',
      'tr',
      'th',
      'td',
      'input'
    ],
    ALLOWED_ATTR: ['href', 'title', 'type', 'checked', 'disabled'],
    ALLOW_DATA_ATTR: false
  });
  const template = document.createElement('template');
  template.innerHTML = sanitized;
  replaceLegacyMentionLinks(template.content, users, localDomain);
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  for (const node of nodes) {
    if (node.parentElement?.closest('code, pre, a, .chat-token')) continue;
    const spoilerParts = splitSpoilers(node.data);
    const hasSpoiler = spoilerParts.some((part) => part.startsWith('||') && part.endsWith('||'));
    const hasToken = tokenizeText(node.data).some((part) => part.kind !== 'text');
    if (!hasSpoiler && !hasToken) continue;
    const fragment = document.createDocumentFragment();
    for (const spoilerPart of spoilerParts) {
      if (spoilerPart.startsWith('||') && spoilerPart.endsWith('||')) {
        const spoiler = document.createElement('span');
        spoiler.className = 'chat-spoiler';
        spoiler.tabIndex = 0;
        spoiler.setAttribute('role', 'button');
        spoiler.setAttribute('aria-label', 'Reveal spoiler');
        spoiler.textContent = spoilerPart.slice(2, -2);
        fragment.append(spoiler);
        continue;
      }
      for (const part of tokenizeText(spoilerPart)) {
        if (part.kind === 'text') fragment.append(document.createTextNode(part.text));
        else {
          if (part.kind === 'mention') {
            fragment.append(
              part.text.startsWith('<@&')
                ? roleMentionSpan(part.text, roles)
                : mentionSpan(part.text, users, localDomain)
            );
          } else if (part.kind === 'emoji' && part.text.startsWith('<')) {
            fragment.append(customEmojiImage(part.text) ?? document.createTextNode(part.text));
          } else {
            const span = document.createElement('span');
            span.className = `chat-token chat-token-${part.kind}`;
            span.textContent = part.text;
            fragment.append(span);
          }
        }
      }
    }
    node.replaceWith(fragment);
  }
  for (const anchor of template.content.querySelectorAll('a')) {
    anchor.setAttribute('rel', 'noopener noreferrer nofollow');
    anchor.setAttribute('target', '_blank');
  }
  for (const input of template.content.querySelectorAll('input')) {
    input.setAttribute('type', 'checkbox');
    input.setAttribute('disabled', '');
    input.removeAttribute('name');
    input.removeAttribute('value');
  }
  return template.innerHTML;
}
