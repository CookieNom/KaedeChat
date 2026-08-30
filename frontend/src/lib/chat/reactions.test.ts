import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  canonicalReactionEmoji,
  reactionEmojiPresentation,
  reactionToggleState,
  recentReactions,
  rememberReaction
} from './reactions';

class MemoryStorage implements Storage {
  #values = new Map<string, string>();

  get length(): number {
    return this.#values.size;
  }

  clear(): void {
    this.#values.clear();
  }

  getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.#values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.#values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }
}

let storage: MemoryStorage;

beforeEach(() => {
  storage = new MemoryStorage();
  vi.stubGlobal('localStorage', storage);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('reaction picker identity', () => {
  it('emits the backend-canonical key for selector-bearing Unicode emoji', () => {
    expect(canonicalReactionEmoji('❤️')).toBe('❤');
    expect(canonicalReactionEmoji('1️⃣')).toBe('1⃣');
    expect(canonicalReactionEmoji('🏳️‍⚧️')).toBe('🏳‍⚧');
  });

  it('preserves qualified custom tokens while canonicalizing and validating their origin', () => {
    expect(canonicalReactionEmoji('<a:Party_blob:75512661369970689@CHAT.Example..>')).toBe(
      '<a:Party_blob:75512661369970689@chat.example>'
    );
    expect(canonicalReactionEmoji('<:party:9223372036854775808@chat.example>')).toBeNull();
    expect(canonicalReactionEmoji('<:x:7@chat.example>')).toBeNull();
    expect(canonicalReactionEmoji('<:party:7@localhost>')).toBeNull();
  });
});

describe('reaction presentation', () => {
  it('restores qualified color presentation without changing API identity', async () => {
    const presentation = await reactionEmojiPresentation('❤');

    expect(presentation).toBe('❤️');
    expect(canonicalReactionEmoji(presentation)).toBe('❤');
    expect(
      reactionToggleState({ reaction_counts: {}, reacted_emoji: [] }, presentation)?.emoji
    ).toBe('❤');
  });

  it('restores selectors inside a qualified ZWJ sequence', async () => {
    const presentation = await reactionEmojiPresentation('🏳‍⚧');

    expect(presentation).toBe('🏳️‍⚧️');
    expect(canonicalReactionEmoji(presentation)).toBe('🏳‍⚧');
  });

  it('leaves canonical custom emoji tokens available for asset rendering', async () => {
    const custom = '<a:party:75512661369970689@emoji.example>';
    expect(await reactionEmojiPresentation(custom)).toBe(custom);
  });
});

describe('reaction toggle state', () => {
  it('removes a canonical heart when the picker supplies its VS16 form', () => {
    expect(
      reactionToggleState({ reaction_counts: { '❤': 1 }, reacted_emoji: ['❤'] }, '❤️')
    ).toEqual({ emoji: '❤', remove: true, exists: true });
  });

  it('matches legacy selector-bearing state without creating a second reaction', () => {
    expect(
      reactionToggleState({ reaction_counts: { '❤️': 2 }, reacted_emoji: ['❤️'] }, '❤')
    ).toEqual({ emoji: '❤', remove: true, exists: true });
  });
});

describe('reaction recents migration', () => {
  it('canonicalizes and deduplicates legacy values while preserving usage rank', () => {
    storage.setItem(
      'kaede:reaction-recents:7@chat.example',
      JSON.stringify([
        { value: '❤️', count: 2, lastUsed: 10 },
        { value: '❤', count: 3, lastUsed: 20 },
        { value: '<:party:7@CHAT.Example.>', count: 4, lastUsed: 30 },
        { value: 'not an emoji', count: 99, lastUsed: 40 }
      ])
    );

    expect(recentReactions('7@chat.example')).toEqual(['❤', '<:party:7@chat.example>', '😂', '👍']);
    expect(JSON.parse(storage.getItem('kaede:reaction-recents:7@chat.example') ?? '[]')).toEqual([
      { value: '❤', count: 5, lastUsed: 20 },
      { value: '<:party:7@chat.example>', count: 4, lastUsed: 30 }
    ]);
  });

  it('stores picker values canonically and uses a selector-free default heart', () => {
    vi.spyOn(Date, 'now').mockReturnValue(50);
    rememberReaction('7@chat.example', '❤️');
    rememberReaction('7@chat.example', '❤');

    expect(JSON.parse(storage.getItem('kaede:reaction-recents:7@chat.example') ?? '[]')).toEqual([
      { value: '❤', count: 2, lastUsed: 50 }
    ]);
    storage.clear();
    expect(recentReactions('7@chat.example')[0]).toBe('❤');
  });
});
