import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path_provider/path_provider.dart';

final class MobileE2EEState {
  const MobileE2EEState({
    required this.accountRef,
    required this.deviceId,
    required this.credential,
    required this.mlsState,
    this.messageCache = const <String, String>{},
  });

  factory MobileE2EEState.fromJson(Map<String, Object?> json) {
    if (json['schema'] != 1 ||
        json['account_ref'] is! String ||
        json['device_id'] is! String ||
        json['credential'] is! String ||
        json['mls_state'] is! String) {
      throw const FormatException('Invalid mobile encryption state.');
    }
    final rawCache = json['message_cache'];
    return MobileE2EEState(
      accountRef: json['account_ref']! as String,
      deviceId: json['device_id']! as String,
      credential: json['credential']! as String,
      mlsState: json['mls_state']! as String,
      messageCache: rawCache is Map
          ? rawCache.map((key, value) => MapEntry('$key', '$value'))
          : const <String, String>{},
    );
  }

  final String accountRef;
  final String deviceId;
  final String credential;
  final String mlsState;
  final Map<String, String> messageCache;

  Map<String, Object?> toJson() => {
        'schema': 1,
        'account_ref': accountRef,
        'device_id': deviceId,
        'credential': credential,
        'mls_state': mlsState,
        'message_cache': messageCache,
      };

  Map<String, Object?> toRecoveryJson() => {
        'schema': 1,
        'accountRef': accountRef,
        'deviceId': deviceId,
        'credential': credential,
        'mlsState': mlsState,
        'messageCache': messageCache,
      };

  static MobileE2EEState fromRecoveryJson(Map<String, Object?> json) {
    final cache = json['messageCache'];
    if (json['schema'] != 1 ||
        json['accountRef'] is! String ||
        json['deviceId'] is! String ||
        json['credential'] is! String ||
        json['mlsState'] is! String) {
      throw const FormatException('Invalid encryption recovery state.');
    }
    return MobileE2EEState(
      accountRef: json['accountRef']! as String,
      deviceId: json['deviceId']! as String,
      credential: json['credential']! as String,
      mlsState: json['mlsState']! as String,
      messageCache: cache is Map
          ? cache.map((key, value) => MapEntry('$key', '$value'))
          : const <String, String>{},
    );
  }
}

/// Encrypted-at-rest MLS state. The wrapping key remains in Keychain or
/// Android Keystore-backed encrypted preferences; the potentially large MLS
/// state is written atomically to app-private storage.
final class MobileE2EEStore {
  const MobileE2EEStore();

  static const _secure = FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
    iOptions: IOSOptions(
      accessibility: KeychainAccessibility.first_unlock_this_device,
      synchronizable: false,
    ),
  );
  static final _aead = AesGcm.with256bits();
  static final _recoveryKdf = Pbkdf2(
    macAlgorithm: Hmac.sha256(),
    iterations: 600000,
    bits: 256,
  );

  Future<String> _accountKey(String accountRef) async {
    final digest = await Sha256().hash(utf8.encode(accountRef));
    return _base64url(digest.bytes);
  }

  Future<File> _file(String accountRef) async {
    final root = await getApplicationSupportDirectory();
    final directory = Directory('${root.path}/e2ee');
    await directory.create(recursive: true);
    return File('${directory.path}/${await _accountKey(accountRef)}.state');
  }

  Future<SecretKey> _wrappingKey(String accountRef) async {
    final name = 'kaede.mobile.e2ee-key.v1.${await _accountKey(accountRef)}';
    final stored = await _secure.read(key: name);
    if (stored != null) {
      final bytes = _decode(stored, maximum: 32);
      if (bytes.length != 32) {
        throw StateError('The protected encryption key is invalid.');
      }
      return SecretKey(bytes);
    }
    final bytes = _random(32);
    await _secure.write(key: name, value: _base64url(bytes));
    return SecretKey(bytes);
  }

  Future<MobileE2EEState?> load(String accountRef) async {
    final file = await _file(accountRef);
    if (!await file.exists()) return null;
    final record = Map<String, Object?>.from(
      jsonDecode(await file.readAsString()) as Map,
    );
    if (record['version'] != 1) {
      throw const FormatException('Unsupported encryption state.');
    }
    final plaintext = await _aead.decrypt(
      SecretBox(
        _decode('${record['ciphertext']}', maximum: 64 * 1024 * 1024),
        nonce: _decode('${record['nonce']}', maximum: 12),
        mac: Mac(_decode('${record['mac']}', maximum: 16)),
      ),
      secretKey: await _wrappingKey(accountRef),
      aad: utf8.encode('kaede-mobile-e2ee-state-v1\u0000$accountRef'),
    );
    final state = MobileE2EEState.fromJson(
      Map<String, Object?>.from(jsonDecode(utf8.decode(plaintext)) as Map),
    );
    if (state.accountRef != accountRef) {
      throw const FormatException(
          'Encryption state belongs to another account.');
    }
    return state;
  }

  Future<void> save(MobileE2EEState state) async {
    final file = await _file(state.accountRef);
    final box = await _aead.encrypt(
      utf8.encode(jsonEncode(state.toJson())),
      secretKey: await _wrappingKey(state.accountRef),
      aad: utf8.encode('kaede-mobile-e2ee-state-v1\u0000${state.accountRef}'),
    );
    final temporary = File('${file.path}.new');
    await temporary.writeAsString(
        jsonEncode({
          'version': 1,
          'nonce': _base64url(box.nonce),
          'ciphertext': _base64url(box.cipherText),
          'mac': _base64url(box.mac.bytes),
        }),
        flush: true);
    await temporary.rename(file.path);
  }

  Future<void> clear(String accountRef) async {
    final key = 'kaede.mobile.e2ee-key.v1.${await _accountKey(accountRef)}';
    await _secure.delete(key: key);
    final file = await _file(accountRef);
    if (await file.exists()) await file.delete();
  }

  Future<String> exportRecovery(
      MobileE2EEState state, String passphrase) async {
    if (passphrase.length < 12) {
      throw ArgumentError('Use at least 12 characters.');
    }
    final salt = _random(16);
    final key = await _recoveryKdf.deriveKey(
      secretKey: SecretKey(utf8.encode(passphrase)),
      nonce: salt,
    );
    final box = await _aead.encrypt(
      utf8.encode(jsonEncode(state.toRecoveryJson())),
      secretKey: key,
      aad: utf8.encode('kaede recovery v1\u0000${state.accountRef}'),
    );
    return jsonEncode({
      'version': 1,
      'kdf': 'PBKDF2-SHA256',
      'iterations': 600000,
      'salt': _base64url(salt),
      'cipher': 'AES-256-GCM',
      'nonce': _base64url(box.nonce),
      'ciphertext': _base64url(<int>[...box.cipherText, ...box.mac.bytes]),
    });
  }

  Future<MobileE2EEState> importRecovery(
    String accountRef,
    String bundle,
    String passphrase,
  ) async {
    final record = Map<String, Object?>.from(jsonDecode(bundle) as Map);
    if (record['version'] != 1 ||
        record['kdf'] != 'PBKDF2-SHA256' ||
        record['iterations'] != 600000 ||
        record['cipher'] != 'AES-256-GCM') {
      throw const FormatException('Unsupported recovery bundle.');
    }
    final salt = _decode('${record['salt']}', maximum: 16);
    final sealed = _decode(
      '${record['ciphertext']}',
      maximum: 64 * 1024 * 1024 + 16,
    );
    if (salt.length != 16 || sealed.length < 17) {
      throw const FormatException('Invalid recovery bundle.');
    }
    final key = await _recoveryKdf.deriveKey(
      secretKey: SecretKey(utf8.encode(passphrase)),
      nonce: salt,
    );
    final plaintext = await _aead.decrypt(
      SecretBox(
        sealed.sublist(0, sealed.length - 16),
        nonce: _decode('${record['nonce']}', maximum: 12),
        mac: Mac(sealed.sublist(sealed.length - 16)),
      ),
      secretKey: key,
      aad: utf8.encode('kaede recovery v1\u0000$accountRef'),
    );
    final state = MobileE2EEState.fromRecoveryJson(
      Map<String, Object?>.from(jsonDecode(utf8.decode(plaintext)) as Map),
    );
    if (state.accountRef != accountRef) {
      throw const FormatException(
          'Recovery bundle belongs to another account.');
    }
    await save(state);
    return state;
  }
}

Uint8List _random(int length) {
  final random = Random.secure();
  return Uint8List.fromList(List.generate(length, (_) => random.nextInt(256)));
}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

Uint8List _decode(String value, {required int maximum}) {
  if (value.isEmpty || value.length > maximum * 2) {
    throw const FormatException('Invalid encoded encryption value.');
  }
  final decoded = base64Url.decode(base64Url.normalize(value));
  if (decoded.length > maximum || _base64url(decoded) != value) {
    throw const FormatException('Non-canonical encryption value.');
  }
  return Uint8List.fromList(decoded);
}
