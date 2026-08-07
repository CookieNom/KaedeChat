export type EmojiCategory =
  'people' | 'nature' | 'food' | 'activity' | 'travel' | 'objects' | 'symbols' | 'flags';

export interface EmojiOption {
  value: string;
  name: string;
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
