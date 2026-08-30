import { describe, expect, it } from 'vitest';
import {
  stickerFromToken,
  stickerItem,
  stickerOptions,
  stickerToken,
  stickerUrl
} from './stickers';

describe('guild sticker identity', () => {
  const sticker = {
    id: '75512661369970688',
    origin_domain: 'CHAT.Example',
    guild_id: '10',
    guild_domain: 'chat.example',
    guild_name: 'Cats',
    name: 'hello_wave',
    description: null,
    animated: false,
    media_hash: 'a'.repeat(64)
  };

  it('creates and parses a stable federation token', () => {
    const token = stickerToken(sticker);
    expect(token).toBe('<sticker:hello_wave:75512661369970688@chat.example>');
    expect(stickerFromToken(token)).toEqual({
      name: 'hello_wave',
      id: '75512661369970688',
      domain: 'chat.example'
    });
  });

  it('rejects malformed identities and creates remote URLs', () => {
    expect(stickerToken({ ...sticker, id: '0' })).toBe('');
    expect(stickerFromToken('<sticker:no:0@chat.example>')).toBeNull();
    expect(stickerUrl(sticker.id, 'CHAT.Example')).toBe(
      'https://chat.example/media/stickers/75512661369970688/thumbnail_512'
    );
  });

  it('sorts the active guild before other guilds', () => {
    const other = { ...sticker, id: '75512661369970689', guild_id: '11', guild_name: 'Alpha' };
    expect(
      stickerOptions([other, sticker], { id: '10', origin_domain: 'chat.example' })[0].id
    ).toBe(sticker.id);
  });

  it('builds the Discord message sticker reference and immutable snapshot', () => {
    const option = stickerOptions([sticker])[0];
    expect(option.value).toBe('75512661369970688@chat.example');
    expect(stickerItem(option)).toEqual({
      id: sticker.id,
      origin_domain: 'chat.example',
      name: sticker.name,
      format_type: 1,
      media_hash: sticker.media_hash
    });
  });
});
