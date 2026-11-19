import 'dart:convert';

import 'package:cryptography/cryptography.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/core/debug_log.dart';

/// AES-256-GCM for the encryption store, using the platform's cipher.
///
/// `package:cryptography`'s [AesGcm] is pure Dart and runs synchronously on
/// the calling isolate, so sealing or opening a megabyte-sized vault freezes
/// the UI and its network sockets for seconds on a phone. The platform cipher
/// does the same work in milliseconds.
///
/// The Dart cipher stays the reference implementation. The platform cipher is
/// used only after it reproduces the Dart cipher's output in this process, and
/// any platform failure, including a rejected tag, is re-evaluated in Dart so
/// callers see exactly the errors they saw before. Where the channel does not
/// exist (tests, background isolates) everything runs in Dart.
final class PlatformAesGcm {
  const PlatformAesGcm();

  static const _channel = MethodChannel('chat.kaede.mobile/aead');
  static const _tagLength = 16;
  static final _dart = AesGcm.with256bits();
  static Future<bool>? _platformVerified;

  Future<SecretBox> encrypt(
    List<int> clearText, {
    required SecretKey secretKey,
    List<int>? nonce,
    List<int> aad = const <int>[],
  }) async {
    // The nonce is always chosen here, by the same generator as before.
    final usedNonce = nonce ?? _dart.newNonce();
    if (await _usePlatform()) {
      final key = Uint8List.fromList(await secretKey.extractBytes());
      try {
        final sealed = await _invoke('seal', key, usedNonce, clearText, aad);
        if (sealed != null && sealed.length == clearText.length + _tagLength) {
          return SecretBox(
            Uint8List.sublistView(sealed, 0, clearText.length),
            nonce: usedNonce,
            mac: Mac(Uint8List.sublistView(sealed, clearText.length)),
          );
        }
      } on Object {
        // Seal with the Dart cipher below.
      } finally {
        key.fillRange(0, key.length, 0);
      }
    }
    return _dart.encrypt(
      clearText,
      secretKey: secretKey,
      nonce: usedNonce,
      aad: aad,
    );
  }

  Future<List<int>> decrypt(
    SecretBox secretBox, {
    required SecretKey secretKey,
    List<int> aad = const <int>[],
  }) async {
    final cipherText = secretBox.cipherText;
    final mac = secretBox.mac.bytes;
    if (mac.length == _tagLength && await _usePlatform()) {
      final key = Uint8List.fromList(await secretKey.extractBytes());
      try {
        final sealed = Uint8List(cipherText.length + _tagLength)
          ..setAll(0, cipherText)
          ..setAll(cipherText.length, mac);
        final opened = await _invoke('open', key, secretBox.nonce, sealed, aad);
        if (opened != null && opened.length == cipherText.length) {
          return opened;
        }
      } on Object {
        // Let the Dart cipher decide, so a bad tag raises the usual error.
      } finally {
        key.fillRange(0, key.length, 0);
      }
    }
    return _dart.decrypt(secretBox, secretKey: secretKey, aad: aad);
  }

  static Future<bool> _usePlatform() =>
      _platformVerified ??= _verifyPlatform().then((verified) {
        // The fallback is silent, so leave a trace of which cipher is in use.
        DebugLog.instance.record(
          DebugEvent.encryptionCipher,
          step: verified ? 'platform' : 'dart',
        );
        return verified;
      });

  /// Checks once per isolate that the platform cipher is interchangeable with
  /// the Dart one. A platform that disagreed on the AAD or tag layout would
  /// otherwise write vaults that no other client could open.
  static Future<bool> _verifyPlatform() async {
    try {
      final key = Uint8List.fromList(List<int>.generate(32, (i) => i));
      final nonce = List<int>.generate(12, (i) => 0xa0 + i);
      final aad = utf8.encode('kaede aead self-check');
      // Longer than one AES block and not block-aligned.
      final clearText = List<int>.generate(45, (i) => (i * 7) & 0xff);
      final reference = await _dart.encrypt(
        clearText,
        secretKey: SecretKey(key),
        nonce: nonce,
        aad: aad,
      );
      final expected = Uint8List.fromList(
        <int>[...reference.cipherText, ...reference.mac.bytes],
      );
      if (!_same(await _invoke('seal', key, nonce, clearText, aad), expected)) {
        return false;
      }
      if (!_same(await _invoke('open', key, nonce, expected, aad), clearText)) {
        return false;
      }
      final forged = Uint8List.fromList(expected);
      forged[forged.length - 1] ^= 1;
      try {
        await _invoke('open', key, nonce, forged, aad);
        return false;
      } on PlatformException {
        return true;
      }
    } on Object {
      return false;
    }
  }

  static Future<Uint8List?> _invoke(
    String method,
    List<int> key,
    List<int> nonce,
    List<int> data,
    List<int> aad,
  ) =>
      _channel.invokeMethod<Uint8List>(method, <String, Object?>{
        'key': _bytes(key),
        'nonce': _bytes(nonce),
        'data': _bytes(data),
        'aad': _bytes(aad),
      });

  // The codec sends only typed data as a byte array.
  static Uint8List _bytes(List<int> value) =>
      value is Uint8List ? value : Uint8List.fromList(value);

  static bool _same(List<int>? actual, List<int> expected) {
    if (actual == null || actual.length != expected.length) return false;
    for (var index = 0; index < expected.length; index++) {
      if (actual[index] != expected[index]) return false;
    }
    return true;
  }
}
