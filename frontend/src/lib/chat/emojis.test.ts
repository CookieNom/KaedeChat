import { describe, expect, it } from 'vitest';

import { emojiCategories, loadUnicodeEmojis } from './emojis';

describe('emoji catalog', () => {
  it('loads the complete catalog with variants and flags', async () => {
    const emojis = await loadUnicodeEmojis();
    const values = new Set(emojis.map((emoji) => emoji.value));

    expect(emojis.length).toBeGreaterThan(3900);
    expect(values.size).toBe(emojis.length);
    expect(values.has('😀')).toBe(true);
    expect(values.has('👍🏿')).toBe(true);
    expect(values.has('👩‍💻')).toBe(true);
    expect(values.has('🏳️‍⚧️')).toBe(true);
    expect(values.has('🇺🇸')).toBe(true);
  });

  it('provides searchable annotations for every supported category', async () => {
    const emojis = await loadUnicodeEmojis();
    const categories = new Set(emojis.map((emoji) => emoji.category));

    expect(categories).toEqual(new Set(emojiCategories.map((category) => category.id)));
    expect(
      emojis.some(
        (emoji) => emoji.value === '😀' && emoji.keywords.includes('happy') && emoji.name.length > 0
      )
    ).toBe(true);
  });
});
