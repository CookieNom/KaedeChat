import { describe, expect, it } from 'vitest';
import { customEmojiToken, customEmojiUrl, emojiCategories, loadUnicodeEmojis } from './emojis';

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

describe('custom emoji identity', () => {
  it('creates deterministic static and animated federation tokens', () => {
    expect(
      customEmojiToken({
        id: '75512661369970688',
        origin_domain: 'CHAT.Example',
        name: 'party_blob'
      })
    ).toBe('<:party_blob:75512661369970688@chat.example>');
    expect(
      customEmojiToken({
        id: '75512661369970689',
        origin_domain: 'chat.example',
        name: 'dance',
        animated: true
      })
    ).toBe('<a:dance:75512661369970689@chat.example>');
  });

  it('rejects malformed identities before inserting them into a message', () => {
    expect(customEmojiToken({ id: '0', origin_domain: 'chat.example', name: 'party' })).toBe('');
    expect(customEmojiToken({ id: '123', origin_domain: 'bad/path', name: 'party' })).toBe('');
    expect(customEmojiUrl('123', 'bad/path')).toBe('');
  });

  it('uses the emoji origin for immutable federated media URLs', () => {
    expect(customEmojiUrl('75512661369970689', 'CHAT.Example')).toBe(
      'https://chat.example/media/emojis/75512661369970689/thumbnail_128'
    );
  });
});
