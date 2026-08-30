import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/privacy_metadata.dart';

Uint8List _jpegWithPrivateExif() {
  final privateText =
      Uint8List.fromList('GPS:51.5007,-0.1246;Owner:Alice'.codeUnits);
  final exif = Uint8List(6 + 26 + privateText.length)
    ..setRange(
        0, 16, <int>[69, 120, 105, 102, 0, 0, 73, 73, 42, 0, 8, 0, 0, 0, 1, 0]);
  final view = ByteData.sublistView(exif)
    ..setUint16(16, 0x0112, Endian.little)
    ..setUint16(18, 3, Endian.little)
    ..setUint32(20, 1, Endian.little)
    ..setUint16(24, 6, Endian.little);
  expect(view.lengthInBytes, exif.length);
  exif.setRange(32, exif.length, privateText);
  final length = exif.length + 2;
  return Uint8List.fromList(<int>[
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
    0xd9,
  ]);
}

void main() {
  test('scrub keeps JPEG pixels, size, and orientation', () {
    final source = _jpegWithPrivateExif();
    final result = scrubImageMetadataBytes(source, 'image/jpeg');

    expect(result.length, source.length);
    expect(
        result.sublist(result.length - 6), source.sublist(source.length - 6));
    expect(String.fromCharCodes(result).contains('Alice'), isFalse);
    expect(ByteData.sublistView(result).getUint16(30, Endian.little), 6);
  });

  test('non-image bytes are unchanged', () {
    final source = Uint8List.fromList(<int>[1, 2, 3]);
    expect(scrubImageMetadataBytes(source, 'application/octet-stream'), source);
  });

  test('image bytes are scrubbed when the picker has no MIME type', () {
    final result = scrubImageMetadataBytes(
        _jpegWithPrivateExif(), 'application/octet-stream');

    expect(String.fromCharCodes(result).contains('Alice'), isFalse);
    expect(ByteData.sublistView(result).getUint16(30, Endian.little), 6);
  });
}
