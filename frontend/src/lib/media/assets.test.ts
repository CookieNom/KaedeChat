import { describe, expect, it } from 'vitest';
import { assetUrl } from './assets';

const hash = 'a'.repeat(64);

describe('assetUrl', () => {
  it('uses the local media route when no remote owner is supplied', () => {
    expect(assetUrl(hash, 'thumbnail_128')).toBe(`/media/assets/${hash}/thumbnail_128?v=2`);
  });

  it('uses the authoritative instance for a remote asset', () => {
    expect(assetUrl(hash, 'thumbnail_128', 'beta.example')).toBe(
      `https://beta.example/media/assets/${hash}/thumbnail_128?v=2`
    );
  });

  it('rejects values that cannot be safe route segments', () => {
    expect(assetUrl('../secret', 'thumbnail_128', 'beta.example')).toBe('');
    expect(assetUrl(hash, '../original', 'beta.example')).toBe('');
    expect(assetUrl(hash, 'thumbnail_128', 'beta.example/path')).toBe('');
  });
});
