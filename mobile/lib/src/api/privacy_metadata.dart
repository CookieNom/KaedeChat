import 'dart:io';
import 'dart:isolate';
import 'dart:typed_data';

const _imageTypes = <String>{
  'image/jpeg',
  'image/png',
  'image/webp',
  'image/gif',
};

String? _detectedImageType(List<int> bytes) {
  if (bytes.length >= 3 &&
      bytes[0] == 0xff &&
      bytes[1] == 0xd8 &&
      bytes[2] == 0xff) {
    return 'image/jpeg';
  }
  const png = <int>[137, 80, 78, 71, 13, 10, 26, 10];
  if (bytes.length >= 8 &&
      png.asMap().entries.every((entry) => bytes[entry.key] == entry.value)) {
    return 'image/png';
  }
  if (bytes.length >= 12 &&
      String.fromCharCodes(bytes.sublist(0, 4)) == 'RIFF' &&
      String.fromCharCodes(bytes.sublist(8, 12)) == 'WEBP') {
    return 'image/webp';
  }
  if (bytes.length >= 6 &&
      <String>{'GIF87a', 'GIF89a'}
          .contains(String.fromCharCodes(bytes.sublist(0, 6)))) {
    return 'image/gif';
  }
  return null;
}

String _ascii(Uint8List bytes, int offset, int length) =>
    String.fromCharCodes(bytes.sublist(offset, offset + length));

int? _orientation(Uint8List bytes, int offset, int length) {
  var start = offset;
  if (length >= 6 && _ascii(bytes, offset, 6) == 'Exif\x00\x00') start += 6;
  if (start + 8 > offset + length) return null;
  final order = _ascii(bytes, start, 2);
  if (order != 'II' && order != 'MM') return null;
  final endian = order == 'II' ? Endian.little : Endian.big;
  final view = ByteData.sublistView(bytes);
  if (view.getUint16(start + 2, endian) != 42) return null;
  final ifd = start + view.getUint32(start + 4, endian);
  if (ifd + 2 > offset + length) return null;
  final count = view.getUint16(ifd, endian);
  if (ifd + 2 + count * 12 > offset + length) return null;
  for (var entry = ifd + 2; entry < ifd + 2 + count * 12; entry += 12) {
    if (view.getUint16(entry, endian) == 0x0112 &&
        view.getUint16(entry + 2, endian) == 3 &&
        view.getUint32(entry + 4, endian) == 1) {
      final value = view.getUint16(entry + 8, endian);
      return value >= 1 && value <= 8 ? value : null;
    }
  }
  return null;
}

Uint8List _minimalExif(int? value, {required bool prefix}) {
  final result = Uint8List((prefix ? 6 : 0) + (value == null ? 14 : 26));
  var offset = 0;
  if (prefix) {
    result.setRange(0, 6, <int>[69, 120, 105, 102, 0, 0]);
    offset = 6;
  }
  result.setRange(offset, offset + 10,
      <int>[73, 73, 42, 0, 8, 0, 0, 0, value == null ? 0 : 1, 0]);
  if (value == null) return result;
  final view = ByteData.sublistView(result);
  view
    ..setUint16(offset + 10, 0x0112, Endian.little)
    ..setUint16(offset + 12, 3, Endian.little)
    ..setUint32(offset + 14, 1, Endian.little)
    ..setUint16(offset + 18, value, Endian.little);
  return result;
}

bool _replaceExif(Uint8List bytes, int offset, int length,
    {required bool prefix}) {
  final value = _orientation(bytes, offset, length);
  bytes.fillRange(offset, offset + length, 0);
  final replacement = _minimalExif(value, prefix: prefix);
  if (replacement.length > length) return false;
  bytes.setRange(offset, offset + replacement.length, replacement);
  return true;
}

bool _jpeg(Uint8List bytes) {
  if (bytes.length < 4 || bytes[0] != 0xff || bytes[1] != 0xd8) return false;
  var offset = 2;
  while (offset + 4 <= bytes.length) {
    if (bytes[offset] != 0xff) return false;
    while (offset < bytes.length && bytes[offset] == 0xff) {
      offset += 1;
    }
    final marker = bytes[offset++];
    if (marker == 0xda || marker == 0xd9) return true;
    if (marker == 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (offset + 2 > bytes.length) return false;
    final length = bytes[offset] << 8 | bytes[offset + 1];
    if (length < 2 || offset + length > bytes.length) return false;
    final dataOffset = offset + 2;
    final dataLength = length - 2;
    if (marker == 0xe1) {
      if (dataLength >= 6 && _ascii(bytes, dataOffset, 6) == 'Exif\x00\x00') {
        _replaceExif(bytes, dataOffset, dataLength, prefix: true);
      } else {
        bytes.fillRange(dataOffset, dataOffset + dataLength, 0);
      }
    } else if (marker == 0xed || marker == 0xfe) {
      bytes.fillRange(dataOffset, dataOffset + dataLength, 0);
    }
    offset += length;
  }
  return false;
}

Uint32List? _crcTable;
int _crc32(Uint8List bytes, int offset, int length) {
  _crcTable ??= Uint32List.fromList(List<int>.generate(256, (index) {
    var value = index;
    for (var bit = 0; bit < 8; bit++) {
      value = value & 1 != 0 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    return value;
  }));
  var crc = 0xffffffff;
  for (var index = offset; index < offset + length; index++) {
    crc = _crcTable![(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) & 0xffffffff;
}

bool _png(Uint8List bytes) {
  const signature = <int>[137, 80, 78, 71, 13, 10, 26, 10];
  if (bytes.length < 12 ||
      !signature
          .asMap()
          .entries
          .every((entry) => bytes[entry.key] == entry.value)) {
    return false;
  }
  final view = ByteData.sublistView(bytes);
  var offset = 8;
  while (offset + 12 <= bytes.length) {
    final length = view.getUint32(offset);
    final end = offset + 12 + length;
    if (end > bytes.length) return false;
    final type = _ascii(bytes, offset + 4, 4);
    final dataOffset = offset + 8;
    if (type == 'eXIf') {
      _replaceExif(bytes, dataOffset, length, prefix: false);
      view.setUint32(
          offset + 8 + length, _crc32(bytes, offset + 4, length + 4));
    } else if (<String>{'tEXt', 'zTXt', 'iTXt', 'tIME'}.contains(type)) {
      bytes
        ..setRange(offset + 4, offset + 8, <int>[112, 114, 73, 86])
        ..fillRange(dataOffset, dataOffset + length, 0);
      view.setUint32(
          offset + 8 + length, _crc32(bytes, offset + 4, length + 4));
    }
    offset = end;
    if (type == 'IEND') return end == bytes.length;
  }
  return false;
}

bool _webp(Uint8List bytes) {
  if (bytes.length < 12 ||
      _ascii(bytes, 0, 4) != 'RIFF' ||
      _ascii(bytes, 8, 4) != 'WEBP') {
    return false;
  }
  final view = ByteData.sublistView(bytes);
  if (view.getUint32(4, Endian.little) + 8 != bytes.length) return false;
  var offset = 12;
  var keptExif = false;
  var flagsOffset = -1;
  while (offset + 8 <= bytes.length) {
    final type = _ascii(bytes, offset, 4);
    final length = view.getUint32(offset + 4, Endian.little);
    final end = offset + 8 + length + (length & 1);
    if (end > bytes.length) return false;
    if (type == 'VP8X' && length >= 1) flagsOffset = offset + 8;
    if (type == 'EXIF') {
      keptExif = _replaceExif(bytes, offset + 8, length,
          prefix:
              length >= 6 && _ascii(bytes, offset + 8, 6) == 'Exif\x00\x00');
      if (!keptExif) bytes.setRange(offset, offset + 4, <int>[74, 85, 78, 75]);
      if (length.isOdd) bytes[offset + 8 + length] = 0;
    } else if (type == 'XMP ') {
      bytes
        ..setRange(offset, offset + 4, <int>[74, 85, 78, 75])
        ..fillRange(offset + 8, offset + 8 + length, 0);
      if (length.isOdd) bytes[offset + 8 + length] = 0;
    }
    offset = end;
  }
  if (flagsOffset >= 0) {
    bytes[flagsOffset] = bytes[flagsOffset] & ~0x0c | (keptExif ? 0x08 : 0);
  }
  return offset == bytes.length;
}

bool _gif(Uint8List bytes) {
  if (bytes.length < 13 ||
      !<String>{'GIF87a', 'GIF89a'}.contains(_ascii(bytes, 0, 6))) {
    return false;
  }
  var offset =
      13 + (bytes[10] & 0x80 != 0 ? 3 * (1 << ((bytes[10] & 7) + 1)) : 0);
  while (offset < bytes.length) {
    final marker = bytes[offset++];
    if (marker == 0x3b) return offset == bytes.length;
    if (marker == 0x2c) {
      if (offset + 9 > bytes.length) return false;
      final packed = bytes[offset + 8];
      offset += 9 + (packed & 0x80 != 0 ? 3 * (1 << ((packed & 7) + 1)) : 0);
      if (offset >= bytes.length) return false;
      offset += 1;
      while (offset < bytes.length && bytes[offset] != 0) {
        offset += 1 + bytes[offset];
      }
      if (offset >= bytes.length) return false;
      offset += 1;
      continue;
    }
    if (marker != 0x21 || offset >= bytes.length) return false;
    final label = bytes[offset++];
    final headerLength = bytes[offset++];
    if (offset + headerLength > bytes.length) return false;
    final xmp = label == 0xff &&
        _ascii(bytes, offset, headerLength).startsWith('XMP DataXMP');
    if (label == 0xfe || xmp) bytes.fillRange(offset, offset + headerLength, 0);
    offset += headerLength;
    while (offset < bytes.length && bytes[offset] != 0) {
      final length = bytes[offset++];
      if (offset + length > bytes.length) return false;
      if (label == 0xfe || xmp) bytes.fillRange(offset, offset + length, 0);
      offset += length;
    }
    if (offset >= bytes.length) return false;
    offset += 1;
  }
  return false;
}

Uint8List scrubImageMetadataBytes(List<int> source, String contentType) {
  if (!_imageTypes.contains(contentType)) {
    contentType = _detectedImageType(source) ?? contentType;
  }
  if (!_imageTypes.contains(contentType)) return Uint8List.fromList(source);
  final result = Uint8List.fromList(source);
  final recognized = switch (contentType) {
    'image/jpeg' => _jpeg(result),
    'image/png' => _png(result),
    'image/webp' => _webp(result),
    _ => _gif(result),
  };
  if (!recognized) {
    throw const FormatException(
        'The selected image format could not be prepared safely.');
  }
  return result;
}

Future<Uint8List> scrubImageMetadata(List<int> source, String contentType) =>
    Isolate.run(() => scrubImageMetadataBytes(source, contentType));

final class PreparedImageFile {
  const PreparedImageFile(this.file, this.temporary);

  final File file;
  final bool temporary;

  Future<void> dispose() async {
    if (!temporary) return;
    if (await file.exists()) await file.delete();
    if (await file.parent.exists()) await file.parent.delete();
  }
}

Future<PreparedImageFile> prepareImageFile(
    File source, String contentType) async {
  if (!_imageTypes.contains(contentType)) {
    final handle = await source.open();
    try {
      contentType = _detectedImageType(await handle.read(12)) ?? contentType;
    } finally {
      await handle.close();
    }
    if (!_imageTypes.contains(contentType)) {
      return PreparedImageFile(source, false);
    }
  }
  final bytes =
      await scrubImageMetadata(await source.readAsBytes(), contentType);
  final directory = await Directory.systemTemp.createTemp('kaede-image-');
  final output = File('${directory.path}/upload');
  await output.writeAsBytes(bytes, flush: true);
  return PreparedImageFile(output, true);
}
