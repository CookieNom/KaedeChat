import { parseCanonicalEntityRef } from './refs';
import type { Message } from './types';

const DEFAULT_RECENT_REACTIONS = ['❤', '😂', '👍', '🔥'];
const STORAGE_PREFIX = 'kaede:reaction-recents:';
const CUSTOM_REACTION =
  /^<(?<animated>a?):(?<name>[A-Za-z0-9_]{2,32}):(?<id>[1-9][0-9]{0,18})@(?<domain>[A-Za-z0-9.-]{1,253})>$/u;
const VARIATION_SELECTORS = /[\uFE0E\uFE0F]/gu;
const EMOJI_VARIATION_SELECTOR = '\uFE0F';
const TEXT_VARIATION_SELECTOR = '\uFE0E';
const ZWJ = '\u200d';
const KEYCAP = 0x20e3;
const LEGACY_EMOJI_BASES = new Set([
  0x00a9, 0x00ae, 0x203c, 0x2049, 0x2122, 0x2139, 0x2194, 0x2195, 0x2196, 0x2197, 0x2198, 0x2199,
  0x21a9, 0x21aa, 0x231a, 0x231b, 0x2328, 0x23cf, 0x24c2, 0x25aa, 0x25ab, 0x25b6, 0x25c0, 0x25fb,
  0x25fc, 0x25fd, 0x25fe, 0x3030, 0x303d, 0x3297, 0x3299
]);

interface StoredReaction {
  value: string;
  count: number;
  lastUsed: number;
}

export interface ReactionToggleState {
  emoji: string;
  remove: boolean;
  exists: boolean;
}

let unicodeReactionPresentations: Promise<ReadonlyMap<string, string>> | undefined;

function isEmojiBase(codepoint: number): boolean {
  return (
    (codepoint >= 0x1f000 && codepoint <= 0x1faff) ||
    (codepoint >= 0x2600 && codepoint <= 0x27bf) ||
    LEGACY_EMOJI_BASES.has(codepoint)
  );
}

function isUnicodeEmojiSequence(value: string): boolean {
  const codepoints = [...value].map((character) => character.codePointAt(0) ?? -1);
  if (
    codepoints.length === 2 &&
    codepoints.every((codepoint) => codepoint >= 0x1f1e6 && codepoint <= 0x1f1ff)
  ) {
    return true;
  }
  if (
    codepoints.length === 2 &&
    '#*0123456789'.includes(String.fromCodePoint(codepoints[0])) &&
    codepoints[1] === KEYCAP
  ) {
    return true;
  }
  if (
    codepoints.length >= 3 &&
    codepoints[0] === 0x1f3f4 &&
    codepoints.at(-1) === 0xe007f &&
    codepoints.slice(1, -1).every((codepoint) => codepoint >= 0xe0061 && codepoint <= 0xe007a)
  ) {
    return true;
  }
  const segments = value.split(ZWJ);
  if (!segments.length || segments.some((segment) => !segment)) return false;
  return segments.every((segment) => {
    const points = [...segment].map((character) => character.codePointAt(0) ?? -1);
    return (
      (points.length === 1 || points.length === 2) &&
      isEmojiBase(points[0]) &&
      (points.length === 1 || (points[1] >= 0x1f3fb && points[1] <= 0x1f3ff))
    );
  });
}

/** Canonical reaction key shared by picker, persistence, UI state, and API calls. */
export function canonicalReactionEmoji(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const custom = CUSTOM_REACTION.exec(value);
  if (custom?.groups) {
    const domain = custom.groups.domain.toLowerCase().replace(/\.+$/u, '');
    const ref = parseCanonicalEntityRef(`${custom.groups.id}@${domain}`);
    if (!ref) return null;
    return `<${custom.groups.animated}:${custom.groups.name}:${ref.id}@${ref.origin_domain}>`;
  }
  const normalized = value.normalize('NFC').replace(VARIATION_SELECTORS, '');
  return normalized && isUnicodeEmojiSequence(normalized) ? normalized : null;
}

function variationSelectorCount(value: string): number {
  return [...value].filter((character) => character === EMOJI_VARIATION_SELECTOR).length;
}

function loadUnicodeReactionPresentations(): Promise<ReadonlyMap<string, string>> {
  unicodeReactionPresentations ??= import('emojibase-data/meta/unicode.json').then(
    ({ default: values }) => {
      const presentations = new Map<string, string>();
      for (const value of values as string[]) {
        if (value.includes(TEXT_VARIATION_SELECTOR)) continue;
        const canonical = canonicalReactionEmoji(value);
        if (!canonical || CUSTOM_REACTION.test(canonical)) continue;
        const current = presentations.get(canonical);
        if (
          current === undefined ||
          variationSelectorCount(value) > variationSelectorCount(current)
        ) {
          presentations.set(canonical, value);
        }
      }
      return presentations;
    }
  );
  return unicodeReactionPresentations;
}

/** Resolve a canonical Unicode identity to its fully-qualified display sequence. */
export async function reactionEmojiPresentation(value: string): Promise<string> {
  const canonical = canonicalReactionEmoji(value);
  if (!canonical || CUSTOM_REACTION.test(canonical)) return canonical ?? value;
  return (await loadUnicodeReactionPresentations()).get(canonical) ?? canonical;
}

export function messageHasOwnReaction(
  message: Pick<Message, 'reacted_emoji'>,
  value: string
): boolean {
  const canonical = canonicalReactionEmoji(value);
  return Boolean(
    canonical && message.reacted_emoji?.some((item) => canonicalReactionEmoji(item) === canonical)
  );
}

export function messageReactionCount(
  message: Pick<Message, 'reaction_counts'>,
  value: string
): number {
  const canonical = canonicalReactionEmoji(value);
  if (!canonical) return 0;
  return Object.entries(message.reaction_counts ?? {}).reduce(
    (total, [item, count]) =>
      canonicalReactionEmoji(item) === canonical && Number.isFinite(Number(count))
        ? total + Math.max(0, Number(count))
        : total,
    0
  );
}

export function reactionToggleState(
  message: Pick<Message, 'reacted_emoji' | 'reaction_counts'>,
  value: string
): ReactionToggleState | null {
  const emoji = canonicalReactionEmoji(value);
  if (!emoji) return null;
  return {
    emoji,
    remove: messageHasOwnReaction(message, emoji),
    exists: messageReactionCount(message, emoji) > 0
  };
}

function storageKey(userKey: string): string {
  return `${STORAGE_PREFIX}${userKey || 'anonymous'}`;
}

function persistStoredReactions(userKey: string, items: StoredReaction[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(storageKey(userKey), JSON.stringify(items));
  } catch {
    // Private browsing and storage policies can disable persistence. Reactions
    // still work; only the personalized shortcut row is lost.
  }
}

function storedReactions(userKey: string): StoredReaction[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey(userKey)) ?? '[]');
    if (!Array.isArray(raw)) return [];
    const merged = new Map<string, StoredReaction>();
    for (const item of raw) {
      const value = canonicalReactionEmoji(item?.value);
      if (
        !value ||
        typeof item.count !== 'number' ||
        !Number.isFinite(item.count) ||
        item.count <= 0 ||
        typeof item.lastUsed !== 'number' ||
        !Number.isFinite(item.lastUsed)
      ) {
        continue;
      }
      const existing = merged.get(value);
      if (existing) {
        existing.count += item.count;
        existing.lastUsed = Math.max(existing.lastUsed, item.lastUsed);
      } else {
        merged.set(value, { value, count: item.count, lastUsed: item.lastUsed });
      }
    }
    const items = [...merged.values()];
    if (JSON.stringify(items) !== JSON.stringify(raw)) persistStoredReactions(userKey, items);
    return items;
  } catch {
    return [];
  }
}

export function recentReactions(userKey: string, limit = 4): string[] {
  const stored = storedReactions(userKey)
    .sort((left, right) => right.count - left.count || right.lastUsed - left.lastUsed)
    .map((item) => item.value);
  return [...new Set([...stored, ...DEFAULT_RECENT_REACTIONS])].slice(0, limit);
}

export function rememberReaction(userKey: string, value: string): void {
  if (typeof localStorage === 'undefined') return;
  const canonical = canonicalReactionEmoji(value);
  if (!canonical) return;
  const items = storedReactions(userKey);
  const existing = items.find((item) => item.value === canonical);
  if (existing) {
    existing.count += 1;
    existing.lastUsed = Date.now();
  } else {
    items.push({ value: canonical, count: 1, lastUsed: Date.now() });
  }
  persistStoredReactions(
    userKey,
    items.sort((left, right) => right.lastUsed - left.lastUsed).slice(0, 24)
  );
}
