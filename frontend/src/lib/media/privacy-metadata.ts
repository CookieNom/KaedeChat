const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];
const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];

function ascii(bytes: Uint8Array, offset: number, length: number): string {
  return String.fromCharCode(...bytes.subarray(offset, offset + length));
}

function detectedImageType(bytes: Uint8Array): string | null {
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff)
    return 'image/jpeg';
  if (bytes.length >= 8 && PNG_SIGNATURE.every((value, index) => bytes[index] === value))
    return 'image/png';
  if (bytes.length >= 12 && ascii(bytes, 0, 4) === 'RIFF' && ascii(bytes, 8, 4) === 'WEBP')
    return 'image/webp';
  if (bytes.length >= 6 && ['GIF87a', 'GIF89a'].includes(ascii(bytes, 0, 6))) return 'image/gif';
  return null;
}

function orientation(bytes: Uint8Array, offset: number, length: number): number | null {
  let start = offset;
  if (length >= 6 && ascii(bytes, offset, 6) === 'Exif\0\0') start += 6;
  if (start + 8 > offset + length) return null;
  const little = ascii(bytes, start, 2) === 'II';
  if (!little && ascii(bytes, start, 2) !== 'MM') return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const read16 = (at: number) => view.getUint16(at, little);
  const read32 = (at: number) => view.getUint32(at, little);
  if (read16(start + 2) !== 42) return null;
  const ifd = start + read32(start + 4);
  if (ifd + 2 > offset + length) return null;
  const count = read16(ifd);
  if (ifd + 2 + count * 12 > offset + length) return null;
  for (let entry = ifd + 2; entry < ifd + 2 + count * 12; entry += 12) {
    if (read16(entry) === 0x0112 && read16(entry + 2) === 3 && read32(entry + 4) === 1) {
      const value = read16(entry + 8);
      return value >= 1 && value <= 8 ? value : null;
    }
  }
  return null;
}

function minimalExif(value: number | null, prefix: boolean): Uint8Array {
  const result = new Uint8Array((prefix ? 6 : 0) + (value === null ? 14 : 26));
  let offset = 0;
  if (prefix) {
    result.set([69, 120, 105, 102, 0, 0]);
    offset = 6;
  }
  result.set([73, 73, 42, 0, 8, 0, 0, 0, value === null ? 0 : 1, 0], offset);
  if (value === null) return result;
  const view = new DataView(result.buffer);
  view.setUint16(offset + 10, 0x0112, true);
  view.setUint16(offset + 12, 3, true);
  view.setUint32(offset + 14, 1, true);
  view.setUint16(offset + 18, value, true);
  return result;
}

function replaceExif(bytes: Uint8Array, offset: number, length: number, prefix: boolean): boolean {
  const value = orientation(bytes, offset, length);
  bytes.fill(0, offset, offset + length);
  const replacement = minimalExif(value, prefix);
  if (replacement.length > length) return false;
  bytes.set(replacement, offset);
  return true;
}

function scrubJpeg(bytes: Uint8Array): boolean {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return false;
  let offset = 2;
  while (offset + 4 <= bytes.length) {
    if (bytes[offset] !== 0xff) return false;
    while (offset < bytes.length && bytes[offset] === 0xff) offset++;
    const marker = bytes[offset++];
    if (marker === 0xda || marker === 0xd9) return true;
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (offset + 2 > bytes.length) return false;
    const length = (bytes[offset] << 8) | bytes[offset + 1];
    if (length < 2 || offset + length > bytes.length) return false;
    const dataOffset = offset + 2;
    const dataLength = length - 2;
    if (marker === 0xe1) {
      if (dataLength >= 6 && ascii(bytes, dataOffset, 6) === 'Exif\0\0') {
        replaceExif(bytes, dataOffset, dataLength, true);
      } else {
        bytes.fill(0, dataOffset, dataOffset + dataLength);
      }
    } else if (marker === 0xed || marker === 0xfe) {
      bytes.fill(0, dataOffset, dataOffset + dataLength);
    }
    offset += length;
  }
  return false;
}

let crcTable: Uint32Array | undefined;
function crc32(bytes: Uint8Array, offset: number, length: number): number {
  crcTable ??= Uint32Array.from({ length: 256 }, (_, index) => {
    let value = index;
    for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    return value >>> 0;
  });
  let crc = 0xffffffff;
  for (let index = offset; index < offset + length; index++) {
    crc = crcTable[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function scrubPng(bytes: Uint8Array): boolean {
  if (bytes.length < 12 || !PNG_SIGNATURE.every((value, index) => bytes[index] === value))
    return false;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 8;
  while (offset + 12 <= bytes.length) {
    const length = view.getUint32(offset);
    const end = offset + 12 + length;
    if (end > bytes.length) return false;
    const type = ascii(bytes, offset + 4, 4);
    const dataOffset = offset + 8;
    if (type === 'eXIf') {
      replaceExif(bytes, dataOffset, length, false);
      view.setUint32(offset + 8 + length, crc32(bytes, offset + 4, length + 4));
    } else if (['tEXt', 'zTXt', 'iTXt', 'tIME'].includes(type)) {
      bytes.set([112, 114, 73, 86], offset + 4);
      bytes.fill(0, dataOffset, dataOffset + length);
      view.setUint32(offset + 8 + length, crc32(bytes, offset + 4, length + 4));
    }
    offset = end;
    if (type === 'IEND') return end === bytes.length;
  }
  return false;
}

function scrubWebp(bytes: Uint8Array): boolean {
  if (bytes.length < 12 || ascii(bytes, 0, 4) !== 'RIFF' || ascii(bytes, 8, 4) !== 'WEBP')
    return false;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(4, true) + 8 !== bytes.length) return false;
  let offset = 12;
  let keptExif = false;
  let flagsOffset = -1;
  while (offset + 8 <= bytes.length) {
    const type = ascii(bytes, offset, 4);
    const length = view.getUint32(offset + 4, true);
    const end = offset + 8 + length + (length & 1);
    if (end > bytes.length) return false;
    if (type === 'VP8X' && length >= 1) flagsOffset = offset + 8;
    if (type === 'EXIF') {
      keptExif = replaceExif(bytes, offset + 8, length, ascii(bytes, offset + 8, 6) === 'Exif\0\0');
      if (!keptExif) bytes.set([74, 85, 78, 75], offset);
      if (length & 1) bytes[offset + 8 + length] = 0;
    } else if (type === 'XMP ') {
      bytes.set([74, 85, 78, 75], offset);
      bytes.fill(0, offset + 8, offset + 8 + length);
      if (length & 1) bytes[offset + 8 + length] = 0;
    }
    offset = end;
  }
  if (flagsOffset >= 0) bytes[flagsOffset] = (bytes[flagsOffset] & ~0x0c) | (keptExif ? 0x08 : 0);
  return offset === bytes.length;
}

function scrubGif(bytes: Uint8Array): boolean {
  if (bytes.length < 13 || !['GIF87a', 'GIF89a'].includes(ascii(bytes, 0, 6))) return false;
  let offset = 13 + (bytes[10] & 0x80 ? 3 * (1 << ((bytes[10] & 7) + 1)) : 0);
  while (offset < bytes.length) {
    const marker = bytes[offset++];
    if (marker === 0x3b) return offset === bytes.length;
    if (marker === 0x2c) {
      if (offset + 9 > bytes.length) return false;
      const packed = bytes[offset + 8];
      offset += 9 + (packed & 0x80 ? 3 * (1 << ((packed & 7) + 1)) : 0);
      if (offset >= bytes.length) return false;
      offset++;
      while (offset < bytes.length && bytes[offset] !== 0) offset += 1 + bytes[offset];
      if (offset >= bytes.length) return false;
      offset++;
      continue;
    }
    if (marker !== 0x21 || offset >= bytes.length) return false;
    const label = bytes[offset++];
    const headerLength = bytes[offset++];
    if (offset + headerLength > bytes.length) return false;
    const xmp = label === 0xff && ascii(bytes, offset, headerLength).startsWith('XMP DataXMP');
    if (label === 0xfe || xmp) bytes.fill(0, offset, offset + headerLength);
    offset += headerLength;
    while (offset < bytes.length && bytes[offset] !== 0) {
      const length = bytes[offset++];
      if (offset + length > bytes.length) return false;
      if (label === 0xfe || xmp) bytes.fill(0, offset, offset + length);
      offset += length;
    }
    if (offset >= bytes.length) return false;
    offset++;
  }
  return false;
}

export function scrubImageMetadataBytes(source: Uint8Array, contentType: string): Uint8Array {
  contentType = IMAGE_TYPES.includes(contentType)
    ? contentType
    : (detectedImageType(source) ?? contentType);
  if (!IMAGE_TYPES.includes(contentType)) return source;
  const result = source.slice();
  const recognized =
    contentType === 'image/jpeg'
      ? scrubJpeg(result)
      : contentType === 'image/png'
        ? scrubPng(result)
        : contentType === 'image/webp'
          ? scrubWebp(result)
          : scrubGif(result);
  if (!recognized) throw new Error('The selected image format could not be prepared safely.');
  return result;
}

export async function scrubImageMetadata(
  file: Blob & { name?: string; type: string }
): Promise<File> {
  const contentType = IMAGE_TYPES.includes(file.type)
    ? file.type
    : detectedImageType(new Uint8Array(await file.slice(0, 12).arrayBuffer()));
  if (contentType === null) {
    return file instanceof File
      ? file
      : new File([file], file.name ?? 'upload', { type: file.type });
  }
  const bytes = scrubImageMetadataBytes(new Uint8Array(await file.arrayBuffer()), contentType);
  return new File([bytes.buffer as ArrayBuffer], file.name ?? 'upload', {
    type: file.type,
    lastModified: file instanceof File ? file.lastModified : Date.now()
  });
}
