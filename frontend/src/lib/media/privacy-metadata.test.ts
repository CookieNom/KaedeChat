import { describe, expect, it } from 'vitest';

import { scrubImageMetadataBytes } from './privacy-metadata';

function jpegWithPrivateExif(): Uint8Array {
  const privateText = new TextEncoder().encode('GPS:51.5007,-0.1246;Owner:Alice');
  const exif = new Uint8Array(6 + 26 + privateText.length);
  exif.set([69, 120, 105, 102, 0, 0, 73, 73, 42, 0, 8, 0, 0, 0, 1, 0]);
  const view = new DataView(exif.buffer);
  view.setUint16(16, 0x0112, true);
  view.setUint16(18, 3, true);
  view.setUint32(20, 1, true);
  view.setUint16(24, 6, true);
  exif.set(privateText, 32);
  const length = exif.length + 2;
  return Uint8Array.from([
    0xff,
    0xd8,
    0xff,
    0xe1,
    length >> 8,
    length & 0xff,
    ...exif,
    0xff,
    0xda,
    0,
    2,
    11,
    22,
    33,
    0xff,
    0xd9
  ]);
}

describe('image privacy metadata', () => {
  it('preserves JPEG pixels, size, and orientation while clearing private EXIF', () => {
    const source = jpegWithPrivateExif();
    const result = scrubImageMetadataBytes(source, 'image/jpeg');

    expect(result).toHaveLength(source.length);
    expect(result.slice(-6)).toEqual(source.slice(-6));
    expect(new TextDecoder().decode(result)).not.toContain('Alice');
    expect(new DataView(result.buffer).getUint16(30, true)).toBe(6);
  });

  it('does not touch non-image uploads', () => {
    const source = Uint8Array.from([1, 2, 3]);
    expect(scrubImageMetadataBytes(source, 'application/octet-stream')).toBe(source);
  });

  it('scrubs images detected from bytes when the picker has no MIME type', () => {
    const source = jpegWithPrivateExif();
    const result = scrubImageMetadataBytes(source, 'application/octet-stream');

    expect(new TextDecoder().decode(result)).not.toContain('Alice');
    expect(new DataView(result.buffer).getUint16(30, true)).toBe(6);
  });
});
