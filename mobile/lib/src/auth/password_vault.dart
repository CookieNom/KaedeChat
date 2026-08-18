import 'dart:convert';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Stores the password-derived account-vault key in Keychain or Android
/// Keystore-backed encrypted preferences. Flutter does not expose a portable
/// non-exportable AES key handle, so protected platform storage is the closest
/// equivalent and keeps the bytes out of SharedPreferences and SQLite.
final class MobilePasswordVault {
  const MobilePasswordVault([
    this.storage = const FlutterSecureStorage(
      aOptions: AndroidOptions(encryptedSharedPreferences: true),
      iOptions: IOSOptions(
        accessibility: KeychainAccessibility.first_unlock_this_device,
        synchronizable: false,
      ),
    ),
  ]);

  final FlutterSecureStorage storage;

  Future<void> write(String accountRef, SecretKey key) async {
    final extracted = await key.extract();
    final bytes = Uint8List.fromList(extracted.bytes);
    try {
      if (bytes.length != 32) {
        throw ArgumentError('The account-vault key must contain 32 bytes.');
      }
      await storage.write(
        key: await _storageKey(accountRef),
        value: _base64url(bytes),
      );
    } finally {
      bytes.fillRange(0, bytes.length, 0);
      // SecretKeyData.extract() may return the caller-owned object itself.
      // Never destroy a key whose lifetime belongs to the caller.
      if (!identical(extracted, key)) extracted.destroy();
    }
  }

  Future<SecretKeyData?> read(String accountRef) async {
    final encoded = await storage.read(key: await _storageKey(accountRef));
    if (encoded == null) return null;
    final bytes = _decodeBase64url(encoded);
    if (bytes.length != 32) {
      bytes.fillRange(0, bytes.length, 0);
      throw const FormatException(
          'The protected account-vault key is invalid.');
    }
    return SecretKeyData(
      bytes,
      overwriteWhenDestroyed: true,
      debugLabel: 'Kaede protected account-vault key',
    );
  }

  Future<void> clear(String accountRef) async =>
      storage.delete(key: await _storageKey(accountRef));

  Future<String> _storageKey(String accountRef) async {
    if (accountRef.isEmpty || accountRef.length > 512) {
      throw ArgumentError('Invalid account reference.');
    }
    final digest = await Sha256().hash(utf8.encode(accountRef));
    return 'kaede.mobile.account-vault-key.v1.${_base64url(digest.bytes)}';
  }
}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

Uint8List _decodeBase64url(String value) {
  if (value.isEmpty ||
      value.length > 128 ||
      !RegExp(r'^[A-Za-z0-9_-]+$').hasMatch(value)) {
    throw const FormatException('Invalid protected key encoding.');
  }
  final decoded =
      Uint8List.fromList(base64Url.decode(base64Url.normalize(value)));
  if (_base64url(decoded) != value) {
    decoded.fillRange(0, decoded.length, 0);
    throw const FormatException('Invalid protected key encoding.');
  }
  return decoded;
}
