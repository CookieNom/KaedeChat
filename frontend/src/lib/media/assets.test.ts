import { afterEach, describe, expect, it, vi } from 'vitest';
import { assetUrl } from './assets';

const hash = 'a'.repeat(64);

describe('assetUrl', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses the local media route when no remote owner is supplied', () => {
    expect(assetUrl(hash, 'thumbnail_128')).toBe(`/media/assets/${hash}/thumbnail_128?v=2`);
  });

  it('uses the authoritative instance for a remote asset', () => {
    expect(assetUrl(hash, 'thumbnail_128', 'beta.example')).toBe(
      `https://beta.example/media/assets/${hash}/thumbnail_128?v=2`
    );
  });

  it('uses the selected home instance for ownerless assets in the desktop app', () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => (key === 'kaede.native.instance' ? 'kaede.chat' : null)
    });
    vi.stubGlobal('window', {
      __TAURI__: { core: { invoke: vi.fn() } },
      location: { hostname: '127.0.0.1' }
    });

    expect(assetUrl(hash, 'thumbnail_128')).toBe(
      `https://kaede.chat/media/assets/${hash}/thumbnail_128?v=2`
    );
  });

  it('rejects values that cannot be safe route segments', () => {
    expect(assetUrl('../secret', 'thumbnail_128', 'beta.example')).toBe('');
    expect(assetUrl(hash, '../original', 'beta.example')).toBe('');
    expect(assetUrl(hash, 'thumbnail_128', 'beta.example/path')).toBe('');
  });
});
