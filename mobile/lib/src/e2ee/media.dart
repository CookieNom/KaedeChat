import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:path_provider/path_provider.dart';

const _chunkSize = 256 * 1024;
const _maximumFileSize = 64 * 1024 * 1024;
const _headerSize = 41;
const _magic = <int>[0x4b, 0x41, 0x45, 0x46];

final class EncryptedMobileUpload {
  const EncryptedMobileUpload(
      {required this.attachment, required this.manifest});

  final EntityRef attachment;
  final Map<String, Object?> manifest;
}

Future<File> downloadEncryptedFile({
  required KaedeRepository repository,
  required Map<String, Object?> manifest,
  required File destination,
  String? historyMediaUrl,
  String? privateMediaUrl,
}) async {
  if (manifest['version'] != 1 || manifest['protocol'] != 'kaede-file-v1') {
    throw const FormatException('Unsupported encrypted file.');
  }
  final attachment = EntityRef(
    Snowflake('${manifest['attachment_id']}'),
    Domain('${manifest['attachment_domain']}'),
  );
  final all = Uint8List.fromList(await repository.api.getBytes(
    attachmentMediaPath(
      attachment,
      historyMediaUrl: historyMediaUrl,
      privateMediaUrl: privateMediaUrl,
    ),
  ));
  final expectedSize = (manifest['ciphertext_size'] as num?)?.toInt();
  final expectedPlainSize = (manifest['plaintext_size'] as num?)?.toInt();
  final expectedChunkSize = (manifest['chunk_size'] as num?)?.toInt();
  if (all.length != expectedSize || all.length < _headerSize) {
    throw const FormatException(
        'Encrypted file size does not match its manifest.');
  }
  final digest = _base64url((await Sha256().hash(all)).bytes);
  if (digest != manifest['ciphertext_sha256']) {
    throw const FormatException('Encrypted file was modified.');
  }
  if (!_magic.asMap().entries.every((entry) => all[entry.key] == entry.value) ||
      all[4] != 1) {
    throw const FormatException('Encrypted file header is invalid.');
  }
  final view = ByteData.sublistView(all);
  final chunkSize = view.getUint32(5, Endian.big);
  final plainSize = view.getUint64(9, Endian.big);
  if (chunkSize != expectedChunkSize || plainSize != expectedPlainSize) {
    throw const FormatException(
        'Encrypted file header does not match its manifest.');
  }
  final salt = all.sublist(17, 33);
  final noncePrefix = all.sublist(33, 41);
  final rawKey = _decode('${manifest['key']}', 32);
  final derived = await Hkdf(hmac: Hmac.sha256(), outputLength: 32).deriveKey(
    secretKey: SecretKey(rawKey),
    nonce: salt,
    info: utf8.encode('kaede attachment content v1'),
  );
  rawKey.fillRange(0, rawKey.length, 0);
  final count = (plainSize + chunkSize - 1) ~/ chunkSize;
  final output = await destination.open(mode: FileMode.write);
  final plaintextHash = Sha256().newHashSink();
  var offset = _headerSize;
  var produced = 0;
  try {
    for (var index = 0; index < count; index++) {
      if (offset + 4 > all.length) {
        throw const FormatException('Encrypted file is truncated.');
      }
      final length = view.getUint32(offset, Endian.big);
      offset += 4;
      if (length < 17 || offset + length > all.length) {
        throw const FormatException('Encrypted file chunk is invalid.');
      }
      final nonce = Uint8List(12)..setRange(0, 8, noncePrefix);
      ByteData.sublistView(nonce).setUint32(8, index, Endian.big);
      final aad = Uint8List(_headerSize + 8)
        ..setRange(0, _headerSize, all.sublist(0, _headerSize));
      final aadView = ByteData.sublistView(aad);
      aadView.setUint32(_headerSize, index, Endian.big);
      aadView.setUint32(_headerSize + 4, count, Endian.big);
      final tagStart = offset + length - 16;
      final plaintext = await AesGcm.with256bits().decrypt(
        SecretBox(
          all.sublist(offset, tagStart),
          nonce: nonce,
          mac: Mac(all.sublist(tagStart, offset + length)),
        ),
        secretKey: derived,
        aad: aad,
      );
      plaintextHash.add(plaintext);
      await output.writeFrom(plaintext);
      produced += plaintext.length;
      offset += length;
    }
    if (offset != all.length || produced != plainSize) {
      throw const FormatException('Encrypted file framing is invalid.');
    }
    plaintextHash.close();
    final plaintextDigest = _base64url((await plaintextHash.hash()).bytes);
    final expectedPlaintextDigest = manifest['plaintext_sha256'];
    if (expectedPlaintextDigest != null &&
        (expectedPlaintextDigest is! String ||
            !RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(expectedPlaintextDigest) ||
            plaintextDigest != expectedPlaintextDigest)) {
      throw const FormatException('Encrypted file plaintext was modified.');
    }
    await output.flush();
    return destination;
  } catch (_) {
    await output.close();
    if (await destination.exists()) await destination.delete();
    rethrow;
  } finally {
    if (await destination.exists()) await output.close();
    all.fillRange(0, all.length, 0);
  }
}

Future<EncryptedMobileUpload> uploadEncryptedFile({
  required KaedeRepository repository,
  required EntityRef channel,
  required File source,
  required String filename,
  required String contentType,
  int? durationMillis,
  String? waveform,
  void Function(int sent, int total)? onProgress,
}) async {
  if ((durationMillis == null) != (waveform == null)) {
    throw const FormatException(
      'Encrypted voice metadata must include duration and waveform together.',
    );
  }
  Uint8List? waveformBytes;
  if (durationMillis != null && waveform != null) {
    try {
      if (durationMillis < 1 ||
          durationMillis > 1200000 ||
          waveform.length < 4 ||
          waveform.length > 344 ||
          !RegExp(
            r'^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$',
          ).hasMatch(waveform)) {
        throw const FormatException('Encrypted voice metadata is invalid.');
      }
      waveformBytes = base64.decode(waveform);
      if (waveformBytes.isEmpty ||
          waveformBytes.length > 256 ||
          base64.encode(waveformBytes) != waveform) {
        throw const FormatException('Encrypted voice metadata is invalid.');
      }
    } on FormatException {
      throw const FormatException('Encrypted voice metadata is invalid.');
    } finally {
      waveformBytes?.fillRange(0, waveformBytes.length, 0);
    }
  }
  final encrypted = await _encryptFile(source, filename, contentType);
  try {
    final ticket = await repository.createAttachmentTicket(
      channel: channel,
      filename: 'encrypted-file',
      contentType: 'application/octet-stream',
      size: await encrypted.file.length(),
      encryptionMode: 'e2ee',
      encryptionProtocol: 'kaede-file-v1',
    );
    await repository.api.putPresignedFile(
      ticket['upload_url']! as String,
      encrypted.file,
      contentType: 'application/octet-stream',
      onProgress: onProgress,
    );
    final attachment = EntityRef(
      Snowflake('${ticket['id']}'),
      Domain('${ticket['origin_domain']}'),
    );
    return EncryptedMobileUpload(
      attachment: attachment,
      manifest: {
        ...encrypted.manifest,
        'attachment_id': attachment.id.value,
        'attachment_domain': attachment.domain.value,
        if (durationMillis != null) 'duration_millis': durationMillis,
        if (waveform != null) 'waveform': waveform,
      },
    );
  } finally {
    if (await encrypted.file.exists()) await encrypted.file.delete();
  }
}

final class _EncryptedFile {
  const _EncryptedFile(this.file, this.manifest);

  final File file;
  final Map<String, Object?> manifest;
}

Future<_EncryptedFile> _encryptFile(
  File source,
  String filename,
  String contentType,
) async {
  final size = await source.length();
  if (size < 1 || size > _maximumFileSize) {
    throw StateError('Encrypted files must be between 1 byte and 64 MiB.');
  }
  final safeFilename = filename.trim().isEmpty ? 'file' : filename.trim();
  final safeContentType =
      (contentType.isEmpty ? 'application/octet-stream' : contentType)
          .toLowerCase();
  if (safeFilename.length > 255 ||
      safeFilename.runes.any((code) => code <= 0x1f || code == 0x7f) ||
      safeContentType.length > 100 ||
      !RegExp(r'^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$')
          .hasMatch(safeContentType)) {
    throw const FormatException('Encrypted file metadata is invalid.');
  }
  final fileId = _random(16);
  final rawKey = _random(32);
  final salt = _random(16);
  final noncePrefix = _random(8);
  final header = Uint8List(_headerSize);
  header.setRange(0, 4, _magic);
  header[4] = 1;
  final headerView = ByteData.sublistView(header);
  headerView.setUint32(5, _chunkSize, Endian.big);
  headerView.setUint64(9, size, Endian.big);
  header.setRange(17, 33, salt);
  header.setRange(33, 41, noncePrefix);
  final derived = await Hkdf(hmac: Hmac.sha256(), outputLength: 32).deriveKey(
    secretKey: SecretKey(rawKey),
    nonce: salt,
    info: utf8.encode('kaede attachment content v1'),
  );
  final root = await getTemporaryDirectory();
  final output = File(
    '${root.path}/kaede-e2ee-${DateTime.now().microsecondsSinceEpoch}-${_base64url(fileId)}',
  );
  final inputHandle = await source.open();
  final outputHandle = await output.open(mode: FileMode.write);
  final hash = Sha256().newHashSink();
  final plaintextHash = Sha256().newHashSink();
  final count = (size + _chunkSize - 1) ~/ _chunkSize;
  try {
    await outputHandle.writeFrom(header);
    hash.add(header);
    for (var index = 0; index < count; index++) {
      final plaintext =
          await inputHandle.read(min(_chunkSize, size - index * _chunkSize));
      plaintextHash.add(plaintext);
      final nonce = Uint8List(12)..setRange(0, 8, noncePrefix);
      ByteData.sublistView(nonce).setUint32(8, index, Endian.big);
      final aad = Uint8List(_headerSize + 8)..setRange(0, _headerSize, header);
      final aadView = ByteData.sublistView(aad);
      aadView.setUint32(_headerSize, index, Endian.big);
      aadView.setUint32(_headerSize + 4, count, Endian.big);
      final box = await AesGcm.with256bits().encrypt(
        plaintext,
        secretKey: derived,
        nonce: nonce,
        aad: aad,
      );
      final framedLength = box.cipherText.length + box.mac.bytes.length;
      final length = ByteData(4)..setUint32(0, framedLength, Endian.big);
      final lengthBytes = length.buffer.asUint8List();
      await outputHandle.writeFrom(lengthBytes);
      await outputHandle.writeFrom(box.cipherText);
      await outputHandle.writeFrom(box.mac.bytes);
      hash
        ..add(lengthBytes)
        ..add(box.cipherText)
        ..add(box.mac.bytes);
      plaintext.fillRange(0, plaintext.length, 0);
    }
    await outputHandle.flush();
    hash.close();
    plaintextHash.close();
    final digest = await hash.hash();
    final plaintextDigest = await plaintextHash.hash();
    return _EncryptedFile(output, {
      'version': 1,
      'protocol': 'kaede-file-v1',
      'file_id': _base64url(fileId),
      'key': _base64url(rawKey),
      'filename': safeFilename,
      'content_type': safeContentType,
      'plaintext_size': size,
      'plaintext_sha256': _base64url(plaintextDigest.bytes),
      'ciphertext_size': await output.length(),
      'ciphertext_sha256': _base64url(digest.bytes),
      'chunk_size': _chunkSize,
    });
  } catch (_) {
    if (await output.exists()) await output.delete();
    rethrow;
  } finally {
    rawKey.fillRange(0, rawKey.length, 0);
    await inputHandle.close();
    await outputHandle.close();
  }
}

Uint8List _random(int length) {
  final random = Random.secure();
  return Uint8List.fromList(List.generate(length, (_) => random.nextInt(256)));
}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

Uint8List _decode(String value, int expectedLength) {
  final decoded = base64Url.decode(base64Url.normalize(value));
  if (decoded.length != expectedLength || _base64url(decoded) != value) {
    throw const FormatException('Invalid encrypted file key.');
  }
  return Uint8List.fromList(decoded);
}
