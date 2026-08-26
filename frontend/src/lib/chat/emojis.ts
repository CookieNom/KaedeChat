export type EmojiCategory =
  'people' | 'nature' | 'food' | 'activity' | 'travel' | 'objects' | 'symbols' | 'flags';

export interface EmojiOption {
  value: string;
  name: string;
  shortcode: string;
  keywords: string[];
  category: EmojiCategory;
}

export interface CustomEmojiOption {
  id: string;
  origin_domain: string;
  name: string;
  url: string;
  /** Stable wire-format token to insert into the message composer. */
  value: string;
  animated?: boolean;
  guild_id: string;
  guild_domain: string;
  guild_name?: string;
}

export interface CustomEmojiGroup {
  key: string;
  name: string;
  emojis: CustomEmojiOption[];
}

export function groupCustomEmojis(emojis: CustomEmojiOption[]): CustomEmojiGroup[] {
  const groups = new Map<string, CustomEmojiGroup>();
  for (const emoji of emojis) {
    const key = `${emoji.guild_id}@${emoji.guild_domain.toLowerCase()}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        key,
        name: emoji.guild_name?.trim() || emoji.guild_domain,
        emojis: []
      };
      groups.set(key, group);
    }
    group.emojis.push(emoji);
  }
  return [...groups.values()];
}

const FEDERATED_DOMAIN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const SNOWFLAKE = /^[1-9][0-9]{0,18}$/;
const MAX_SNOWFLAKE = 9223372036854775807n;
const CUSTOM_NAME = /^[A-Za-z0-9_]{2,32}$/;

export function customEmojiToken(emoji: {
  id: string;
  origin_domain: string;
  name: string;
  animated?: boolean;
}): string {
  if (
    !validSnowflake(emoji.id) ||
    !FEDERATED_DOMAIN.test(emoji.origin_domain) ||
    !CUSTOM_NAME.test(emoji.name)
  )
    return '';
  return `<${emoji.animated ? 'a' : ''}:${emoji.name}:${emoji.id}@${emoji.origin_domain.toLowerCase()}>`;
}

export function customEmojiUrl(id: string, domain: string, variant = 'thumbnail_128'): string {
  if (!validSnowflake(id) || !FEDERATED_DOMAIN.test(domain)) return '';
  const safeVariant =
    variant === 'original' || variant === 'thumbnail_512' ? variant : 'thumbnail_128';
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname.toLowerCase();
  const path = `/media/emojis/${id}/${safeVariant}`;
  return domain.toLowerCase() === localDomain ? path : `https://${domain.toLowerCase()}${path}`;
}

function validSnowflake(value: string): boolean {
  return SNOWFLAKE.test(value) && BigInt(value) <= MAX_SNOWFLAKE;
}

interface EmojiRecord {
  emoji: string;
  label: string;
  tags?: string[];
  group?: number;
  order?: number;
  skins?: EmojiRecord[];
}

const groupCategories: Record<number, EmojiCategory> = {
  0: 'people',
  1: 'people',
  2: 'people',
  3: 'nature',
  4: 'food',
  5: 'travel',
  6: 'activity',
  7: 'objects',
  8: 'symbols',
  9: 'flags'
};

export const emojiCategories: Array<{ id: EmojiCategory; label: string; icon: string }> = [
  { id: 'people', label: 'Smileys & people', icon: '😀' },
  { id: 'nature', label: 'Animals & nature', icon: '🐻' },
  { id: 'food', label: 'Food & drink', icon: '🍕' },
  { id: 'activity', label: 'Activities', icon: '🎮' },
  { id: 'travel', label: 'Travel & places', icon: '🚀' },
  { id: 'objects', label: 'Objects', icon: '💡' },
  { id: 'symbols', label: 'Symbols', icon: '✨' },
  { id: 'flags', label: 'Flags', icon: '🏳️' }
];

let emojiPromise: Promise<EmojiOption[]> | undefined;

function makeOption(record: EmojiRecord, inheritedTags: string[] = []): EmojiOption | undefined {
  const category = record.group === undefined ? undefined : groupCategories[record.group];
  if (!category || !record.emoji) return undefined;

  return {
    value: record.emoji,
    name: record.label.toLowerCase(),
    shortcode: record.label
      .toLowerCase()
      .replace(/[^a-z0-9+_-]+/g, '_')
      .replace(/^_+|_+$/g, ''),
    keywords: [...new Set([record.label, ...inheritedTags, ...(record.tags ?? [])])].map(
      (keyword) => keyword.toLowerCase()
    ),
    category
  };
}

function mapEmojiData(records: EmojiRecord[]): EmojiOption[] {
  const options: EmojiOption[] = [];
  const seen = new Set<string>();

  for (const record of [...records].sort((left, right) => (left.order ?? 0) - (right.order ?? 0))) {
    const variants = [record, ...(record.skins ?? [])];
    for (const variant of variants) {
      if (seen.has(variant.emoji)) continue;
      const option = makeOption(variant, record.tags);
      if (!option) continue;
      seen.add(variant.emoji);
      options.push(option);
    }
  }

  return options;
}

/** Load the full Unicode/CLDR emoji catalog only when the picker is opened. */
export function loadUnicodeEmojis(): Promise<EmojiOption[]> {
  emojiPromise ??= import('emojibase-data/en/data.json').then(({ default: records }) =>
    mapEmojiData(records as EmojiRecord[])
  );
  return emojiPromise;
}

function emojiSearchScore(emoji: EmojiOption, needle: string): number {
  if (!needle) return 3;
  if (emoji.shortcode === needle) return 0;
  if (emoji.shortcode.startsWith(needle)) return 1;
  if (emoji.keywords.some((keyword) => keyword === needle || keyword.startsWith(needle))) return 2;
  if (
    emoji.shortcode.includes(needle) ||
    emoji.name.includes(needle) ||
    emoji.keywords.some((keyword) => keyword.includes(needle))
  )
    return 3;
  return Number.POSITIVE_INFINITY;
}

export function unicodeEmojiCompletions(
  emojis: EmojiOption[],
  query: string,
  limit = 40
): CompletionOption[] {
  const needle = query.trim().toLocaleLowerCase();
  return emojis
    .map((emoji, index) => ({ emoji, index, score: emojiSearchScore(emoji, needle) }))
    .filter((item) => Number.isFinite(item.score))
    .sort((left, right) => left.score - right.score || left.index - right.index)
    .slice(0, limit)
    .map(({ emoji }) => ({
      value: emoji.value,
      label: `:${emoji.shortcode}:`,
      detail: emoji.name,
      emoji: emoji.value,
      kind: 'unicode-emoji' as const
    }));
}
import type { CompletionOption } from './completion';
