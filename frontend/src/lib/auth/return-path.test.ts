import { describe, expect, it } from 'vitest';
import { safeReturnPath } from './return-path';

describe('post-login return paths', () => {
  it('accepts application paths on the current origin', () => {
    expect(safeReturnPath('/invite/Ab12?from=mail', 'https://chat.example')).toBe(
      '/invite/Ab12?from=mail'
    );
    expect(safeReturnPath('/g/1/2', 'https://chat.example')).toBe('/g/1/2');
  });

  it('rejects authorities, backslash authorities, and unrelated paths', () => {
    expect(safeReturnPath('//evil.example', 'https://chat.example')).toBeNull();
    expect(safeReturnPath('/\\evil.example', 'https://chat.example')).toBeNull();
    expect(safeReturnPath('/logout', 'https://chat.example')).toBeNull();
  });
});
