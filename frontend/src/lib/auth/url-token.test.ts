import { afterEach, describe, expect, it, vi } from 'vitest';

import { consumeUrlToken } from './url-token';

describe('one-time URL credentials', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('prefers a fragment token and removes every URL copy before returning it', () => {
    const replaceState = vi.fn();
    const state = { navigation: 1 };
    vi.stubGlobal('window', {
      location: {
        href: 'https://chat.example/reset-password?token=legacy&keep=1#token=secret&next=%2Fhome'
      },
      history: { state, replaceState }
    });

    expect(consumeUrlToken()).toBe('secret');
    expect(replaceState).toHaveBeenCalledWith(state, '', '/reset-password?keep=1#next=%2Fhome');
    expect(JSON.stringify(replaceState.mock.calls)).not.toContain('secret');
    expect(JSON.stringify(replaceState.mock.calls)).not.toContain('legacy');
  });

  it('leaves history untouched when the credential is absent', () => {
    const replaceState = vi.fn();
    vi.stubGlobal('window', {
      location: { href: 'https://chat.example/verify?keep=1' },
      history: { state: null, replaceState }
    });

    expect(consumeUrlToken()).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });
});
