import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:kaede_mobile/src/core/refs.dart';

const mobilePasswordKdfVersion = 2;
const mobilePasswordKdfAlgorithm = 'PBKDF2-SHA256';
const mobilePasswordKdfIterations = 600000;
const _mobilePasswordKdfDomain = 'kaede-password-kdf-v2';

sealed class MobilePasswordKdfContext {
  const MobilePasswordKdfContext({required this.vaultSalt});

  factory MobilePasswordKdfContext.fromJson(Map<String, Object?> json) {
    final version = json['version'];
    final vaultSalt = json['vault_salt'];
    if (vaultSalt is! String) {
      throw const FormatException('The server returned an invalid vault salt.');
    }
    _decodeBase64url(vaultSalt, expectedLength: 16);
    if (version == mobilePasswordKdfVersion &&
        json['algorithm'] == mobilePasswordKdfAlgorithm &&
        json['iterations'] == mobilePasswordKdfIterations &&
        json['auth_salt'] is String) {
      final authSalt = json['auth_salt']! as String;
      _decodeBase64url(authSalt, expectedLength: 16);
      return ModernMobilePasswordKdfContext(
        authSalt: authSalt,
        vaultSalt: vaultSalt,
      );
    }
    if (version == 0 &&
        json['algorithm'] == 'legacy' &&
        json['iterations'] == 0 &&
        json['auth_salt'] == null) {
      return LegacyMobilePasswordKdfContext(vaultSalt: vaultSalt);
    }
    throw const FormatException(
      'This server uses an unsupported password protection scheme.',
    );
  }

  int get version;
  final String vaultSalt;
}

final class ModernMobilePasswordKdfContext extends MobilePasswordKdfContext {
  const ModernMobilePasswordKdfContext({
    required this.authSalt,
    required super.vaultSalt,
  });

  @override
  int get version => mobilePasswordKdfVersion;
  final String authSalt;

  Map<String, Object?> toJson() => <String, Object?>{
        'version': mobilePasswordKdfVersion,
        'algorithm': mobilePasswordKdfAlgorithm,
        'iterations': mobilePasswordKdfIterations,
        'auth_salt': authSalt,
        'vault_salt': vaultSalt,
      };

  Map<String, Object?> toAuthenticationJson() => <String, Object?>{
        'version': mobilePasswordKdfVersion,
        'algorithm': mobilePasswordKdfAlgorithm,
        'iterations': mobilePasswordKdfIterations,
        'auth_salt': authSalt,
      };
}

final class LegacyMobilePasswordKdfContext extends MobilePasswordKdfContext {
  const LegacyMobilePasswordKdfContext({required super.vaultSalt});

  @override
  int get version => 0;
}

final class PreparedMobilePassword {
  const PreparedMobilePassword({
    required this.authenticationSecret,
    required this.vaultKey,
    required this.context,
    this.passwordUpgrade,
  });

  final String authenticationSecret;
  final SecretKeyData vaultKey;
  final MobilePasswordKdfContext context;
  final Map<String, Object?>? passwordUpgrade;

  void destroy() => vaultKey.destroy();
}

final class PreparedMobileResetPassword {
  const PreparedMobileResetPassword({
    required this.authenticationSecret,
    required this.passwordKdf,
  });

  final String authenticationSecret;
  final Map<String, Object?> passwordKdf;
}

Future<PreparedMobilePassword> prepareMobilePassword(
  String password,
  MobilePasswordKdfContext context,
  Domain instance,
) async {
  _validatePassword(password);
  final material = _passwordMaterial(password);
  try {
    final vaultKey = await _deriveKey(
      material,
      context.vaultSalt,
      instance,
      purpose: 'vault',
    );
    if (context case final ModernMobilePasswordKdfContext modern) {
      try {
        return PreparedMobilePassword(
          authenticationSecret: await _authenticationSecret(
            material,
            modern.authSalt,
            instance,
          ),
          vaultKey: vaultKey,
          context: context,
        );
      } on Object {
        vaultKey.destroy();
        rethrow;
      }
    }
    final upgradeSalt = _randomBytes(16);
    try {
      final upgraded = ModernMobilePasswordKdfContext(
        authSalt: _base64url(upgradeSalt),
        vaultSalt: context.vaultSalt,
      );
      return PreparedMobilePassword(
        authenticationSecret: password,
        vaultKey: vaultKey,
        context: context,
        passwordUpgrade: <String, Object?>{
          'password': await _authenticationSecret(
            material,
            upgraded.authSalt,
            instance,
          ),
          'password_kdf': upgraded.toAuthenticationJson(),
        },
      );
    } on Object {
      vaultKey.destroy();
      rethrow;
    } finally {
      upgradeSalt.fillRange(0, upgradeSalt.length, 0);
    }
  } finally {
    material.destroy();
  }
}

Future<PreparedMobilePassword> prepareMobileRegistrationPassword(
  String password,
  Domain instance,
) async {
  _validatePassword(password, newCredential: true);
  final authSalt = _randomBytes(16);
  final vaultSalt = _randomBytes(16);
  try {
    final context = ModernMobilePasswordKdfContext(
      authSalt: _base64url(authSalt),
      vaultSalt: _base64url(vaultSalt),
    );
    return prepareMobilePassword(password, context, instance);
  } finally {
    authSalt.fillRange(0, authSalt.length, 0);
    vaultSalt.fillRange(0, vaultSalt.length, 0);
  }
}

Future<PreparedMobileResetPassword> prepareMobileResetPassword(
  String password,
  Domain instance,
) async {
  _validatePassword(password, newCredential: true);
  final authSalt = _randomBytes(16);
  final material = _passwordMaterial(password);
  try {
    final encodedSalt = _base64url(authSalt);
    return PreparedMobileResetPassword(
      authenticationSecret: await _authenticationSecret(
        material,
        encodedSalt,
        instance,
      ),
      // The reset request carries authentication metadata only. The server
      // separately rotates the account-vault salt and removes the old portable
      // vault because a recovery reset cannot prove possession of its key.
      passwordKdf: <String, Object?>{
        'version': mobilePasswordKdfVersion,
        'algorithm': mobilePasswordKdfAlgorithm,
        'iterations': mobilePasswordKdfIterations,
        'auth_salt': encodedSalt,
      },
    );
  } finally {
    material.destroy();
    authSalt.fillRange(0, authSalt.length, 0);
  }
}

SecretKeyData _passwordMaterial(String password) {
  return SecretKeyData(
    Uint8List.fromList(utf8.encode(password)),
    overwriteWhenDestroyed: true,
    debugLabel: 'Kaede password material',
  );
}

void _validatePassword(String password, {bool newCredential = false}) {
  if (password.isEmpty) throw ArgumentError('Enter your password.');
  if (password.length > 256) {
    throw ArgumentError('Password must be at most 256 characters.');
  }
  if (newCredential && password.length < 10) {
    throw ArgumentError('Password must be at least 10 characters.');
  }
}

Future<String> _authenticationSecret(
  SecretKeyData material,
  String encodedSalt,
  Domain instance,
) async {
  final derived = await _deriveKey(
    material,
    encodedSalt,
    instance,
    purpose: 'auth',
  );
  try {
    return _base64url(derived.bytes);
  } finally {
    derived.destroy();
  }
}

Future<SecretKeyData> _deriveKey(
  SecretKeyData material,
  String encodedSalt,
  Domain instance, {
  required String purpose,
}) async {
  if (purpose != 'auth' && purpose != 'vault') {
    throw ArgumentError.value(purpose, 'purpose');
  }
  final serverSalt = _decodeBase64url(encodedSalt, expectedLength: 16);
  final prefix = utf8.encode(
    '$_mobilePasswordKdfDomain\u0000$purpose\u0000${instance.value}\u0000',
  );
  final salt = Uint8List(prefix.length + serverSalt.length)
    ..setRange(0, prefix.length, prefix)
    ..setRange(prefix.length, prefix.length + serverSalt.length, serverSalt);
  try {
    final derived = await Pbkdf2.hmacSha256(
      iterations: mobilePasswordKdfIterations,
      bits: 256,
    ).deriveKey(secretKey: material, nonce: salt);
    final extracted = await derived.extract();
    try {
      return SecretKeyData(
        Uint8List.fromList(extracted.bytes),
        overwriteWhenDestroyed: true,
        debugLabel: 'Kaede password-derived key',
      );
    } finally {
      if (!identical(extracted, derived)) extracted.destroy();
      derived.destroy();
    }
  } finally {
    serverSalt.fillRange(0, serverSalt.length, 0);
    salt.fillRange(0, salt.length, 0);
  }
}

Uint8List _randomBytes(int length) {
  final random = Random.secure();
  return Uint8List.fromList(
    List<int>.generate(length, (_) => random.nextInt(256)),
  );
}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

Uint8List _decodeBase64url(String value, {required int expectedLength}) {
  if (value.length != 22 || !RegExp(r'^[A-Za-z0-9_-]+$').hasMatch(value)) {
    throw const FormatException('Invalid password KDF salt.');
  }
  final decoded =
      Uint8List.fromList(base64Url.decode(base64Url.normalize(value)));
  if (decoded.length != expectedLength || _base64url(decoded) != value) {
    decoded.fillRange(0, decoded.length, 0);
    throw const FormatException('Invalid password KDF salt.');
  }
  return decoded;
}
