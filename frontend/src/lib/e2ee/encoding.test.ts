import { expect, it } from 'vitest';
import { base64url, fromBase64url } from './encoding';

it('accepts only canonical unpadded encodings within the decoded size limit', () => {
  for (const bytes of [[], [255], [251, 255], [255, 255, 255]]) {
    const encoded = base64url(Uint8Array.from(bytes));
    expect([...fromBase64url(encoded, bytes.length)]).toEqual(bytes);
    if (bytes.length) expect(() => fromBase64url(encoded, bytes.length - 1)).toThrow();
  }
  for (const invalid of ['A', '_x', '-_9', '/w', '_w==', '_w\n']) {
    expect(() => fromBase64url(invalid)).toThrow();
  }
});
