import { describe, expect, it } from 'vitest';
import { klipyGifUrl } from './gifs';

describe('klipyGifUrl', () => {
  it('accepts a standalone KLIPY HTTPS media URL', () => {
    expect(klipyGifUrl('https://media.klipy.com/example/reaction.gif')).toBe(
      'https://media.klipy.com/example/reaction.gif'
    );
  });

  it('rejects mixed message content and untrusted origins', () => {
    expect(klipyGifUrl('look https://media.klipy.com/example/reaction.gif')).toBeNull();
    expect(klipyGifUrl('https://evil.example/reaction.gif')).toBeNull();
    expect(klipyGifUrl('http://media.klipy.com/example/reaction.gif')).toBeNull();
    expect(klipyGifUrl('https://user@media.klipy.com/example/reaction.gif')).toBeNull();
  });
});
